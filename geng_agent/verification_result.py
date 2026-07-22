from __future__ import annotations

from typing import Any


WRITER_REVIEW_STATUS = "ready_for_review"
FINAL_MATCHED_STATUS = "matched"
TASK_REPORTER_ACCEPTED = "accepted"
TASK_REPORTER_REVISE = "revise"
TASK_REPORTER_ROUTE_NONE = "none"
TASK_REPORTER_ROUTE_WRITER = "writer"
TASK_REPORTER_ROUTE_REPORTER = "reporter"

def partition_writer_delivery_issues(result: Any) -> tuple[list[str], list[str]]:
    """Keep execution failures blocking while treating hand-off prose as advisory."""

    blockers: list[str] = []
    warnings: list[str] = []
    for issue in writer_delivery_issues(result):
        if issue.startswith(("writer status must be", "writer summary is empty", "writer must declare")):
            warnings.append(issue)
        else:
            blockers.append(issue)
    return blockers, warnings


def normalize_task_verification(result: Any, expected_task_id: str) -> dict[str, Any]:
    """Fill non-scientific hand-off metadata without changing verdict or differences."""

    if not isinstance(result, dict):
        return {}
    normalized = dict(result)
    normalized.setdefault("schema_version", "1.0")
    normalized.setdefault("task_id", str(expected_task_id))
    verdict = str(normalized.get("verdict") or "")
    if verdict == TASK_REPORTER_ACCEPTED:
        normalized["revision_target"] = TASK_REPORTER_ROUTE_NONE
    elif verdict == TASK_REPORTER_REVISE and str(normalized.get("revision_target") or "") not in {
        TASK_REPORTER_ROUTE_WRITER,
        TASK_REPORTER_ROUTE_REPORTER,
    }:
        normalized["revision_target"] = TASK_REPORTER_ROUTE_WRITER
    normalized.setdefault("comparison_summary", "Reporter returned no textual comparison summary.")
    normalized.setdefault("differences", [])
    normalized.setdefault("non_material_differences", [])
    evidence = normalized.get("evidence_files")
    if not isinstance(evidence, list) or not any(str(value).strip() for value in evidence):
        normalized["evidence_files"] = ["inputs/task_report_input.json"]
    normalized.setdefault("feedback", [])
    if str(normalized.get("confidence") or "") not in {"low", "medium", "high"}:
        normalized["confidence"] = "medium"
    normalized.setdefault("local_assets", [])
    normalized.setdefault("paper_assets", [])
    normalized.setdefault("remaining_uncertainties", [])
    return normalized


def partition_task_verification_issues(result: Any, expected_task_id: str) -> tuple[list[str], list[str]]:
    """Separate scientific identity/verdict contradictions from delivery metadata."""

    blockers: list[str] = []
    warnings: list[str] = []
    for issue in task_verification_issues(result, expected_task_id):
        if issue.startswith((
            "task_verification_result.json is not an object",
            "task_id must be",
            "verdict must be",
            "accepted result cannot contain material differences",
            "revise result requires concrete differences",
        )):
            blockers.append(issue)
        else:
            warnings.append(issue)
    return blockers, warnings


def writer_delivery_issues(result: Any) -> list[str]:
    if not isinstance(result, dict):
        return ["task_agent_result.json is not an object"]
    issues: list[str] = []
    if str(result.get("status") or "") != WRITER_REVIEW_STATUS:
        issues.append(f"writer status must be {WRITER_REVIEW_STATUS}")
    if not str(result.get("summary") or "").strip():
        issues.append("writer summary is empty")
    execution = result.get("execution_summary")
    if not isinstance(execution, dict):
        issues.append("execution_summary is missing")
    else:
        try:
            full_run_count = int(execution.get("full_run_count") or 0)
        except (TypeError, ValueError):
            full_run_count = 0
        if full_run_count < 1:
            issues.append("writer must report at least one full run")
        if execution.get("last_returncode") != 0:
            issues.append("writer last full run did not return 0")
    images = result.get("local_image_paths")
    if not isinstance(images, list) or not any(str(item).strip() for item in images):
        issues.append("writer must declare at least one local image")
    return issues


