from __future__ import annotations

import copy
import hashlib
import json
import re
import unicodedata
from typing import Any


_FIGURE_RE = re.compile(
    r"\bfig(?:ure)?s?\.?\s*(\d{1,3})(?!\d)(?:\s*\(([a-z])\)|([a-z])\b)?|"
    r"图\s*(\d{1,3})(?!\d)(?:\s*[（(]([a-z])[）)]|([a-z])\b)?",
    re.IGNORECASE,
)


def semantic_merge_engineering_facts(base: dict[str, Any], addition: dict[str, Any]) -> tuple[dict[str, Any], int]:
    merged = copy.deepcopy(base) if isinstance(base, dict) else {}
    facts = [dict(item) for item in merged.get("engineering_facts", []) if isinstance(item, dict)]
    index = {canonical_fact_key(item): position for position, item in enumerate(facts)}
    meta = dict(merged.get("_meta", {})) if isinstance(merged.get("_meta"), dict) else {}

    semantic_meta = dict(meta.get("semantic_merge", {})) if isinstance(meta.get("semantic_merge"), dict) else {}
    conflict_records = [item for item in semantic_meta.get("fact_conflicts", []) if isinstance(item, dict)]
    conflict_fingerprints = {str(item.get("fingerprint")) for item in conflict_records}
    added = enriched = new_conflicts = 0

    for candidate in addition.get("engineering_facts", []) if isinstance(addition, dict) else []:
        if not isinstance(candidate, dict):
            continue
        key = canonical_fact_key(candidate)
        if key not in index:
            index[key] = len(facts)
            facts.append(copy.deepcopy(candidate))
            added += 1
            continue
        current = facts[index[key]]
        changed, conflicts = _merge_fact_fields(current, candidate, key)
        if changed:
            enriched += 1
        for conflict in conflicts:
            fingerprint = conflict["fingerprint"]
            if fingerprint in conflict_fingerprints:
                continue
            conflict_fingerprints.add(fingerprint)
            conflict_records.append(conflict)
            new_conflicts += 1

    merged["engineering_facts"] = facts
    merged["missing_information"] = _merge_named_items(
        merged.get("missing_information"),
        addition.get("missing_information") if isinstance(addition, dict) else None,
    )
    semantic_meta.update(
        {
            "merge_version": 2,
            "last_added": added,
            "last_enriched": enriched,
            "last_new_conflicts": new_conflicts,
            "fact_conflicts": conflict_records,
        }
    )
    meta["semantic_merge"] = semantic_meta
    merged["_meta"] = meta
    return merged, added + enriched + new_conflicts


