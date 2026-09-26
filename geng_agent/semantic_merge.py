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


def semantic_merge_repro_tasks(base, addition, *, merge_mode="snapshot"):
    """Compatibility wrapper: publish the planner's complete task document unchanged.

    New pipeline code already publishes the final plan directly. Keeping this
    old import lossless prevents callers from restoring tasks the planner merged.
    """
    return copy.deepcopy(addition), int(base != addition)


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
