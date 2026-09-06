"""Permissive local normalization for reproduction-task generation.

The Task Designer may omit legacy descriptive fields or use near-miss enum and
reference shapes. The normalizer preserves every recoverable scientific task,
repairs cosmetic structure, and supplies only the minimum deterministic hand-off
needed by the Writer. When no task can be recovered, it creates one explicit
fallback task whose uncertainty remains visible downstream instead of terminating
the pipeline on schema shape alone.
"""

from __future__ import annotations

import copy
import re
from typing import Any, get_args

from pydantic import ValidationError

from .facts_normalize import _map_enum, _norm_token, _salvage_array_objects
from .json_utils import prepare_json_candidate
from .task_acceptance_normalize import (
    DIRECTION_SYNONYMS,
    _acceptance_text,
    _contract_id_token,
    _default_core_conclusion,
    _finite_float_or_none,
    _is_presentation_only,
    _map_direction,
    _normalize_comparison,
    _normalize_scientific_acceptance,
    _normalize_trend,
    _numeric_magnitude_and_unit,
    _stable_contract_id,
)
from .task_relationship_normalize import (
    _normalize_backfill_handoff,
    _normalize_execution_relationships,
    _relationship_string_list,
)
from .schema_models import (
    AssumptionRisk,
    FactType,
    MetricName,
    MissingImpact,
    ReproTask,
    TaskSpecificationStatus,
    TrendDirection,
)

_ALLOWED_METRICS = set(get_args(MetricName))
_ALLOWED_RISK = set(get_args(AssumptionRisk))
_ALLOWED_FACT_TYPES = set(get_args(FactType))
_ALLOWED_IMPACTS = set(get_args(MissingImpact))
_ALLOWED_SPEC_STATUSES = set(get_args(TaskSpecificationStatus))

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
    "formula_chain",
    "parameter_matrix",
    "baseline_definitions",
    "statistical_protocol",
    "validation_anchors",
    "scientific_acceptance",
}
_ASSUMPTION_KEYS = {
    "name",
    "default_value",
    "reason",
    "risk",
    "request_id",
    "field_ids",
    "sensitivity_check",
}
_DOC_KEYS = {
    "schema_version",
    "backfill_handoff",
    "execution_relationships",
    "repro_tasks",
    "_meta",
}

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
        data = {"repro_tasks": []}
        coercions.append("top-level was not a JSON object")

    out = copy.deepcopy(data)
    existing_meta = out.get("_meta") if isinstance(out.get("_meta"), dict) else {}
    raw_handoff = out.get("backfill_handoff", existing_meta.get("backfill_handoff"))
    raw_relationships = out.get("execution_relationships")

    if out.get("schema_version") != "2.0":
        out["schema_version"] = "2.0"
        coercions.append("schema_version -> '2.0'")

    extra_top = [key for key in out if key not in _DOC_KEYS]
    for key in extra_top:
        out.pop(key, None)
    if extra_top:
        coercions.append(f"dropped unknown top-level keys {sorted(extra_top)}")

    fact_keys, name_to_types, alias_to_key = _build_fact_index(facts)
    tasks = out.get("repro_tasks")
    if isinstance(tasks, list):
        normalized_tasks: list[dict[str, Any]] = []
        for index, raw_task in enumerate(tasks):
            if isinstance(raw_task, dict):
                task = raw_task
            elif isinstance(raw_task, str) and raw_task.strip():
                task = {"target": raw_task.strip(), "figure_or_claim": raw_task.strip()}
                coercions.append(
                    f"tasks[{index}] converted a text-only task into a minimal scientific task"
                )
            else:
                coercions.append(
                    f"tasks[{index}] ignored an entry with no recoverable scientific goal"
                )
                continue
            _normalize_task(task, index, coercions, fact_keys, name_to_types, alias_to_key)
            normalized_tasks.append(task)
        out["repro_tasks"] = normalized_tasks
    else:
        out["repro_tasks"] = []
        if tasks is not None:
            coercions.append("repro_tasks was not a list -> []")
    out["backfill_handoff"] = _normalize_backfill_handoff(raw_handoff, coercions)
    out["execution_relationships"] = _normalize_execution_relationships(
        raw_relationships,
        coercions,
    )
    if isinstance(out.get("_meta"), dict):
        out["_meta"].pop("backfill_handoff", None)
    return out, coercions


def _normalize_task(task: dict[str, Any], index: int, coercions: list[str], fact_keys: set[tuple[str, str]], name_to_types: dict[str, set[str]], alias_to_key: dict[str, tuple[str, str]]) -> None:
    _ensure_minimum_task_fields(task, index, coercions)
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

    _normalize_scientific_acceptance(task, index, coercions)

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
    for key in (
        "formula_chain",
        "parameter_matrix",
        "baseline_definitions",
        "statistical_protocol",
        "validation_anchors",
    ):
        task[key] = _normalize_spec_items(
            task.get(key), key, index, coercions, fact_keys, name_to_types, alias_to_key
        )


