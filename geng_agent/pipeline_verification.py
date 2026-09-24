"""Mechanical aggregation of preserved independent Reporter notes."""
from __future__ import annotations
from typing import Any


def build_terminal_review_summary(verification_result: dict[str, Any]) -> dict[str, Any]:
    counts = dict(verification_result.get("outcome_counts") or {})
    summary = {"enabled": True, "passed": bool(verification_result.get("all_terminal")),
               "all_terminal": bool(verification_result.get("all_terminal")),
               "scientific_all_successful": bool(verification_result.get("all_successful")),
               "all_full_runs_observed": bool(verification_result.get("all_full_runs_observed")),
               "outcome_counts": counts, "verification_result": verification_result,
               "mode": "preserved_reporter_decisions"}
    return {"all_successful": bool(verification_result.get("all_successful")),
            "outcome_counts": counts, "writer_review_document": dict(summary),
            "writer_summary_result": dict(summary)}
