"""Local repair of second-round reproduction-task generation.

This mirrors ``facts_normalize`` for the ``repro_tasks`` stage. In real runs the LLM
designs sensible tasks but trips strict validation on cosmetic issues: a ``metric``
or ``expected_trend.direction`` enum synonym outside the closed ``Literal`` set,
``required_facts`` emitted as bare strings or dicts with extra keys (``fact_id``,
``source_chunk_id``) instead of ``{type, name}``, stray top-level metadata
(``paper_id``, ``venue`` …), or a truncated JSON stream. Strict all-or-nothing
validation then discards the whole task set and forces the generic local fallback.

Three conservative, auditable layers run *before* the strict schema check:

1. normalization     - map enum synonyms, strip unknown keys, repair ``required_facts``
   refs against the actually-extracted facts, coerce nested shapes. Logged to ``_meta``.
2. partial acceptance - drop only the individual tasks that still cannot validate,
   keep the rest (the schema already requires >= 1 task, which acts as the floor).
3. truncation salvage - recover the largest parseable prefix of ``repro_tasks``.

Traceability is preserved: a ``required_facts`` ref is kept only if it resolves to a
real extracted fact; refs and tasks that cannot be repaired are dropped and recorded.
"""

from __future__ import annotations

import copy
import re
from typing import Any, get_args

from pydantic import ValidationError

from .facts_normalize import _map_enum, _norm_token, _salvage_array_objects
from .json_utils import prepare_json_candidate
from .schema_models import AssumptionRisk, FactType, MetricName, MissingImpact, ReproTask, TrendDirection

_ALLOWED_METRICS = set(get_args(MetricName))
_ALLOWED_DIRECTIONS = set(get_args(TrendDirection))
_ALLOWED_RISK = set(get_args(AssumptionRisk))
_ALLOWED_FACT_TYPES = set(get_args(FactType))
_ALLOWED_IMPACTS = set(get_args(MissingImpact))

_TASK_KEYS = {
    "task_id",
    "target",
    "metric",
    "metric_formula",
    "figure_or_claim",
    "expected_artifacts",
    "output_columns",
    "expected_trend",
    "comparison",
    "required_facts",
    "missing_fact_requests",
    "assumptions",
    "risk_if_unreproducible",
}
_TREND_KEYS = {"x_axis", "y_axis", "direction", "reason"}
_COMPARISON_KEYS = {"baselines", "curve_groups", "tolerance"}
_ASSUMPTION_KEYS = {"name", "default_value", "reason", "risk"}
_DOC_KEYS = {"repro_tasks", "_meta"}

METRIC_SYNONYMS = {
    "ber": "bit_error_rate",
    "bit_error_ratio": "bit_error_rate",
    "bit_error_probability": "bit_error_rate",
    "probability_of_bit_error": "bit_error_rate",
    "ser": "symbol_error_rate",
    "symbol_error_ratio": "symbol_error_rate",
    "data_rate": "throughput",
    "goodput": "throughput",
    "latency": "delay",
    "se": "spectral_efficiency",
    "outage": "outage_probability",
    "pout": "outage_probability",
    "ee": "energy_efficiency",
    "acc": "accuracy",
    "classification_accuracy": "accuracy",
    "mse": "loss",
    "training_loss": "loss",
    "cost": "loss",
}

DIRECTION_SYNONYMS = {
    "decreases": "decreasing",
    "decrease": "decreasing",
    "down": "decreasing",
    "falling": "decreasing",
    "declining": "decreasing",
    "monotonically_decreasing": "decreasing",
    "increases": "increasing",
    "increase": "increasing",
    "up": "increasing",
    "rising": "increasing",
    "growing": "increasing",
    "monotonically_increasing": "increasing",
    "constant": "flat",
    "stable": "flat",
    "unchanged": "flat",
    "non_monotonic": "unknown",
    "mixed": "unknown",
    "varies": "unknown",
    "na": "unknown",
    "none": "unknown",
}

_RISK_SYNONYMS = {"med": "medium", "moderate": "medium", "mid": "medium", "hi": "high", "critical": "high", "severe": "high", "lo": "low", "minor": "low", "unknown": "medium"}