def semantic_merge_repro_tasks(
    base: dict[str, Any],
    addition: dict[str, Any],
    *,
    merge_mode: str = "incremental",
) -> tuple[dict[str, Any], int]:
    """Merge supplemental evidence, or publish a complete final task snapshot.

    Incremental callers retain earlier observations. A final Designer response
    replaces each supplied task, including deliberately emptied lists, while an
    omitted task is retained to avoid silently losing experiment coverage. The
    caller must persist the previous snapshot in its audit before publication.
    """

    if merge_mode == "snapshot":
        return _merge_final_task_snapshot(base, addition)
    if merge_mode != "incremental":
        raise ValueError(f"unknown task merge mode: {merge_mode}")
    merged = copy.deepcopy(base) if isinstance(base, dict) else {}
    tasks = [dict(item) for item in merged.get("repro_tasks", []) if isinstance(item, dict)]
    index = {canonical_task_key(item): position for position, item in enumerate(tasks)}
    task_id_index = {
        _normalize(item.get("task_id")): position
        for position, item in enumerate(tasks)
        if _normalize(item.get("task_id"))
    }
    meta = dict(merged.get("_meta", {})) if isinstance(merged.get("_meta"), dict) else {}
    merged["schema_version"] = str(
        (addition.get("schema_version") if isinstance(addition, dict) else None)
        or merged.get("schema_version")
        or "2.0"
    )
    handoff = _latest_explicit_handoff(merged, addition)
    merged["backfill_handoff"] = handoff
    meta.pop("backfill_handoff", None)
    relationships, relationship_added, relationship_refreshed = (
        merge_execution_relationships(
            merged.get("execution_relationships"),
            addition.get("execution_relationships") if isinstance(addition, dict) else None,
        )
    )
    merged["execution_relationships"] = relationships
    semantic_meta = dict(meta.get("semantic_merge", {})) if isinstance(meta.get("semantic_merge"), dict) else {}
    conflicts = [item for item in semantic_meta.get("task_conflicts", []) if isinstance(item, dict)]
    fingerprints = {str(item.get("fingerprint")) for item in conflicts}
    added = enriched = new_conflicts = 0

    for candidate in addition.get("repro_tasks", []) if isinstance(addition, dict) else []:
        if not isinstance(candidate, dict):
            continue
        key = canonical_task_key(candidate)
        task_id = _normalize(candidate.get("task_id"))
        existing_position = task_id_index.get(task_id) if task_id else None
        if existing_position is None:
            existing_position = index.get(key)
        if existing_position is None:
            index[key] = len(tasks)
            if task_id:
                task_id_index[task_id] = len(tasks)
            tasks.append(copy.deepcopy(candidate))
            added += 1
            continue
        current = tasks[existing_position]
        changed, task_conflicts = _merge_task_fields(current, candidate, key)
        if changed:
            enriched += 1
        for conflict in task_conflicts:
            if conflict["fingerprint"] in fingerprints:
                continue
            fingerprints.add(conflict["fingerprint"])
            conflicts.append(conflict)
            new_conflicts += 1

    merged["repro_tasks"] = tasks
    semantic_meta.update(
        {
            "merge_version": 4,
            "last_added": added,
            "last_enriched": enriched,
            "last_new_conflicts": new_conflicts,
            "last_relationship_added": relationship_added,
            "last_relationship_refreshed": relationship_refreshed,
            "task_conflicts": conflicts,
        }
    )
    meta["semantic_merge"] = semantic_meta
    merged["_meta"] = meta
    return merged, added + enriched + new_conflicts


def _merge_final_task_snapshot(
    base: dict[str, Any], addition: dict[str, Any]
) -> tuple[dict[str, Any], int]:
    merged = copy.deepcopy(base) if isinstance(base, dict) else {}
    tasks = [item for item in merged.get("repro_tasks", []) if isinstance(item, dict)]
    by_id = {_normalize(item.get("task_id")): index for index, item in enumerate(tasks)}
    by_key: dict[tuple[str, ...], list[int]] = {}
    for index, item in enumerate(tasks):
        key = canonical_task_key(item)
        if key[0]:
            by_key.setdefault(key, []).append(index)
    updated: set[int] = set()
    task_id_aliases: dict[str, str] = {}
    added_ids: list[str] = []
    changed = 0
    candidates = addition.get("repro_tasks", []) if isinstance(addition, dict) else []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        position = by_id.get(_normalize(candidate.get("task_id")))
        if position is None:
            matches = by_key.get(canonical_task_key(candidate), [])
            if len(matches) == 1:
                position = matches[0]
        current = copy.deepcopy(candidate)
        if position is None:
            position = len(tasks)
            tasks.append(current)
            by_id[_normalize(current.get("task_id"))] = position
            key = canonical_task_key(current)
            if key[0]:
                by_key.setdefault(key, []).append(position)
            added_ids.append(str(current.get("task_id") or ""))
            changed += 1
        else:
            # Scientific scope is matched above; keep the established identity
            # used by relationships, audit records, and downstream output paths.
            current["task_id"] = tasks[position].get("task_id")
            candidate_id = str(candidate.get("task_id") or "")
            if candidate_id and candidate_id != str(current["task_id"]):
                task_id_aliases[candidate_id] = str(current["task_id"])
            if current != tasks[position]:
                changed += 1
            tasks[position] = current
        updated.add(position)

    previous_relationships, _, _ = merge_execution_relationships(
        [], merged.get("execution_relationships")
    )
    supplied_relationships = addition.get("execution_relationships")
    relationships, _, _ = merge_execution_relationships(
        [],
        supplied_relationships
        if isinstance(supplied_relationships, list)
        else previous_relationships,
    )
    for relationship in relationships:
        for field in ("task_ids", "consumer_task_ids"):
            if isinstance(relationship.get(field), list):
                relationship[field] = list(dict.fromkeys(
                    task_id_aliases.get(str(task_id), str(task_id))
                    for task_id in relationship[field]
                ))
        producer = relationship.get("producer_task_id")
        if producer is not None:
            relationship["producer_task_id"] = task_id_aliases.get(str(producer), str(producer))
    removed_relationship_ids = sorted(
        {item["relationship_id"] for item in previous_relationships}
        - {item["relationship_id"] for item in relationships}
    )
    meta = merged.get("_meta") if isinstance(merged.get("_meta"), dict) else {}
    previous_semantic = meta.get("semantic_merge", {})
    meta["semantic_merge"] = {
        "merge_version": 5,
        "merge_mode": "snapshot",
        "preserved_task_ids": [
            str(item.get("task_id") or "")
            for index, item in enumerate(tasks) if index not in updated
        ],
        "updated_task_ids": [
            str(tasks[index].get("task_id") or "") for index in sorted(updated)
        ],
        "added_task_ids": added_ids,
        "removed_relationship_ids": removed_relationship_ids,
        "task_id_aliases": task_id_aliases,
        "previous_conflict_count": len(previous_semantic.get("task_conflicts", []))
        if isinstance(previous_semantic, dict) else 0,
        # Previous conflicts belong to the audit snapshot, not the current
        # authoritative definitions that have now been replaced.
        "task_conflicts": [],
    }
    meta.pop("backfill_handoff", None)
    merged.update({
        "schema_version": str(addition.get("schema_version") or merged.get("schema_version") or "2.0"),
        "backfill_handoff": _latest_explicit_handoff(merged, addition),
        "execution_relationships": relationships,
        "repro_tasks": tasks,
        "_meta": meta,
    })
    return merged, changed + int(relationships != previous_relationships)


