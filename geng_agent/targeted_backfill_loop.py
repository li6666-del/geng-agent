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


def _task_backfill_handoff(tasks: dict[str, Any]) -> dict[str, Any]:
    raw = tasks.get("backfill_handoff") if isinstance(tasks, dict) else None
    if not isinstance(raw, dict) and isinstance(tasks, dict):
        meta = tasks.get("_meta") if isinstance(tasks.get("_meta"), dict) else {}
        raw = meta.get("backfill_handoff")
    provided = isinstance(raw, dict)
    raw = raw if isinstance(raw, dict) else {}
    ready = raw.get("ready_for_writer")
    ready_for_writer = ready if isinstance(ready, bool) else True
    request_ids: list[str] = []
    for item in raw.get("blocking_request_ids", []) if isinstance(raw.get("blocking_request_ids"), list) else []:
        request_id = item.strip() if isinstance(item, str) else ""
        if request_id and request_id not in request_ids:
            request_ids.append(request_id)
    return {
        "provided": provided,
        "ready_for_writer": ready_for_writer,
        "blocking_request_ids": request_ids,
        "reason": str(raw.get("reason") or "").strip(),
    }


def _resolve_blocking_request_ids(
    requested_ids: list[str], known_requests: list[dict[str, Any]]
) -> tuple[list[str], list[str]]:
    aliases: dict[str, str] = {}
    for request in known_requests:
        aggregate_id = str(request.get("request_id") or "")
        if not aggregate_id:
            continue
        aliases[aggregate_id] = aggregate_id
        for source_id in request.get("source_request_ids", []) if isinstance(request.get("source_request_ids"), list) else []:
            source_id = str(source_id or "").strip()
            if source_id:
                aliases[source_id] = aggregate_id

    resolved: list[str] = []
    ignored: list[str] = []
    for requested_id in requested_ids:
        aggregate_id = aliases.get(requested_id)
        if aggregate_id is None:
            if requested_id not in ignored:
                ignored.append(requested_id)
            continue
        if aggregate_id not in resolved:
            resolved.append(aggregate_id)
    return resolved, ignored


