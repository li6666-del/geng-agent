from __future__ import annotations

from pathlib import Path

from typing import Any

WRITER_REVIEW_STATUS = "ready_for_review"
FINAL_MATCHED_STATUS = "matched"
def _paper_basis_review(item: dict[str, Any], workspace: Path | None) -> dict[str, Any] | None:
    """Accept a Reporter's scope correction only with copied original-paper evidence.

    Writer accounts, Designer navigation JSON and a model-supplied 'verified'
    flag cannot authorize a correction. This is an optional scientific note,
    never a required schema gate.
    """
    review = item.get("basis_review")
    if not isinstance(review, dict):
        return None
    status = str(review.get("status") or "")
    reason = str(review.get("reason") or "").strip()
    paths = _string_list(review.get("paper_evidence_files"))
    verified = bool(workspace is not None and reason and paths)
    for raw in paths:
        relative = Path(raw)
        try:
            root = Path(workspace).resolve() if workspace is not None else Path()
            path = root / relative
            resolved = path.resolve(strict=True)
            roots = [root / "paper_evidence" / part for part in ("source", "full_paper_pages", "mineru_figure_candidates")]
            verified = verified and not relative.is_absolute() and ".." not in relative.parts and resolved.is_file()
            verified = verified and any(resolved.is_relative_to(base) for base in roots)
            verified = verified and not any(p.is_symlink() or (hasattr(p, "is_junction") and p.is_junction()) for p in (path, *path.parents) if p != root.parent)
        except (OSError, ValueError, TypeError):
            verified = False
    if status not in {"not_applicable", "disputed", "corrected"}:
        return None
    return {**review, "status": status, "reason": reason,
            "paper_evidence_files": paths, "paper_evidence_verified": bool(verified)}


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _scientific_acceptance(task: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(task, dict):
        return {}
    value = task.get("scientific_acceptance")
    return value if isinstance(value, dict) else {}


def _normalize_core_conclusions(raw: dict[str, Any], task: dict[str, Any] | None,
                                evidence_workspace: Path | None = None) -> Any:
    """Preserve the Reporter's in-scope assessments without semantic reclassification."""
    del task
    items = raw.get("core_conclusions")
    if not isinstance(items, list):
        return items if items is not None else []
    result = []
    for item in items:
        if not isinstance(item, dict):
            result.append(item)
            continue
        normalized = dict(item)
        basis = _paper_basis_review(item, evidence_workspace)
        if basis:
            normalized["basis_review"] = basis
        result.append(normalized)
    return result


def _normalize_numeric_item(target: dict[str, Any], candidate: dict[str, Any], *,
                            target_id: str, workspace: Path | None) -> dict[str, Any]:
    """Keep the Reporter's quantities; the host does not fill scientific anchors."""
    del target, target_id
    result = dict(candidate)
    basis = _paper_basis_review(candidate, workspace)
    if basis:
        result["basis_review"] = basis
    return result


def _normalize_numeric_comparisons(raw: dict[str, Any], task: dict[str, Any] | None,
                                   evidence_workspace: Path | None = None) -> Any:
    del task
    items = raw.get("key_numeric_comparisons")
    return [_normalize_numeric_item({}, item,
                target_id=str(item.get("target_id") or ""), workspace=evidence_workspace)
            if isinstance(item, dict) else item for item in items] if isinstance(items, list) else (items if items is not None else [])


def _normalize_rerun_evidence(raw: dict[str, Any]) -> Any:
    value = raw.get("rerun_evidence")
    return dict(value) if isinstance(value, dict) else value


def rerun_evidence_path_issues(
    result: Any,
    workspace: Path | str | None,
    *,
    paper_evidence_dir: str = "paper_evidence",
) -> list[str]:
    """Require rerun paper evidence to be an existing trusted workspace file.

    Missing references are observations for the supervisor, not permission for
    the host to alter the Reporter action or scientific conclusion.
    """

    evidence = result.get("rerun_evidence") if isinstance(result, dict) else None
    if not isinstance(evidence, dict):
        return []
    raw_workspace = str(workspace or "").strip()
    if not raw_workspace:
        return ["rerun evidence cannot be validated without a Reporter workspace"]
    workspace_path = Path(raw_workspace)
    evidence_root = workspace_path / paper_evidence_dir
    try:
        workspace_resolved = workspace_path.resolve()
        evidence_root_resolved = evidence_root.resolve()
    except (OSError, ValueError):
        return ["rerun evidence workspace could not be resolved"]
    if (
        not workspace_resolved.is_dir()
        or evidence_root.is_symlink()
        or not evidence_root_resolved.is_dir()
        or not evidence_root_resolved.is_relative_to(workspace_resolved)
    ):
        return ["trusted paper evidence directory is missing"]

    paths = _string_list(evidence.get("paper_evidence_files"))
    if not paths:
        return ["rerun evidence has no paper evidence files"]
    issues: list[str] = []
    for raw_path in paths:
        path = workspace_resolved / raw_path
        try:
            resolved = path.resolve()
            inside = resolved.is_relative_to(evidence_root_resolved)
        except (OSError, ValueError):
            resolved = path
            inside = False
        if not inside or not resolved.is_file() or path.is_symlink():
            issues.append(
                "rerun paper evidence is missing or outside trusted paper evidence: "
                + raw_path
            )
    return issues
def normalize_task_verification(result: Any, expected_task_id: str, *,
        task: dict[str, Any] | None = None, run_valid_hint: bool | None = None,
        evidence_workspace: Path | None = None) -> dict[str, Any]:
    """Bind the note to its dispatched task without judging its conclusion."""
    raw = result if isinstance(result, dict) else {}
    protocol_issues = []
    if not isinstance(result, dict):
        protocol_issues.append("Reporter note is not a JSON object")
    elif not any(not str(key).startswith("_") for key in raw):
        protocol_issues.append("Reporter note is empty")
    reported_task_id = raw.get("task_id")
    reporter_action = raw.get("host_action")
    # The dispatch already owns task identity. Only an explicit rerun request
    # changes the next operation; all other notes proceed to reporting.
    host_action = "rerun_writer" if reporter_action == "rerun_writer" else "complete"
    observations = _string_list(raw.get("_engineering_issues"))
    if reported_task_id is not None and reported_task_id != expected_task_id:
        observations.append("Reporter task_id differs from the dispatched task; the original value is retained")
    if reporter_action is not None and (
        not isinstance(reporter_action, str) or reporter_action not in {"complete", "rerun_writer"}
    ):
        observations.append("Reporter action is not an explicit rerun request; the note proceeds to reporting")
    if not raw.get("outcome"):
        observations.append("Reporter did not supply a separate outcome label; inspect the original note")
    if not raw.get("decision_reason"):
        observations.append("Reporter did not supply a separate decision_reason field")
    core = _normalize_core_conclusions(raw, task, evidence_workspace)
    numeric = _normalize_numeric_comparisons(raw, task, evidence_workspace)
    for items, id_key, expected in (
        (core, "claim_id", _scientific_acceptance(task).get("core_conclusions", [])),
        (numeric, "target_id", _scientific_acceptance(task).get("key_numeric_targets", [])),
    ):
        entries = items if isinstance(items, list) else []
        ids = [str(item.get(id_key) or "") for item in entries if isinstance(item, dict)]
        missing = {str(item.get(id_key)) for item in (expected if isinstance(expected, list) else [])
                   if isinstance(item, dict) and item.get(id_key)} - set(ids)
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in entries):
            observations.append("Scientific observations use another representation; inspect the original content")
        if missing:
            observations.append("Navigation IDs not separately listed: " + ", ".join(sorted(missing)))
        if len(ids) != len(set(ids)) or any(not item for item in ids):
            observations.append("Observation IDs contain duplicates or missing labels")
    for item in [*(core if isinstance(core, list) else []), *(numeric if isinstance(numeric, list) else [])]:
        if not isinstance(item, dict):
            continue
        basis = item.get("basis_review")
        if isinstance(basis, dict) and not basis.get("paper_evidence_verified"):
            observations.append("Basis correction references unavailable original-paper evidence")
    rerun = _normalize_rerun_evidence(raw)
    engineering_status = ("handoff_failed" if protocol_issues
        else "execution_failed" if run_valid_hint is False
        else "verified" if run_valid_hint is True else "unverified_execution")
    return {
        **raw,
        "task_id": expected_task_id, "assigned_task_id": expected_task_id,
        "reported_task_id": reported_task_id,
        "outcome": raw.get("outcome"), "decision_authority": "reporter",
        "reporter_action": reporter_action, "host_action": host_action,
        "engineering_status": engineering_status, "host_run_valid": run_valid_hint,
        "engineering_issues": [*protocol_issues, *observations], "handoff_issues": protocol_issues,
        "host_observations": observations,
        "rerun_reason": raw.get("rerun_reason") or (rerun.get("rerun_reason") if isinstance(rerun, dict) else None) or "none",
        "run_valid": raw.get("run_valid"),
        "core_conclusions": core, "key_numeric_comparisons": numeric,
        "rerun_evidence": rerun,
        "verified_facts": raw.get("verified_facts", []),
    }