def merge_execution_relationships(
    base: Any,
    addition: Any,
) -> tuple[list[dict[str, Any]], int, int]:
    """Merge valid relationship snapshots by stable ID without resolving task refs."""

    relationships: list[dict[str, Any]] = []
    index_by_id: dict[str, int] = {}
    for raw in base if isinstance(base, list) else []:
        relationship = _valid_execution_relationship(raw)
        if relationship is None:
            continue
        relationship_id = str(relationship["relationship_id"])
        existing_index = index_by_id.get(relationship_id)
        if existing_index is None:
            index_by_id[relationship_id] = len(relationships)
            relationships.append(relationship)
        else:
            relationships[existing_index] = relationship

    added = refreshed = 0
    for raw in addition if isinstance(addition, list) else []:
        relationship = _valid_execution_relationship(raw)
        if relationship is None:
            continue
        relationship_id = str(relationship["relationship_id"])
        existing_index = index_by_id.get(relationship_id)
        if existing_index is None:
            index_by_id[relationship_id] = len(relationships)
            relationships.append(relationship)
            added += 1
            continue
        if relationships[existing_index] != relationship:
            relationships[existing_index] = relationship
            refreshed += 1
    return relationships, added, refreshed


def _valid_execution_relationship(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    relationship_id = str(value.get("relationship_id") or "").strip()
    raw_task_ids = value.get("task_ids")
    task_ids = list(
        dict.fromkeys(
            str(task_id).strip()
            for task_id in (raw_task_ids if isinstance(raw_task_ids, list) else [])
            if str(task_id).strip()
        )
    )
    if not relationship_id or len(task_ids) < 2:
        return None
    relationship = copy.deepcopy(value)
    relationship["relationship_id"] = relationship_id
    relationship["task_ids"] = task_ids
    return relationship


def _latest_explicit_handoff(
    base: dict[str, Any],
    addition: Any,
) -> dict[str, Any]:
    if isinstance(addition, dict) and isinstance(addition.get("backfill_handoff"), dict):
        return copy.deepcopy(addition["backfill_handoff"])
    if isinstance(base.get("backfill_handoff"), dict):
        return copy.deepcopy(base["backfill_handoff"])
    return {
        "ready_for_writer": True,
        "blocking_request_ids": [],
        "reason": "Host inferred a non-blocking Writer handoff during semantic merge.",
        "inferred": True,
    }


def canonical_fact_key(fact: dict[str, Any]) -> tuple[str, str, str, str, str]:
    value = fact.get("value") if isinstance(fact.get("value"), dict) else {}
    source = fact.get("source") if isinstance(fact.get("source"), dict) else {}
    anchor = canonical_figure_ref(
        " ".join(
            [
                str(fact.get("name") or ""),
                str(source.get("figure_ref") or ""),
                str(source.get("quote") or ""),
            ]
        )
    )
    method = _first_value(value, "method", "algorithm", "baseline", "scheme", "receiver")
    regime = _first_value(value, "regime", "scenario", "condition", "setting", "channel")
    return (
        _normalize(fact.get("type")),
        _normalize_fact_label(fact.get("name")),
        anchor,
        _normalize(method),
        _normalize(regime),
    )


def canonical_task_key(task: dict[str, Any]) -> tuple[str, str, str, str]:
    experiment_id = _normalize(task.get("experiment_id"))
    anchor = canonical_figure_ref(
        " ".join(str(task.get(key) or "") for key in ("figure_or_claim", "target", "task_id"))
    )
    identity = experiment_id or anchor or _normalize(task.get("figure_or_claim"))
    return (
        identity,
        _normalize(task.get("metric")),
        "",
        "",
    )


def canonical_figure_ref(text: str) -> str:
    matches: list[str] = []
    for match in _FIGURE_RE.finditer(text or ""):
        number = match.group(1) or match.group(4)
        subfigure = (match.group(2) or match.group(3) or match.group(5) or match.group(6) or "").lower()
        ref = f"fig:{number}:{subfigure}" if subfigure else f"fig:{number}"
        if ref not in matches:
            matches.append(ref)
    return "|".join(matches)


def semantic_conflicts(document: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    meta = document.get("_meta") if isinstance(document, dict) else None
    semantic = meta.get("semantic_merge") if isinstance(meta, dict) else None
    key = "fact_conflicts" if kind == "fact" else "task_conflicts"
    values = semantic.get(key) if isinstance(semantic, dict) else None
    return [item for item in values if isinstance(item, dict)] if isinstance(values, list) else []


def _merge_fact_fields(current: dict[str, Any], candidate: dict[str, Any], key: tuple[str, ...]) -> tuple[bool, list[dict[str, Any]]]:
    changed = False
    conflicts: list[dict[str, Any]] = []
    current_value = current.get("value") if isinstance(current.get("value"), dict) else {}
    candidate_value = candidate.get("value") if isinstance(candidate.get("value"), dict) else {}
    merged_value = dict(current_value)
    for field, value in candidate_value.items():
        if field not in merged_value or merged_value[field] in (None, "", [], {}):
            merged_value[field] = value
            changed = True
        elif value not in (None, "", [], {}) and not _equivalent(merged_value[field], value):
            conflicts.append(_conflict("fact", key, f"value.{field}", merged_value[field], value, current, candidate))
    if changed:
        current["value"] = merged_value
    # Evidence labels describe a particular value. If the candidate disputes
    # any existing value, retain its source/quality in the conflict record
    # instead of attaching those labels to the older canonical value.
    if not conflicts:
        if _confidence_rank(candidate.get("confidence")) > _confidence_rank(current.get("confidence")):
            current["confidence"] = candidate.get("confidence")
            changed = True
        if _evidence_rank(candidate.get("evidence_kind")) > _evidence_rank(current.get("evidence_kind")):
            current["evidence_kind"] = candidate.get("evidence_kind")
            changed = True
        if not current.get("derivation") and candidate.get("derivation"):
            current["derivation"] = candidate.get("derivation")
            changed = True
    return changed, conflicts


def _merge_task_fields(current: dict[str, Any], candidate: dict[str, Any], key: tuple[str, ...]) -> tuple[bool, list[dict[str, Any]]]:
    changed = False
    conflicts: list[dict[str, Any]] = []
    # scientific_acceptance is one coherent authority snapshot. Combining its
    # nested arrays across rounds would let stale and refined criteria drift apart.
    candidate_acceptance = candidate.get("scientific_acceptance")
    if isinstance(candidate_acceptance, dict) and candidate_acceptance != current.get(
        "scientific_acceptance"
    ):
        current["scientific_acceptance"] = copy.deepcopy(candidate_acceptance)
        changed = True
    for field in (
        "expected_artifacts",
        "output_columns",
        "required_facts",
        "assumptions",
        "formula_chain",
        "parameter_matrix",
        "baseline_definitions",
        "statistical_protocol",
        "validation_anchors",
    ):
        candidate_items = candidate.get(field)
        if not isinstance(candidate_items, list) or not candidate_items:
            continue
        merged = _stable_union(current.get(field), candidate_items)
        if merged != current.get(field):
            current[field] = merged
            changed = True
    current_comparison = current.get("comparison") if isinstance(current.get("comparison"), dict) else {}
    candidate_comparison = candidate.get("comparison") if isinstance(candidate.get("comparison"), dict) else {}
    for field in ("baselines", "curve_groups"):
        candidate_items = candidate_comparison.get(field)
        if not isinstance(candidate_items, list) or not candidate_items:
            continue
        merged = _stable_union(current_comparison.get(field), candidate_items)
        if merged != current_comparison.get(field):
            current_comparison[field] = merged
            changed = True
    if current_comparison:
        current["comparison"] = current_comparison
    for field in ("metric", "metric_formula", "figure_or_claim"):
        left, right = current.get(field), candidate.get(field)
        if left not in (None, "") and right not in (None, "") and not _equivalent(left, right):
            conflicts.append(_conflict("task", key, field, left, right, current, candidate))
    return changed, conflicts


def _conflict(kind: str, key: tuple[str, ...], field: str, left: Any, right: Any, base: dict[str, Any], addition: dict[str, Any]) -> dict[str, Any]:
    raw = json.dumps([kind, key, field, left, right], ensure_ascii=False, sort_keys=True, default=str)
    return {
        "fingerprint": hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20],
        "kind": kind,
        "canonical_key": list(key),
        "field": field,
        "base_value": left,
        "candidate_value": right,
        "base_source": base.get("source"),
        "candidate_source": addition.get("source"),
        "status": "unresolved",
    }


def _merge_named_items(left: Any, right: Any) -> list[Any]:
    return _stable_union(left, right, key=lambda item: _normalize(item.get("name")) if isinstance(item, dict) else _normalize(item))


def _stable_union(left: Any, right: Any, key=None) -> list[Any]:
    left_items = list(left) if isinstance(left, list) else []
    right_items = list(right) if isinstance(right, list) else []
    key_fn = key or (lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True, default=str))
    seen = {key_fn(item) for item in left_items}
    result = list(left_items)
    for item in right_items:
        marker = key_fn(item)
        if marker in seen:
            continue
        seen.add(marker)
        result.append(item)
    return result


