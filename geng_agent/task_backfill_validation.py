"""Normalization and evidence-contract validation for targeted backfill."""

from __future__ import annotations

import copy
from typing import Any

from .facts_normalize import finalize_engineering_facts
from .schemas import ValidationIssue
from .task_backfill_contracts import (
    _EVIDENCED_STATUSES,
    _EXPECTED_EVIDENCE_KIND,
    _FACT_TYPES,
    _FIELD_STATUSES,
    _fact_lookup,
    _request_fields,
)

def finalize_targeted_backfill(
    data: Any,
    requests: list[dict[str, Any]],
    existing_facts: dict[str, Any],
    valid_chunk_ids: set[str] | None,
    valid_pages: set[int] | None = None,
) -> dict[str, Any]:
    """Normalize a partial backfill while preserving every structurally usable field."""
    raw = copy.deepcopy(data) if isinstance(data, dict) else {}
    facts = finalize_engineering_facts(raw, valid_chunk_ids, valid_pages)
    expected = {
        str(request.get("request_id") or ""): {
            str(field.get("field_id") or "") for field in _request_fields(request)
        }
        for request in requests
        if str(request.get("request_id") or "")
    }
    by_request: dict[str, dict[str, Any]] = {}
    warnings: list[dict[str, str]] = []

    def warn(path: str, message: str) -> None:
        warnings.append({"path": path, "message": message})

    raw_resolutions = raw.get("request_resolutions")
    if raw_resolutions is not None and not isinstance(raw_resolutions, list):
        warn("$.request_resolutions", "non-list request_resolutions was ignored")
        raw_resolutions = []

    for resolution_index, resolution in enumerate(
        raw_resolutions if isinstance(raw_resolutions, list) else []
    ):
        base = f"$.request_resolutions[{resolution_index}]"
        if not isinstance(resolution, dict):
            warn(base, "non-object request resolution was ignored")
            continue
        request_id = str(resolution.get("request_id") or "").strip()
        if request_id not in expected:
            warn(f"{base}.request_id", "unknown request id was ignored")
            continue

        normalized = by_request.setdefault(
            request_id, {"request_id": request_id, "field_results": []}
        )
        seen_fields = {
            str(item.get("field_id") or "")
            for item in normalized["field_results"]
            if isinstance(item, dict)
        }
        raw_fields = resolution.get("field_results")
        if not isinstance(raw_fields, list):
            warn(f"{base}.field_results", "non-list field_results was ignored")
            continue

        for field_index, field in enumerate(raw_fields):
            field_base = f"{base}.field_results[{field_index}]"
            if not isinstance(field, dict):
                warn(field_base, "non-object field result was ignored")
                continue
            field_id = str(field.get("field_id") or "").strip()
            if field_id not in expected[request_id]:
                warn(f"{field_base}.field_id", "unknown field id was ignored")
                continue
            if field_id in seen_fields:
                warn(f"{field_base}.field_id", "duplicate field result was ignored")
                continue
            status = str(field.get("status") or "").strip().lower()
            if status not in _FIELD_STATUSES:
                warn(f"{field_base}.status", "unknown status was ignored")
                continue

            fact_refs: list[dict[str, str]] = []
            for ref in (
                field.get("fact_refs", [])
                if isinstance(field.get("fact_refs"), list)
                else []
            ):
                if not isinstance(ref, dict):
                    continue
                fact_type = str(ref.get("type") or "other").strip()
                name = str(ref.get("name") or "").strip()
                marker = (fact_type, name)
                if (
                    fact_type in _FACT_TYPES
                    and name
                    and marker
                    not in {(item["type"], item["name"]) for item in fact_refs}
                ):
                    fact_refs.append({"type": fact_type, "name": name})

            locations = field.get("searched_locations")
            note = str(field.get("note") or "").strip()
            if not note:
                note = "No note supplied by the fact specialist."
                warn(f"{field_base}.note", "empty note was replaced with a format placeholder")
            normalized["field_results"].append(
                {
                    "field_id": field_id,
                    "status": status,
                    "fact_refs": fact_refs,
                    "searched_locations": [
                        str(location).strip()
                        for location in locations
                        if str(location).strip()
                    ]
                    if isinstance(locations, list)
                    else [],
                    "note": note,
                }
            )
            seen_fields.add(field_id)

    normalized_resolutions = [
        by_request[request_id]
        for request_id in expected
        if request_id in by_request and by_request[request_id]["field_results"]
    ]
    facts["request_resolutions"] = normalized_resolutions
    meta = dict(facts.get("_meta", {})) if isinstance(facts.get("_meta"), dict) else {}
    meta["targeted_request_count"] = len(expected)
    meta["partial_backfill_normalization"] = {
        "accepted_request_count": len(normalized_resolutions),
        "accepted_field_count": sum(
            len(item["field_results"]) for item in normalized_resolutions
        ),
        "warning_count": len(warnings),
        "warnings": warnings[:200],
    }
    facts["_meta"] = meta
    return facts

