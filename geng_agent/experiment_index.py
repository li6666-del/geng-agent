from __future__ import annotations

import re
from typing import Any

from .mineru_adapter import task_figure_candidates
from .semantic_merge import canonical_figure_ref


def build_local_experiment_index(
    facts: Any,
    tasks: Any,
    paper: Any,
    figure_index: Any = None,
) -> dict[str, Any]:
    """Build a versioned evidence map without predicting reproduction success."""
    fact_items = _list_from_document(facts, "engineering_facts")
    task_items = _list_from_document(tasks, "repro_tasks")
    chunks = _paper_chunks(paper)
    fact_lookup = _fact_lookup(fact_items)

    experiments: list[dict[str, Any]] = []
    for index, task in enumerate(task_items, start=1):
        task_data = _as_dict(task)
        task_id = _string(task_data.get("task_id")) or f"experiment_{index:02d}"
        figure_or_table = _string(task_data.get("figure_or_claim"))
        metric = _string(task_data.get("metric"))
        required_facts = _required_fact_refs(task_data)
        matched_facts = _matched_facts(required_facts, fact_lookup)
        source_pages, source_chunk_ids = _sources_for_experiment(
            figure_or_table=figure_or_table,
            metric=metric,
            matched_facts=matched_facts,
            chunks=chunks,
        )
        mineru_pages = [
            int(candidate.get("page"))
            for candidate in task_figure_candidates(_as_dict(figure_index), task_data)
            if isinstance(candidate.get("page"), int)
        ]
        for page in mineru_pages:
            if page not in source_pages:
                source_pages.append(page)
        limitations = _limitations_for(
            task_data=task_data,
            figure_or_table=figure_or_table,
            metric=metric,
            required_facts=required_facts,
            matched_facts=matched_facts,
            source_pages=source_pages,
            source_chunk_ids=source_chunk_ids,
        )
        comparison = _as_dict(task_data.get("comparison"))
        experiments.append(
            {
                "experiment_id": _experiment_id(task_id, index),
                "title": _title_for(task_data, figure_or_table, metric),
                "figure_or_table": figure_or_table or None,
                "task_id": task_id,
                "metric": metric or None,
                "source_pages": source_pages,
                "source_chunk_ids": source_chunk_ids,
                "required_facts": required_facts,
                "limitations": limitations,
                "subfigure": _subfigure(figure_or_table),
                "claim": _string(task_data.get("target")) or figure_or_table,
                "methods": _curve_groups(comparison),
                "baselines": _string_items(comparison.get("baselines")),
                "regimes": _regimes(task_data),
                "parameters": _parameters(task_data),
            }
        )

    return {
        "schema_version": "2.0",
        "experiments": experiments,
        "_meta": {
            "local_fallback_used": True,
            "builder": "local_experiment_index_v3_mineru_enriched",
            "experiment_count": len(experiments),
            "mineru_enriched": bool(_as_dict(figure_index).get("figures")),
        },
    }


def _subfigure(text: str) -> str | None:
    ref = canonical_figure_ref(text).split("|", 1)[0]
    parts = ref.split(":")
    return parts[2] if len(parts) == 3 else None


def _string_items(value: Any) -> list[str]:
    return [str(item).strip() for item in value if str(item).strip()] if isinstance(value, list) else []


def _curve_groups(comparison: dict[str, Any]) -> list[str]:
    return _string_items(comparison.get("curve_groups"))


