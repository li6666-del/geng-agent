from __future__ import annotations

from copy import deepcopy
from contextlib import nullcontext
import hashlib
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from .agentic_analysis import CODEX_ANALYSIS_BACKEND, parse_owner_handoff
from .outputs import write_json, write_text
from .pipeline_helpers import (
    _is_non_retryable_llm_error,
    _temporary_client_timeout,
    build_text_only_evidence_prompt,
)
from .prompt_identity import analysis_contract_identity, scientific_cache_value
from .runtime_status import _load_valid_stage_cache, build_stage_cache_metadata
from .schemas import ValidationIssue
from .scientific_materiality import SCIENTIFIC_POLICY_ID
from .stage_cleanup import _clear_stage_outputs


def load_or_create_stage_json(
    pipeline: Any,
    *,
    output_path: Path,
    output_dir: Path,
    audit_dir: Path,
    prompt: str,
    stage_label: str,
    cleanup_stage: str,
    schema_stage: str,
    max_attempts: int,
    resume: bool,
    request_timeout: float | None = None,
    fallback_factory: Callable[[Exception], dict[str, Any] | None] | None = None,
    candidate_normalizer: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    repair_preservation_validator: (
        Callable[[dict[str, Any], dict[str, Any]], list[ValidationIssue]] | None
    ) = None,
    salvage_failed_candidates: bool = False,
    truncation_recovery: Callable[[str], dict[str, Any] | None] | None = None,
    images: list[Any] | None = None,
    client: Any = None,
    backend: str = "llm",
    cache_inputs: Any = None,
    codex_stage_runner: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    cache_reuse_enabled = cache_inputs is not None
    cache_metadata = build_stage_cache_metadata(
        stage_label=stage_label,
        schema_stage=schema_stage,
        prompt=prompt,
        policy_version=SCIENTIFIC_POLICY_ID,
        inputs={
            "handoff_policy": "supervisor-science-owner-v2",
            "scientific_inputs": scientific_cache_value(cache_inputs if cache_inputs is not None else {}),
            "contract": analysis_contract_identity(
                stage_label=stage_label, schema_stage=schema_stage,
                backend=backend, prompt=prompt, client=client or getattr(pipeline, "client", None),
            ),
            "images": [{"label": image.label, "mime_type": image.mime_type,
                        "sha256": hashlib.sha256(image.data_b64.encode("ascii")).hexdigest()}
                       for image in images or []],
        },
    )

    # An owner document is evidence. Do not normalize science, salvage a
    # truncated subset, or synthesize replacement tasks after a failed request.
    def bind_cache(candidate: dict[str, Any]) -> dict[str, Any]:
        result = deepcopy(candidate)
        meta = dict(result.get("_meta", {})) if isinstance(result.get("_meta"), dict) else {}
        meta["cache"] = cache_metadata
        result["_meta"] = meta
        return result

    if resume and cache_reuse_enabled and output_path.exists():
        cached = _load_valid_stage_cache(path=output_path, audit_dir=audit_dir,
            stage_label=stage_label, schema_stage=schema_stage,
            expected_cache_metadata=cache_metadata)
        if cached is not None:
            cached.setdefault("_meta", {})["cache_reused"] = True
            return cached

    write_text(audit_dir / f"{stage_label}.md", prompt)
    common = dict(prompt=prompt, stage_label=stage_label, schema_stage=schema_stage,
        audit_dir=audit_dir, max_attempts=1, candidate_normalizer=bind_cache, images=images)
    if backend == CODEX_ANALYSIS_BACKEND:
        parsed = codex_stage_runner(output_dir=output_dir, **common)
    elif backend == "llm":
        parsed = pipeline._call_validated_json(request_timeout=request_timeout, client=client, **common)
    else:
        raise ValueError(f"unknown analysis backend: {backend}")
    parsed = bind_cache(parsed)
    parsed["_meta"]["cache_reused"] = False
    write_json(output_path, parsed)
    try:
        preserved_output = output_path.relative_to(output_dir).as_posix()
    except ValueError:
        preserved_output = ""
    _clear_stage_outputs(output_dir, cleanup_stage, preserve_audit=True,
        preserve_paths={preserved_output} if preserved_output else set())
    return parsed


def complete_maybe_multimodal(
    pipeline: Any,
    prompt: str,
    *,
    schema_stage: str,
    images: list[Any] | None,
    client: Any = None,
    system_message: str,
    input_observer: Callable[[str, list[Any], dict[str, Any]], None] | None = None,
) -> str:
    client = client or pipeline.client
    if client is None:
        raise RuntimeError("LLM client is required for analysis_backend='llm'")
    response_format = {"type": "json_object"}
    def observe(actual_prompt: str, actual_images: list[Any], **visibility: Any) -> None:
        if input_observer is not None:
            input_observer(actual_prompt, actual_images, {
                "system_message": system_message, "response_format": response_format,
                **visibility,
            })

    if images:
        if hasattr(client, "complete_multimodal"):
            observe(prompt, images, mode="multimodal")
            try:
                return client.complete_multimodal(
                    prompt, images=images, system=system_message,
                    response_format=response_format,
                )
            except Exception as exc:
                detail = str(exc).lower()
                unsupported = any(signature in detail for signature in (
                    "unsupported image", "does not support image", "image input is not supported",
                    "vision is not supported", "image_url is not supported",
                ))
                if not unsupported or _is_non_retryable_llm_error(detail):
                    raise
                reason = "The provider explicitly rejected image input."
        else:
            reason = "The configured client has no multimodal completion capability."
        image_labels = [str(getattr(image, "label", "")) for image in images]
        prompt = build_text_only_evidence_prompt(prompt, image_labels, reason)
        observe(prompt, [], mode="text_only_downgrade", omitted_image_labels=image_labels, reason=reason)
    else:
        observe(prompt, [], mode="text")
    return client.complete(
        prompt,
        system=system_message,
        response_format=response_format,
    )


def call_validated_json(
    pipeline: Any, prompt: str, stage_label: str, schema_stage: str,
    audit_dir: Path, max_attempts: int,
    request_timeout: float | None = None, candidate_normalizer: Callable | None = None,
    repair_preservation_validator: Callable | None = None,
    truncation_recovery: Callable | None = None,
    images: list[Any] | None = None, client: Any = None,
) -> dict[str, Any]:
    """Request once and report transport problems to the owning supervisor node."""
    from .supervisor import NodeFailure
    from .progress import PipelineCancelled

    client = client or pipeline.client
    snapshot = audit_dir / "api_prompt_inputs" / uuid4().hex
    snapshot.mkdir(parents=True, exist_ok=True)
    delivered_visibility: dict[str, Any] = {}

    def record_input(actual_prompt: str, actual_images: list[Any], visibility: dict[str, Any]) -> None:
        delivered_visibility.clear()
        delivered_visibility.update({key: value for key, value in visibility.items()
                                     if key not in {"system_message", "response_format"}})
        delivered_visibility["supplied_image_labels"] = [image.label for image in actual_images]
        write_text(snapshot / "brief.md", actual_prompt)
        write_text(audit_dir / f"{stage_label}_llm_attempt_1_brief.md", actual_prompt)
        manifest = {"backend": "llm", "prompt_sha256": hashlib.sha256(actual_prompt.encode("utf-8")).hexdigest(),
            "mode": "evidence_reasoning", "visibility": visibility, "prompt_path": str(snapshot / "brief.md"),
            "images": [{"label": image.label, "mime_type": image.mime_type,
                        "sha256": hashlib.sha256(image.data_b64.encode("ascii")).hexdigest()}
                       for image in actual_images or []]}
        write_json(snapshot / "input.json", manifest)
        write_json(audit_dir / f"{stage_label}_llm_attempt_1_input.json", manifest)

    try:
        request_audit = client.audit_requests(audit_dir / "api_requests", f"{stage_label}_attempt_1") if hasattr(client, "audit_requests") else nullcontext()
        with _temporary_client_timeout(client, request_timeout), request_audit:
            raw = pipeline._complete_maybe_multimodal(prompt, schema_stage=schema_stage,
                images=images, client=client, input_observer=record_input)
        write_text(snapshot / "raw.txt", raw)
        write_text(audit_dir / f"raw_{stage_label}_attempt_1.txt", raw)
        write_text(audit_dir / f"raw_{stage_label}.txt", raw)
        parsed, parse_issue = parse_owner_handoff(raw, schema_stage)
    except PipelineCancelled:
        raise
    except Exception as exc:
        raise NodeFailure(f"{stage_label} did not produce a readable handoff: {exc}",
            result={"candidate_snapshot": str(snapshot)}) from exc
    write_json(snapshot / "candidate.json", parsed)
    if candidate_normalizer is not None:
        parsed = candidate_normalizer(parsed)
    write_json(snapshot / "handoff.json", parsed)
    observations = ([{"kind": "json_unreadable", "message": parse_issue}]
                    if parse_issue else [])
    write_json(audit_dir / f"handoff_{stage_label}_attempt_1.json", {
        "handoff_recorded": True, "content_validation_performed": False,
        "observations": observations, "decision_owner": "next_stage", "candidate_snapshot": str(snapshot)})
    meta = dict(parsed.get("_meta", {})) if isinstance(parsed.get("_meta"), dict) else {}
    meta.update({"evidence_visibility": delivered_visibility, "host_observations": observations})
    parsed["_meta"] = meta
    return parsed
