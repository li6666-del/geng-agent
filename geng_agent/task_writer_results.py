"""Aggregate writer outcomes and apply verified Reporter conclusions."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .outputs import write_json
from .security import FOUNDATION_STATIC_SECURITY_ADVISORY_CATEGORIES, split_static_security_issues
from .task_writer_files import _read_optional_json_object
from .task_writer_packaging import _expected_paths_from_project_manifest, _freeze_repro_project_package
from .verification_result import FINAL_MATCHED_STATUS, WRITER_REVIEW_STATUS, verification_result_issues


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
    blocking_security = any(
        not isinstance(issue, dict)
        or str(issue.get("severity") or "error").strip().lower() != "warning"
        for issue in security_issues
    )
    all_checks_passed = (
        total > 0
        and passed == total
        and validation.get("required_files_present") is True
        and validation.get("python_compiles") is not False
        and validation.get("foundation_integrity_ok") is not False
        and not blocking_requirements
        and not blocking_security
    )
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
        "foundation_integrity_violations": (
            validation.get("foundation_violations")
            if isinstance(validation.get("foundation_violations"), list)
            else []
        ),
    }

def _classify_task_writer_security_issues(
    issues: list[dict[str, Any]],
    *,
    foundation: dict[str, Any] | None,
    foundation_integrity_issues: list[dict[str, Any]] | None = None,
) -> list[dict[str, str]]:
    """Apply the Foundation-only advisory exception to strict scanner findings.

    The global scanner remains fail-closed. A finding is downgraded only when
    its category is explicitly approved for Foundation code, its file is owned
    by the validated Foundation manifest, and the assembled project still
    matches the Foundation hashes and frozen layout. Task Writer files
    therefore remain strict even when a Foundation is installed in the same
    project. Omitting the integrity result is deliberately fail-closed.
    """

    foundation_paths: set[str] = set()
    foundation_is_current = (
        isinstance(foundation, dict)
        and foundation_integrity_issues is not None
        and not foundation_integrity_issues
    )
    if foundation_is_current:
        assert isinstance(foundation, dict)
        manifest = foundation.get("manifest")
        files = manifest.get("files") if isinstance(manifest, dict) else None
        for item in files if isinstance(files, list) else []:
            if isinstance(item, dict):
                path = str(item.get("path") or "").replace("\\", "/")
                if path:
                    foundation_paths.add(path)

    classified: list[dict[str, str]] = []
    for issue in issues:
        issue_file = str(issue.get("file") or "").replace("\\", "/")
        advisory_categories = (
            FOUNDATION_STATIC_SECURITY_ADVISORY_CATEGORIES
            if issue_file in foundation_paths
            else frozenset()
        )
        blocking, warnings = split_static_security_issues(
            [issue],
            advisory_categories=advisory_categories,
        )
        classified.extend(warnings or blocking)
    return classified

def _task_writer_runtime_task_passed(record: dict[str, Any]) -> bool:
    if isinstance(record.get("host_execution"), dict) and record["host_execution"].get("passed") is False:
        return False
    delivery_passed = bool(record.get("writer_completed")) and str(record.get("task_writer_status") or "") in {
        WRITER_REVIEW_STATUS,
        FINAL_MATCHED_STATUS,
    }
    if not delivery_passed:
        return False
    verification = (
        record.get("verification_result")
        if isinstance(record.get("verification_result"), dict)
        else record.get("task_verification")
    )
    if isinstance(verification, dict):
        return (
            verification.get("run_valid") is True
            and verification.get("outcome") != "execution_failed"
        )
    # Build-only workflows have no independent scientific verification yet.
    return True

def apply_verified_result(
    *,
    task_records: list[dict[str, Any]],
    verification_result: dict[str, Any],
    output_dir: Path,
    audit_dir: Path,
    repro_project_dir: Path,
) -> dict[str, Any]:
    """Attach every normal scientific terminal outcome to the Writer records."""

    expected_task_ids = [str(record.get("task_id") or "") for record in task_records]
    result_issues = verification_result_issues(verification_result, expected_task_ids)
    if result_issues:
        raise ValueError("cannot finalize incomplete task outcomes: " + "; ".join(result_issues))
    if not verification_result.get("all_terminal"):
        raise ValueError("cannot finalize while a task still requests a Writer rerun")

    outcome_by_id = {
        str(item.get("task_id")): item
        for item in verification_result.get("tasks", [])
        if isinstance(item, dict) and str(item.get("task_id") or "")
    }
    for record in task_records:
        task_id = str(record.get("task_id") or "")
        task_outcome = outcome_by_id.get(task_id)
        if not isinstance(task_outcome, dict):
            raise ValueError(f"missing terminal outcome for {task_id}")
        outcome = str(task_outcome.get("outcome") or "inconclusive_missing_information")
        record["task_writer_status"] = (
            FINAL_MATCHED_STATUS
            if outcome in {"reproduced", "reproduced_with_assumptions"}
            else WRITER_REVIEW_STATUS
        )
        record["scientific_outcome"] = outcome
        record["verification_result"] = task_outcome
        record["verification_verified"] = True

    previous_runtime = _read_optional_json_object(output_dir / "runtime_result.json")
    runtime_result = _task_writer_runtime_result(
        task_records=task_records,
        validation=(
            previous_runtime.get("validation")
            if isinstance(previous_runtime.get("validation"), dict)
            else {"host_validation_skipped": True}
        ),
        requirement_warnings=(
            previous_runtime.get("requirements_warnings")
            if isinstance(previous_runtime.get("requirements_warnings"), list)
            else []
        ),
        security_issues=(
            previous_runtime.get("security_issues")
            if isinstance(previous_runtime.get("security_issues"), list)
            else []
        ),
        requirement_issues=(
            previous_runtime.get("requirements_issues")
            if isinstance(previous_runtime.get("requirements_issues"), list)
            else []
        ),
    )
    runtime_result["verification_verified"] = True
    runtime_result["verification_mode"] = "host_derived_core_conclusion_outcomes"
    runtime_result["scientific_all_terminal"] = True
    runtime_result["scientific_all_successful"] = bool(
        verification_result.get("all_successful")
    )
    runtime_result["scientific_outcome_counts"] = verification_result.get("outcome_counts", {})
    write_json(output_dir / "runtime_result.json", runtime_result)
    write_json(
        audit_dir / "03c_task_writers_records.json",
        {"verification_result": verification_result, "tasks": task_records},
    )
    status_path = audit_dir / "03c_task_writers_status.json"
    status = _read_optional_json_object(status_path)
    all_successful = bool(verification_result.get("all_successful"))
    status.update(
        {
            "stop_class": "verified_matched" if all_successful else "verified_terminal",
            "stopped_reason": (
                "all tasks reproduced the assigned core conclusions"
                if all_successful
                else "all tasks reached reportable scientific terminal outcomes"
            ),
            "runtime": {"passed": runtime_result.get("passed"), "coverage": runtime_result.get("coverage")},
            "tasks": [
                {
                    "task_id": record.get("task_id"),
                    "status": record.get("task_writer_status"),
                    "scientific_outcome": record.get("scientific_outcome"),
                    "verification_verified": True,
                }
                for record in task_records
            ],
        }
    )
    write_json(status_path, status)
    config_path = repro_project_dir / "config.json"
    config = _read_optional_json_object(config_path)
    config["task_statuses"] = {
        str(record.get("task_id")): str(record.get("task_writer_status") or WRITER_REVIEW_STATUS)
        for record in task_records
    }
    config["scientific_outcomes"] = {
        str(record.get("task_id")): str(record.get("scientific_outcome") or "")
        for record in task_records
    }
    config["verification_verified"] = True
    write_json(config_path, config)

    # Reporter terminalization is the last mutation inside the portable project.
    # Re-freeze after that write so the committed inventory and text manifest
    # describe the package users actually receive.  The scientific smoke already
    # ran before reporting; this pass revalidates structure and every file hash.
    manifest_path = output_dir / "repro_project_manifest.json"
    if manifest_path.is_file():
        previous_manifest = _read_optional_json_object(manifest_path)
        meta = previous_manifest.get("_meta")
        task_manifest = meta.get("tasks_manifest") if isinstance(meta, dict) else None
        if not isinstance(task_manifest, dict):
            raise RuntimeError("cannot final-freeze package without tasks_manifest metadata")
        expected_paths = _expected_paths_from_project_manifest(previous_manifest)
        if not expected_paths:
            raise RuntimeError("cannot final-freeze package without declared project paths")
        _final_manifest, final_portability = _freeze_repro_project_package(
            repro_project_dir=repro_project_dir,
            output_dir=output_dir,
            audit_path=audit_dir / "03c_project_portability_final.json",
            task_manifest=task_manifest,
            expected_paths=expected_paths,
            analysis_snapshot_hash=str(meta.get("analysis_snapshot_hash") or ""),
            foundation_snapshot_hash=str(meta.get("foundation_snapshot_hash") or ""),
            environment_hash=str(meta.get("environment_lock_hash") or ""),
            run_smoke=False,
        )
        validation = runtime_result.get("validation")
        if not isinstance(validation, dict):
            validation = {}
            runtime_result["validation"] = validation
        validation["portable"] = bool(final_portability.get("portable"))
        final_inventory = final_portability.get("inventory")
        validation["final_package_inventory_sha256"] = (
            final_inventory.get("inventory_sha256")
            if isinstance(final_inventory, dict)
            else None
        )
        validation["final_package_portability_audit"] = (
            "audit/03c_project_portability_final.json"
        )
        write_json(output_dir / "runtime_result.json", runtime_result)
    return runtime_result

def _task_writer_alignment_summary(task_records: list[dict[str, Any]]) -> dict[str, Any]:
    if not task_records:
        return {
            "overall_alignment": "inconclusive",
            "overall_result_credibility": "low",
            "overall_summary": "没有可审查的复现任务。",
        }
    if any(_task_writer_blocked_by_codex(record) for record in task_records):
        return {
            "overall_alignment": "inconclusive",
            "overall_result_credibility": "low",
            "overall_summary": "至少一个 Codex task writer 因额度或限流被阻塞，不能把缺失任务视为科学复现失败。",
        }
    if any(not record.get("writer_completed") for record in task_records):
        return {
            "overall_alignment": "inconclusive",
            "overall_result_credibility": "low",
            "overall_summary": "部分自治 writer 未正常完成，不能给出强复现结论。",
        }
    statuses = {str(record.get("task_writer_status") or "failed") for record in task_records}
    if statuses <= {WRITER_REVIEW_STATUS, FINAL_MATCHED_STATUS}:
        return {
            "overall_alignment": "candidate",
            "overall_result_credibility": "medium",
            "overall_summary": "所有自治 writer 均已完成 full 并提交待独立审查的结果。",
        }
    return {
        "overall_alignment": "inconclusive",
        "overall_result_credibility": "low",
        "overall_summary": "至少一个 writer 遭遇外部进程错误，任务尚未完成。",
    }

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
