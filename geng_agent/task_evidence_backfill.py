from __future__ import annotations

import copy
import re
from typing import Any


_IMPACT_RANK = {"low": 0, "medium": 1, "high": 2}


def collect_missing_fact_requests(
    tasks: dict[str, Any], *, minimum_impact: str = "medium"
) -> list[dict[str, Any]]:
    """Merge equivalent task evidence requests into one deterministic search worklist."""
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for task in tasks.get("repro_tasks", []) if isinstance(tasks, dict) else []:
        if not isinstance(task, dict):
            continue
        task_id = str(task.get("task_id") or "").strip()
        requests = task.get("missing_fact_requests")
        for request in requests if isinstance(requests, list) else []:
            if not isinstance(request, dict):
                continue
            fact_type = str(request.get("type") or "other").strip()
            name = str(request.get("name") or "").strip()
            if not name:
                continue
            impact = str(request.get("impact") or "low").strip().lower()
            key = (_normalize(fact_type), _normalize(name))
            current = grouped.setdefault(
                key,
                {
                    "type": fact_type,
                    "name": name,
                    "impact": "low",
                    "task_ids": [],
                    "source_request_ids": [],
                    "why_needed": [],
                    "search_targets": [],
                },
            )
            _append_unique(current["task_ids"], task_id)
            _append_unique(current["source_request_ids"], str(request.get("request_id") or "").strip())
            _append_unique(current["why_needed"], str(request.get("why_needed") or "").strip())
            for target in request.get("search_targets", []) if isinstance(request.get("search_targets"), list) else []:
                _append_unique(current["search_targets"], str(target).strip())
            if _IMPACT_RANK.get(impact, 0) > _IMPACT_RANK.get(current["impact"], 0):
                current["impact"] = impact

    worklist: list[dict[str, Any]] = []
    for key in sorted(grouped):
        item = grouped[key]
        if _IMPACT_RANK.get(item["impact"], 0) < _IMPACT_RANK.get(minimum_impact, 1):
            continue
        item["request_id"] = f"backfill_{len(worklist) + 1:03d}"
        worklist.append(item)
    return worklist


def summarize_backfill_resolution(
    requests: list[dict[str, Any]], facts: dict[str, Any]
) -> dict[str, Any]:
    fact_items = facts.get("engineering_facts", []) if isinstance(facts, dict) else []
    fact_lookup = {
        (_normalize(item.get("type")), _normalize(item.get("name"))): item
        for item in fact_items
        if isinstance(item, dict)
    }
    resolved: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for request in requests:
        key = (_normalize(request.get("type")), _normalize(request.get("name")))
        if key in fact_lookup:
            matched = fact_lookup[key]
            resolved.append(
                {
                    **request,
                    "matched_fact": {
                        "type": str(matched.get("type") or "other"),
                        "name": str(matched.get("name") or ""),
                    },
                }
            )
        else:
            unresolved.append(request)
    return {
        "request_count": len(requests),
        "resolved_count": len(resolved),
        "unresolved_count": len(unresolved),
        "resolved": resolved,
        "unresolved": unresolved,
    }


def reconcile_final_tasks(
    preliminary_tasks: dict[str, Any],
    candidate_tasks: dict[str, Any],
    resolution: dict[str, Any],
) -> dict[str, Any]:
    """Keep the draft task set stable while applying resolved evidence deterministically."""
    preliminary = [
        item for item in preliminary_tasks.get("repro_tasks", [])
        if isinstance(item, dict)
    ]
    candidate_by_id = {
        str(item.get("task_id") or ""): item
        for item in candidate_tasks.get("repro_tasks", [])
        if isinstance(item, dict) and str(item.get("task_id") or "")
    }
    resolved_by_task: dict[str, list[dict[str, str]]] = {}
    resolved_keys: set[tuple[str, str]] = set()
    for request in resolution.get("resolved", []) if isinstance(resolution, dict) else []:
        if not isinstance(request, dict):
            continue
        key = (_normalize(request.get("type")), _normalize(request.get("name")))
        resolved_keys.add(key)
        matched_fact = request.get("matched_fact") if isinstance(request.get("matched_fact"), dict) else request
        ref = {
            "type": str(matched_fact.get("type") or "other"),
            "name": str(matched_fact.get("name") or ""),
        }
        for task_id in request.get("task_ids", []) if isinstance(request.get("task_ids"), list) else []:
            resolved_by_task.setdefault(str(task_id), []).append(ref)

    final_tasks: list[dict[str, Any]] = []
    restored_task_ids: list[str] = []
    for draft in preliminary:
        task_id = str(draft.get("task_id") or "")
        candidate = candidate_by_id.get(task_id)
        if candidate is None:
            candidate = draft
            restored_task_ids.append(task_id)
        final = copy.deepcopy(candidate)

        refs = [item for item in final.get("required_facts", []) if isinstance(item, dict)]
        ref_keys = {(_normalize(item.get("type")), _normalize(item.get("name"))) for item in refs}
        for ref in resolved_by_task.get(task_id, []):
            key = (_normalize(ref.get("type")), _normalize(ref.get("name")))
            if key not in ref_keys:
                refs.append(ref)
                ref_keys.add(key)
        final["required_facts"] = refs

        requests: list[dict[str, Any]] = []
        request_keys: set[tuple[str, str]] = set()
        sources = [draft.get("missing_fact_requests"), final.get("missing_fact_requests")]
        for source in sources:
            for request in source if isinstance(source, list) else []:
                if not isinstance(request, dict):
                    continue
                key = (_normalize(request.get("type")), _normalize(request.get("name")))
                if key in resolved_keys or key in request_keys:
                    continue
                request_keys.add(key)
                requests.append(copy.deepcopy(request))
        final["missing_fact_requests"] = requests
        final_tasks.append(final)

    meta = (
        copy.deepcopy(preliminary_tasks.get("_meta", {}))
        if isinstance(preliminary_tasks.get("_meta"), dict)
        else {}
    )
    if isinstance(candidate_tasks.get("_meta"), dict):
        meta.update(copy.deepcopy(candidate_tasks["_meta"]))
    meta["task_set_reconciliation"] = {
        "preliminary_task_count": len(preliminary),
        "candidate_task_count": len(candidate_by_id),
        "final_task_count": len(final_tasks),
        "restored_task_ids": restored_task_ids,
        "discarded_candidate_task_ids": sorted(set(candidate_by_id) - {str(item.get('task_id') or '') for item in preliminary}),
    }
    return {"repro_tasks": final_tasks, "_meta": meta}


def _append_unique(items: list[str], value: str) -> None:
    if value and value not in items:
        items.append(value)


def _normalize(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").strip().lower()).strip()
