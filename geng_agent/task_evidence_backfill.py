"""Public façade and final-task reconciliation for targeted evidence backfill."""

from __future__ import annotations

import copy
import re
from typing import Any

from .facts_normalize import finalize_engineering_facts
from .schema_models import BackfillFieldStatus, FactType
from .schemas import ValidationIssue
from .semantic_merge import merge_execution_relationships
from .task_backfill_contracts import (
    _EVIDENCED_STATUSES,
    _EXPECTED_EVIDENCE_KIND,
    _FACT_TYPES,
    _FIELD_STATUSES,
    _IMPACT_RANK,
    _append_unique,
    _fact_lookup,
    _ledger_latest,
    _merge_required_fields,
    _normalize,
    _request_field_keys,
    _request_fields,
)
from .task_backfill_worklist import (
    collect_missing_fact_requests,
    filter_actionable_requests,
    merge_request_worklists,
)
from .task_backfill_validation import (
    backfill_normalization_issues,
    finalize_targeted_backfill,
    validate_targeted_backfill,
    validate_terminal_gap_assumptions,
)
from .task_backfill_ledger import (
    compute_material_backfill_delta,
    cumulative_resolution_from_ledger,
    summarize_backfill_resolution,
    update_search_ledger,
)


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
        # The acceptance contract is a single authority snapshot. A refresh may
        # omit it while updating facts; in that case retain the previous snapshot,
        # but never field-merge two contracts into a drifting hybrid.
        if not isinstance(final.get("scientific_acceptance"), dict) and isinstance(
            draft.get("scientific_acceptance"), dict
        ):
            final["scientific_acceptance"] = copy.deepcopy(
                draft["scientific_acceptance"]
            )

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

    preliminary_ids = {str(item.get("task_id") or "") for item in preliminary}
    added_candidate_task_ids: list[str] = []
    final_task_ids = set(preliminary_ids)
    discarded_candidate_task_ids: list[str] = []
    for raw_candidate in candidate_tasks.get("repro_tasks", []):
        if not isinstance(raw_candidate, dict):
            continue
        task_id = str(raw_candidate.get("task_id") or "")
        if not task_id or task_id in final_task_ids:
            continue
        if not _has_material_task_basis(raw_candidate):
            discarded_candidate_task_ids.append(task_id)
            continue
        final = copy.deepcopy(raw_candidate)
        refs = [
            item for item in final.get("required_facts", []) if isinstance(item, dict)
        ]
        ref_keys = {
            (_normalize(item.get("type")), _normalize(item.get("name")))
            for item in refs
        }
        for ref in resolved_by_task.get(task_id, []):
            key = (_normalize(ref.get("type")), _normalize(ref.get("name")))
            if key not in ref_keys:
                refs.append(ref)
                ref_keys.add(key)
        final["required_facts"] = refs
        final["missing_fact_requests"] = [
            copy.deepcopy(request)
            for request in final.get("missing_fact_requests", [])
            if isinstance(request, dict)
            and (_normalize(request.get("type")), _normalize(request.get("name")))
            not in resolved_keys
        ]
        final_tasks.append(final)
        added_candidate_task_ids.append(task_id)
        final_task_ids.add(task_id)

    relationships, relationship_added, relationship_refreshed = (
        merge_execution_relationships(
            preliminary_tasks.get("execution_relationships"),
            candidate_tasks.get("execution_relationships"),
        )
    )
    handoff = _reconciled_backfill_handoff(preliminary_tasks, candidate_tasks)
    schema_version = str(
        candidate_tasks.get("schema_version")
        or preliminary_tasks.get("schema_version")
        or "2.0"
    )

    meta = (
        copy.deepcopy(preliminary_tasks.get("_meta", {}))
        if isinstance(preliminary_tasks.get("_meta"), dict)
        else {}
    )
    if isinstance(candidate_tasks.get("_meta"), dict):
        meta.update(copy.deepcopy(candidate_tasks["_meta"]))
    meta.pop("backfill_handoff", None)
    meta["task_set_reconciliation"] = {
        "preliminary_task_count": len(preliminary),
        "candidate_task_count": len(candidate_by_id),
        "final_task_count": len(final_tasks),
        "restored_task_ids": restored_task_ids,
        "added_candidate_task_ids": added_candidate_task_ids,
        "discarded_candidate_task_ids": discarded_candidate_task_ids,
        "relationship_count": len(relationships),
        "relationship_added": relationship_added,
        "relationship_refreshed": relationship_refreshed,
    }
    return {
        "schema_version": schema_version,
        "backfill_handoff": handoff,
        "execution_relationships": relationships,
        "repro_tasks": final_tasks,
        "_meta": meta,
    }


def _reconciled_backfill_handoff(
    preliminary_tasks: dict[str, Any],
    candidate_tasks: dict[str, Any],
) -> dict[str, Any]:
    candidate = candidate_tasks.get("backfill_handoff")
    if isinstance(candidate, dict):
        return copy.deepcopy(candidate)
    preliminary = preliminary_tasks.get("backfill_handoff")
    if isinstance(preliminary, dict):
        return copy.deepcopy(preliminary)
    return {
        "ready_for_writer": True,
        "blocking_request_ids": [],
        "reason": "Host inferred a non-blocking Writer handoff during task reconciliation.",
        "inferred": True,
    }


def _has_material_task_basis(task: dict[str, Any]) -> bool:
    required_facts = task.get("required_facts")
    if any(
        isinstance(ref, dict)
        and str(ref.get("type") or "").strip()
        and str(ref.get("name") or "").strip()
        for ref in (required_facts if isinstance(required_facts, list) else [])
    ):
        return True

    acceptance = task.get("scientific_acceptance")
    if isinstance(acceptance, dict):
        conclusions = acceptance.get("core_conclusions")
        for conclusion in (conclusions if isinstance(conclusions, list) else []):
            if (
                isinstance(conclusion, dict)
                and str(conclusion.get("statement") or "").strip()
                and _explicit_paper_anchor(conclusion.get("paper_anchor"))
            ):
                return True
        numeric_targets = acceptance.get("key_numeric_targets")
        for target in (numeric_targets if isinstance(numeric_targets, list) else []):
            if not isinstance(target, dict):
                continue
            if (
                str(target.get("name") or "").strip()
                and target.get("paper_magnitude") is not None
                and str(target.get("evidence_quality") or "") != "unavailable"
            ):
                return True

    if _explicit_paper_anchor(task.get("figure_or_claim")):
        return bool(str(task.get("metric") or task.get("target") or "").strip())
    return False


def _explicit_paper_anchor(value: Any) -> bool:
    text = str(value or "").strip()
    return bool(
        re.search(
            r"(?:fig(?:ure)?|table|eq(?:uation)?)\s*[.(:#-]*\s*(?:\d+|[ivx]+)|"
            r"(?:\u56fe|\u8868|\u5f0f)\s*(?:\d+|[\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341\u767e]+)",
            text,
            re.IGNORECASE,
        )
    )
