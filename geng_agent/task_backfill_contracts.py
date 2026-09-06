"""Shared constants and structural helpers for targeted evidence backfill."""

from __future__ import annotations

import copy
import re
from typing import Any, get_args

from .schema_models import BackfillFieldStatus, FactType

_IMPACT_RANK = {"low": 0, "medium": 1, "high": 2}
_FACT_TYPES = set(get_args(FactType))
_FIELD_STATUSES = set(get_args(BackfillFieldStatus))
_EVIDENCED_STATUSES = {
    "resolved_explicit",
    "resolved_derived",
    "resolved_visual_estimate",
}
_EXPECTED_EVIDENCE_KIND = {
    "resolved_explicit": "paper_explicit",
    "resolved_derived": "paper_derived",
    "resolved_visual_estimate": "visual_estimate",
}

def _request_fields(request: dict[str, Any]) -> list[dict[str, Any]]:
    fields = request.get("required_fields")
    cleaned = [copy.deepcopy(field) for field in fields if isinstance(field, dict)] if isinstance(fields, list) else []
    if cleaned:
        return cleaned
    return [
        {
            "field_id": "answer",
            "description": str(request.get("why_needed") or request.get("name") or "answer the request"),
            "affects": ["implementation"],
        }
    ]

def _merge_required_fields(target: list[dict[str, Any]], additions: list[dict[str, Any]]) -> None:
    index = {str(item.get("field_id") or "").casefold(): item for item in target}
    for field in additions:
        field_id = str(field.get("field_id") or "").strip()
        if not field_id:
            continue
        key = field_id.casefold()
        if key not in index:
            candidate = copy.deepcopy(field)
            target.append(candidate)
            index[key] = candidate
            continue
        current = index[key]
        for affected in field.get("affects", []) if isinstance(field.get("affects"), list) else []:
            current.setdefault("affects", [])
            _append_unique(current["affects"], str(affected).strip())

def _request_field_keys(requests: list[dict[str, Any]]) -> set[tuple[str, str]]:
    return {
        (str(request.get("request_id") or ""), str(field.get("field_id") or ""))
        for request in requests
        for field in _request_fields(request)
    }

def _fact_lookup(document: dict[str, Any] | None) -> dict[tuple[str, str], dict[str, Any]]:
    return {
        (str(item.get("type") or ""), str(item.get("name") or "")): item
        for item in (document or {}).get("engineering_facts", [])
        if isinstance(item, dict)
    }

def _ledger_latest(ledger: dict[str, Any] | None) -> list[dict[str, Any]]:
    latest = (ledger or {}).get("latest")
    if isinstance(latest, list):
        return [item for item in latest if isinstance(item, dict)]
    entries = (ledger or {}).get("entries")
    if not isinstance(entries, list):
        return []
    latest_map: dict[tuple[str, str], dict[str, Any]] = {}
    for item in entries:
        if isinstance(item, dict):
            latest_map[(str(item.get("request_id") or ""), str(item.get("field_id") or ""))] = item
    return [latest_map[key] for key in sorted(latest_map)]

def _append_unique(items: list[str], value: str) -> None:
    if value and value not in items:
        items.append(value)

def _normalize(value: Any) -> str:
    return re.sub(r"[\W_]+", " ", str(value or "").strip().casefold(), flags=re.UNICODE).strip()