def backfill_normalization_issues(data: dict[str, Any]) -> list[ValidationIssue]:
    meta = data.get("_meta") if isinstance(data, dict) else None
    normalization = (
        meta.get("partial_backfill_normalization")
        if isinstance(meta, dict)
        else None
    )
    warnings = (
        normalization.get("warnings")
        if isinstance(normalization, dict)
        else None
    )
    return [
        ValidationIssue(
            str(item.get("path") or "$"),
            str(item.get("message") or "backfill item was normalized"),
        )
        for item in warnings
        if isinstance(item, dict)
    ] if isinstance(warnings, list) else []

def validate_targeted_backfill(
    data: dict[str, Any],
    requests: list[dict[str, Any]],
    existing_facts: dict[str, Any],
) -> list[ValidationIssue]:
    """Validate field coverage and evidence references for one targeted round."""
    issues: list[ValidationIssue] = []
    expected = {
        str(request.get("request_id") or ""): {
            str(field.get("field_id") or "") for field in _request_fields(request)
        }
        for request in requests
    }
    fact_lookup = _fact_lookup(existing_facts)
    fact_lookup.update(_fact_lookup(data))
    seen_requests: set[str] = set()
    seen_fields: dict[str, set[str]] = {}

    resolutions = data.get("request_resolutions")
    if not isinstance(resolutions, list):
        return [ValidationIssue("$.request_resolutions", "must be a list")]
    for resolution_index, resolution in enumerate(resolutions):
        if not isinstance(resolution, dict):
            continue
        request_id = str(resolution.get("request_id") or "")
        base = f"$.request_resolutions[{resolution_index}]"
        if request_id not in expected:
            issues.append(ValidationIssue(f"{base}.request_id", "must refer to a targeted request"))
            continue
        if request_id in seen_requests:
            issues.append(ValidationIssue(f"{base}.request_id", "duplicate request resolution"))
        seen_requests.add(request_id)
        fields_seen = seen_fields.setdefault(request_id, set())
        field_results = resolution.get("field_results")
        if not isinstance(field_results, list):
            continue
        for field_index, field in enumerate(field_results):
            if not isinstance(field, dict):
                continue
            field_id = str(field.get("field_id") or "")
            field_base = f"{base}.field_results[{field_index}]"
            if field_id not in expected[request_id]:
                issues.append(ValidationIssue(f"{field_base}.field_id", "must refer to a requested field"))
                continue
            if field_id in fields_seen:
                issues.append(ValidationIssue(f"{field_base}.field_id", "duplicate field result"))
            fields_seen.add(field_id)
            status = str(field.get("status") or "")
            refs = field.get("fact_refs") if isinstance(field.get("fact_refs"), list) else []
            if status in _EVIDENCED_STATUSES:
                if not refs:
                    issues.append(ValidationIssue(f"{field_base}.fact_refs", "resolved field requires evidence fact refs"))
                expected_kind = _EXPECTED_EVIDENCE_KIND[status]
                for ref_index, ref in enumerate(refs):
                    key = (
                        str(ref.get("type") or "") if isinstance(ref, dict) else "",
                        str(ref.get("name") or "") if isinstance(ref, dict) else "",
                    )
                    fact = fact_lookup.get(key)
                    if fact is None:
                        issues.append(ValidationIssue(f"{field_base}.fact_refs[{ref_index}]", "must refer to an existing or newly extracted fact"))
                    elif str(fact.get("evidence_kind") or "paper_explicit") != expected_kind:
                        issues.append(ValidationIssue(f"{field_base}.fact_refs[{ref_index}]", f"evidence kind must match {expected_kind}"))
                    elif expected_kind == "paper_derived" and not str(fact.get("derivation") or "").strip():
                        issues.append(ValidationIssue(f"{field_base}.fact_refs[{ref_index}]", "derived evidence requires a derivation chain"))
            elif status == "not_found_in_paper":
                if refs:
                    issues.append(ValidationIssue(f"{field_base}.fact_refs", "not-found field cannot claim evidence facts"))
                if not field.get("searched_locations"):
                    issues.append(ValidationIssue(f"{field_base}.searched_locations", "not-found field must record searched locations"))
            elif status == "ambiguous_or_conflicting":
                if not field.get("searched_locations"):
                    issues.append(ValidationIssue(f"{field_base}.searched_locations", "ambiguous field must record searched locations"))

    for request_id, field_ids in expected.items():
        if request_id not in seen_requests:
            issues.append(ValidationIssue("$.request_resolutions", f"missing resolution for {request_id}"))
            continue
        missing = field_ids - seen_fields.get(request_id, set())
        for field_id in sorted(missing):
            issues.append(ValidationIssue("$.request_resolutions", f"missing field result for {request_id}/{field_id}"))
    return issues

