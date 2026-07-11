from __future__ import annotations

import hashlib
import json
import re
from typing import Any


_FIGURE_RE = re.compile(
    r"\bfig(?:ure)?s?\.?\s*(\d{1,3})(?!\d)(?:\s*\(([a-z])\)|([a-z])\b)?|"
    r"图\s*(\d{1,3})(?!\d)(?:\s*[（(]([a-z])[）)]|([a-z])\b)?",
    re.IGNORECASE,
)


def semantic_merge_engineering_facts(base: dict[str, Any], addition: dict[str, Any]) -> tuple[dict[str, Any], int]:
    merged = dict(base) if isinstance(base, dict) else {}
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
            facts.append(dict(candidate))
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


def semantic_merge_repro_tasks(base: dict[str, Any], addition: dict[str, Any]) -> tuple[dict[str, Any], int]:
    merged = dict(base) if isinstance(base, dict) else {}
    tasks = [dict(item) for item in merged.get("repro_tasks", []) if isinstance(item, dict)]
    index = {canonical_task_key(item): position for position, item in enumerate(tasks)}
    task_id_index = {
        _normalize(item.get("task_id")): position
        for position, item in enumerate(tasks)
        if _normalize(item.get("task_id"))
    }
    meta = dict(merged.get("_meta", {})) if isinstance(merged.get("_meta"), dict) else {}
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
            tasks.append(dict(candidate))
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
            "merge_version": 2,
            "last_added": added,
            "last_enriched": enriched,
            "last_new_conflicts": new_conflicts,
            "task_conflicts": conflicts,
        }
    )
    meta["semantic_merge"] = semantic_meta
    merged["_meta"] = meta
    return merged, added + enriched + new_conflicts


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
        _normalize(fact.get("name")),
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
    if _confidence_rank(candidate.get("confidence")) > _confidence_rank(current.get("confidence")):
        current["confidence"] = candidate.get("confidence")
        changed = True
    return changed, conflicts


def _merge_task_fields(current: dict[str, Any], candidate: dict[str, Any], key: tuple[str, ...]) -> tuple[bool, list[dict[str, Any]]]:
    changed = False
    conflicts: list[dict[str, Any]] = []
    for field in ("expected_artifacts", "output_columns", "required_facts", "assumptions"):
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
    if isinstance(value, (list, tuple, set)):
        value = " ".join(str(item) for item in value)
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _equivalent(left: Any, right: Any) -> bool:
    return _normalize(left) == _normalize(right)


def _confidence_rank(value: Any) -> int:
    return {"low": 0, "medium": 1, "high": 2}.get(str(value).lower(), 0)
