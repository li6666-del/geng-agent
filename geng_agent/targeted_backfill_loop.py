from __future__ import annotations

import copy
from typing import Any, Callable

from .facts_coverage import merge_engineering_facts
from .task_evidence_backfill import (
    collect_missing_fact_requests,
    compute_material_backfill_delta,
    cumulative_resolution_from_ledger,
    filter_actionable_requests,
    merge_request_worklists,
    reconcile_final_tasks,
    summarize_backfill_resolution,
    update_search_ledger,
)


BackfillRunner = Callable[
    [int, list[dict[str, Any]], dict[str, Any], dict[str, Any], dict[str, Any]],
    dict[str, Any],
]
TaskRefresher = Callable[
    [int, dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]],
    dict[str, Any],
]
TaskNormalizer = Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]
RoundObserver = Callable[[int, dict[str, Any]], None]


def run_targeted_backfill_loop(
    *,
    initial_facts: dict[str, Any],
    preliminary_tasks: dict[str, Any],
    run_backfill: BackfillRunner,
    refresh_tasks: TaskRefresher,
    normalize_tasks: TaskNormalizer,
    max_rounds: int = 6,
    on_round: RoundObserver | None = None,
) -> dict[str, Any]:
    """Iterate only when refreshed tasks expose previously unseen evidence fields."""
    facts = copy.deepcopy(initial_facts)
    tasks = copy.deepcopy(preliminary_tasks)
    ledger: dict[str, Any] = {"entries": [], "latest": [], "round_count": 0}
    known_requests = collect_missing_fact_requests(tasks)
    cumulative_backfill: dict[str, Any] = {
        "paper_domain": "communication",
        "paper_repro_type": initial_facts.get("paper_repro_type", "other"),
        "engineering_facts": [],
        "missing_information": [],
    }
    round_summaries: list[dict[str, Any]] = []
    stop_reason = "no_material_requests" if not known_requests else "max_rounds_reached"

    for round_index in range(1, max(1, int(max_rounds)) + 1):
        actionable = filter_actionable_requests(known_requests, ledger)
        if not actionable:
            stop_reason = "all_requests_terminal" if known_requests else "no_material_requests"
            break

        previous_ledger = copy.deepcopy(ledger)
        before_requests = copy.deepcopy(known_requests)
        backfill_result = run_backfill(round_index, actionable, facts, tasks, ledger)
        cumulative_backfill, _ = merge_engineering_facts(cumulative_backfill, backfill_result)
        facts, raw_fact_delta = merge_engineering_facts(facts, backfill_result)

        round_resolution = summarize_backfill_resolution(actionable, facts, backfill_result)
        ledger = update_search_ledger(
            ledger,
            round_index=round_index,
            requests=actionable,
            resolution=round_resolution,
        )
        cumulative_resolution = cumulative_resolution_from_ledger(
            known_requests, facts, ledger
        )
        candidate_tasks = refresh_tasks(
            round_index, tasks, facts, cumulative_resolution, ledger
        )
        candidate_tasks = normalize_tasks(candidate_tasks, facts)
        candidate_requests = collect_missing_fact_requests(candidate_tasks)
        known_requests = merge_request_worklists(known_requests, candidate_requests)
        cumulative_resolution = cumulative_resolution_from_ledger(
            known_requests, facts, ledger
        )
        tasks = reconcile_final_tasks(tasks, candidate_tasks, cumulative_resolution)
        tasks = normalize_tasks(tasks, facts)

        refreshed_requests = collect_missing_fact_requests(tasks)
        known_requests = merge_request_worklists(known_requests, refreshed_requests)
        cumulative_resolution = cumulative_resolution_from_ledger(
            known_requests, facts, ledger
        )
        delta = compute_material_backfill_delta(
            previous_ledger,
            ledger,
            before_requests,
            known_requests,
            raw_fact_delta=raw_fact_delta,
        )
        remaining = filter_actionable_requests(known_requests, ledger)
        round_summary = {
            "round": round_index,
            "request_count": len(actionable),
            "requested_field_count": sum(
                len(request.get("required_fields", [])) for request in actionable
            ),
            "resolution": round_resolution,
            "cumulative_resolution": cumulative_resolution,
            "delta": delta,
            "remaining_request_count": len(remaining),
            "remaining_field_count": sum(
                len(request.get("required_fields", [])) for request in remaining
            ),
        }
        round_summaries.append(round_summary)
        if on_round is not None:
            on_round(round_index, round_summary)

        if not remaining:
            stop_reason = "all_requests_terminal"
            break
    else:
        stop_reason = "max_rounds_reached"

    final_resolution = cumulative_resolution_from_ledger(known_requests, facts, ledger)
    return {
        "facts": facts,
        "tasks": tasks,
        "cumulative_backfill": cumulative_backfill,
        "ledger": ledger,
        "resolution": final_resolution,
        "known_requests": known_requests,
        "round_summaries": round_summaries,
        "round_count": len(round_summaries),
        "stop_reason": stop_reason,
        "max_rounds": max(1, int(max_rounds)),
    }