def _ensure_minimum_task_fields(
    task: dict[str, Any],
    index: int,
    coercions: list[str],
) -> None:
    """Preserve a runnable scientific hand-off without requiring legacy prose fields."""

    task_id = _acceptance_text(task.get("task_id"))
    if not task_id:
        task_id = f"task_{index + 1}"
        coercions.append(f"tasks[{index}].task_id -> {task_id!r}")
    task["task_id"] = task_id

    goal = _recover_task_scientific_goal(task, task_id)
    if not _acceptance_text(task.get("target")):
        task["target"] = goal
        coercions.append(f"tasks[{index}].target recovered from scientific task semantics")
    else:
        task["target"] = _acceptance_text(task.get("target"))

    if not _acceptance_text(task.get("figure_or_claim")):
        task["figure_or_claim"] = task["target"]
        coercions.append(f"tasks[{index}].figure_or_claim -> target")
    else:
        task["figure_or_claim"] = _acceptance_text(task.get("figure_or_claim"))

    if not _acceptance_text(task.get("metric_formula")):
        task["metric_formula"] = (
            "Use the paper-defined or task-appropriate metric computation and "
            "disclose any necessary assumption."
        )
        coercions.append(f"tasks[{index}].metric_formula -> non-blocking default")

    if not isinstance(task.get("expected_trend"), dict):
        task["expected_trend"] = {
            "x_axis": "",
            "y_axis": "",
            "direction": "unknown",
            "reason": "Judge the task by its scientific acceptance conclusions.",
        }
        coercions.append(f"tasks[{index}].expected_trend -> advisory unknown trend")

    if not isinstance(task.get("comparison"), dict):
        task["comparison"] = {
            "baselines": [],
            "curve_groups": [],
            "tolerance": "host scientific materiality policy",
        }
        coercions.append(f"tasks[{index}].comparison -> advisory host policy")

    if not _acceptance_text(task.get("risk_if_unreproducible")):
        task["risk_if_unreproducible"] = (
            "This task's primary paper conclusion would remain unsupported."
        )
        coercions.append(f"tasks[{index}].risk_if_unreproducible -> scientific default")


def _recover_task_scientific_goal(task: dict[str, Any], task_id: str) -> str:
    for key in ("target", "figure_or_claim", "claim", "title", "description"):
        value = _acceptance_text(task.get(key))
        if value:
            return value
    acceptance = task.get("scientific_acceptance")
    if isinstance(acceptance, dict):
        conclusions = acceptance.get("core_conclusions")
        for item in conclusions if isinstance(conclusions, list) else []:
            if isinstance(item, dict):
                statement = _acceptance_text(item.get("statement"))
                if statement:
                    return statement
    trend = task.get("expected_trend")
    if isinstance(trend, dict):
        reason = _acceptance_text(trend.get("reason"))
        if reason:
            return reason
    return f"Reproduce the primary scientific result assigned to {task_id}."

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
        field_ids = item.get("field_ids")
        cleaned.append(
            {
                "name": name,
                "default_value": item.get("default_value"),
                "reason": reason,
                "risk": risk,
                "request_id": (
                    str(item.get("request_id")).strip()
                    if item.get("request_id") is not None
                    else None
                ),
                "field_ids": [
                    str(field_id).strip()
                    for field_id in field_ids
                    if str(field_id).strip()
                ] if isinstance(field_ids, list) else [],
                "sensitivity_check": str(item.get("sensitivity_check") or "").strip(),
            }
        )
    return cleaned