def run_targeted_backfill_loop(
    *,
    initial_facts: dict[str, Any],
    preliminary_tasks: dict[str, Any],
    run_backfill: BackfillRunner,
    refresh_tasks: TaskRefresher,
    normalize_tasks: TaskNormalizer,
    max_rounds: int = 3,
    on_round: RoundObserver | None = None,
) -> dict[str, Any]:
    """Run only task-expert-selected blockers, with bounded unresolved re-search."""
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
    limit = max(1, int(max_rounds))

    initial_handoff = _task_backfill_handoff(tasks)
    resolved_ids, ignored_ids = _resolve_blocking_request_ids(
        initial_handoff["blocking_request_ids"], known_requests
    )
    final_handoff = {
        **initial_handoff,
        "resolved_blocking_request_ids": resolved_ids,
        "ignored_blocking_request_ids": ignored_ids,
    }
    pending_request_ids = set(resolved_ids)

    if not known_requests:
        stop_reason = "no_initial_requests"
    elif initial_handoff["ready_for_writer"]:
        stop_reason = (
            "preliminary_task_handoff_ready"
            if initial_handoff["provided"]
            else "preliminary_task_handoff_default"
        )
    elif not resolved_ids:
        stop_reason = "no_selected_blockers"
    else:
        stop_reason = "safety_limit_reached"

    for round_index in range(1, limit + 1):
        if stop_reason != "safety_limit_reached":
            break
        selected_requests = [
            request
            for request in known_requests
            if str(request.get("request_id") or "") in pending_request_ids
        ]
        actionable = filter_actionable_requests(
            selected_requests,
            ledger,
            max_unresolved_attempts=2,
        )
        if not actionable:
            stop_reason = "selected_requests_exhausted"
            break

        previous_ledger = copy.deepcopy(ledger)
        before_requests = copy.deepcopy(known_requests)
        try:
            backfill_result = run_backfill(
                round_index, actionable, facts, tasks, ledger
            )
        except Exception as exc:
            stop_reason = "backfill_error"
            round_summary = {
                "round": round_index,
                "phase": "backfill",
                "request_count": len(actionable),
                "error": f"{type(exc).__name__}: {exc}",
                "degraded_to_writer": True,
                "handoff": final_handoff,
            }
            round_summaries.append(round_summary)
            if on_round is not None:
                on_round(round_index, round_summary)
            break
        cumulative_backfill, _ = merge_engineering_facts(
            cumulative_backfill, backfill_result
        )
        facts, raw_fact_delta = merge_engineering_facts(facts, backfill_result)

        round_resolution = summarize_backfill_resolution(
            actionable, facts, backfill_result
        )
        ledger = update_search_ledger(
            ledger,
            round_index=round_index,
            requests=actionable,
            resolution=round_resolution,
        )
        cumulative_resolution = cumulative_resolution_from_ledger(
            known_requests, facts, ledger
        )
        try:
            candidate_tasks = refresh_tasks(
                round_index, tasks, facts, cumulative_resolution, ledger
            )
        except Exception as exc:
            stop_reason = "task_refresh_error"
            round_summary = {
                "round": round_index,
                "phase": "task_refresh",
                "request_count": len(actionable),
                "error": f"{type(exc).__name__}: {exc}",
                "degraded_to_writer": True,
                "new_facts_retained": True,
                "cumulative_resolution": cumulative_resolution,
                "handoff": final_handoff,
            }
            round_summaries.append(round_summary)
            if on_round is not None:
                on_round(round_index, round_summary)
            break
        candidate_tasks = normalize_tasks(candidate_tasks, facts)
        candidate_handoff = _task_backfill_handoff(candidate_tasks)
        candidate_requests = collect_missing_fact_requests(candidate_tasks)
        known_requests = merge_request_worklists(known_requests, candidate_requests)
        cumulative_resolution = cumulative_resolution_from_ledger(
            known_requests, facts, ledger
        )
        tasks = reconcile_final_tasks(tasks, candidate_tasks, cumulative_resolution)
        task_meta = (
            dict(tasks.get("_meta", {}))
            if isinstance(tasks.get("_meta"), dict)
            else {}
        )
        if candidate_handoff["provided"]:
            task_meta["backfill_handoff"] = {
                key: value
                for key, value in candidate_handoff.items()
                if key != "provided"
            }
        else:
            task_meta.pop("backfill_handoff", None)
        tasks["_meta"] = task_meta
        tasks = normalize_tasks(tasks, facts)

        refreshed_requests = collect_missing_fact_requests(tasks)
        known_requests = merge_request_worklists(known_requests, refreshed_requests)
        cumulative_resolution = cumulative_resolution_from_ledger(
            known_requests, facts, ledger
        )
        handoff = _task_backfill_handoff(tasks)
        resolved_ids, ignored_ids = _resolve_blocking_request_ids(
            handoff["blocking_request_ids"], known_requests
        )
        resolved_id_set = set(resolved_ids)
        followup_requests = [
            request
            for request in known_requests
            if str(request.get("request_id") or "") in resolved_id_set
        ]
        remaining = filter_actionable_requests(
            followup_requests,
            ledger,
            max_unresolved_attempts=2,
        )
        total_open = filter_actionable_requests(
            known_requests,
            ledger,
            max_unresolved_attempts=2,
        )
        delta = compute_material_backfill_delta(
            previous_ledger,
            ledger,
            before_requests,
            known_requests,
            raw_fact_delta=raw_fact_delta,
        )
        final_handoff = {
            **handoff,
            "resolved_blocking_request_ids": resolved_ids,
            "ignored_blocking_request_ids": ignored_ids,
        }
        round_summary = {
            "round": round_index,
            "request_count": len(actionable),
            "requested_field_count": sum(
                len(request.get("required_fields", [])) for request in actionable
            ),
            "resolution": round_resolution,
            "cumulative_resolution": cumulative_resolution,
            "delta": delta,
            "handoff": final_handoff,
            "remaining_request_count": len(remaining),
            "remaining_field_count": sum(
                len(request.get("required_fields", [])) for request in remaining
            ),
            "total_open_request_count": len(total_open),
            "total_open_field_count": sum(
                len(request.get("required_fields", [])) for request in total_open
            ),
        }
        round_summaries.append(round_summary)
        if on_round is not None:
            on_round(round_index, round_summary)

        if handoff["ready_for_writer"]:
            stop_reason = (
                "task_expert_handoff_ready"
                if handoff["provided"]
                else "task_expert_handoff_default"
            )
            break
        if not resolved_ids:
            stop_reason = "no_selected_blockers"
            break
        if not remaining:
            stop_reason = "selected_requests_exhausted"
            break
        pending_request_ids = {
            str(request.get("request_id") or "") for request in remaining
        }
    else:
        stop_reason = "safety_limit_reached"

    final_resolution = cumulative_resolution_from_ledger(
        known_requests, facts, ledger
    )
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
        "max_rounds": limit,
        "final_handoff": final_handoff,
    }
