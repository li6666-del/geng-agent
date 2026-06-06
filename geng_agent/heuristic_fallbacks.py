from __future__ import annotations

import re
from typing import Any


def build_fallback_engineering_facts(*, paper: dict[str, Any], reason: str) -> dict[str, Any]:
    chunks = _chunks(paper)
    facts: list[dict[str, Any]] = []

    for name in ("AWGN", "Rayleigh", "Rician", "Nakagami"):
        source = _source_for(chunks, [name])
        if source is not None:
            facts.append(
                {
                    "type": "channel_model",
                    "name": name,
                    "value": {"detected_by": "keyword"},
                    "source": source,
                    "confidence": "low",
                    "used_for_reproduction": True,
                }
            )

    for name in ("BPSK", "QPSK", "16-QAM", "64-QAM", "QAM", "OFDM", "MIMO"):
        source = _source_for(chunks, [name])
        if source is not None:
            facts.append(
                {
                    "type": "modulation" if name not in {"OFDM", "MIMO"} else "algorithm",
                    "name": name,
                    "value": {"detected_by": "keyword"},
                    "source": source,
                    "confidence": "low",
                    "used_for_reproduction": name in {"BPSK", "QPSK", "16-QAM", "64-QAM", "QAM", "OFDM"},
                }
            )

    metric_patterns = [
        ("BER", "bit_error_rate"),
        ("SER", "symbol_error_rate"),
        ("throughput", "throughput"),
        ("accuracy", "accuracy"),
        ("outage", "outage_probability"),
    ]
    for keyword, metric_name in metric_patterns:
        source = _source_for(chunks, [keyword, metric_name])
        if source is not None:
            facts.append(
                {
                    "type": "metric",
                    "name": metric_name,
                    "value": {"keyword": keyword},
                    "source": source,
                    "confidence": "low",
                    "used_for_reproduction": metric_name in {"bit_error_rate", "symbol_error_rate", "throughput", "accuracy"},
                }
            )

    snr_values = _extract_snr_values(chunks)
    snr_source = _source_for(chunks, ["SNR", "Eb/N0", "Eb/No", "dB"])
    if snr_source is not None:
        facts.append(
            {
                "type": "simulation_parameter",
                "name": "SNR",
                "value": {"snr_db_observed": snr_values[:16], "unit": "dB"},
                "source": snr_source,
                "confidence": "low",
                "used_for_reproduction": True,
            }
        )

    figure_source = _source_for(chunks, ["figure", "fig.", "simulation", "results", "BER"])
    if figure_source is not None:
        facts.append(
            {
                "type": "figure_claim",
                "name": "primary_result_curve",
                "value": {"detected_by": "result_or_figure_keyword"},
                "source": figure_source,
                "confidence": "low",
                "used_for_reproduction": True,
            }
        )

    return {
        "paper_domain": "communication",
        "paper_repro_type": _guess_repro_type(chunks),
        "engineering_facts": _dedupe_facts(facts),
        "missing_information": [
            {
                "name": "LLM engineering fact extraction failed",
                "why_needed": "The local fallback uses keyword detection, so all extracted engineering facts need human review.",
                "impact": "high",
            },
            {
                "name": "Exact simulation implementation details",
                "why_needed": "Stable reproduction needs channel equations, random seeds, iteration counts, and baseline settings.",
                "impact": "high",
            },
            {
                "name": "Original numeric curve points",
                "why_needed": "The fallback can check qualitative trends but cannot claim point-level reproduction.",
                "impact": "medium",
            },
        ],
        "_meta": {
            "local_fallback_used": True,
            "fallback_stage": "engineering_facts",
            "fallback_name": "keyword_engineering_facts_v1",
            "fallback_reason": reason,
        },
    }


def build_fallback_repro_tasks(*, facts: dict[str, Any], paper: dict[str, Any], reason: str) -> dict[str, Any]:
    fact_refs = _required_fact_refs(facts)
    metric = _choose_metric(facts, paper)
    if metric == "throughput":
        formula = "throughput = successfully_delivered_bits / elapsed_time"
        trend = {"x_axis": "snr_db", "y_axis": "throughput", "direction": "increasing", "reason": "Higher SNR usually improves successful transmission rate."}
        columns = ["snr_db", "throughput"]
        target = "Throughput vs SNR"
    elif metric == "accuracy":
        formula = "accuracy = correct_predictions / total_predictions"
        trend = {"x_axis": "snr_db", "y_axis": "accuracy", "direction": "increasing", "reason": "Cleaner channels usually improve classification accuracy."}
        columns = ["snr_db", "accuracy"]
        target = "Accuracy vs SNR"
    elif metric == "symbol_error_rate":
        formula = "symbol_error_rate = symbol_errors / total_symbols"
        trend = {"x_axis": "snr_db", "y_axis": "symbol_error_rate", "direction": "decreasing", "reason": "Higher SNR usually lowers symbol errors."}
        columns = ["snr_db", "symbol_error_rate"]
        target = "SER vs SNR"
    else:
        metric = "bit_error_rate"
        formula = "bit_error_rate = bit_errors / total_bits"
        trend = {"x_axis": "snr_db", "y_axis": "bit_error_rate", "direction": "decreasing", "reason": "Higher SNR usually lowers bit errors in a signal chain."}
        columns = ["snr_db", "bit_error_rate"]
        target = "BER vs SNR"

    return {
        "repro_tasks": [
            {
                "task_id": "fallback_reproduce_primary_curve",
                "target": target,
                "metric": metric,
                "metric_formula": formula,
                "figure_or_claim": "primary result curve detected by local fallback",
                "expected_artifacts": ["outputs/results.csv", "outputs/ber_comparison.png", "outputs/summary.json"],
                "output_columns": columns,
                "expected_trend": trend,
                "comparison": {
                    "baselines": ["local deterministic fallback baseline"],
                    "curve_groups": ["fallback_simulated"],
                    "tolerance": "qualitative trend only; point-level agreement requires manual extraction of paper curve values",
                },
                "required_facts": fact_refs,
                "assumptions": [
                    {
                        "name": "local_task_fallback",
                        "default_value": True,
                        "reason": "The LLM did not produce a valid reproduction task, so a minimal task was inferred from detected communication keywords.",
                        "risk": "high",
                    }
                ],
                "risk_if_unreproducible": "The fallback task is only a smoke-level reproducibility probe and cannot replace a paper-specific reproduction plan.",
            }
        ],
        "_meta": {
            "local_fallback_used": True,
            "fallback_stage": "repro_tasks",
            "fallback_name": "primary_curve_task_v1",
            "fallback_reason": reason,
        },
    }