def verification_result_issues(result: Any, expected_task_ids: list[str]) -> list[str]:
    if not isinstance(result, dict):
        return ["verification_result.json is not an object"]
    issues: list[str] = []
    raw_tasks = result.get("tasks") if isinstance(result.get("tasks"), list) else []
    by_id = {
        str(item.get("task_id")): item
        for item in raw_tasks
        if isinstance(item, dict) and str(item.get("task_id") or "")
    }
    if len(by_id) != len(raw_tasks):
        issues.append("task verification IDs must be non-empty and unique")
    expected = {str(task_id) for task_id in expected_task_ids if str(task_id)}
    missing = sorted(expected - set(by_id))
    unexpected = sorted(set(by_id) - expected)
    if missing:
        issues.append("missing task verification results: " + ", ".join(missing))
    if unexpected:
        issues.append("unexpected task verification results: " + ", ".join(unexpected))

    accepted_count = 0
    for task_id in expected_task_ids:
        item = by_id.get(task_id)
        if item is None:
            continue
        verdict = str(item.get("verdict") or "")
        if verdict not in {"accepted", "revise"}:
            issues.append(f"{task_id}: verdict must be accepted or revise")
            continue
        if not str(item.get("comparison_summary") or "").strip():
            issues.append(f"{task_id}: comparison_summary is empty")
        evidence = item.get("evidence_files")
        if not isinstance(evidence, list) or not any(str(value).strip() for value in evidence):
            issues.append(f"{task_id}: evidence_files is empty")
        if verdict == "accepted":
            accepted_count += 1
        else:
            differences = item.get("differences")
            feedback = item.get("feedback")
            if not isinstance(differences, list) or not any(str(value).strip() for value in differences):
                issues.append(f"{task_id}: revise verdict requires concrete differences")
            if not isinstance(feedback, list) or not any(str(value).strip() for value in feedback):
                issues.append(f"{task_id}: revise verdict requires actionable feedback")
    expected_all = bool(expected) and accepted_count == len(expected)
    if result.get("all_accepted") is not expected_all:
        issues.append(f"all_accepted must be {expected_all}")
    return issues


def feedback_from_verification(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    feedback: dict[str, dict[str, Any]] = {}
    for item in result.get("tasks", []) if isinstance(result.get("tasks"), list) else []:
        if not isinstance(item, dict) or str(item.get("verdict") or "") != "revise":
            continue
        task_id = str(item.get("task_id") or "")
        if task_id:
            feedback[task_id] = item
    return feedback


def task_verification_issues(result: Any, expected_task_id: str) -> list[str]:
    """Validate one isolated reporter result without imposing scientific targets."""
    if not isinstance(result, dict):
        return ["task_verification_result.json is not an object"]
    issues: list[str] = []
    if str(result.get("schema_version") or "") != "1.0":
        issues.append("schema_version must be 1.0")
    task_id = str(result.get("task_id") or "")
    if task_id != str(expected_task_id):
        issues.append(f"task_id must be {expected_task_id}")
    verdict = str(result.get("verdict") or "")
    if verdict not in {TASK_REPORTER_ACCEPTED, TASK_REPORTER_REVISE}:
        issues.append("verdict must be accepted or revise")
    if not str(result.get("comparison_summary") or "").strip():
        issues.append("comparison_summary is empty")
    evidence = result.get("evidence_files")
    if not isinstance(evidence, list) or not any(str(value).strip() for value in evidence):
        issues.append("evidence_files is empty")
    confidence = str(result.get("confidence") or "")
    if confidence not in {"low", "medium", "high"}:
        issues.append("confidence must be low, medium, or high")
    differences = result.get("differences")
    if not isinstance(differences, list):
        issues.append("differences must be a list")
        differences = []
    feedback = result.get("feedback")
    if not isinstance(feedback, list):
        issues.append("feedback must be a list")
        feedback = []
    route = str(result.get("revision_target") or "")
    if verdict == TASK_REPORTER_ACCEPTED:
        if route != TASK_REPORTER_ROUTE_NONE:
            issues.append("accepted result must use revision_target none")
        if any(str(value).strip() for value in differences):
            issues.append("accepted result cannot contain material differences")
        if confidence == "low":
            issues.append("accepted result cannot have low confidence")
    elif verdict == TASK_REPORTER_REVISE:
        if route not in {TASK_REPORTER_ROUTE_WRITER, TASK_REPORTER_ROUTE_REPORTER}:
            issues.append("revise result must target writer or reporter")
        if not any(str(value).strip() for value in differences):
            issues.append("revise result requires concrete differences")
        if not any(str(value).strip() for value in feedback):
            issues.append("revise result requires actionable feedback")
    return issues


def aggregate_task_verifications(task_results: list[dict[str, Any]]) -> dict[str, Any]:
    """Build the case-level audit document from isolated task decisions."""
    tasks: list[dict[str, Any]] = []
    for result in task_results:
        if not isinstance(result, dict):
            continue
        tasks.append(
            {
                "task_id": result.get("task_id"),
                "verdict": result.get("verdict"),
                "comparison_summary": result.get("comparison_summary"),
                "differences": result.get("differences", []),
                "evidence_files": result.get("evidence_files", []),
                "feedback": result.get("feedback", []),
                "confidence": result.get("confidence"),
            }
        )
    return {
        "schema_version": "1.0",
        "all_accepted": bool(tasks) and all(
            str(item.get("verdict") or "") == TASK_REPORTER_ACCEPTED for item in tasks
        ),
        "tasks": tasks,
    }
