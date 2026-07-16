from __future__ import annotations

import copy
import hashlib
import re
from typing import Any, get_args

from .facts_normalize import finalize_engineering_facts
from .schema_models import BackfillFieldStatus, FactType
from .schemas import ValidationIssue


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
    requests: list[dict[str, Any]], ledger: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """Only send fields that have not already received a terminal field result."""
    terminal = {
        (str(item.get("request_id") or ""), str(item.get("field_id") or ""))
        for item in _ledger_latest(ledger)
        if str(item.get("status") or "") in _FIELD_STATUSES
    }
    actionable: list[dict[str, Any]] = []
    for request in requests:
        request_id = str(request.get("request_id") or "")
        fields = [
            field
            for field in _request_fields(request)
            if (request_id, str(field.get("field_id") or "")) not in terminal
        ]
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


def finalize_targeted_backfill(
    data: Any,
    requests: list[dict[str, Any]],
    existing_facts: dict[str, Any],
    valid_chunk_ids: set[str] | None,
    valid_pages: set[int] | None = None,
) -> dict[str, Any]:
    """Normalize a backfill candidate without losing its request-resolution contract."""
    raw = copy.deepcopy(data) if isinstance(data, dict) else {}
    facts = finalize_engineering_facts(raw, valid_chunk_ids, valid_pages)
    request_ids = {str(request.get("request_id") or "") for request in requests}
    normalized_resolutions: list[dict[str, Any]] = []
    for resolution in raw.get("request_resolutions", []) if isinstance(raw, dict) else []:
        if not isinstance(resolution, dict):
            continue
        request_id = str(resolution.get("request_id") or "").strip()
        field_results: list[dict[str, Any]] = []
        for field in (
            resolution.get("field_results", [])
            if isinstance(resolution.get("field_results"), list)
            else []
        ):
            if not isinstance(field, dict):
                continue
            status = str(field.get("status") or "").strip().lower()
            fact_refs: list[dict[str, str]] = []
            for ref in field.get("fact_refs", []) if isinstance(field.get("fact_refs"), list) else []:
                if not isinstance(ref, dict):
                    continue
                fact_type = str(ref.get("type") or "other").strip()
                name = str(ref.get("name") or "").strip()
                if fact_type in _FACT_TYPES and name:
                    marker = (fact_type, name)
                    if marker not in {(item["type"], item["name"]) for item in fact_refs}:
                        fact_refs.append({"type": fact_type, "name": name})
            locations = field.get("searched_locations")
            field_results.append(
                {
                    "field_id": str(field.get("field_id") or "").strip(),
                    "status": status,
                    "fact_refs": fact_refs,
                    "searched_locations": [
                        str(location).strip()
                        for location in locations
                        if str(location).strip()
                    ] if isinstance(locations, list) else [],
                    "note": str(field.get("note") or "").strip(),
                }
            )
        normalized_resolutions.append(
            {"request_id": request_id, "field_results": field_results}
        )
    facts["request_resolutions"] = normalized_resolutions
    meta = dict(facts.get("_meta", {})) if isinstance(facts.get("_meta"), dict) else {}
    meta["targeted_request_count"] = len(request_ids)
    facts["_meta"] = meta
    return facts


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


def reconcile_final_tasks(
    preliminary_tasks: dict[str, Any],
    candidate_tasks: dict[str, Any],
    resolution: dict[str, Any],
) -> dict[str, Any]:
    """Keep task identities stable while applying field-resolved evidence."""
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
        refs = request.get("matched_facts") if isinstance(request.get("matched_facts"), list) else []
        for ref in refs:
            if not isinstance(ref, dict):
                continue
            normalized_ref = {
                "type": str(ref.get("type") or "other"),
                "name": str(ref.get("name") or ""),
            }
            for task_id in request.get("task_ids", []) if isinstance(request.get("task_ids"), list) else []:
                resolved_by_task.setdefault(str(task_id), []).append(normalized_ref)

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
        request_by_key: dict[tuple[str, str], dict[str, Any]] = {}
        sources = [draft.get("missing_fact_requests"), final.get("missing_fact_requests")]
        for source in sources:
            for request in source if isinstance(source, list) else []:
                if not isinstance(request, dict):
                    continue
                key = (_normalize(request.get("type")), _normalize(request.get("name")))
                if key in resolved_keys:
                    continue
                if key not in request_by_key:
                    candidate_request = copy.deepcopy(request)
                    requests.append(candidate_request)
                    request_by_key[key] = candidate_request
                    continue
                current_request = request_by_key[key]
                _merge_required_fields(
                    current_request.setdefault("required_fields", []),
                    _request_fields(request),
                )
                for list_key in ("search_targets",):
                    current_request.setdefault(list_key, [])
                    for value in request.get(list_key, []) if isinstance(request.get(list_key), list) else []:
                        _append_unique(current_request[list_key], str(value).strip())
                incoming_impact = str(request.get("impact") or "low")
                if _IMPACT_RANK.get(incoming_impact, 0) > _IMPACT_RANK.get(str(current_request.get("impact") or "low"), 0):
                    current_request["impact"] = incoming_impact
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
        "discarded_candidate_task_ids": sorted(
            set(candidate_by_id) - {str(item.get("task_id") or "") for item in preliminary}
        ),
    }
    return {"repro_tasks": final_tasks, "_meta": meta}


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
