"""Aggregate writer outcomes and apply verified Reporter conclusions."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .outputs import write_json
from .task_writer_files import _read_optional_json_object
from .verification_result import FINAL_MATCHED_STATUS, WRITER_REVIEW_STATUS


def _task_writer_runtime_result(
    *,
    task_records: list[dict[str, Any]],
    validation: dict[str, Any],
    requirement_warnings: list[dict[str, Any]],
    security_issues: list[dict[str, Any]],
    manifest_issues: list[dict[str, Any]] | None = None,
    requirement_issues: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    del manifest_issues
    blocking_requirements = list(requirement_issues or [])
    passed = sum(1 for record in task_records if _task_writer_runtime_task_passed(record))
    delivered = sum(1 for record in task_records if record.get("writer_completed"))
    total = len(task_records)
    valid_task_ids = [str(record.get("task_id")) for record in task_records if _task_writer_runtime_task_passed(record)]
    valid_csv_files: list[str] = []
    valid_png_files: list[str] = []
    valid_summary_json_files: list[str] = []
    valid_artifact_files: list[str] = []
    for record in task_records:
        if not _task_writer_runtime_task_passed(record):
            continue
        artifacts = record.get("artifacts") if isinstance(record.get("artifacts"), dict) else {}
        output_subdir = str(record.get("output_subdir") or record.get("task_id") or "")
        csv_files = artifacts.get("csv_files") if isinstance(artifacts.get("csv_files"), list) else []
        png_files = artifacts.get("png_files") if isinstance(artifacts.get("png_files"), list) else []
        summary_files = artifacts.get("summary_json_files") if isinstance(artifacts.get("summary_json_files"), list) else []
        artifact_files = (
            artifacts.get("artifact_files")
            if isinstance(artifacts.get("artifact_files"), list)
            else []
        )
        valid_csv_files.extend(f"{output_subdir}/{item}" for item in csv_files if isinstance(item, str))
        valid_png_files.extend(f"{output_subdir}/{item}" for item in png_files if isinstance(item, str))
        valid_summary_json_files.extend(
            f"{output_subdir}/{item}" for item in summary_files if isinstance(item, str)
        )
        valid_artifact_files.extend(
            f"{output_subdir}/{item}" for item in artifact_files if isinstance(item, str)
        )
    # Runtime success describes the observed full executions, not generated
    # filenames, optional compile scans or a second interpretation of warnings.
    all_checks_passed = total > 0 and passed == total
    return {
        "enabled": True,
        "passed": bool(all_checks_passed),
        "run_profile": "task_writer_full",
        "repair_backend": "codex_task_writers",
        "per_task_orchestration": True,
        "host_repeated_full": False,
        "tasks_total": total,
        "tasks_passed": passed,
        "coverage": f"{passed}/{total}",
        "deliveries_passed": delivered,
        "delivery_coverage": f"{delivered}/{total}",
        "partial_success": {
            "has_partial_output": bool(0 < passed < total),
            "valid_task_ids": valid_task_ids,
            "valid_csv_files": valid_csv_files,
            "valid_png_files": valid_png_files,
            "valid_summary_json_files": valid_summary_json_files,
            "valid_artifact_files": valid_artifact_files,
        },
        "per_task": [
            {
                "task_id": record.get("task_id"),
                "module": record.get("module"),
                "passed": _task_writer_runtime_task_passed(record),
                "writer_completed": bool(record.get("writer_completed")),
                "task_writer_status": record.get("task_writer_status"),
                "writer_error_kind": record.get("writer_error_kind"),
                "blocked_reason": record.get("blocked_reason"),
                "task_reporter_outcome": (
                    record.get("task_verification", {}).get("outcome")
                    if isinstance(record.get("task_verification"), dict)
                    else None
                ),
                "task_reporter_host_action": (
                    record.get("task_verification", {}).get("host_action")
                    if isinstance(record.get("task_verification"), dict)
                    else None
                ),
                "task_reporter_rerun_reason": (
                    record.get("task_verification", {}).get("rerun_reason")
                    if isinstance(record.get("task_verification"), dict)
                    else None
                ),
                "execution_summary": record.get("execution_summary"),
                "artifacts": record.get("artifacts"),
            }
            for record in task_records
        ],
        "validation": validation,
        "requirements_warnings": requirement_warnings,
        "requirements_issues": blocking_requirements,
        "security_issues": security_issues,
    }


def _task_writer_runtime_task_passed(record: dict[str, Any]) -> bool:
    host = record.get("host_execution")
    return isinstance(host, dict) and host.get("passed") is True


def apply_verified_result(
    *, task_records: list[dict[str, Any]], verification_result: dict[str, Any],
    output_dir: Path, audit_dir: Path, repro_project_dir: Path,
) -> dict[str, Any]:
    """Publish notes beside execution evidence without rewriting or refreezing code."""
    del repro_project_dir
    by_id = {item.get("assigned_task_id") or item.get("task_id"): item
             for item in verification_result.get("tasks", [])
             if isinstance(item, dict) and (item.get("assigned_task_id") or item.get("task_id"))}
    for record in task_records:
        note = by_id.get(record.get("task_id"))
        if note is not None:
            record["verification_result"] = note
            record["scientific_outcome"] = note.get("outcome")
            record["verification_verified"] = note.get("handoff_accepted", False)
    runtime = _read_optional_json_object(output_dir / "runtime_result.json")
    runtime.update(verification_mode="preserved_reporter_decisions",
        scientific_all_terminal=verification_result.get("all_terminal", False),
        scientific_all_successful=verification_result.get("all_successful", False),
        all_full_runs_observed=verification_result.get("all_full_runs_observed", False),
        scientific_outcome_counts=verification_result.get("outcome_counts", {}))
    write_json(output_dir / "runtime_result.json", runtime)
    write_json(audit_dir / "03c_task_writers_records.json", {"verification_result": verification_result, "tasks": task_records})
    return runtime


def _task_writer_alignment_summary(task_records: list[dict[str, Any]]) -> dict[str, Any]:
    """Keep dispatch diagnostics separate from scientific interpretation."""
    return {"task_count": len(task_records), "deliveries_completed": sum(bool(item.get("writer_completed")) for item in task_records)}

def _compact_task_writer_review(record: dict[str, Any]) -> dict[str, Any]:
    result = record.get("result_json") if isinstance(record.get("result_json"), dict) else {}
    return {
        "task_id": record.get("task_id"),
        "task_writer_status": record.get("task_writer_status"),
        "writer_completed": record.get("writer_completed"),
        "summary": result.get("summary"),
        "differences": result.get("differences", []),
        "possible_causes": result.get("possible_causes", []),
        "remaining_uncertainties": result.get("remaining_uncertainties", []),
        "evidence_files": result.get("evidence_files", []),
        "writer_error_kind": record.get("writer_error_kind"),
        "blocked_reason": record.get("blocked_reason"),
    }

def _task_writer_stop_class(task_records: list[dict[str, Any]]) -> str:
    if not task_records:
        return "no_tasks"
    if any(_task_writer_blocked_by_codex(record) for record in task_records):
        return "blocked_by_codex"
    if any(not record.get("writer_completed") for record in task_records):
        return "writer_failures"
    if any(record.get("task_writer_status") == "failed" for record in task_records):
        return "external_failures"
    return "ready_for_review"

def _task_writer_stopped_reason(task_records: list[dict[str, Any]]) -> str:
    stop_class = _task_writer_stop_class(task_records)
    return {
        "no_tasks": "no reproduction tasks were available",
        "blocked_by_codex": "one or more Codex task writers were blocked by usage limits or rate limits",
        "writer_failures": "one or more autonomous task writers did not complete",
        "external_failures": "one or more task writers stopped because of an external process failure",
        "ready_for_review": "all task writers submitted successful full results for independent verification",
    }.get(stop_class, stop_class)

def _task_writer_blocked_by_codex(record: dict[str, Any]) -> bool:
    return str(record.get("writer_error_kind") or "") in {
        "codex_usage_limit",
        "codex_rate_limit",
    }
