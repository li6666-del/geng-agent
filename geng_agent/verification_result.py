from __future__ import annotations

import math
from pathlib import Path

from typing import Any

from .scientific_materiality import (
    TERMINAL_SCIENTIFIC_OUTCOMES,
    WRITER_RERUN_REASONS,
    is_material_numeric_ratio,
    symmetric_magnitude_ratio,
)


WRITER_REVIEW_STATUS = "ready_for_review"
FINAL_MATCHED_STATUS = "matched"
TASK_REPORTER_RERUN_NONE = "none"
TASK_REPORTER_RERUN_CORE_CONCLUSION_FAILED = "core_conclusion_failed"
TASK_REPORTER_RERUN_KEY_NUMERIC_RATIO_GE_10 = "key_numeric_ratio_ge_10"
TASK_REPORTER_RERUN_INVALID_RUN = "invalid_run"
TASK_REPORTER_WRITER_RERUN_REASONS = WRITER_RERUN_REASONS

_CORE_STATUSES = frozenset(
    {"supported", "unsupported", "unassessable_missing_information"}
)


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _scientific_acceptance(task: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(task, dict):
        return {}
    value = task.get("scientific_acceptance")
    return value if isinstance(value, dict) else {}


def _normalize_core_item(
    raw: Any,
    *,
    claim_id: str,
    fallback_observation: str = "",
    force_claim_id: bool = False,
) -> dict[str, Any]:
    item = raw if isinstance(raw, dict) else {}
    status = str(item.get("status") or "").strip()
    if status not in _CORE_STATUSES:
        supported = item.get("supported")
        if supported is True:
            status = "supported"
        elif supported is False:
            status = "unsupported"
        else:
            status = "unassessable_missing_information"
    observation = str(
        item.get("local_observation")
        or item.get("evidence")
        or ""
    ).strip()
    evidence_files = _string_list(item.get("evidence_files"))
    if status in {"supported", "unsupported"} and not observation and not evidence_files:
        status = "unassessable_missing_information"
    return {
        "claim_id": (
            claim_id
            if force_claim_id
            else str(item.get("claim_id") or claim_id).strip()
        ),
        "status": status,
        # Explanatory host text describes missing evidence. It must never be
        # considered an observation that supports a scientific conclusion.
        "local_observation": observation or fallback_observation,
        "evidence_files": evidence_files,
    }


def _combine_core_observations(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Retain conflicting assessments of one claim instead of last-write wins."""

    result = dict(items[0])
    statuses = {item["status"] for item in items}
    result["status"] = (
        "unsupported" if "unsupported" in statuses
        else "unassessable_missing_information"
        if "unassessable_missing_information" in statuses
        else "supported"
    )
    result["local_observation"] = "\n".join(dict.fromkeys(
        str(item.get("local_observation") or "") for item in items
        if str(item.get("local_observation") or "")
    ))
    result["evidence_files"] = list(dict.fromkeys(
        path for item in items for path in item["evidence_files"]
    ))
    return result


def _normalize_core_conclusions(
    raw: dict[str, Any],
    task: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    raw_items = raw.get("core_conclusions")
    candidates = [item for item in raw_items if isinstance(item, dict)] if isinstance(raw_items, list) else []
    contract = _scientific_acceptance(task)
    criteria = [
        item
        for item in contract.get("core_conclusions", [])
        if isinstance(item, dict)
    ]
    normalized: list[dict[str, Any]] = []
    consumed: set[int] = set()
    for index, criterion in enumerate(criteria):
        claim_id = str(criterion.get("claim_id") or f"claim_{index + 1}").strip()
        matches = [
            item_index for item_index, item in enumerate(candidates)
            if item_index not in consumed
            and str(item.get("claim_id") or "").strip() == claim_id
        ]
        if not matches:
            matches = [
                item_index for item_index, item in enumerate(candidates)
                if item_index not in consumed
                and not str(item.get("claim_id") or "").strip()
            ][:1]
        consumed.update(matches)
        observations = [candidates[item_index] for item_index in matches] or [{}]
        normalized.append(_combine_core_observations([
            _normalize_core_item(
                observation,
                claim_id=claim_id,
                force_claim_id=True,
                fallback_observation=(
                    "Reporter did not provide a conclusion assessment; "
                    "recorded as missing information rather than blocking the flow."
                ),
            ) for observation in observations
        ]))

    # A Designer omission or stale ID must not erase an independently observed
    # failure. Preserve every unconsumed Reporter claim, including unsupported
    # mechanism/method findings outside the Designer's navigation list.
    by_id = {item["claim_id"]: index for index, item in enumerate(normalized)}
    for index, candidate in enumerate(candidates):
        if index in consumed:
            continue
        item = _normalize_core_item(candidate, claim_id=f"reported_claim_{index + 1}")
        position = by_id.get(item["claim_id"])
        if position is None:
            by_id[item["claim_id"]] = len(normalized)
            normalized.append(item)
        else:
            normalized[position] = _combine_core_observations([normalized[position], item])
    if normalized:
        return normalized
    task_id = str((task or {}).get("task_id") or "task")
    return [
        _normalize_core_item(
            {},
            claim_id=f"{task_id}.core",
            fallback_observation="No itemized conclusion was available.",
        )
    ]
def _normalize_numeric_comparisons(
    raw: dict[str, Any],
    task: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    raw_items = raw.get("key_numeric_comparisons")
    candidates = [item for item in raw_items if isinstance(item, dict)] if isinstance(raw_items, list) else []
    targets = [
        item
        for item in _scientific_acceptance(task).get("key_numeric_targets", [])
        if isinstance(item, dict)
    ]
    normalized: list[dict[str, Any]] = []
    consumed: set[int] = set()
    for index, target in enumerate(targets):
        target_id = str(target.get("target_id") or f"numeric_{index + 1}").strip()
        candidate_index = next(
            (
                item_index
                for item_index, item in enumerate(candidates)
                if item_index not in consumed
                and str(item.get("target_id") or "").strip() == target_id
            ),
            None,
        )
        if candidate_index is None:
            candidate_index = next(
                (
                    item_index
                    for item_index, item in enumerate(candidates)
                    if item_index not in consumed
                    and not str(item.get("target_id") or "").strip()
                ),
                None,
            )
        candidate = candidates[candidate_index] if candidate_index is not None else {}
        if candidate_index is not None:
            consumed.add(candidate_index)
        candidate = candidate if isinstance(candidate, dict) else {}
        paper_value = _finite_number(target.get("paper_magnitude"))
        if paper_value is None:
            paper_value = _finite_number(candidate.get("paper_magnitude"))
        local_value = _finite_number(candidate.get("local_magnitude"))
        normalized.append(
            {
                "target_id": target_id,
                "name": str(target.get("name") or candidate.get("name") or target_id),
                "paper_magnitude": paper_value,
                "local_magnitude": local_value,
                "symmetric_ratio": symmetric_magnitude_ratio(paper_value, local_value),
                "unavailable_reason": str(candidate.get("unavailable_reason") or "").strip(),
            }
        )

    # Task-Designer IDs are navigation aids, not authority to erase material
    # evidence. Preserve Reporter comparisons that were not consumed above.
    for candidate_index, candidate in enumerate(candidates):
        if candidate_index in consumed:
            continue
        target_id = str(
            candidate.get("target_id") or f"reported_numeric_{candidate_index + 1}"
        ).strip()
        paper_value = _finite_number(candidate.get("paper_magnitude"))
        local_value = _finite_number(candidate.get("local_magnitude"))
        normalized.append(
            {
                "target_id": target_id,
                "name": str(candidate.get("name") or target_id),
                "paper_magnitude": paper_value,
                "local_magnitude": local_value,
                "symmetric_ratio": symmetric_magnitude_ratio(paper_value, local_value),
                "unavailable_reason": str(candidate.get("unavailable_reason") or "").strip(),
            }
        )
    return normalized


def _normalize_rerun_evidence(raw: dict[str, Any]) -> dict[str, Any] | None:
    value = raw.get("rerun_evidence")
    if not isinstance(value, dict):
        return None
    reason = str(value.get("rerun_reason") or "none").strip()
    if reason not in {"none", *WRITER_RERUN_REASONS}:
        reason = "none"
    return {
        "rerun_reason": reason,
        "contract_item_ids": _string_list(value.get("contract_item_ids")),
        "paper_evidence_files": _string_list(value.get("paper_evidence_files")),
        "causal_change": str(value.get("causal_change") or "").strip(),
        "change_targets": _string_list(value.get("change_targets")),
        "predicted_effect": str(value.get("predicted_effect") or "").strip(),
    }


def rerun_evidence_path_issues(
    result: Any,
    workspace: Path | str | None,
    *,
    paper_evidence_dir: str = "paper_evidence",
) -> list[str]:
    """Require rerun paper evidence to be an existing trusted workspace file.

    Invalid evidence only cancels another Writer run. Callers retain it as an
    advisory warning and continue to a reportable terminal outcome.
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
def _rerun_reason_if_actionable(
    *,
    run_valid: bool | None,
    core: list[dict[str, Any]],
    numeric: list[dict[str, Any]],
    evidence: dict[str, Any] | None,
) -> str:
    if not isinstance(evidence, dict):
        return "none"
    reason = str(evidence.get("rerun_reason") or "none")
    if reason not in WRITER_RERUN_REASONS:
        return "none"
    if not (
        _string_list(evidence.get("contract_item_ids"))
        and _string_list(evidence.get("paper_evidence_files"))
        and str(evidence.get("causal_change") or "").strip()
        and _string_list(evidence.get("change_targets"))
        and str(evidence.get("predicted_effect") or "").strip()
    ):
        return "none"

    evidence_ids = set(_string_list(evidence.get("contract_item_ids")))
    if reason == "invalid_run":
        known_ids = {
            str(item.get("claim_id") or "")
            for item in core
            if str(item.get("claim_id") or "")
        } | {
            str(item.get("target_id") or "")
            for item in numeric
            if str(item.get("target_id") or "")
        }
        return (
            reason
            if run_valid is False
            and bool(evidence_ids)
            and evidence_ids <= known_ids
            else "none"
        )
    if reason == "core_conclusion_failed":
        failed_ids = {
            str(item.get("claim_id") or "")
            for item in core
            if item.get("status") == "unsupported"
        }
        return (
            reason
            if bool(evidence_ids) and evidence_ids <= failed_ids
            else "none"
        )
    material_ids = {
        str(item.get("target_id") or "")
        for item in numeric
        if is_material_numeric_ratio(item.get("symmetric_ratio"))
    }
    return (
        reason
        if bool(evidence_ids) and evidence_ids <= material_ids
        else "none"
    )


def _derive_run_valid(
    raw: dict[str, Any],
    run_valid_hint: bool | None,
) -> bool | None:
    """Combine host execution evidence with Reporter-observed output validity.

    A definite host failure cannot be overridden. A successful process is not
    sufficient to prove that its scientific outputs are readable and usable,
    so a Reporter may still mark that run invalid; an actual rerun remains
    subject to the causal-evidence checks below.
    """

    reported = raw.get("run_valid")
    if run_valid_hint is False:
        return False
    if reported is False:
        return False
    if run_valid_hint is True:
        return True
    return reported if isinstance(reported, bool) else None
def _has_material_core_assumption(task: dict[str, Any] | None) -> bool:
    if not isinstance(task, dict):
        return False
    assumptions = task.get("assumptions")
    if isinstance(assumptions, list) and any(
        isinstance(item, dict)
        and str(item.get("risk") or "").strip().lower() == "high"
        for item in assumptions
    ):
        return True
    gaps = _scientific_acceptance(task).get("information_gaps")
    return isinstance(gaps, list) and any(
        isinstance(item, dict)
        and bool(_string_list(item.get("affects_claim_ids")))
        for item in gaps
    )


def _derive_outcome(
    *,
    task: dict[str, Any] | None,
    run_valid: bool | None,
    core: list[dict[str, Any]],
    numeric: list[dict[str, Any]],
    remaining_uncertainties: list[str],
    rerun_reason: str,
) -> tuple[str, str]:
    if rerun_reason in WRITER_RERUN_REASONS:
        if run_valid is False:
            return "execution_failed", "rerun_writer"
        return "not_reproduced", "rerun_writer"
    if run_valid is not True:
        return "execution_failed", "complete"
    statuses = {str(item.get("status") or "") for item in core}
    if "unsupported" in statuses:
        return "not_reproduced", "complete"
    if "unassessable_missing_information" in statuses or not core:
        return "inconclusive_missing_information", "complete"
    if any(
        item.get("paper_magnitude") is not None and item.get("local_magnitude") is None
        for item in numeric
    ):
        return "inconclusive_missing_information", "complete"
    if any(is_material_numeric_ratio(item.get("symmetric_ratio")) for item in numeric):
        return "not_reproduced", "complete"
    return (
        "reproduced_with_assumptions" if _has_material_core_assumption(task) else "reproduced",
        "complete",
    )


def normalize_task_verification(
    result: Any,
    expected_task_id: str,
    *,
    task: dict[str, Any] | None = None,
    run_valid_hint: bool | None = None,
) -> dict[str, Any]:
    """Normalize a Reporter note and let the host derive action and outcome.

    Missing structure becomes explicit uncertainty. It never causes a Writer rerun.
    """

    raw = result if isinstance(result, dict) else {}
    core = _normalize_core_conclusions(raw, task)
    numeric = _normalize_numeric_comparisons(raw, task)
    remaining_uncertainties = _string_list(raw.get("remaining_uncertainties"))
    run_valid = _derive_run_valid(raw, run_valid_hint)
    rerun_evidence = _normalize_rerun_evidence(raw)
    rerun_reason = _rerun_reason_if_actionable(
        run_valid=run_valid,
        core=core,
        numeric=numeric,
        evidence=rerun_evidence,
    )
    outcome, host_action = _derive_outcome(
        task=task,
        run_valid=run_valid,
        core=core,
        numeric=numeric,
        remaining_uncertainties=remaining_uncertainties,
        rerun_reason=rerun_reason,
    )
    ratios = [
        float(item["symmetric_ratio"])
        for item in numeric
        if _finite_number(item.get("symmetric_ratio")) is not None
    ]
    feedback = _string_list(raw.get("feedback"))
    if host_action == "rerun_writer" and rerun_evidence is not None:
        feedback = feedback or [str(rerun_evidence.get("causal_change") or "")]
    confidence = str(raw.get("confidence") or "medium")
    if confidence not in {"low", "medium", "high"}:
        confidence = "medium"
    summary = str(raw.get("comparison_summary") or "").strip()
    if not summary:
        summary = "Reporter supplied no prose summary; the host retained the itemized outcome."
    return {
        "schema_version": "2.0",
        "task_id": str(expected_task_id),
        "outcome": outcome,
        "host_action": host_action,
        "rerun_reason": rerun_reason,
        "run_valid": run_valid,
        "core_conclusions": core,
        "key_numeric_comparisons": numeric,
        "max_key_numeric_ratio": max(ratios) if ratios else None,
        "comparison_summary": summary,
        "differences": _string_list(raw.get("differences")),
        "non_material_differences": _string_list(raw.get("non_material_differences")),
        "evidence_files": _string_list(raw.get("evidence_files")),
        "feedback": feedback,
        "confidence": confidence,
        "remaining_uncertainties": remaining_uncertainties,
        "rerun_evidence": rerun_evidence,
        "local_assets": _string_list(raw.get("local_assets")),
        "paper_assets": _string_list(raw.get("paper_assets")),
    }
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
        issues.append("writer did not declare a local image; structured evidence may be used instead")
    if require_stopping_assessment and not isinstance(result.get("stopping_assessment"), dict):
        issues.append("writer stopping_assessment is absent; Reporter will assess the result directly")
    return issues


def task_verification_issues(result: Any, expected_task_id: str) -> list[str]:
    """Only flag contradictions that make host routing impossible."""

    if not isinstance(result, dict):
        return ["task_verification_result.json is not an object"]
    issues: list[str] = []
    if str(result.get("task_id") or "") != str(expected_task_id):
        issues.append(f"task_id must be {expected_task_id}")
    if str(result.get("outcome") or "") not in TERMINAL_SCIENTIFIC_OUTCOMES:
        issues.append("outcome is not a recognized scientific outcome")
    if str(result.get("host_action") or "") not in {"complete", "rerun_writer"}:
        issues.append("host_action must be complete or rerun_writer")
    if str(result.get("host_action") or "") == "rerun_writer" and str(
        result.get("rerun_reason") or ""
    ) not in WRITER_RERUN_REASONS:
        issues.append("rerun_writer requires an allowed scientific rerun reason")
    return issues


def partition_task_verification_issues(
    result: Any,
    expected_task_id: str,
) -> tuple[list[str], list[str]]:
    blockers: list[str] = []
    warnings: list[str] = []
    for issue in task_verification_issues(result, expected_task_id):
        if issue.startswith(("task_verification_result.json", "task_id must be")):
            blockers.append(issue)
        else:
            warnings.append(issue)
    return blockers, warnings


def writer_revision_allowed(result: Any, expected_task_id: str) -> bool:
    if not isinstance(result, dict) or str(result.get("task_id") or "") != str(expected_task_id):
        return False
    if str(result.get("host_action") or "") != "rerun_writer":
        return False
    reason = str(result.get("rerun_reason") or "none")
    evidence = result.get("rerun_evidence")
    if not isinstance(evidence, dict):
        return False
    evidence_ids = set(_string_list(evidence.get("contract_item_ids")))
    known_ids = {
        str(item.get("claim_id") or "")
        for item in result.get("core_conclusions", [])
        if isinstance(item, dict) and str(item.get("claim_id") or "")
    } | {
        str(item.get("target_id") or "")
        for item in result.get("key_numeric_comparisons", [])
        if isinstance(item, dict) and str(item.get("target_id") or "")
    }
    reason_ids_are_consistent = False
    if reason == "invalid_run":
        reason_ids_are_consistent = (
            result.get("run_valid") is False
            and bool(evidence_ids)
            and evidence_ids <= known_ids
        )
    elif reason == "core_conclusion_failed":
        failed_ids = {
            str(item.get("claim_id") or "")
            for item in result.get("core_conclusions", [])
            if isinstance(item, dict)
            and item.get("status") == "unsupported"
            and str(item.get("claim_id") or "")
        }
        reason_ids_are_consistent = bool(evidence_ids) and evidence_ids <= failed_ids
    elif reason == "key_numeric_ratio_ge_10":
        material_ids = {
            str(item.get("target_id") or "")
            for item in result.get("key_numeric_comparisons", [])
            if isinstance(item, dict)
            and is_material_numeric_ratio(item.get("symmetric_ratio"))
            and str(item.get("target_id") or "")
        }
        reason_ids_are_consistent = bool(evidence_ids) and evidence_ids <= material_ids
    return (
        reason in WRITER_RERUN_REASONS
        and str(evidence.get("rerun_reason") or "") == reason
        and bool(evidence_ids)
        and bool(_string_list(evidence.get("paper_evidence_files")))
        and bool(str(evidence.get("causal_change") or "").strip())
        and bool(_string_list(evidence.get("change_targets")))
        and bool(str(evidence.get("predicted_effect") or "").strip())
        and reason_ids_are_consistent
    )
def aggregate_task_verifications(task_results: list[dict[str, Any]]) -> dict[str, Any]:
    tasks: list[dict[str, Any]] = []
    keys = (
        "task_id",
        "outcome",
        "host_action",
        "rerun_reason",
        "run_valid",
        "core_conclusions",
        "key_numeric_comparisons",
        "max_key_numeric_ratio",
        "comparison_summary",
        "differences",
        "non_material_differences",
        "evidence_files",
        "feedback",
        "confidence",
        "remaining_uncertainties",
    )
    for result in task_results:
        if isinstance(result, dict):
            tasks.append({key: result.get(key) for key in keys})
    outcome_counts: dict[str, int] = {}
    for item in tasks:
        outcome = str(item.get("outcome") or "inconclusive_missing_information")
        outcome_counts[outcome] = outcome_counts.get(outcome, 0) + 1
    all_terminal = bool(tasks) and all(
        str(item.get("host_action") or "") == "complete"
        and str(item.get("outcome") or "") in TERMINAL_SCIENTIFIC_OUTCOMES
        for item in tasks
    )
    all_successful = all_terminal and all(
        str(item.get("outcome") or "") in {"reproduced", "reproduced_with_assumptions"}
        for item in tasks
    )
    return {
        "schema_version": "2.0",
        "all_terminal": all_terminal,
        "all_successful": all_successful,
        "outcome_counts": outcome_counts,
        "tasks": tasks,
    }


def verification_result_issues(result: Any, expected_task_ids: list[str]) -> list[str]:
    if not isinstance(result, dict):
        return ["verification_result.json is not an object"]
    issues: list[str] = []
    raw_tasks = result.get("tasks") if isinstance(result.get("tasks"), list) else []
    ids = [str(item.get("task_id") or "") for item in raw_tasks if isinstance(item, dict)]
    if any(not task_id for task_id in ids) or len(ids) != len(set(ids)):
        issues.append("task verification IDs should be non-empty and unique")
    expected = {str(task_id) for task_id in expected_task_ids if str(task_id)}
    missing = sorted(expected - set(ids))
    unexpected = sorted(set(ids) - expected)
    if missing:
        issues.append("missing task verification results: " + ", ".join(missing))
    if unexpected:
        issues.append("unexpected task verification results: " + ", ".join(unexpected))
    if result.get("all_terminal") is not (
        bool(raw_tasks)
        and all(
            isinstance(item, dict)
            and item.get("host_action") == "complete"
            and item.get("outcome") in TERMINAL_SCIENTIFIC_OUTCOMES
            for item in raw_tasks
        )
    ):
        issues.append("all_terminal is inconsistent with task outcomes")
    return issues


def feedback_from_verification(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(item.get("task_id")): item
        for item in result.get("tasks", [])
        if isinstance(item, dict)
        and str(item.get("task_id") or "")
        and item.get("host_action") == "rerun_writer"
    }
