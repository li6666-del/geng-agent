from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable

"""Cache/status helpers: validate & reuse stage caches, detect a matching paper cache,
inspect produced outputs, and assess partial success of a failed run."""

from .outputs import write_json
from .pipeline_helpers import _read_json_file
from .schemas import ValidationIssue, validate_stage


STAGE_CACHE_FORMAT_VERSION = "semantic_inputs_v2"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_stage_cache_metadata(
    *,
    stage_label: str,
    schema_stage: str,
    prompt: str,
    policy_version: str,
    inputs: Any,
) -> dict[str, str]:
    """Build an explicit content-addressed resume key without adding a stage gate."""
    from .schema_models import model_for_stage

    schema_hash = _canonical_sha256(model_for_stage(schema_stage).model_json_schema())
    identity = {
        "format_version": STAGE_CACHE_FORMAT_VERSION,
        "stage_label": stage_label,
        "schema_stage": schema_stage,
        "policy_version": policy_version,
        "inputs_sha256": _canonical_sha256(inputs),
    }
    diagnostics = {
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "schema_sha256": schema_hash,
    }
    return {**identity, **diagnostics, "fingerprint": _canonical_sha256(identity)}


def _load_result_review_document(output_dir: Path, result_review_result: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(result_review_result, dict) or result_review_result.get("passed") is not True:
        return {}
    path = output_dir / "result_review.json"
    if not path.exists():
        md_path = output_dir / "result_review.md"
        if md_path.exists():
            status = dict(result_review_result)
            status["_meta"] = {"markdown_review": True}
            status["markdown_path"] = str(md_path)
            return status
        return {}
    try:
        return _read_json_file(path)
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def _paper_cache_matches(cached: dict[str, Any], paper_path: Path) -> bool:
    chunks = cached.get("chunks")
    meta = cached.get("_meta") if isinstance(cached.get("_meta"), dict) else {}
    expected_hash = cached.get("source_sha256") or cached.get("paper_sha256") or meta.get("source_sha256")
    if (
        not isinstance(chunks, list)
        or not chunks
        or not isinstance(expected_hash, str)
        or len(expected_hash) != 64
        or not paper_path.is_file()
    ):
        return False
    try:
        return _sha256_file(paper_path).casefold() == expected_hash.casefold()
    except OSError:
        return False


def _load_valid_stage_cache(
    *,
    path: Path,
    audit_dir: Path,
    stage_label: str,
    schema_stage: str,
    extra_validation: Callable[[dict[str, Any]], list[ValidationIssue]] | None = None,
    required_files: set[str] | None = None,
    expected_cache_metadata: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    try:
        cached = _read_json_file(path)
    except Exception as exc:
        write_json(
            audit_dir / f"resume_invalid_{stage_label}.json",
            {"ok": False, "errors": [{"path": "$", "message": f"cache read error: {exc}"}]},
        )
        return None

    if expected_cache_metadata is not None:
        meta = cached.get("_meta") if isinstance(cached.get("_meta"), dict) else {}
        actual_cache = meta.get("cache") if isinstance(meta.get("cache"), dict) else {}
        expected_fingerprint = expected_cache_metadata.get("fingerprint")
        if not expected_fingerprint or actual_cache.get("fingerprint") != expected_fingerprint:
            write_json(
                audit_dir / f"resume_invalid_{stage_label}.json",
                {
                    "ok": False,
                    "errors": [{"path": "$._meta.cache", "message": "cache scientific inputs or policy changed"}],
                    "expected_cache": expected_cache_metadata,
                    "actual_cache": actual_cache,
                },
            )
            return None

    # required_files: per-task manifests have a different required set (no run_experiment.py,
    # plus tasks/*.py) — without this override a cached per-task manifest always fails the
    # default-set validation and the whole codegen silently re-runs on resume.
    issues = validate_stage(schema_stage, cached, required_files=required_files)
    if extra_validation is not None:
        issues.extend(extra_validation(cached))
    if issues:
        write_json(
            audit_dir / f"resume_invalid_{stage_label}.json",
            {"ok": False, "errors": [issue.as_dict() for issue in issues]},
        )
        return None

    write_json(
        audit_dir / f"resume_used_{stage_label}.json",
        {
            "ok": True,
            "source": str(path),
            "cache_fingerprint": (
                expected_cache_metadata.get("fingerprint") if expected_cache_metadata is not None else None
            ),
        },
    )
    return cached


def _load_cached_runtime_result(output_dir: Path, repro_project_dir: Path) -> dict[str, Any] | None:
    candidates = [output_dir / "runtime_result.json", output_dir / "generated_files.json"]
    for path in candidates:
        if not path.exists():
            continue
        try:
            data = _read_json_file(path)
        except Exception:
            continue
        runtime_result = data.get("runtime_result") if path.name == "generated_files.json" else data
        if not isinstance(runtime_result, dict) or runtime_result.get("passed") is not True:
            continue
        artifacts = runtime_result.get("artifacts")
        if not isinstance(artifacts, dict):
            continue
        current_artifacts = _inspect_cached_outputs(repro_project_dir)
        if any(
            current_artifacts.get(key)
            for key in ("has_csv", "has_png", "has_summary_json")
        ):
            runtime_result["artifacts"] = current_artifacts
            return runtime_result
    return None


def _inspect_cached_outputs(repro_project_dir: Path) -> dict[str, Any]:
    from .outputs import inspect_output_artifacts

    return inspect_output_artifacts(repro_project_dir)


def _assess_partial_success(runtime_result: dict[str, Any]) -> dict[str, Any]:
    """Decide whether a failed generated-project run still produced usable partial output.

    A single crashing experiment should not sink the whole run: if the project wrote at
    least one valid output CSV before failing, keep the generated project (and surface the
    risk) instead of masking everything with a deterministic template.
    """
    artifacts = runtime_result.get("artifacts") if isinstance(runtime_result, dict) else None
    artifacts = artifacts if isinstance(artifacts, dict) else {}
    csv_files = artifacts.get("csv_files") if isinstance(artifacts.get("csv_files"), list) else []
    png_files = artifacts.get("png_files") if isinstance(artifacts.get("png_files"), list) else []
    summary_files = artifacts.get("summary_json_files") if isinstance(artifacts.get("summary_json_files"), list) else []
    return {
        "has_partial_output": bool(csv_files or png_files or summary_files),
        "valid_csv_files": list(csv_files),
        "valid_png_files": list(png_files),
        "valid_summary_json_files": list(summary_files),
        "note": "generated project failed guarded execution but produced usable structured or visual evidence",
    }


def _load_cached_result_review_status(output_dir: Path) -> dict[str, Any] | None:
    result_json_path = output_dir / "result_review.json"
    result_md_path = output_dir / "result_review.md"
    if not result_md_path.exists():
        return None
    error_path = output_dir / "result_review_error.json"
    if error_path.exists() and error_path.stat().st_mtime >= result_md_path.stat().st_mtime:
        return None
    if result_json_path.exists():
        try:
            parsed = _read_json_file(result_json_path)
        except Exception:
            return None
        if validate_stage("result_review", parsed):
            return None

    status = {
        "enabled": True,
        "passed": True,
        "result_review_markdown_path": str(result_md_path),
        "attempts": 0,
        "reason": "reused cached result_review.md",
    }
    if result_json_path.exists():
        status["result_review_path"] = str(result_json_path)
    generated_files_path = output_dir / "generated_files.json"
    if generated_files_path.exists():
        try:
            generated = _read_json_file(generated_files_path)
            cached_status = generated.get("result_review")
            if isinstance(cached_status, dict) and cached_status.get("passed") is True:
                status.update(cached_status)
        except Exception:
            pass
    return status
