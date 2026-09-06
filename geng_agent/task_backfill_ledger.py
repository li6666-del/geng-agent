"""Search-ledger updates and cumulative evidence reconciliation."""

from __future__ import annotations

import copy
from typing import Any

from .task_backfill_contracts import (
    _EVIDENCED_STATUSES,
    _FIELD_STATUSES,
    _fact_lookup,
    _ledger_latest,
    _request_field_keys,
    _request_fields,
)

def summarize_backfill_resolution(
    requests: list[dict[str, Any]],
    facts: dict[str, Any],
    backfill_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Summarize explicit field results; fact-name presence alone never resolves a request."""
    by_request = {
        str(item.get("request_id") or ""): item
        for item in (backfill_result or {}).get("request_resolutions", [])
        if isinstance(item, dict)
    }
    resolved: list[dict[str, Any]] = []
    terminal_unresolved: list[dict[str, Any]] = []
    open_requests: list[dict[str, Any]] = []
    fact_lookup = _fact_lookup(facts)
    for request in requests:
        request_id = str(request.get("request_id") or "")
        result = by_request.get(request_id, {})
        result_by_field = {
            str(item.get("field_id") or ""): item
            for item in result.get("field_results", [])
            if isinstance(item, dict)
        }
        fields: list[dict[str, Any]] = []
        complete = True
        all_evidenced = True
        matched_facts: list[dict[str, str]] = []
        for field in _request_fields(request):
            field_id = str(field.get("field_id") or "")
            field_result = result_by_field.get(field_id)
            if field_result is None or str(field_result.get("status") or "") not in _FIELD_STATUSES:
                complete = False
                all_evidenced = False
                fields.append({**field, "status": "open", "fact_refs": []})
                continue
            status = str(field_result.get("status") or "")
            refs = [ref for ref in field_result.get("fact_refs", []) if isinstance(ref, dict)]
            if status not in _EVIDENCED_STATUSES:
                all_evidenced = False
            for ref in refs:
                key = (str(ref.get("type") or ""), str(ref.get("name") or ""))
                if key in fact_lookup and {"type": key[0], "name": key[1]} not in matched_facts:
                    matched_facts.append({"type": key[0], "name": key[1]})
            fields.append({**field, **copy.deepcopy(field_result)})
        item = {
            **copy.deepcopy(request),
            "field_results": fields,
            "matched_facts": matched_facts,
            "terminal": complete,
        }
        if complete and all_evidenced:
            resolved.append(item)
        elif complete:
            terminal_unresolved.append(item)
        else:
            open_requests.append(item)
    unresolved = [*terminal_unresolved, *open_requests]
    return {
        "request_count": len(requests),
        "resolved_count": len(resolved),
        "terminal_unresolved_count": len(terminal_unresolved),
        "open_count": len(open_requests),
        "unresolved_count": len(unresolved),
        "resolved": resolved,
        "terminal_unresolved": terminal_unresolved,
        "open": open_requests,
        "unresolved": unresolved,
    }

def update_search_ledger(
    ledger: dict[str, Any] | None,
    *,
    round_index: int,
    requests: list[dict[str, Any]],
    resolution: dict[str, Any],
) -> dict[str, Any]:
    entries = [copy.deepcopy(item) for item in (ledger or {}).get("entries", []) if isinstance(item, dict)]
    by_request = {
        str(item.get("request_id") or ""): item
        for item in [*resolution.get("resolved", []), *resolution.get("unresolved", [])]
        if isinstance(item, dict)
    }
    for request in requests:
        request_id = str(request.get("request_id") or "")
        result = by_request.get(request_id, {})
        fields = {
            str(item.get("field_id") or ""): item
            for item in result.get("field_results", [])
            if isinstance(item, dict)
        }
        for field in _request_fields(request):
            field_id = str(field.get("field_id") or "")
            field_result = fields.get(field_id, {})
            entries.append(
                {
                    "round": round_index,
                    "request_id": request_id,
                    "field_id": field_id,
                    "task_ids": list(request.get("task_ids", [])),
                    "source_request_ids": list(request.get("source_request_ids", [])),
                    "status": str(field_result.get("status") or "open"),
                    "fact_refs": copy.deepcopy(field_result.get("fact_refs", [])),
                    "searched_locations": copy.deepcopy(field_result.get("searched_locations", [])),
                    "note": str(field_result.get("note") or ""),
                }
            )
    latest_map: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in entries:
        latest_map[(str(entry.get("request_id") or ""), str(entry.get("field_id") or ""))] = entry
    latest = [latest_map[key] for key in sorted(latest_map)]
    return {"entries": entries, "latest": latest, "round_count": max(round_index, int((ledger or {}).get("round_count") or 0))}

def cumulative_resolution_from_ledger(
    requests: list[dict[str, Any]], facts: dict[str, Any], ledger: dict[str, Any] | None
) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for entry in _ledger_latest(ledger):
        grouped.setdefault(str(entry.get("request_id") or ""), []).append(
            {
                "field_id": str(entry.get("field_id") or ""),
                "status": str(entry.get("status") or "open"),
                "fact_refs": copy.deepcopy(entry.get("fact_refs", [])),
                "searched_locations": copy.deepcopy(entry.get("searched_locations", [])),
                "note": str(entry.get("note") or ""),
            }
        )
    backfill = {
        "request_resolutions": [
            {"request_id": request_id, "field_results": fields}
            for request_id, fields in grouped.items()
        ]
    }
    return summarize_backfill_resolution(requests, facts, backfill)

def compute_material_backfill_delta(
    previous_ledger: dict[str, Any] | None,
    current_ledger: dict[str, Any] | None,
    before_requests: list[dict[str, Any]],
    after_requests: list[dict[str, Any]],
    *,
    raw_fact_delta: int,
) -> dict[str, Any]:
    previous = {
        (str(item.get("request_id") or ""), str(item.get("field_id") or "")): str(item.get("status") or "")
        for item in _ledger_latest(previous_ledger)
    }
    current = {
        (str(item.get("request_id") or ""), str(item.get("field_id") or "")): str(item.get("status") or "")
        for item in _ledger_latest(current_ledger)
    }
    evidence_field_delta = sum(
        1
        for key, status in current.items()
        if status in _EVIDENCED_STATUSES and previous.get(key) not in _EVIDENCED_STATUSES
    )
    terminal_unresolved_delta = sum(
        1
        for key, status in current.items()
        if status in {"not_found_in_paper", "ambiguous_or_conflicting"}
        and key not in previous
    )
    before_fields = _request_field_keys(before_requests)
    after_fields = _request_field_keys(after_requests)
    new_request_fields = sorted(f"{request_id}/{field_id}" for request_id, field_id in after_fields - before_fields)
    material_delta = evidence_field_delta + len(new_request_fields)
    return {
        "material_delta": material_delta,
        "evidence_field_delta": evidence_field_delta,
        "terminal_unresolved_delta": terminal_unresolved_delta,
        "new_request_field_count": len(new_request_fields),
        "new_request_fields": new_request_fields,
        "raw_fact_delta": int(raw_fact_delta),
        "raw_fact_delta_controls_iteration": False,
    }