def _regimes(task: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for item in task.get("assumptions", []) if isinstance(task.get("assumptions"), list) else []:
        if not isinstance(item, dict):
            continue
        name = _string(item.get("name"))
        if name:
            values.append(f"{name}={item.get('default_value')}")
    return values


def _parameters(task: dict[str, Any]) -> dict[str, Any]:
    return {
        _string(item.get("name")): item.get("default_value")
        for item in task.get("assumptions", []) if isinstance(task.get("assumptions"), list)
        if isinstance(item, dict) and _string(item.get("name"))
    }


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump()
        return dumped if isinstance(dumped, dict) else {}
    return {}


def _list_from_document(document: Any, key: str) -> list[Any]:
    data = _as_dict(document)
    values = data.get(key)
    return values if isinstance(values, list) else []


def _paper_chunks(paper: Any) -> list[dict[str, Any]]:
    data = _as_dict(paper)
    chunks = data.get("chunks")
    if not isinstance(chunks, list):
        chunks = data.get("paper_chunks")
    if not isinstance(chunks, list):
        return []
    return [_as_dict(chunk) for chunk in chunks if _as_dict(chunk)]


def _fact_lookup(facts: list[Any]) -> dict[tuple[str, str], dict[str, Any]]:
    lookup: dict[tuple[str, str], dict[str, Any]] = {}
    for fact in facts:
        fact_data = _as_dict(fact)
        fact_type = _string(fact_data.get("type"))
        name = _string(fact_data.get("name"))
        if fact_type and name:
            lookup[(fact_type.lower(), name.lower())] = fact_data
    return lookup


def _required_fact_refs(task: dict[str, Any]) -> list[dict[str, str]]:
    refs: list[dict[str, str]] = []
    raw_refs = task.get("required_facts")
    if not isinstance(raw_refs, list):
        return refs
    for raw_ref in raw_refs:
        ref = _as_dict(raw_ref)
        fact_type = _string(ref.get("type"))
        name = _string(ref.get("name"))
        if fact_type and name:
            refs.append({"type": fact_type, "name": name})
    return refs


def _matched_facts(
    required_facts: list[dict[str, str]], fact_lookup: dict[tuple[str, str], dict[str, Any]]
) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for ref in required_facts:
        match = fact_lookup.get((ref["type"].lower(), ref["name"].lower()))
        if match is not None:
            matches.append(match)
    return matches


def _sources_for_experiment(
    *,
    figure_or_table: str,
    metric: str,
    matched_facts: list[dict[str, Any]],
    chunks: list[dict[str, Any]],
) -> tuple[list[int], list[str]]:
    pages: list[int] = []
    chunk_ids: list[str] = []
    for fact in matched_facts:
        source = _as_dict(fact.get("source"))
        _append_source(pages, chunk_ids, source)

    keywords = [figure_or_table, metric, _metric_alias(metric)]
    for chunk in chunks:
        text = _string(chunk.get("text"))
        if text and _contains_any(text, keywords):
            _append_source(pages, chunk_ids, chunk)

    return pages, chunk_ids


def _append_source(pages: list[int], chunk_ids: list[str], source: dict[str, Any]) -> None:
    page = source.get("page")
    if isinstance(page, int) and page not in pages:
        pages.append(page)
    chunk_id = _string(source.get("chunk_id"))
    if chunk_id and chunk_id not in chunk_ids:
        chunk_ids.append(chunk_id)


def _contains_any(text: str, keywords: list[str]) -> bool:
    lowered = text.lower()
    for keyword in keywords:
        if keyword and keyword.lower() in lowered:
            return True
    return False


def _metric_alias(metric: str) -> str:
    aliases = {
        "bit_error_rate": "BER",
        "symbol_error_rate": "SER",
        "throughput": "throughput",
        "delay": "delay",
        "spectral_efficiency": "spectral efficiency",
        "outage_probability": "outage",
        "energy_efficiency": "energy efficiency",
        "accuracy": "accuracy",
        "loss": "loss",
    }
    return aliases.get(metric, metric)


def _limitations_for(
    *,
    task_data: dict[str, Any],
    figure_or_table: str,
    metric: str,
    required_facts: list[dict[str, str]],
    matched_facts: list[dict[str, Any]],
    source_pages: list[int],
    source_chunk_ids: list[str],
) -> list[str]:
    limitations: list[str] = []
    if not figure_or_table:
        limitations.append("Missing figure_or_claim; cannot name the exact paper figure/table or claim.")
    if not metric:
        limitations.append("Missing metric; experiment cannot be tied to a measurable output.")
    if not required_facts:
        limitations.append("Missing required_facts; fact dependencies need human review.")

    matched_keys = {(_string(fact.get("type")).lower(), _string(fact.get("name")).lower()) for fact in matched_facts}
    for ref in required_facts:
        key = (ref["type"].lower(), ref["name"].lower())
        if key not in matched_keys:
            limitations.append(f"Required fact not found in engineering_facts: {ref['type']}:{ref['name']}.")

    if not source_pages:
        limitations.append("No source_pages could be recovered from facts or paper chunks.")
    if not source_chunk_ids:
        limitations.append("No source_chunk_ids could be recovered from facts or paper chunks.")
    if not _string(task_data.get("target")):
        limitations.append("Missing target; title was inferred from figure_or_claim and metric.")
    return limitations


def _experiment_id(task_id: str, index: int) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", task_id).strip("_").lower()
    return f"exp_{slug or index:02d}" if isinstance(slug or index, int) else f"exp_{slug}"


def _title_for(task: dict[str, Any], figure_or_table: str, metric: str) -> str:
    target = _string(task.get("target"))
    if target:
        return target
    if figure_or_table and metric:
        return f"{figure_or_table} / {metric}"
    return figure_or_table or metric or "Untitled experiment"


def _string(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""