def _chunks(paper: dict[str, Any]) -> list[dict[str, Any]]:
    chunks = paper.get("chunks")
    return [chunk for chunk in chunks if isinstance(chunk, dict)] if isinstance(chunks, list) else []


def _source_for(chunks: list[dict[str, Any]], keywords: list[str]) -> dict[str, Any] | None:
    lowered_keywords = [keyword.lower() for keyword in keywords]
    for chunk in chunks:
        text = str(chunk.get("text") or "")
        lowered_text = text.lower()
        if any(keyword in lowered_text for keyword in lowered_keywords):
            quote = _quote_for(text, keywords)
            return {
                "chunk_id": str(chunk.get("chunk_id") or "unknown"),
                "page": chunk.get("page") if isinstance(chunk.get("page"), int) else None,
                "section": str(chunk.get("section") or "unknown"),
                "quote": quote,
            }
    if chunks:
        chunk = chunks[0]
        return {
            "chunk_id": str(chunk.get("chunk_id") or "unknown"),
            "page": chunk.get("page") if isinstance(chunk.get("page"), int) else None,
            "section": str(chunk.get("section") or "unknown"),
            "quote": _quote_for(str(chunk.get("text") or ""), []),
        }
    return None


def _quote_for(text: str, keywords: list[str]) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for keyword in keywords:
        pattern = re.compile(re.escape(keyword), re.IGNORECASE)
        for line in lines:
            if pattern.search(line):
                return _trim(line)
    return _trim(lines[0] if lines else "No extractable quote; local fallback used an empty paper chunk.")


def _trim(value: str, limit: int = 240) -> str:
    value = re.sub(r"\s+", " ", value).strip()
    return value[:limit] if len(value) <= limit else value[: limit - 3] + "..."


def _extract_snr_values(chunks: list[dict[str, Any]]) -> list[float]:
    text = "\n".join(str(chunk.get("text") or "") for chunk in chunks)
    values: list[float] = []
    for match in re.finditer(r"(?i)(?:snr|eb/n0|eb/no|ebno)?[^.\n]{0,30}?(-?\d+(?:\.\d+)?)\s*dB", text):
        value = float(match.group(1))
        if -50 <= value <= 80 and value not in values:
            values.append(value)
    return values


def _guess_repro_type(chunks: list[dict[str, Any]]) -> str:
    text = "\n".join(str(chunk.get("text") or "") for chunk in chunks).lower()
    if "mimo" in text or "ofdm" in text:
        return "mimo_ofdm"
    if "ldpc" in text or "turbo code" in text or "polar code" in text or "channel coding" in text:
        return "channel_coding"
    if "modulation recognition" in text or "classification" in text:
        return "modulation_recognition"
    if "dataset" in text or "hardware" in text or "fpga" in text:
        return "hardware_dataset"
    if "protocol" in text or "routing" in text or "mac layer" in text:
        return "network_protocol"
    if "optimization" in text or "resource allocation" in text:
        return "optimization_algorithm"
    if "neural network" in text or "deep learning" in text:
        return "ml_communication"
    return "signal_chain"


def _choose_metric(facts: dict[str, Any], paper: dict[str, Any]) -> str:
    blob = (str(facts) + "\n" + str(paper)).lower()
    if "bit_error_rate" in blob or "ber" in blob:
        return "bit_error_rate"
    if "symbol_error_rate" in blob or "ser" in blob:
        return "symbol_error_rate"
    if "throughput" in blob:
        return "throughput"
    if "accuracy" in blob:
        return "accuracy"
    return "bit_error_rate"


def _required_fact_refs(facts: dict[str, Any]) -> list[dict[str, str]]:
    refs: list[dict[str, str]] = []
    for fact in facts.get("engineering_facts", []) if isinstance(facts.get("engineering_facts"), list) else []:
        if not isinstance(fact, dict) or not fact.get("used_for_reproduction"):
            continue
        fact_type = fact.get("type")
        name = fact.get("name")
        if isinstance(fact_type, str) and isinstance(name, str):
            refs.append({"type": fact_type, "name": name})
        if len(refs) >= 4:
            break
    return refs


def _dedupe_facts(facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    deduped: list[dict[str, Any]] = []
    for fact in facts:
        key = (str(fact.get("type")), str(fact.get("name")))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(fact)
    return deduped[:16]