def _map_metric(value: Any) -> tuple[str, bool]:
    mapped, changed = _map_enum(value, _ALLOWED_METRICS, METRIC_SYNONYMS, "other")
    if mapped != "other":
        return mapped, changed
    if isinstance(value, str):  # substring fallback for free-text metric names
        token = _norm_token(value)
        for needle, target in (("bit_error", "bit_error_rate"), ("symbol_error", "symbol_error_rate"), ("throughput", "throughput"), ("outage", "outage_probability"), ("spectral", "spectral_efficiency"), ("energy_eff", "energy_efficiency"), ("accuracy", "accuracy"), ("delay", "delay")):
            if needle in token:
                return target, True
    return "other", changed


def _map_direction(value: Any) -> tuple[str, bool]:
    mapped, changed = _map_enum(value, _ALLOWED_DIRECTIONS, DIRECTION_SYNONYMS, "unknown")
    if mapped != "unknown" or (isinstance(value, str) and _norm_token(value) in {"unknown", "na", "none", "non_monotonic", "mixed", "varies"}):
        return mapped, changed
    if isinstance(value, str):  # substring fallback ("decreasing then flat", "BER falls", ...)
        token = _norm_token(value)
        if "decreas" in token or "fall" in token or "declin" in token:
            return "decreasing", True
        if "increas" in token or "ris" in token or "grow" in token:
            return "increasing", True
        if "flat" in token or "constant" in token or "stable" in token:
            return "flat", True
    return "unknown", changed


def _compact_token(value: str) -> str:
    return _norm_token(value).replace("_", "")


def _fact_aliases(fact_type: str, name: str, value: Any) -> set[str]:
    aliases = {_norm_token(name), _compact_token(name)}
    aliases.add(re.sub(r"_channel$", "", _norm_token(name)))
    aliases.add(re.sub(r"_model$", "", _norm_token(name)))

    for match in re.finditer(r"\(([^)]+)\)", name):
        aliases.add(_norm_token(match.group(1)))
        aliases.add(_compact_token(match.group(1)))

    figure_match = re.search(r"(?i)\bfig(?:ure)?\s*(\d+)(?:\s*-\s*(\d+))?", name)
    if figure_match:
        start = int(figure_match.group(1))
        end = int(figure_match.group(2) or start)
        for number in range(start, end + 1):
            aliases.update({f"figure_{number}", f"fig_{number}", f"f_fig_{number}"})

    lowered = name.lower()
    compact_name = _compact_token(name)
    if fact_type == "channel_model":
        if "awgn" in lowered:
            aliases.update({"awgn", "ch_awgn", "f_ch_awgn"})
        if "rayleigh" in lowered:
            aliases.update({"rayleigh", "ch_rayleigh", "f_ch_rayleigh"})
        if "multipath" in lowered and "rayleigh" in lowered:
            aliases.update({"multipath_rayleigh", "rayleigh_multipath", "ch_rayleigh_multipath", "ch_multipath_rayleigh", "f_ch_rayleigh_multipath", "f_ch_multipath_rayleigh"})
    elif fact_type == "modulation":
        aliases.update({compact_name, f"mod_{compact_name}", f"f_mod_{compact_name}"})
    elif fact_type == "metric":
        if "ber" in lowered or "bit error" in lowered:
            aliases.update({"ber", "metric_ber", "f_metric_ber", "bit_error_rate", "metric_bit_error_rate"})

    if isinstance(value, dict):
        _add_value_aliases(aliases, fact_type, name, value)
    return {alias for alias in aliases if alias}


def _add_value_aliases(aliases: set[str], fact_type: str, name: str, value: dict[str, Any]) -> None:
    lowered = name.lower()
    if "roll" in lowered:
        rolloff = value.get("roll_off_factor") or value.get("rolloff")
        if rolloff is not None:
            token = _number_token(rolloff)
            aliases.update({f"roll_{token}", f"rc_roll_{token}", f"f_rc_roll_{token}"})
    if "down" in lowered and "sampl" in lowered:
        factor = value.get("factor") or value.get("down_sampling_factor")
        if factor is not None:
            token = _number_token(factor)
            aliases.update({f"downsample_{token}", f"down_sampling_{token}", f"rx_downsample_{token}", f"f_rx_downsample_{token}"})
    if "snr" in lowered:
        snr = value.get("value_dB") or value.get("snr_db") or value.get("snr")
        if snr is not None:
            token = _number_token(snr)
            aliases.update({f"snr_{token}", f"snr_{token}db", f"f_snr_{token}db"})
    if "doppler" in lowered:
        doppler = value.get("value_Hz") or value.get("doppler_hz")
        if doppler is not None:
            token = _number_token(doppler)
            aliases.update({f"doppler_{token}", f"doppler_{token}hz", f"f_doppler_{token}hz"})


