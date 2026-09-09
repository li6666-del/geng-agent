from __future__ import annotations

import math
from pathlib import Path

from typing import Any

from .scientific_materiality import (
    TERMINAL_SCIENTIFIC_OUTCOMES,
    WRITER_RERUN_REASONS,
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
    {"supported", "unsupported", "unassessable_missing_information", "not_applicable"}
)


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


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _scientific_acceptance(task: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(task, dict):
        return {}
    value = task.get("scientific_acceptance")
    return value if isinstance(value, dict) else {}


def _normalize_core_conclusions(raw: dict[str, Any], task: dict[str, Any] | None,
                                evidence_workspace: Path | None = None) -> list[dict[str, Any]]:
    """Preserve the Reporter's assessments, including independent findings."""
    del task
    items = raw.get("core_conclusions")
    result = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        normalized = dict(item)
        basis = _paper_basis_review(item, evidence_workspace)
        if basis:
            normalized["basis_review"] = basis
        result.append(normalized)
    return result


def _normalize_numeric_item(target: dict[str, Any], candidate: dict[str, Any], *,
                            target_id: str, workspace: Path | None) -> dict[str, Any]:
    # Text, units, signs, scale and comparability are Reporter decisions. Arithmetic
    # is diagnostic only and never changes the decision or authorizes another run.
    fields = {"name", "metric", "unit", "regime", "paper_magnitude", "local_magnitude", "comparison_status",
              "comparison_reason", "local_metric", "local_unit", "local_regime", "unavailable_reason", "basis_review"}
    result = {key: value for key, value in {**target, **candidate}.items() if key in fields}
    result["target_id"] = target_id
    result.setdefault("name", target_id)
    basis = _paper_basis_review(candidate, workspace)
    if basis:
        result["basis_review"] = basis
    if basis and basis.get("status") == "corrected" and "paper_magnitude" not in candidate:
        result["paper_magnitude"] = basis.get("corrected_paper_magnitude")
    paper = _finite_number(result.get("paper_magnitude"))
    local = _finite_number(result.get("local_magnitude"))
    result["paper_magnitude"] = paper
    result["local_magnitude"] = local
    result["symmetric_ratio"] = (symmetric_magnitude_ratio(paper, local)
        if candidate.get("comparison_status") == "comparable" else None)
    return result


def _normalize_numeric_comparisons(raw: dict[str, Any], task: dict[str, Any] | None,
                                   evidence_workspace: Path | None = None) -> list[dict[str, Any]]:
    targets = {str(item.get("target_id")): item
               for item in _scientific_acceptance(task).get("key_numeric_targets", [])
               if isinstance(item, dict)}
    items = raw.get("key_numeric_comparisons")
    return [_normalize_numeric_item(targets.get(str(item.get("target_id")), {}), item,
                target_id=str(item.get("target_id") or ""), workspace=evidence_workspace)
            for item in items if isinstance(item, dict)] if isinstance(items, list) else []


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
def _rerun_reason_if_actionable(*, run_valid: bool | None,
        core: list[dict[str, Any]], numeric: list[dict[str, Any]],
        evidence: dict[str, Any] | None) -> str:
    """Check a routing contract, without deciding scientific materiality."""
    del run_valid
    if not isinstance(evidence, dict):
        return "none"
    reason = str(evidence.get("rerun_reason") or "none")
    known_ids = {str(item.get("claim_id") or "") for item in core} | {
        str(item.get("target_id") or "") for item in numeric}
    ids = set(_string_list(evidence.get("contract_item_ids")))
    if (reason in WRITER_RERUN_REASONS and ids and ids <= known_ids
        and _string_list(evidence.get("paper_evidence_files"))
        and str(evidence.get("causal_change") or "").strip()
        and _string_list(evidence.get("change_targets"))
        and str(evidence.get("predicted_effect") or "").strip()):
        return reason
    return "none"


def normalize_task_verification(result: Any, expected_task_id: str, *,
        task: dict[str, Any] | None = None, run_valid_hint: bool | None = None,
        evidence_workspace: Path | None = None) -> dict[str, Any]:
    """Receive a v3 Reporter decision. Host checks never rewrite its science.

    An incomplete handoff is an engineering state, not missing paper information.
    Original notes remain in the Reporter audit workspace, including rejected ones.
    """
    raw = result if isinstance(result, dict) else {}
    core = _normalize_core_conclusions(raw, task, evidence_workspace)
    numeric = _normalize_numeric_comparisons(raw, task, evidence_workspace)
    issues = []
    if raw.get("schema_version") != "3.0":
        issues.append("Reporter must submit the v3 decision protocol; old notes require Reporter review")
    if raw.get("task_id") != expected_task_id:
        issues.append("Reporter task_id does not match the assigned task")
    outcome = raw.get("outcome")
    if not isinstance(outcome, str) or outcome not in TERMINAL_SCIENTIFIC_OUTCOMES - {"review_incomplete"}:
        issues.append("Reporter must supply an explicit scientific outcome")
        outcome = "review_incomplete"
    reason = str(raw.get("decision_reason") or "").strip()
    if not reason:
        issues.append("Reporter must supply the direct decision_reason")
    action = raw.get("host_action")
    if not isinstance(action, str) or action not in {"complete", "rerun_writer"}:
        issues.append("Reporter must supply complete or rerun_writer")
        action = "complete"
    if "run_valid" not in raw or (raw["run_valid"] is not None and not isinstance(raw["run_valid"], bool)):
        issues.append("Reporter run_valid must be boolean or null")
    for items, id_key, expected in (
        (core, "claim_id", _scientific_acceptance(task).get("core_conclusions", [])),
        (numeric, "target_id", _scientific_acceptance(task).get("key_numeric_targets", [])),
    ):
        ids = [str(item.get(id_key) or "") for item in items]
        if any(not value for value in ids) or len(ids) != len(set(ids)):
            issues.append("Reporter observations need unique explicit " + id_key)
        missing = {str(item.get(id_key)) for item in expected if isinstance(item, dict) and item.get(id_key)} - set(ids)
        if missing:
            issues.append("Reporter must account for navigation IDs (including not_applicable): " + ", ".join(sorted(missing)))
    if any(not isinstance(item.get("status"), str) or item["status"] not in _CORE_STATUSES for item in core):
        issues.append("Reporter conclusion status is missing or invalid")
    if any(not isinstance(item.get("comparison_status"), str) or item["comparison_status"] not in {"comparable", "incompatible", "disputed", "not_applicable", "unavailable"}
           or not str(item.get("comparison_reason") or "").strip() for item in numeric):
        issues.append("Reporter numeric comparisons need comparison_status and comparison_reason")
    rerun = _normalize_rerun_evidence(raw)
    rerun_reason = _rerun_reason_if_actionable(run_valid=raw.get("run_valid"), core=core, numeric=numeric, evidence=rerun)
    if action == "rerun_writer" and rerun_reason == "none":
        issues.append("Reporter rerun request needs a complete causal plan with existing observation IDs")
    evidence_issues = _string_list(raw.get("_engineering_issues"))
    for item in [*core, *numeric]:
        basis = item.get("basis_review")
        if isinstance(basis, dict) and not basis.get("paper_evidence_verified"):
            evidence_issues.append("Basis correction lacks an existing original-paper evidence reference")
    engineering_status = ("handoff_failed" if issues else "evidence_invalid" if evidence_issues
        else "execution_failed" if run_valid_hint is False
        else "verified" if run_valid_hint is True else "unverified_execution")
    if issues or evidence_issues:
        action = "complete"  # Caller may repair the Reporter handoff; never rerun science for it.
    ratios = [x["symmetric_ratio"] for x in numeric if _finite_number(x.get("symmetric_ratio")) is not None]
    return {
        "schema_version": "3.0", "task_id": expected_task_id,
        "outcome": outcome, "decision_reason": reason, "decision_authority": "reporter",
        "reporter_action": raw.get("host_action"), "host_action": action,
        "engineering_status": engineering_status,
        "engineering_issues": [*issues, *evidence_issues], "handoff_issues": issues,
        "rerun_reason": rerun_reason if action == "rerun_writer" else "none",
        "run_valid": False if run_valid_hint is False else raw.get("run_valid") if isinstance(raw.get("run_valid"), bool) else None,
        "core_conclusions": core, "key_numeric_comparisons": numeric,
        "max_key_numeric_ratio": max(ratios) if ratios else None,
        "comparison_summary": str(raw.get("comparison_summary") or ""),
        "report_explanation": str(raw.get("report_explanation") or ""),
        "report_title": str(raw.get("report_title") or ""),
        **{key: _string_list(raw.get(key)) for key in (
            "differences", "non_material_differences", "evidence_files", "feedback",
            "remaining_uncertainties", "local_assets", "paper_assets", "asset_notes")},
        "confidence": raw.get("confidence") if isinstance(raw.get("confidence"), str) and raw["confidence"] in {"low", "medium", "high"} else "medium",
        "rerun_evidence": rerun,
        "verified_facts": raw.get("verified_facts") if isinstance(raw.get("verified_facts"), list) else [],
    }


def verification_scientifically_successful(result: dict[str, Any]) -> bool:
    return (result.get("outcome") in {"reproduced", "reproduced_with_assumptions"}
        and result.get("engineering_status") == "verified" and result.get("run_valid") is True
        and result.get("host_action") == "complete" and result.get("reporter_action") != "rerun_writer"
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
    blockers = list(result.get("handoff_issues", [])) if isinstance(result, dict) else []
    warnings: list[str] = []
    for issue in task_verification_issues(result, expected_task_id):
        if issue.startswith(("task_verification_result.json", "task_id must be")):
            blockers.append(issue)
        else:
            warnings.append(issue)
    return blockers, warnings


def writer_revision_allowed(result: Any, expected_task_id: str) -> bool:
    if (not isinstance(result, dict) or result.get("task_id") != expected_task_id
        or result.get("host_action") != "rerun_writer" or result.get("handoff_issues")
        or result.get("engineering_status") in {"handoff_failed", "evidence_invalid"}):
        return False
    return _rerun_reason_if_actionable(run_valid=result.get("run_valid"),
        core=result.get("core_conclusions", []), numeric=result.get("key_numeric_comparisons", []),
        evidence=result.get("rerun_evidence")) == result.get("rerun_reason") != "none"


def effective_task_outcome(item: dict[str, Any]) -> str:
    """Eligibility for publication, distinct from the preserved Reporter verdict."""
    if item.get("engineering_status") in {"handoff_failed", "evidence_invalid", "unverified_execution"}:
        return "review_incomplete"
    if item.get("engineering_status") == "execution_failed":
        return "execution_failed"
    if item.get("outcome") in {"reproduced", "reproduced_with_assumptions"} and not verification_scientifically_successful(item):
        return "review_incomplete"
    return str(item.get("outcome") or "review_incomplete")


def aggregate_task_verifications(task_results: list[dict[str, Any]]) -> dict[str, Any]:
    tasks: list[dict[str, Any]] = []
    keys = (
        "task_id",
        "decision_reason", "decision_authority", "reporter_action",
        "engineering_status", "engineering_issues", "handoff_issues",
        "outcome",
        "host_action",
        "rerun_reason",
        "run_valid",
        "core_conclusions",
        "key_numeric_comparisons",
        "max_key_numeric_ratio",
        "comparison_summary",
        "report_explanation",
        "report_title",
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
        outcome = effective_task_outcome(item)
        outcome_counts[outcome] = outcome_counts.get(outcome, 0) + 1
    all_terminal = bool(tasks) and all(
        str(item.get("host_action") or "") == "complete"
        and str(item.get("outcome") or "") in TERMINAL_SCIENTIFIC_OUTCOMES
        for item in tasks
    )
    all_successful = all_terminal and all(
        verification_scientifically_successful(item)
        for item in tasks
    )
    return {
        "schema_version": "3.0",
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
