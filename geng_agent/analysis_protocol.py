"""Compatibility surface: analysis handoffs have no host schema gate."""
from __future__ import annotations

from typing import Any


ANALYSIS_STAGES = frozenset({
    "paper_understanding", "engineering_facts", "repro_tasks", "experiment_plan",
    "targeted_fact_backfill", "paper_thesis", "scientific_architecture", "experiment_index",
})


def analysis_protocol_issues(stage: str, data: Any) -> list:
    """No preflight output-schema gate. Actual consumers report read failures."""
    return []