def validate_terminal_gap_assumptions(
    tasks: dict[str, Any], resolution: dict[str, Any]
) -> list[ValidationIssue]:
    """Every terminal unresolved field needs a linked assumption and sensitivity check."""
    issues: list[ValidationIssue] = []
    task_by_id = {
        str(task.get("task_id") or ""): (index, task)
        for index, task in enumerate(tasks.get("repro_tasks", []))
        if isinstance(task, dict)
    }
    for request in resolution.get("terminal_unresolved", []) if isinstance(resolution, dict) else []:
        if not isinstance(request, dict):
            continue
        request_id = str(request.get("request_id") or "")
        unresolved_fields = {
            str(field.get("field_id") or "")
            for field in request.get("field_results", [])
            if isinstance(field, dict)
            and str(field.get("status") or "") not in _EVIDENCED_STATUSES
        }
        for task_id in request.get("task_ids", []) if isinstance(request.get("task_ids"), list) else []:
            located = task_by_id.get(str(task_id))
            if located is None:
                issues.append(ValidationIssue("$.repro_tasks", f"missing task {task_id} for unresolved request {request_id}"))
                continue
            task_index, task = located
            covered: set[str] = set()
            for assumption in task.get("assumptions", []) if isinstance(task.get("assumptions"), list) else []:
                if not isinstance(assumption, dict) or str(assumption.get("request_id") or "") != request_id:
                    continue
                if not str(assumption.get("sensitivity_check") or "").strip():
                    continue
                covered.update(
                    str(field_id)
                    for field_id in assumption.get("field_ids", [])
                    if str(field_id)
                )
            for field_id in sorted(unresolved_fields - covered):
                issues.append(
                    ValidationIssue(
                        f"$.repro_tasks[{task_index}].assumptions",
                        f"terminal gap {request_id}/{field_id} requires a linked sensitivity assumption",
                    )
                )
    return issues