def _normalize_spec_items(
    items: Any,
    key: str,
    index: int,
    coercions: list[str],
    fact_keys: set[tuple[str, str]],
    name_to_types: dict[str, set[str]],
    alias_to_key: dict[str, tuple[str, str]],
) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    cleaned: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        status = str(item.get("status") or "unresolved").strip().lower()
        if status not in _ALLOWED_SPEC_STATUSES:
            status = "unresolved"
        evidence_facts = _normalize_required_facts(
            item.get("evidence_facts"), index, coercions,
            fact_keys, name_to_types, alias_to_key,
        )
        cleaned.append(
            {
                "name": name,
                "value": item.get("value"),
                "status": status,
                "evidence_facts": evidence_facts,
                "note": str(item.get("note") or ""),
            }
        )
    if len(cleaned) != len(items):
        coercions.append(f"tasks[{index}] normalized {key}")
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
        raw_fields = item.get("required_fields")
        required_fields: list[dict[str, Any]] = []
        seen_fields: set[str] = set()
        for field_index, field in enumerate(raw_fields if isinstance(raw_fields, list) else []):
            if not isinstance(field, dict):
                continue
            field_id = str(field.get("field_id") or "").strip()
            description = str(field.get("description") or "").strip()
            if not field_id or not description or field_id.casefold() in seen_fields:
                continue
            seen_fields.add(field_id.casefold())
            affects = field.get("affects")
            required_fields.append(
                {
                    "field_id": field_id,
                    "description": description,
                    "affects": [
                        str(value).strip()
                        for value in affects
                        if str(value).strip()
                    ] if isinstance(affects, list) else [],
                }
            )
        if not required_fields:
            required_fields = [
                {
                    "field_id": "answer",
                    "description": why_needed,
                    "affects": ["implementation"],
                }
            ]
            coercions.append(
                f"tasks[{index}].missing_fact_requests[{request_index}] added legacy answer field"
            )
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
                "required_fields": required_fields,
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


def _fallback_scientific_goal(facts: Any) -> str:
    if isinstance(facts, dict):
        fact_items = facts.get("engineering_facts")
        for item in fact_items if isinstance(fact_items, list) else []:
            if not isinstance(item, dict):
                continue
            name = _acceptance_text(item.get("name"))
            if name:
                return f"Reproduce the paper's primary scientific result associated with {name}."
    return "Reproduce the paper's primary scientific conclusion."


def _minimum_task_seed(source: Any, index: int, facts: Any) -> dict[str, Any]:
    source = source if isinstance(source, dict) else {}
    task_id = _acceptance_text(source.get("task_id")) or f"task_{index + 1}"
    goal = _recover_task_scientific_goal(source, task_id)
    if goal == f"Reproduce the primary scientific result assigned to {task_id}.":
        goal = _fallback_scientific_goal(facts)
    seed: dict[str, Any] = {
        "task_id": task_id,
        "target": goal,
        "figure_or_claim": _acceptance_text(source.get("figure_or_claim")) or goal,
    }
    if isinstance(source.get("scientific_acceptance"), dict):
        seed["scientific_acceptance"] = copy.deepcopy(source["scientific_acceptance"])
    return seed


def finalize_repro_tasks(data: Any, facts: Any) -> dict[str, Any]:
    """Normalize every recoverable task and guarantee a minimal scientific hand-off."""

    normalized, coercions = normalize_repro_tasks_candidate(data, facts)
    fact_keys, name_to_types, alias_to_key = _build_fact_index(facts)
    raw_tasks = normalized.get("repro_tasks")
    tasks = raw_tasks if isinstance(raw_tasks, list) else []
    empty_recovery = False
    if not tasks:
        empty_recovery = True
        seed = _minimum_task_seed({}, 0, facts)
        _normalize_task(seed, 0, coercions, fact_keys, name_to_types, alias_to_key)
        tasks = [seed]
        coercions.append("repro_tasks added one minimal task because no scientific task was recoverable")

    kept: list[dict[str, Any]] = []
    repaired: list[dict[str, Any]] = []
    for index, task in enumerate(tasks):
        reason = _task_rejection_reason(task)
        if reason is None:
            kept.append(task)
            continue
        seed = _minimum_task_seed(task, index, facts)
        _normalize_task(seed, index, coercions, fact_keys, name_to_types, alias_to_key)
        second_reason = _task_rejection_reason(seed)
        if second_reason is None:
            kept.append(seed)
            repaired.append(
                {
                    "index": index,
                    "task_id": seed.get("task_id"),
                    "reason": reason,
                }
            )
            continue
        coercions.append(
            f"tasks[{index}] could not retain malformed metadata ({second_reason}); "
            "a generic scientific hand-off will replace it"
        )

    if not kept:
        empty_recovery = True
        seed = _minimum_task_seed({}, 0, facts)
        _normalize_task(seed, 0, coercions, fact_keys, name_to_types, alias_to_key)
        kept = [seed]

    doc: dict[str, Any] = {
        "schema_version": "2.0",
        "backfill_handoff": normalized["backfill_handoff"],
        "execution_relationships": normalized["execution_relationships"],
        "repro_tasks": kept,
    }
    meta = dict(normalized["_meta"]) if isinstance(normalized.get("_meta"), dict) else {}
    if coercions:
        meta["normalization_used"] = True
        meta["coercion_count"] = len(coercions)
        meta["coercions"] = coercions[:50]
    if repaired:
        meta["minimum_handoff_repair_used"] = True
        meta["repaired_task_count"] = len(repaired)
        meta["repaired_tasks"] = repaired[:50]
    if empty_recovery:
        meta["empty_task_recovery_used"] = True
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
