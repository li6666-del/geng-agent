"""Missing-fact request planning and actionable worklist management."""

from __future__ import annotations

import copy
import hashlib
from typing import Any

from .task_backfill_contracts import (
    _EVIDENCED_STATUSES,
    _IMPACT_RANK,
    _append_unique,
    _ledger_latest,
    _merge_required_fields,
    _normalize,
    _request_fields,
)

def collect_missing_fact_requests(
    tasks: dict[str, Any], *, minimum_impact: str | None = None
) -> list[dict[str, Any]]:
    """Merge equivalent task requests into a stable, field-aware worklist."""
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
            impact = str(request.get("impact") or "medium").strip().lower()
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
                    "required_fields": [],
                },
            )
            _append_unique(current["task_ids"], task_id)
            _append_unique(
                current["source_request_ids"],
                str(request.get("request_id") or "").strip(),
            )
            _append_unique(
                current["why_needed"], str(request.get("why_needed") or "").strip()
            )
            for target in (
                request.get("search_targets", [])
                if isinstance(request.get("search_targets"), list)
                else []
            ):
                _append_unique(current["search_targets"], str(target).strip())
            _merge_required_fields(current["required_fields"], _request_fields(request))
            if _IMPACT_RANK.get(impact, 0) > _IMPACT_RANK.get(current["impact"], 0):
                current["impact"] = impact

    worklist: list[dict[str, Any]] = []
    threshold = _IMPACT_RANK.get(str(minimum_impact).lower(), 0) if minimum_impact else None
    for key in sorted(grouped):
        item = grouped[key]
        if threshold is not None and _IMPACT_RANK.get(item["impact"], 0) < threshold:
            continue
        stable_key = f"{key[0]}|{key[1]}"
        item["request_id"] = f"backfill_{hashlib.sha256(stable_key.encode('utf-8')).hexdigest()[:12]}"
        worklist.append(item)
    return worklist

def filter_actionable_requests(
    requests: list[dict[str, Any]],
    ledger: dict[str, Any] | None,
    *,
    max_unresolved_attempts: int = 1,
) -> list[dict[str, Any]]:
    """Keep open fields while bounding every non-evidenced search attempt."""
    entries = [
        item
        for item in (ledger or {}).get("entries", [])
        if isinstance(item, dict)
    ]
    latest = {
        (str(item.get("request_id") or ""), str(item.get("field_id") or "")): item
        for item in _ledger_latest(ledger)
    }
    search_attempts: dict[tuple[str, str], int] = {}
    for item in entries:
        key = (
            str(item.get("request_id") or ""),
            str(item.get("field_id") or ""),
        )
        search_attempts[key] = search_attempts.get(key, 0) + 1

    unresolved_limit = max(1, int(max_unresolved_attempts))
    actionable: list[dict[str, Any]] = []
    for request in requests:
        request_id = str(request.get("request_id") or "")
        fields: list[dict[str, Any]] = []
        for field in _request_fields(request):
            key = (request_id, str(field.get("field_id") or ""))
            status = str(latest.get(key, {}).get("status") or "open")
            if status in _EVIDENCED_STATUSES:
                continue
            if search_attempts.get(key, 0) >= unresolved_limit:
                continue
            fields.append(field)
        if not fields:
            continue
        candidate = copy.deepcopy(request)
        candidate["required_fields"] = fields
        actionable.append(candidate)
    return actionable

def merge_request_worklists(
    base: list[dict[str, Any]], addition: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Preserve every discovered task field while keeping stable aggregate request ids."""
    merged = [copy.deepcopy(item) for item in base]
    by_id = {str(item.get("request_id") or ""): item for item in merged}
    for incoming in addition:
        request_id = str(incoming.get("request_id") or "")
        if request_id not in by_id:
            candidate = copy.deepcopy(incoming)
            merged.append(candidate)
            by_id[request_id] = candidate
            continue
        current = by_id[request_id]
        for key in ("task_ids", "source_request_ids", "why_needed", "search_targets"):
            current.setdefault(key, [])
            for value in incoming.get(key, []) if isinstance(incoming.get(key), list) else []:
                _append_unique(current[key], str(value).strip())
        _merge_required_fields(
            current.setdefault("required_fields", []), _request_fields(incoming)
        )
        incoming_impact = str(incoming.get("impact") or "low")
        if _IMPACT_RANK.get(incoming_impact, 0) > _IMPACT_RANK.get(str(current.get("impact") or "low"), 0):
            current["impact"] = incoming_impact
    return merged