def verification_scientifically_successful(result: dict[str, Any]) -> bool:
    """Summarize the Reporter's accepted conclusion, separate from run evidence."""
    return (result.get("outcome") in ("reproduced", "reproduced_with_assumptions")
        and result.get("host_action") == "complete"
        and result.get("handoff_accepted", True) is True
        and result.get("coordination_status") != "stopped"
        and not result.get("handoff_issues"))


def partition_writer_delivery_issues(
    result: Any,
    *,
    require_stopping_assessment: bool = False,
) -> tuple[list[str], list[str]]:
    warnings = writer_delivery_issues(
        result,
        require_stopping_assessment=require_stopping_assessment,
    )
    # Writer-authored JSON is disclosure metadata, not execution authority.
    # The caller combines real host status and readable artifacts separately.
    return [], warnings


def writer_delivery_issues(
    result: Any,
    *,
    require_stopping_assessment: bool = False,
) -> list[str]:
    if not isinstance(result, dict):
        return ["task_agent_result.json is not an object"]
    issues: list[str] = []
    if str(result.get("status") or "") != WRITER_REVIEW_STATUS:
        issues.append(f"writer status should be {WRITER_REVIEW_STATUS}")
    if not str(result.get("summary") or "").strip():
        issues.append("writer summary is empty")
    execution = result.get("execution_summary")
    # New handoffs reference host receipts instead of transcribing counters.
    # Keep old self-reports readable, but their absence is not missing execution.
    if isinstance(execution, dict):
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
        issues.append("writer did not declare a local image; structured evidence may be used instead")
    if require_stopping_assessment and not isinstance(result.get("stopping_assessment"), dict):
        issues.append("writer stopping_assessment is absent; Reporter will assess the result directly")
    return issues


