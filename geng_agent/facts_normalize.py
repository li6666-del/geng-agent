"""Local repair of first-round engineering-fact extractions.

The LLM almost always extracts the right facts; what fails strict validation is
usually cosmetic: an enum synonym outside the closed ``Literal`` set, a ``null``
section, an empty quote, a stray extra key, or a truncated JSON stream on a long
paper. Strict all-or-nothing validation turns a 95%-correct extraction into a 0,
which forces the generic keyword fallback and caps reproduction accuracy.

This module adds three conservative, auditable layers that run *before* the strict
schema check, so the LLM's real extraction is kept whenever possible:

1. normalization  - map enum synonyms to allowed values, fix null/empty fields,
   and retain unknown scientific payload under ``value``. Every change is logged.
2. partial acceptance - drop only individual facts that cannot be represented by
   the structural schema; incomplete provenance is retained and marked unverified.
3. truncation salvage - when the JSON stream is cut off, recover the largest
   parseable prefix of the ``engineering_facts`` array.

This module does not judge scientific content or remove facts because they are
approximate visual reads, appendix formulas, bounds, or apparent contradictions.
Those observations stay available with their source and confidence. Structural
rejections, provenance uncertainty, and all coercions remain auditable.
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any, get_args

from pydantic import ValidationError

from .json_utils import prepare_json_candidate
from .schema_models import (
    Confidence,
    EngineeringFact,
    FactType,
    MissingImpact,
    PaperReproType,
)
from .schemas import ValidationIssue


# Accepting a single grounded fact beats the keyword fallback; we only fall back
# when zero usable facts survive. Tunable.
MIN_ENGINEERING_FACTS = 1

_ALLOWED_FACT_TYPES = set(get_args(FactType))
_ALLOWED_REPRO_TYPES = set(get_args(PaperReproType))
_ALLOWED_CONFIDENCE = set(get_args(Confidence))
_ALLOWED_IMPACT = set(get_args(MissingImpact))

_FACT_KEYS = {
    "type",
    "name",
    "value",
    "source",
    "confidence",
    "used_for_reproduction",
    "evidence_kind",
    "derivation",
}
_SOURCE_KEYS = {"source_kind", "chunk_id", "page", "section", "quote", "figure_ref"}
_FIGURE_SOURCE_TOKENS = {"figure", "image", "diagram", "plot", "fig", "chart", "graph", "subfigure"}
_EVIDENCE_KINDS = {"paper_explicit", "paper_derived", "visual_estimate"}

FACT_TYPE_SYNONYMS = {
    "parameter": "simulation_parameter",
    "param": "simulation_parameter",
    "params": "simulation_parameter",
    "simulation": "simulation_parameter",
    "sim_param": "simulation_parameter",
    "simulation_setting": "simulation_parameter",
    "config": "simulation_parameter",
    "setting": "simulation_parameter",
    "channel": "channel_model",
    "fading": "channel_model",
    "fading_model": "channel_model",
    "mod": "modulation",
    "modulation_scheme": "modulation",
    "scheme": "modulation",
    "constellation": "modulation",
    "code": "coding",
    "coding_scheme": "coding",
    "fec": "coding",
    "error_correction": "coding",
    "channel_code": "coding",
    "kpi": "metric",
    "performance_metric": "metric",
    "measure": "metric",
    "reference": "baseline",
    "benchmark": "baseline",
    "baseline_scheme": "baseline",
    "figure": "figure_claim",
    "fig": "figure_claim",
    "plot": "figure_claim",
    "curve": "figure_claim",
    "claim": "figure_claim",
    "result": "figure_claim",
    "algo": "algorithm",
    "method": "algorithm",
    "procedure": "algorithm",
    "data": "dataset",
    "data_set": "dataset",
    "network": "topology",
    "network_topology": "topology",
    "architecture": "topology",
    "hw": "hardware",
    "device": "hardware",
    "platform": "hardware",
}

REPRO_TYPE_SYNONYMS = {
    "phy": "signal_chain",
    "physical_layer": "signal_chain",
    "phy_simulation": "signal_chain",
    "signal": "signal_chain",
    "signal_processing": "signal_chain",
    "link": "signal_chain",
    "link_level": "signal_chain",
    "waveform": "signal_chain",
    "transceiver": "signal_chain",
    "communication_system": "signal_chain",
    "system_model": "signal_chain",
    "amc": "modulation_recognition",
    "modulation": "modulation_recognition",
    "modrec": "modulation_recognition",
    "modulation_classification": "modulation_recognition",
    "coding": "channel_coding",
    "ecc": "channel_coding",
    "ofdm": "mimo_ofdm",
    "mimo": "mimo_ofdm",
    "ofdm_mimo": "mimo_ofdm",
    "massive_mimo": "mimo_ofdm",
    "protocol": "network_protocol",
    "mac": "network_protocol",
    "networking": "network_protocol",
    "routing": "network_protocol",
    "ml": "ml_communication",
    "dl": "ml_communication",
    "deep_learning": "ml_communication",
    "machine_learning": "ml_communication",
    "neural": "ml_communication",
    "ai": "ml_communication",
    "optimization": "optimization_algorithm",
    "optim": "optimization_algorithm",
    "resource_allocation": "optimization_algorithm",
    "measurement": "hardware_dataset",
    "dataset": "hardware_dataset",
    "hardware": "hardware_dataset",
    "testbed": "hardware_dataset",
    "sdr": "hardware_dataset",
}

_CONFIDENCE_SYNONYMS = {"med": "medium", "moderate": "medium", "mid": "medium", "hi": "high", "lo": "low", "uncertain": "low", "unknown": "low"}
_IMPACT_SYNONYMS = {"med": "medium", "moderate": "medium", "mid": "medium", "hi": "high", "critical": "high", "severe": "high", "lo": "low", "minor": "low", "unknown": "low"}


def _norm_token(value: str) -> str:
    return re.sub(r"[\s\-]+", "_", value.strip().lower())


def _map_enum(value: Any, allowed: set[str], synonyms: dict[str, str], default: str) -> tuple[str, bool]:
    if isinstance(value, str):
        if value in allowed:
            return value, False
        token = _norm_token(value)
        if token in allowed:
            return token, token != value
        if token in synonyms:
            return synonyms[token], True
    return default, True


def _coerce_bool(value: Any, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        token = value.strip().lower()
        if token in {"true", "yes", "1", "y"}:
            return True
        if token in {"false", "no", "0", "n"}:
            return False
    return default


def _coerce_page(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        match = re.search(r"\d+", value)
        return int(match.group()) if match else None
    return None


def normalize_engineering_facts_candidate(data: Any) -> tuple[dict[str, Any], list[str]]:
    """Return a coerced copy of the candidate plus a log of every change made."""
    coercions: list[str] = []
    if not isinstance(data, dict):
        return {"paper_domain": "communication", "paper_repro_type": "other", "engineering_facts": [], "missing_information": []}, ["top-level was not a JSON object"]

    out = copy.deepcopy(data)

    domain = out.get("paper_domain")
    if domain != "communication":
        out["paper_domain"] = "communication"
        coercions.append(f"paper_domain {domain!r} -> 'communication'")

    repro_type = out.get("paper_repro_type")
    mapped_type, changed = _map_enum(repro_type, _ALLOWED_REPRO_TYPES, REPRO_TYPE_SYNONYMS, "other")
    out["paper_repro_type"] = mapped_type
    if changed:
        coercions.append(f"paper_repro_type {repro_type!r} -> {mapped_type!r}")

    facts = out.get("engineering_facts")
    if isinstance(facts, list):
        for index, fact in enumerate(facts):
            if isinstance(fact, dict):
                _normalize_fact(fact, index, coercions)
    else:
        out["engineering_facts"] = []
        if facts is not None:
            coercions.append("engineering_facts was not a list -> []")

    out["missing_information"] = _normalize_missing(out.get("missing_information"), coercions)
    return out, coercions


def _normalize_fact(fact: dict[str, Any], index: int, coercions: list[str]) -> None:
    value = fact.get("value")
    if not isinstance(value, dict):
        value = {} if value is None else {"value": value}
        fact["value"] = value
        coercions.append(f"facts[{index}].value -> object")
    extra_payload = {
        key: copy.deepcopy(fact.pop(key))
        for key in list(fact)
        if key not in _FACT_KEYS
    }
    if extra_payload:
        extensions = value.get("_extensions")
        if not isinstance(extensions, dict):
            extensions = {"_previous": copy.deepcopy(extensions)} if extensions is not None else {}
        extensions.update(extra_payload)
        value["_extensions"] = extensions
        coercions.append(f"facts[{index}] retained unknown keys {sorted(extra_payload)}")

    raw_type = fact.get("type")
    mapped_type, changed = _map_enum(raw_type, _ALLOWED_FACT_TYPES, FACT_TYPE_SYNONYMS, "other")
    fact["type"] = mapped_type
    if changed:
        coercions.append(f"facts[{index}].type {raw_type!r} -> {mapped_type!r}")

    raw_conf = fact.get("confidence")
    mapped_conf, changed = _map_enum(raw_conf, _ALLOWED_CONFIDENCE, _CONFIDENCE_SYNONYMS, "low")
    fact["confidence"] = mapped_conf
    if changed:
        coercions.append(f"facts[{index}].confidence {raw_conf!r} -> {mapped_conf!r}")

    raw_used = fact.get("used_for_reproduction")
    used = _coerce_bool(raw_used, default=True)
    if not isinstance(raw_used, bool) or raw_used != used:
        fact["used_for_reproduction"] = used
        coercions.append(f"facts[{index}].used_for_reproduction {raw_used!r} -> {used!r}")

    source = fact.get("source")
    if not isinstance(source, dict):
        if source is not None:
            value["_source_original"] = copy.deepcopy(source)
        source = {}
        fact["source"] = source
        coercions.append(f"facts[{index}].source synthesized as unverified")
    _normalize_source(source, index, coercions, value)

    raw_evidence_kind = fact.get("evidence_kind")
    inferred_kind = (
        "visual_estimate"
        if isinstance(fact.get("source"), dict)
        and fact["source"].get("source_kind") == "figure"
        else "paper_explicit"
    )
    if raw_evidence_kind not in _EVIDENCE_KINDS:
        fact["evidence_kind"] = inferred_kind
        if raw_evidence_kind is not None:
            coercions.append(
                f"facts[{index}].evidence_kind {raw_evidence_kind!r} -> {inferred_kind!r}"
            )

    derivation = fact.get("derivation")
    if derivation is not None and not isinstance(derivation, str):
        fact["derivation"] = str(derivation)
        coercions.append(f"facts[{index}].derivation coerced to string")


def _normalize_source(
    source: dict[str, Any],
    index: int,
    coercions: list[str],
    fact_value: dict[str, Any],
) -> None:
    extra_payload = {
        key: copy.deepcopy(source.pop(key))
        for key in list(source)
        if key not in _SOURCE_KEYS
    }
    if extra_payload:
        extensions = fact_value.get("_source_extensions")
        if not isinstance(extensions, dict):
            extensions = (
                {"_previous": copy.deepcopy(extensions)} if extensions is not None else {}
            )
        extensions.update(extra_payload)
        fact_value["_source_extensions"] = extensions
        coercions.append(f"facts[{index}].source retained unknown keys {sorted(extra_payload)}")

    # source_kind: map figure synonyms to "figure", everything else to "text". New fields are
    # filled silently (the model may omit them); only an actually-changed value is logged.
    raw_kind = source.get("source_kind")
    if isinstance(raw_kind, str):
        kind = "figure" if _norm_token(raw_kind) in _FIGURE_SOURCE_TOKENS else "text"
        if kind != raw_kind:
            coercions.append(f"facts[{index}].source.source_kind {raw_kind!r} -> {kind!r}")
        source["source_kind"] = kind
    else:
        source["source_kind"] = (
            "figure"
            if str(source.get("figure_ref") or "").strip()
            else "text"
        )

    # chunk_id is optional for figure sources; ensure the key exists (may be null).
    if "chunk_id" not in source:
        source["chunk_id"] = None
    elif source.get("chunk_id") is not None and not isinstance(
        source.get("chunk_id"), str
    ):
        raw_chunk_id = source.get("chunk_id")
        source["chunk_id"] = str(raw_chunk_id)
        coercions.append(
            f"facts[{index}].source.chunk_id {raw_chunk_id!r} -> {source['chunk_id']!r}"
        )

    if not isinstance(source.get("figure_ref"), str):
        source["figure_ref"] = "" if source.get("figure_ref") is None else str(source.get("figure_ref"))

    section = source.get("section")
    if section is None or "section" not in source:
        source["section"] = ""
        coercions.append(f"facts[{index}].source.section -> ''")
    elif not isinstance(section, str):
        source["section"] = str(section)
        coercions.append(f"facts[{index}].source.section coerced to string")

    if "page" not in source:
        source["page"] = None
        coercions.append(f"facts[{index}].source.page -> null")
    else:
        page = source.get("page")
        coerced = _coerce_page(page)
        if coerced != page and not (page is None and coerced is None):
            source["page"] = coerced
            coercions.append(f"facts[{index}].source.page {page!r} -> {coerced!r}")

    quote = source.get("quote")
    if not isinstance(quote, str) or not quote.strip():
        source["quote"] = "Source details unavailable; retained as unverified evidence."
        coercions.append(f"facts[{index}].source.quote -> unverified placeholder")


def _normalize_missing(missing: Any, coercions: list[str]) -> list[dict[str, Any]]:
    if not isinstance(missing, list):
        return []
    cleaned: list[dict[str, Any]] = []
    for item in missing:
        if not isinstance(item, dict):
            coercions.append("dropped non-object missing_information entry")
            continue
        name = item.get("name")
        why = item.get("why_needed")
        if not (isinstance(name, str) and name.strip()) or not (isinstance(why, str) and why.strip()):
            coercions.append("dropped missing_information entry lacking name/why_needed")
            continue
        impact, _ = _map_enum(item.get("impact"), _ALLOWED_IMPACT, _IMPACT_SYNONYMS, "medium")
        cleaned.append({"name": name, "why_needed": why, "impact": impact})
    return cleaned


def _fact_rejection_reason(
    fact: Any, valid_chunk_ids: set[str] | None, valid_pages: set[int] | None = None
) -> str | None:
    if not isinstance(fact, dict):
        return "not a JSON object"
    del valid_chunk_ids, valid_pages
    try:
        EngineeringFact.model_validate(fact)
    except ValidationError as exc:
        error = exc.errors()[0]
        loc = ".".join(str(part) for part in error.get("loc", ()))
        return f"{loc or '$'}: {error.get('msg', 'invalid value')}"
    return None


def _source_verification_issues(
    fact: dict[str, Any],
    valid_chunk_ids: set[str] | None,
    valid_pages: set[int] | None,
) -> list[str]:
    source = fact.get("source") if isinstance(fact.get("source"), dict) else {}
    reasons: list[str] = []
    if source.get("source_kind") == "figure":
        page = source.get("page")
        if not isinstance(page, int):
            reasons.append("figure source has no paper page")
        elif valid_pages is not None and page not in valid_pages:
            reasons.append("figure source page was not rendered for inspection")
    else:
        chunk_id = source.get("chunk_id")
        if not isinstance(chunk_id, str) or not chunk_id.strip():
            reasons.append("text source has no chunk id")
        elif valid_chunk_ids is not None and chunk_id not in valid_chunk_ids:
            reasons.append("text source chunk id is absent from paper_chunks.json")
    quote = source.get("quote")
    if not isinstance(quote, str) or quote.startswith("Source details unavailable;"):
        reasons.append("source quote is incomplete")
    return list(dict.fromkeys(reasons))


def _mark_unverified(fact: dict[str, Any], reasons: list[str]) -> None:
    value = fact.get("value")
    if not isinstance(value, dict):
        value = {"value": copy.deepcopy(value)}
        fact["value"] = value
    prior = value.get("_provenance")
    prior_reasons = (
        prior.get("reasons", [])
        if isinstance(prior, dict) and isinstance(prior.get("reasons"), list)
        else []
    )
    value["_provenance"] = {
        "verified": False,
        "reasons": list(dict.fromkeys([*map(str, prior_reasons), *reasons])),
    }


def select_valid_engineering_facts(
    data: dict[str, Any], valid_chunk_ids: set[str] | None, valid_pages: set[int] | None = None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    facts = data.get("engineering_facts")
    if not isinstance(facts, list):
        return kept, dropped
    for index, fact in enumerate(facts):
        reason = _fact_rejection_reason(fact, valid_chunk_ids, valid_pages)
        if reason is None:
            provenance_issues = _source_verification_issues(
                fact, valid_chunk_ids, valid_pages
            )
            if provenance_issues:
                _mark_unverified(fact, provenance_issues)
            kept.append(fact)
        else:
            dropped.append(
                {
                    "index": index,
                    "name": fact.get("name") if isinstance(fact, dict) else None,
                    "reason": reason,
                }
            )
    return kept, dropped


def finalize_engineering_facts(
    data: Any, valid_chunk_ids: set[str] | None, valid_pages: set[int] | None = None
) -> dict[str, Any]:
    """Normalize facts, retaining structurally valid facts with provenance flags."""
    normalized, coercions = normalize_engineering_facts_candidate(data)
    kept, dropped = select_valid_engineering_facts(normalized, valid_chunk_ids, valid_pages)

    doc: dict[str, Any] = {
        "paper_domain": "communication",
        "paper_repro_type": normalized.get("paper_repro_type", "other"),
        "engineering_facts": kept,
        "missing_information": normalized.get("missing_information", []),
    }

    meta = dict(normalized["_meta"]) if isinstance(normalized.get("_meta"), dict) else {}
    if coercions:
        meta["normalization_used"] = True
        meta["coercion_count"] = len(coercions)
        meta["coercions"] = coercions[:50]
    if dropped:
        meta["partial_acceptance_used"] = True
        meta["dropped_fact_count"] = len(dropped)
        meta["kept_fact_count"] = len(kept)
        meta["dropped_facts"] = dropped[:50]
    unverified = [
        fact
        for fact in kept
        if isinstance(fact.get("value"), dict)
        and isinstance(fact["value"].get("_provenance"), dict)
        and fact["value"]["_provenance"].get("verified") is False
    ]
    if unverified:
        meta["unverified_fact_count"] = len(unverified)
    if meta:
        doc["_meta"] = meta
    return doc


def engineering_facts_floor_issues(doc: dict[str, Any], min_facts: int = MIN_ENGINEERING_FACTS) -> list[ValidationIssue]:
    facts = doc.get("engineering_facts")
    count = len(facts) if isinstance(facts, list) else 0
    if count < min_facts:
        return [
            ValidationIssue(
                "$.engineering_facts",
                f"only {count} valid engineering fact(s) survived normalization; need at least {min_facts}",
            )
        ]
    return []


def _salvage_array_objects(text: str, bracket_index: int) -> list[Any]:
    objects: list[Any] = []
    depth = 0
    in_string = False
    escaped = False
    object_start: int | None = None
    index = bracket_index + 1
    length = len(text)
    while index < length:
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        else:
            if char == '"':
                in_string = True
            elif char == "{":
                if depth == 0:
                    object_start = index
                depth += 1
            elif char == "}":
                if depth > 0:
                    depth -= 1
                    if depth == 0 and object_start is not None:
                        fragment = text[object_start : index + 1]
                        try:
                            objects.append(json.loads(fragment))
                        except json.JSONDecodeError:
                            pass
                        object_start = None
            elif char == "]" and depth == 0:
                break
        index += 1
    return objects


def recover_truncated_engineering_facts(raw: str) -> dict[str, Any] | None:
    """Best-effort recovery of a truncated facts payload by salvaging complete
    objects from the ``engineering_facts`` array. Returns ``None`` if nothing usable
    can be recovered."""
    if not isinstance(raw, str):
        return None
    text = prepare_json_candidate(raw)
    key_index = text.find('"engineering_facts"')
    if key_index < 0:
        return None
    bracket_index = text.find("[", key_index)
    if bracket_index < 0:
        return None
    objects = _salvage_array_objects(text, bracket_index)
    if not objects:
        return None

    doc: dict[str, Any] = {"engineering_facts": objects, "missing_information": []}
    domain_match = re.search(r'"paper_domain"\s*:\s*"([^"]+)"', text)
    if domain_match:
        doc["paper_domain"] = domain_match.group(1)
    type_match = re.search(r'"paper_repro_type"\s*:\s*"([^"]+)"', text)
    if type_match:
        doc["paper_repro_type"] = type_match.group(1)
    doc["_meta"] = {"truncation_recovered": True, "recovered_fact_count": len(objects)}
    return doc
