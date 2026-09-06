from __future__ import annotations

from typing import Any


def build_terminal_review_summary(
    verification_result: dict[str, Any],
) -> dict[str, Any]:
    """Derive the host-owned run summary from terminal task outcomes.

    This is intentionally deterministic and side-effect free. Reporter execution,
    task-record mutation, and artifact writes remain owned by the pipeline.
    """

    all_successful = bool(verification_result.get("all_successful"))
    outcome_counts = {
        str(key): int(value)
        for key, value in verification_result.get("outcome_counts", {}).items()
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0
    }
    successful_tasks = outcome_counts.get("reproduced", 0) + outcome_counts.get(
        "reproduced_with_assumptions", 0
    )
    if all_successful:
        terminal_alignment = "match"
        terminal_credibility = "high"
        terminal_summary = "All tasks reproduced their assigned core conclusions."
    elif successful_tasks:
        terminal_alignment = "partial_match"
        terminal_credibility = "medium"
        terminal_summary = (
            "Some tasks reproduced their core conclusions and others reached "
            "non-positive terminal outcomes."
        )
    elif outcome_counts.get("not_reproduced", 0) and not (
        outcome_counts.get("inconclusive_missing_information", 0)
        or outcome_counts.get("execution_failed", 0)
    ):
        terminal_alignment = "mismatch"
        terminal_credibility = "high"
        terminal_summary = "No task reproduced its assigned core conclusion."
    else:
        terminal_alignment = "inconclusive"
        terminal_credibility = "low"
        terminal_summary = (
            "No positive reproduction conclusion is available because task "
            "evidence is inconclusive or execution failed."
        )

    writer_review_document = {
        "_meta": {"mode": "host_derived_core_conclusion_outcomes"},
        "passed": True,
        "overall_alignment": terminal_alignment,
        "overall_result_credibility": terminal_credibility,
        "overall_summary": terminal_summary,
        "verification_result": verification_result,
    }
    writer_summary_result = {
        "enabled": True,
        "passed": True,
        "scientific_all_successful": all_successful,
        "all_terminal": bool(verification_result.get("all_terminal")),
        "outcome_counts": outcome_counts,
        "mode": "host_derived_core_conclusion_outcomes",
        "overall_alignment": terminal_alignment,
        "overall_result_credibility": terminal_credibility,
        "verification_result": verification_result,
    }
    return {
        "all_successful": all_successful,
        "outcome_counts": outcome_counts,
        "writer_review_document": writer_review_document,
        "writer_summary_result": writer_summary_result,
    }
