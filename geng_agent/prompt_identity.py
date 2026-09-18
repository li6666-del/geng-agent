"""Small, stage-local identities for the instructions actually used by workers."""
from __future__ import annotations

import hashlib
import inspect
import json
from pathlib import Path
from typing import Any, Iterable


def normalized_prompt(text: str) -> str:
    """Ignore transport line endings and trailing whitespace, not scientific wording."""
    return "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")).strip("\n")


def text_identity(text: str) -> str:
    return hashlib.sha256(normalized_prompt(text).encode("utf-8")).hexdigest()


def scientific_cache_value(value: Any) -> Any:
    """Remove only known audit bookkeeping; retain scientific metadata and ordering."""
    if isinstance(value, list):
        return [scientific_cache_value(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        if key == "_meta" and isinstance(item, dict):
            item = {name: field for name, field in item.items() if name not in {
                "cache", "analysis_backend", "analysis_stage_label", "analysis_attempt",
                "generated_at", "created_at", "updated_at", "duration_s", "analysis_resume_source",
                "cache_reused",
            }}
            if not item:
                continue
        result[key] = scientific_cache_value(item)
    return result


def file_identity(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {"path": str(path)}
    try:
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
                size += len(chunk)
        result.update(sha256=digest.hexdigest(), bytes=size)
    except OSError as exc:
        result["unavailable"] = type(exc).__name__
    return result


def model_identity(role: str, *, backend: str = "codex", client: Any = None) -> dict[str, Any]:
    if backend == "codex":
        from .model_config import resolve_model_config
        return {"backend": backend, **resolve_model_config(role).identity()}
    # Credentials and endpoint query strings must never enter audit documents.
    endpoint = str(getattr(client, "base_url", "") or "")
    return {"backend": backend, "client_type": type(client).__qualname__,
            "endpoint_identity": hashlib.sha256(endpoint.encode()).hexdigest(),
            **{key: getattr(client, key, None) for key in ("model", "temperature", "thinking", "reasoning_effort")}}


def role_contract_identity(*, role: str, prompt: str,
                           policy_texts: Iterable[str] = (),
                           image_paths: Iterable[Path] = ()) -> dict[str, Any]:
    return {"version": "role-contract-v1", "role": role,
            "prompt_sha256": text_identity(prompt),
            "policy_sha256": [text_identity(text) for text in policy_texts],
            "model": model_identity(role),
            # Content, not absolute location, identifies a portable attachment.
            "images": [{key: value for key, value in file_identity(Path(path)).items() if key != "path"}
                       for path in image_paths]}


def analysis_contract_identity(*, stage_label: str, schema_stage: str,
                               backend: str, prompt: str, client: Any = None) -> dict[str, Any]:
    from .agentic_analysis import _build_analysis_brief
    from .pipeline import SYSTEM_MESSAGE
    from . import pipeline_helpers
    from . import analysis_repair
    from .schema_models import model_for_stage

    repairs = {name: text_identity(inspect.getsource(getattr(pipeline_helpers, name)))
               for name in ("build_json_inline_retry_prompt", "build_json_scientific_retry_prompt", "build_json_preservation_retry_prompt")
               if hasattr(pipeline_helpers, name)}
    repairs.update({name: text_identity(inspect.getsource(getattr(analysis_repair, name)))
                    for name in ("scientific_correction_paths", "preserved_science_issues")})
    if backend != "codex":
        repairs["build_text_only_evidence_prompt"] = text_identity(inspect.getsource(pipeline_helpers.build_text_only_evidence_prompt))
    schema = model_for_stage(schema_stage).model_json_schema()
    return {"version": "analysis-contract-v1", "stage": stage_label,
            "prompt_sha256": text_identity(prompt),
            "system_sha256": text_identity(SYSTEM_MESSAGE) if backend != "codex" else None,
            "wrapper_sha256": text_identity(inspect.getsource(_build_analysis_brief)) if backend == "codex" else None,
            "repair_sha256": repairs,
            "schema_sha256": hashlib.sha256(json.dumps(schema, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
            "model": model_identity("analysis", backend=backend, client=client)}