def _first_value(value: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if value.get(key) not in (None, "", [], {}):
            return value[key]
    return ""


def _normalize(value: Any) -> str:
    """Normalize identifier typography without discarding scientific symbols."""

    text = "" if value is None else str(value)
    return " ".join(unicodedata.normalize("NFC", text).split())


def _normalize_fact_label(value: Any) -> str:
    """Fold ordinary words in descriptive labels, not scientific identifiers.

    A label such as ``SNR range`` is prose and may recur as ``snr range``.
    Standalone identifiers, short symbols/units, mixed-case tokens (mW, MHz),
    punctuation, and non-ASCII letters remain exact. Scientific values use
    _equivalent, never this label-only aliasing.
    """

    label = _normalize(value)
    words = label.split()
    if len(words) < 2:
        return label
    return " ".join(
        word.lower()
        if len(word) >= 3 and word.isascii() and word.isalpha()
        and (word.islower() or word.isupper() or word.istitle())
        else word
        for word in words
    )


def _equivalent(left: Any, right: Any) -> bool:
    """Compare scientific values without an implicit numerical tolerance.

    Signs, decimal points, exponents, units, case, and Unicode letters
    are semantic content. Evidence conflicts are cheaper than silently erasing
    a difference that a later expert needs to adjudicate.
    """

    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    if isinstance(left, str) and isinstance(right, str):
        return " ".join(unicodedata.normalize("NFC", left).split()) == " ".join(
            unicodedata.normalize("NFC", right).split()
        )
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(
            _equivalent(value, right[key]) for key, value in left.items()
        )
    if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        return len(left) == len(right) and all(
            _equivalent(a, b) for a, b in zip(left, right)
        )
    return type(left) is type(right) and left == right


def _confidence_rank(value: Any) -> int:
    return {"low": 0, "medium": 1, "high": 2}.get(str(value).lower(), 0)


def _evidence_rank(value: Any) -> int:
    return {
        "visual_estimate": 0,
        "paper_derived": 1,
        "paper_explicit": 2,
    }.get(str(value).lower(), 0)
