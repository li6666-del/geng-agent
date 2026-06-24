from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

"""Cache/status helpers: validate & reuse stage caches, detect a matching paper cache,
inspect produced outputs, and assess partial success of a failed run."""

from .outputs import write_json
from .pipeline_helpers import _read_json_file
from .schemas import ValidationIssue, validate_stage


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
    source_path = cached.get("source_path")
    chunks = cached.get("chunks")
    if not isinstance(source_path, str) or not isinstance(chunks, list) or not chunks:
        return False
    try:
        return Path(source_path).expanduser().resolve() == paper_path.expanduser().resolve()
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
) -> dict[str, Any] | None:
    try:
        cached = _read_json_file(path)
    except Exception as exc:
        write_json(
            audit_dir / f"resume_invalid_{stage_label}.json",
            {"ok": False, "errors": [{"path": "$", "message": f"cache read error: {exc}"}]},
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

    write_json(audit_dir / f"resume_used_{stage_label}.json", {"ok": True, "source": str(path)})
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
        if current_artifacts.get("has_csv") and current_artifacts.get("has_png") and current_artifacts.get("has_summary_json"):
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
        "has_partial_output": bool(csv_files),
        "valid_csv_files": list(csv_files),
        "valid_png_files": list(png_files),
        "valid_summary_json_files": list(summary_files),
        "note": "generated project failed guarded execution but produced valid partial outputs",
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