def task_verification_issues(result: Any, expected_task_id: str) -> list[str]:
    """Only an absent or unreadable note prevents a Reporter handoff."""
    del expected_task_id
    if not isinstance(result, dict):
        return ["task_verification_result.json is not an object"]
    return list(result.get("handoff_issues") or []) if result.get("handoff_issues") else (
        ["task_verification_result.json is empty"] if not result else []
    )


def partition_task_verification_issues(result: Any, expected_task_id: str) -> tuple[list[str], list[str]]:
    return task_verification_issues(result, expected_task_id), list(result.get("host_observations", [])) if isinstance(result, dict) else []


def effective_task_outcome(item: dict[str, Any]) -> str:
    """Legacy accessor: never replace the Reporter's outcome with an engineering label."""
    return str(item.get("outcome") or "")


def aggregate_task_verifications(task_results: list[dict[str, Any]]) -> dict[str, Any]:
    tasks = [dict(result) for result in task_results if isinstance(result, dict)]
    counts: dict[str, int] = {}
    for item in tasks:
        label = item.get("outcome")
        if isinstance(label, str) and label:
            counts[label] = counts.get(label, 0) + 1
    all_terminal = bool(tasks) and all(
        item.get("host_action") == "complete" or item.get("coordination_status") == "stopped"
        for item in tasks)
    return {"schema_version": "3.0", "tasks": tasks, "outcome_counts": counts,
            "all_terminal": all_terminal,
            "all_successful": bool(tasks) and all(verification_scientifically_successful(item) for item in tasks),
            "all_full_runs_observed": bool(tasks) and all(item.get("host_run_valid") is True for item in tasks)}


def feedback_from_verification(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(item.get("task_id")): item
        for item in result.get("tasks", [])
        if isinstance(item, dict)
        and str(item.get("task_id") or "")
        and item.get("host_action") == "rerun_writer"
    }
