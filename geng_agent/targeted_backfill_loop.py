"""Dispatch only the Planner's explicitly selected evidence requests.

The host records attempts and appends evidence verbatim. It neither guesses
whether a search resolved a scientific gap nor repairs/reduces the task set.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Callable

from .task_backfill_worklist import collect_missing_fact_requests, merge_request_worklists
from .task_backfill_ledger import (
    cumulative_resolution_from_ledger, summarize_backfill_resolution, update_search_ledger,
)


def _task_backfill_handoff(tasks: dict[str, Any]) -> dict[str, Any]:
    raw = tasks.get("backfill_handoff")
    if not isinstance(raw, dict):
        return {"provided": False, "ready_for_writer": None, "blocking_request_ids": [],
                "reason": "No explicit Planner handoff; supervisor assessment is required."}
    return {**deepcopy(raw), "provided": True}


def _append_evidence(base: dict, addition: dict) -> tuple[dict, int]:
    result = deepcopy(base)
    count = 0
    for key in ("engineering_facts", "missing_information"):
        added = addition.get(key)
        if not isinstance(added, list):
            continue
        current = result.get(key)
        if current is None:
            current = []
        if not isinstance(current, list):
            # Preserve the original value; make the incompatible container
            # visible instead of converting or silently losing it.
            result.setdefault("_meta", {}).setdefault("backfill_observations", []).append(
                {"field": key, "original": deepcopy(current), "addition": deepcopy(added)})
            continue
        current = deepcopy(current)
        for item in added:
            if item not in current:
                current.append(deepcopy(item))
                count += int(key == "engineering_facts")
        result[key] = current
    extras = {key: deepcopy(value) for key, value in addition.items()
              if key not in {"engineering_facts", "missing_information", "_meta"}}
    if extras:
        meta = result.get("_meta") if isinstance(result.get("_meta"), dict) else {}
        meta.setdefault("backfill_document_annotations", []).append(extras)
        result["_meta"] = meta
    return result, count


def run_targeted_backfill_loop(
    *, initial_facts: dict[str, Any], preliminary_tasks: dict[str, Any],
    run_backfill: Callable, refresh_tasks: Callable,
    normalize_tasks: Callable | None = None, max_rounds: int = 3,
    on_round: Callable | None = None, audit_dir=None,
) -> dict[str, Any]:
    from .supervisor import RunSupervisor, StageBlocked, current_supervisor
    from .supervisor_tools import SupervisorTool
    from .outputs import write_json
    supervisor = current_supervisor()
    if supervisor is None:
        if audit_dir is None:
            raise ValueError("A standalone backfill session requires an explicit audit_dir")
        from pathlib import Path
        supervisor = RunSupervisor(Path(audit_dir).parent, Path(audit_dir),
            {"objective": "选择必要的信息补查，保留无法确定的事实", "tasks": preliminary_tasks})
    facts, tasks = deepcopy(initial_facts), deepcopy(preliminary_tasks)
    ledger = {"entries": [], "latest": [], "round_count": 0}
    known_requests = collect_missing_fact_requests(tasks)
    cumulative_backfill = {"engineering_facts": [], "missing_information": []}
    round_summaries: list[dict] = []
    limit = max(0, int(max_rounds))
    searches = 0
    replans = 0
    refresh_pending = False
    errors: dict[str, str] = {}

    def selected_requests():
        handoff = _task_backfill_handoff(tasks)
        requests = collect_missing_fact_requests(tasks)
        ids = set(handoff.get("blocking_request_ids") or [])
        return [request for request in requests if request.get("request_id") in ids]

    def can_handoff():
        if refresh_pending:
            return False
        handoff = _task_backfill_handoff(tasks)
        if handoff.get("ready_for_writer") is True:
            return True
        # A missing declaration stays unknown. With no selected evidence
        # request, it is not a reason for the host to veto the next stage.
        return not handoff.get("provided") and not collect_missing_fact_requests(tasks)

    def offer():
        if can_handoff():
            return []
        common = {"planner_handoff": _task_backfill_handoff(tasks), "search_count": searches,
                  "search_budget": limit, "ledger": ledger, "errors": errors}
        tools = []
        if searches < limit and not refresh_pending and selected_requests():
            call = lambda decision: run_backfill(searches + 1, selected_requests(), facts, tasks, ledger)
            tools.append(SupervisorTool("search", "执行 Planner 的明确缺口补查；没有新依据时可以停止搜索。",
                call, inputs={**common, "requests": selected_requests()}, resume=call, resources=("analysis",)))
        def replan(decision):
            return refresh_tasks(replans + 1, tasks, facts,
                cumulative_resolution_from_ledger(known_requests, facts, ledger), ledger)
        tools.append(SupervisorTool("replan", "让 Planner 根据新事实或主持人的澄清指令重新定稿，不缩小论文目标。",
            replan, inputs=common, resume=replan, resources=("analysis",)))
        return tools

    def received(name, value):
        nonlocal facts, tasks, cumulative_backfill, ledger, known_requests
        nonlocal searches, replans, refresh_pending
        errors.pop(name, None)
        if name == "search":
            requests = selected_requests()
            searches += 1
            cumulative_backfill, _ = _append_evidence(cumulative_backfill, value)
            facts, added = _append_evidence(facts, value)
            resolution = summarize_backfill_resolution(requests, facts, value)
            ledger = update_search_ledger(ledger, round_index=searches, requests=requests, resolution=resolution)
            summary = {"round": searches, "request_count": len(requests), "new_fact_count": added,
                       "resolution": resolution, "decision_owner": "supervisor"}
            round_summaries.append(summary)
            if on_round:
                on_round(searches, summary)
            refresh_pending = True
        elif name == "replan":
            replans += 1
            tasks = deepcopy(value)
            known_requests = merge_request_worklists(known_requests, collect_missing_fact_requests(tasks))
            refresh_pending = False
        write_json(supervisor.root / "backfill_state.json", {"facts": facts, "tasks": tasks,
            "ledger": ledger, "refresh_pending": refresh_pending,
            "planner_handoff": _task_backfill_handoff(tasks)})

    def failed(name, exc):
        errors[name] = f"{type(exc).__name__}: {exc}"
        if on_round:
            on_round(searches + 1, {"error": errors[name], "facts": facts, "tasks": tasks,
                                   "ledger": ledger, "handoff_incomplete": True})

    def routine_route(snapshot):
        if can_handoff() and not snapshot["active"]:
            return {"action": "finish", "status": "owner_handoff_recorded",
                    "diagnosis": "Planner handoff and unresolved evidence are recorded for the next stage."}
        if refresh_pending and not errors and "replan" in snapshot["ready"]:
            return {"action": "start", "next_nodes": ["replan"], "status": "routine",
                    "diagnosis": "New search evidence must return to the Planner before handoff."}
        return None

    decision = supervisor.tools.run("backfill", offer,
        state=lambda: {"facts": facts, "tasks": tasks, "ledger": ledger, "errors": errors,
                       "refresh_pending": refresh_pending,
                       "planner_handoff": _task_backfill_handoff(tasks)},
        on_result=received, on_error=failed, routine_selector=routine_route)
    if not can_handoff():
        raise StageBlocked("backfill", decision)
    final_handoff = _task_backfill_handoff(tasks)
    stop_reason = ("planner_ready_handoff" if final_handoff.get("ready_for_writer") is True
                   else "planner_handoff_unknown_carried")
    write_json(supervisor.root / "backfill_state.json", {"facts": facts, "tasks": tasks,
        "ledger": ledger, "refresh_pending": refresh_pending,
        "planner_handoff": final_handoff, "stop_reason": stop_reason})
    return {"facts": facts, "tasks": tasks, "cumulative_backfill": cumulative_backfill,
        "ledger": ledger, "resolution": cumulative_resolution_from_ledger(known_requests, facts, ledger),
        "known_requests": known_requests, "round_summaries": round_summaries,
        "round_count": searches, "stop_reason": stop_reason,
        "max_rounds": limit, "final_handoff": final_handoff, "supervisor_decision": decision}