def _number_token(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return _norm_token(str(value))
    if number.is_integer():
        return str(int(number))
    return str(number).rstrip("0").rstrip(".").replace(".", "_")


def _build_fact_index(facts: Any) -> tuple[set[tuple[str, str]], dict[str, set[str]], dict[str, tuple[str, str]]]:
    keys: set[tuple[str, str]] = set()
    name_to_types: dict[str, set[str]] = {}
    alias_to_key: dict[str, tuple[str, str]] = {}
    items = facts.get("engineering_facts") if isinstance(facts, dict) else None
    if isinstance(items, list):
        for fact in items:
            if isinstance(fact, dict):
                fact_type, name = fact.get("type"), fact.get("name")
                if isinstance(fact_type, str) and isinstance(name, str):
                    key = (fact_type, name)
                    keys.add(key)
                    name_to_types.setdefault(name, set()).add(fact_type)
                    for alias in _fact_aliases(fact_type, name, fact.get("value")):
                        alias_to_key.setdefault(alias, key)
                        alias_to_key.setdefault(alias.replace("_", ""), key)
    return keys, name_to_types, alias_to_key


def normalize_repro_tasks_candidate(data: Any, facts: Any) -> tuple[dict[str, Any], list[str]]:
    """Return a coerced copy of the candidate plus a log of every change made."""
    coercions: list[str] = []
    if not isinstance(data, dict):
        return {"repro_tasks": []}, ["top-level was not a JSON object"]

    out = copy.deepcopy(data)

    extra_top = [key for key in out if key not in _DOC_KEYS]
    for key in extra_top:
        out.pop(key, None)
    if extra_top:
        coercions.append(f"dropped unknown top-level keys {sorted(extra_top)}")

    fact_keys, name_to_types, alias_to_key = _build_fact_index(facts)
    tasks = out.get("repro_tasks")
    if isinstance(tasks, list):
        for index, task in enumerate(tasks):
            if isinstance(task, dict):
                _normalize_task(task, index, coercions, fact_keys, name_to_types, alias_to_key)
    else:
        out["repro_tasks"] = []
        if tasks is not None:
            coercions.append("repro_tasks was not a list -> []")
    return out, coercions


def _normalize_task(task: dict[str, Any], index: int, coercions: list[str], fact_keys: set[tuple[str, str]], name_to_types: dict[str, set[str]], alias_to_key: dict[str, tuple[str, str]]) -> None:
    extra = [key for key in task if key not in _TASK_KEYS]
    for key in extra:
        task.pop(key, None)
    if extra:
        coercions.append(f"tasks[{index}] dropped unknown keys {sorted(extra)}")

    raw_metric = task.get("metric")
    metric, changed = _map_metric(raw_metric)
    task["metric"] = metric
    if changed:
        coercions.append(f"tasks[{index}].metric {raw_metric!r} -> {metric!r}")

    trend = task.get("expected_trend")
    if isinstance(trend, dict):
        _normalize_trend(trend, index, coercions)

    comparison = task.get("comparison")
    if isinstance(comparison, dict):
        _normalize_comparison(comparison, index, coercions)

    for key in ("expected_artifacts", "output_columns"):
        value = task.get(key)
        if isinstance(value, list):
            task[key] = [item for item in value if isinstance(item, str) and item.strip()]
        elif value is None or key not in task:
            task[key] = []
            coercions.append(f"tasks[{index}].{key} -> []")

    task["required_facts"] = _normalize_required_facts(task.get("required_facts"), index, coercions, fact_keys, name_to_types, alias_to_key)
    task["missing_fact_requests"] = _normalize_missing_fact_requests(
        task.get("missing_fact_requests"), index, coercions
    )
    task["assumptions"] = _normalize_assumptions(task.get("assumptions"), index, coercions)


def _normalize_trend(trend: dict[str, Any], index: int, coercions: list[str]) -> None:
    extra = [key for key in trend if key not in _TREND_KEYS]
    for key in extra:
        trend.pop(key, None)
    if extra:
        coercions.append(f"tasks[{index}].expected_trend dropped unknown keys {sorted(extra)}")
    raw_dir = trend.get("direction")
    direction, changed = _map_direction(raw_dir)
    trend["direction"] = direction
    if changed:
        coercions.append(f"tasks[{index}].expected_trend.direction {raw_dir!r} -> {direction!r}")
    for key in ("x_axis", "y_axis", "reason"):
        value = trend.get(key)
        if value is None or key not in trend:
            trend[key] = ""
        elif not isinstance(value, str):
            trend[key] = str(value)


def _normalize_comparison(comparison: dict[str, Any], index: int, coercions: list[str]) -> None:
    extra = [key for key in comparison if key not in _COMPARISON_KEYS]
    for key in extra:
        comparison.pop(key, None)
    if extra:
        coercions.append(f"tasks[{index}].comparison dropped unknown keys {sorted(extra)}")
    for key in ("baselines", "curve_groups"):
        value = comparison.get(key)
        comparison[key] = [item for item in value if isinstance(item, str)] if isinstance(value, list) else []
    tolerance = comparison.get("tolerance")
    if not (isinstance(tolerance, str) and tolerance.strip()):
        comparison["tolerance"] = "unspecified"
        coercions.append(f"tasks[{index}].comparison.tolerance -> 'unspecified'")


def _normalize_required_facts(items: Any, index: int, coercions: list[str], fact_keys: set[tuple[str, str]], name_to_types: dict[str, set[str]], alias_to_key: dict[str, tuple[str, str]]) -> list[dict[str, str]]:
    if not isinstance(items, list):
        return []
    resolved: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    dropped = 0
    for ref in items:
        key = _resolve_required_fact_ref(ref, fact_keys, name_to_types, alias_to_key)
        if key is None:
            dropped += 1
            continue
        if key in seen:
            continue
        seen.add(key)
        resolved.append({"type": key[0], "name": key[1]})
    if dropped:
        coercions.append(f"tasks[{index}] dropped {dropped} required_fact ref(s) with no matching extracted fact")
    return resolved


def _resolve_required_fact_ref(ref: Any, fact_keys: set[tuple[str, str]], name_to_types: dict[str, set[str]], alias_to_key: dict[str, tuple[str, str]]) -> tuple[str, str] | None:
    name: Any = None
    ref_type: Any = None
    if isinstance(ref, dict):
        name, ref_type = ref.get("name"), ref.get("type")
        for alias_key in ("fact_id", "id", "source_fact", "source_fact_id"):
            if not (isinstance(name, str) and name.strip()) and isinstance(ref.get(alias_key), str):
                name = ref.get(alias_key)
    elif isinstance(ref, str):
        name = ref
    if not (isinstance(name, str) and name.strip()):
        return None
    if isinstance(ref_type, str) and (ref_type, name) in fact_keys:
        return (ref_type, name)
    types = name_to_types.get(name)
    if types and len(types) == 1:
        return (next(iter(types)), name)

    for alias in _ref_aliases(name):
        key = alias_to_key.get(alias) or alias_to_key.get(alias.replace("_", ""))
        if key is not None:
            return key
    return None


def _ref_aliases(value: str) -> set[str]:
    token = _norm_token(value)
    aliases = {token, token.replace("_", "")}
    stripped = re.sub(r"^f_", "", token)
    aliases.add(stripped)
    aliases.add(stripped.replace("_", ""))
    for prefix in ("ch_", "mod_", "metric_", "fig_", "snr_", "rx_", "rc_", "doppler_", "eq_"):
        if stripped.startswith(prefix):
            tail = stripped[len(prefix) :]
            aliases.update({tail, tail.replace("_", ""), prefix + tail})
            if prefix == "fig_":
                aliases.update({f"figure_{tail}", f"f_fig_{tail}"})
    aliases.add(token.replace("psk", "_psk"))
    aliases.add(stripped.replace("psk", "_psk"))
    return {alias for alias in aliases if alias}


def _normalize_assumptions(items: Any, index: int, coercions: list[str]) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    cleaned: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name, reason = item.get("name"), item.get("reason")
        if not (isinstance(name, str) and name.strip()) or not (isinstance(reason, str) and reason.strip()):
            continue
        risk, _ = _map_enum(item.get("risk"), _ALLOWED_RISK, _RISK_SYNONYMS, "medium")
        cleaned.append({"name": name, "default_value": item.get("default_value"), "reason": reason, "risk": risk})
    return cleaned


def _normalize_missing_fact_requests(
    items: Any, index: int, coercions: list[str]
) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    cleaned: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for request_index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        why_needed = str(item.get("why_needed") or "").strip()
        fact_type = str(item.get("type") or "other").strip()
        if fact_type not in _ALLOWED_FACT_TYPES:
            fact_type = "other"
        if not name or not why_needed:
            continue
        key = (fact_type, name.casefold())
        if key in seen:
            continue
        seen.add(key)
        impact = str(item.get("impact") or "medium").strip().lower()
        if impact not in _ALLOWED_IMPACTS:
            impact = "medium"
        request_id = str(item.get("request_id") or f"task_{index + 1}_request_{request_index + 1}").strip()
        search_targets = item.get("search_targets")
        cleaned.append(
            {
                "request_id": request_id,
                "type": fact_type,
                "name": name,
                "why_needed": why_needed,
                "impact": impact,
                "search_targets": [
                    str(target).strip()
                    for target in search_targets if str(target).strip()
                ] if isinstance(search_targets, list) else [],
            }
        )
    if len(cleaned) != len(items):
        coercions.append(f"tasks[{index}] normalized missing_fact_requests")
    return cleaned


def _task_rejection_reason(task: Any) -> str | None:
    if not isinstance(task, dict):
        return "not a JSON object"
    try:
        ReproTask.model_validate(task)
    except ValidationError as exc:
        error = exc.errors()[0]
        loc = ".".join(str(part) for part in error.get("loc", ()))
        return f"{loc or '$'}: {error.get('msg', 'invalid value')}"
    return None


def select_valid_repro_tasks(data: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    tasks = data.get("repro_tasks")
    if not isinstance(tasks, list):
        return kept, dropped
    for index, task in enumerate(tasks):
        reason = _task_rejection_reason(task)
        if reason is None:
            kept.append(task)
        else:
            dropped.append({"index": index, "task_id": task.get("task_id") if isinstance(task, dict) else None, "reason": reason})
    return kept, dropped


def finalize_repro_tasks(data: Any, facts: Any) -> dict[str, Any]:
    """Normalize, drop irreparable tasks, and assemble a strict-schema-clean document."""
    normalized, coercions = normalize_repro_tasks_candidate(data, facts)
    kept, dropped = select_valid_repro_tasks(normalized)

    doc: dict[str, Any] = {"repro_tasks": kept}
    meta = dict(normalized["_meta"]) if isinstance(normalized.get("_meta"), dict) else {}
    if coercions:
        meta["normalization_used"] = True
        meta["coercion_count"] = len(coercions)
        meta["coercions"] = coercions[:50]
    if dropped:
        meta["partial_acceptance_used"] = True
        meta["dropped_task_count"] = len(dropped)
        meta["kept_task_count"] = len(kept)
        meta["dropped_tasks"] = dropped[:50]
    if meta:
        doc["_meta"] = meta
    return doc


def recover_truncated_repro_tasks(raw: str) -> dict[str, Any] | None:
    """Best-effort recovery of a truncated tasks payload by salvaging complete objects
    from the ``repro_tasks`` array. Returns ``None`` if nothing usable can be recovered."""
    if not isinstance(raw, str):
        return None
    text = prepare_json_candidate(raw)
    key_index = text.find('"repro_tasks"')
    if key_index < 0:
        return None
    bracket_index = text.find("[", key_index)
    if bracket_index < 0:
        return None
    objects = _salvage_array_objects(text, bracket_index)
    if not objects:
        return None
    return {"repro_tasks": objects, "_meta": {"truncation_recovered": True, "recovered_task_count": len(objects)}}
