"""Collect and normalize one logical task writer delivery."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .outputs import inspect_output_artifacts
from .task_writer_contracts import TASK_WRITER_TERMINAL_STATUS
from .task_writer_execution_binding import _task_execution_binding_issues
from .task_writer_files import _read_optional_json_object, _task_result_file_path, _writer_delivery_path_is_fresh
from .verification_result import partition_writer_delivery_issues, writer_delivery_issues


def _collect_task_writer_delivery(
    *,
    index: int,
    task: dict[str, Any],
    manifest_entry: dict[str, Any],
    sandbox: Path,
    writer_status: dict[str, Any],
    require_stopping_assessment: bool = False,
    allow_root_result_fallback: bool = True,
    fresh_since: float | None = None,
) -> dict[str, Any]:
    """Collect writer-owned outputs without repairing or format-gating them."""
    task_id = str(task.get("task_id") or manifest_entry.get("task_id") or f"task_{index}")
    module = str(manifest_entry.get("module") or "")
    output_subdir = str(manifest_entry.get("output_subdir") or task.get("task_id") or "task")
    result_path, _ = _task_result_file_path(
        sandbox,
        output_subdir,
        "task_agent_result.json",
        allow_root_fallback=allow_root_result_fallback,
    )
    markdown_path, _ = _task_result_file_path(
        sandbox,
        output_subdir,
        "task_agent_result.md",
        allow_root_fallback=allow_root_result_fallback,
    )
    result_is_fresh = _writer_delivery_path_is_fresh(result_path, fresh_since)
    markdown_is_fresh = _writer_delivery_path_is_fresh(markdown_path, fresh_since)
    result_doc = _read_optional_json_object(result_path) if result_is_fresh else {}
    reported_status = str(result_doc.get("status") or "")
    delivery_issues = writer_delivery_issues(
        result_doc,
        require_stopping_assessment=require_stopping_assessment,
    )
    delivery_blockers, delivery_warnings = partition_writer_delivery_issues(
        result_doc,
        require_stopping_assessment=require_stopping_assessment,
    )
    artifacts = inspect_output_artifacts(
        sandbox,
        since=fresh_since,
        subdir=output_subdir,
        declared_artifacts=task.get("expected_artifacts"),
    )
    binding_issues = _task_execution_binding_issues(
        sandbox=sandbox,
        task_id=task_id,
        result_doc=result_doc,
    )
    if binding_issues:
        binding_warnings = [f'shared_component_advisory: {issue}' for issue in binding_issues]
        delivery_issues.extend(binding_warnings)
        delivery_warnings.extend(binding_warnings)
    # Writer JSON is self-reported disclosure. A malformed/incomplete object
    # stays visible as warnings, while readable scientific artifacts still
    # advance to the independent Reporter.
    delivery_usable = bool(result_doc) or bool(artifacts.get("has_artifacts"))
    if not delivery_usable:
        blocker = "writer produced neither a readable result note nor a scientific artifact"
        delivery_blockers.append(blocker)
        delivery_issues.append(blocker)
    host_execution = None
    if writer_status.get("execution_receipts_required"):
        from .execution_receipts import find_host_execution
        host_execution = find_host_execution(sandbox, Path(writer_status["execution_audit_dir"]), task_id)
        if writer_status.get("error_kind") in {"foundation_modified", "evidence_modified"}:
            host_execution["passed"] = False
            host_execution.setdefault("issues", []).append("execution used an unauthorized Foundation revision")
    status = TASK_WRITER_TERMINAL_STATUS if delivery_usable else "failed"
    local_images = _collect_writer_images(
        sandbox=sandbox,
        output_subdir=output_subdir,
        declared=result_doc.get("local_image_paths"),
        fallback_pattern="*.png",
        exclude_names={"paper_target_crop.png", "paper_target_locator.png"},
    )
    return {
        "index": index,
        "task_id": task_id,
        "module": module,
        "output_subdir": output_subdir,
        "sandbox": str(sandbox),
        "writer_status": writer_status,
        "host_execution": host_execution,
        "writer_completed": delivery_usable,
        "task_writer_status": status,
        "writer_reported_status": reported_status or None,
        "delivery_validation_issues": delivery_issues,
        "delivery_blockers": delivery_blockers,
        "delivery_warnings": delivery_warnings,
        "process_warning": None if writer_status.get("ok") else (writer_status.get("error") or writer_status.get("blocked_reason") or "writer process ended after producing a usable delivery"),
        "result_json": result_doc,
        "result_json_path": str(result_path) if result_is_fresh else None,
        "result_markdown_path": str(markdown_path) if markdown_is_fresh else None,
        "execution_summary": result_doc.get("execution_summary", {}),
        "artifacts": artifacts,
        "local_images": local_images,
        "writer_error_kind": writer_status.get("error_kind"),
        "blocked_reason": writer_status.get("blocked_reason"),
    }

def _collect_writer_images(
    *,
    sandbox: Path,
    output_subdir: str,
    declared: Any,
    fallback_pattern: str,
    exclude_names: set[str] | None = None,
) -> list[str]:
    output_dir = sandbox / "outputs" / output_subdir
    candidates: list[Path] = []
    values = declared if isinstance(declared, list) else [declared] if isinstance(declared, str) else []
    for raw in values:
        path = Path(str(raw))
        candidates.extend([path] if path.is_absolute() else [sandbox / path, output_dir / path.name])
    candidates.extend(sorted(output_dir.glob(fallback_pattern)) if output_dir.exists() else [])
    excluded = {name.lower() for name in (exclude_names or set())}
    result: list[str] = []
    seen: set[str] = set()
    for path in candidates:
        if not path.is_file() or path.suffix.lower() != ".png" or path.name.lower() in excluded:
            continue
        key = str(path.resolve()).lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(str(path.resolve()))
    return result
