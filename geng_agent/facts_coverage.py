"""Deterministic evidence coverage and fact-merge helpers.

The global extractor establishes a high-recall experiment map. Preliminary tasks then
request only execution-critical missing evidence. This module provides two pure pieces
used to audit that task-driven flow:

1. anchor coverage -- enumerate the figures/tables the paper *references* (from chunk
   text) and compare them with extracted facts and finalized reproduction tasks.
2. semantic merge/dedup -- preserve subfigures, enrich incomplete records, retain
   conflicts, and merge targeted fact-backfill rounds into the global facts.

No I/O, no LLM, no randomness here -- everything is unit-testable in isolation.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .semantic_merge import semantic_merge_engineering_facts


# Keep subfigure identity: Fig. 9(a) and Fig. 9(b) are different experiments.  The
# attached-letter branch has no leading whitespace, which avoids treating the first
# letter of the following prose word as a subfigure.
_FIG_TOKEN = re.compile(
    r"\bfig(?:ure)?s?\.?\s*(\d{1,2})(?!\d)(?:\s*\(([a-z])\)|([a-z])\b)?",
    re.IGNORECASE,
)
# A table reference. The KEYWORD is case-insensitive but the NUMBER is case-sensitive so a
# bare roman branch can't swallow ordinary words ("Table is shown" must NOT become Table I).
_TABLE_TOKEN = re.compile(r"(?i:\btables?\.?\s*)(\d{1,2}|[IVX]{1,5})\b")
# A trailing "and/, N" list right after a Fig/Table token ("Figs. 3 and 4, 5"). Arabic only
# on purpose -- roman lists ("Tables I and II") are rare and not worth the false-positive risk.
_LIST_TAIL = re.compile(r"^(?:\s*(?:,|and|&)\s*\d{1,2}[a-z]?)+", re.IGNORECASE)
_LIST_NUM = re.compile(r"\d{1,2}")


def _norm_anchor(raw: str) -> str:
    token = raw.strip()
    return str(int(token)) if token.isdigit() else token.upper()


def _scan_anchors(text: str, token_re: re.Pattern) -> set[str]:
    found: set[str] = set()
    for match in token_re.finditer(text):
        found.add(_norm_anchor(match.group(1)))
        tail = text[match.end(): match.end() + 30]
        list_match = _LIST_TAIL.match(tail)
        if list_match:
            for num in _LIST_NUM.findall(list_match.group(0)):
                found.add(_norm_anchor(num))
    return found


def _scan_figure_anchors(text: str) -> set[str]:
    found: set[str] = set()
    for match in _FIG_TOKEN.finditer(text):
        number = _norm_anchor(match.group(1))
        subfigure = (match.group(2) or match.group(3) or "").lower()
        found.add(f"{number}:{subfigure}" if subfigure else number)
        tail = text[match.end(): match.end() + 30]
        list_match = _LIST_TAIL.match(tail)
        if list_match:
            found.update(_norm_anchor(num) for num in _LIST_NUM.findall(list_match.group(0)))
    return found


def _prefer_subfigure_anchors(anchors: set[str]) -> set[str]:
    parents_with_children = {anchor.split(":", 1)[0] for anchor in anchors if ":" in anchor}
    return {anchor for anchor in anchors if anchor not in parents_with_children}


def _sorted_anchors(anchors: set[str]) -> list[str]:
    def key(anchor: str) -> tuple[int, int, str]:
        number, _, subfigure = anchor.partition(":")
        if number.isdigit():
            return (0, int(number), subfigure)
        return (1, 0, anchor)

    return sorted(anchors, key=key)


def enumerate_paper_anchors(chunks: list[dict[str, Any]] | None) -> dict[str, list[str]]:
    """Figures/tables the paper text references (deterministic, from chunk text)."""
    figures: set[str] = set()
    tables: set[str] = set()
    for chunk in chunks or []:
        if not isinstance(chunk, dict):
            continue
        text = str(chunk.get("text", ""))
        if not text:
            continue
        figures |= _scan_figure_anchors(text)
        tables |= _scan_anchors(text, _TABLE_TOKEN)
    return {"figures": _sorted_anchors(_prefer_subfigure_anchors(figures)), "tables": _sorted_anchors(tables)}


def _fact_text_blob(fact: dict[str, Any]) -> str:
    if not isinstance(fact, dict):
        return ""
    parts = [str(fact.get("name", ""))]
    source = fact.get("source")
    if isinstance(source, dict):
        parts.append(str(source.get("figure_ref", "")))
        parts.append(str(source.get("quote", "")))
        parts.append(str(source.get("section", "")))
    value = fact.get("value")
    if value is not None:
        try:
            parts.append(json.dumps(value, ensure_ascii=False))
        except (TypeError, ValueError):
            parts.append(str(value))
    return " ".join(parts)


def facts_referenced_anchors(facts: list[dict[str, Any]] | None) -> dict[str, set[str]]:
    """Figures/tables the extracted facts actually mention (so they count as covered).
    Matching requires the 'fig'/'table' keyword, so a bare number in a value never
    falsely covers an anchor."""
    figures: set[str] = set()
    tables: set[str] = set()
    for fact in facts or []:
        blob = _fact_text_blob(fact)
        if not blob:
            continue
        figures |= _scan_figure_anchors(blob)
        tables |= _scan_anchors(blob, _TABLE_TOKEN)
    return {"figures": _prefer_subfigure_anchors(figures), "tables": tables}


def compute_fact_coverage(
    chunks: list[dict[str, Any]] | None, facts: list[dict[str, Any]] | None
) -> dict[str, Any]:
    """Coverage report: which paper figures/tables are (un)covered by the current facts."""
    anchors = enumerate_paper_anchors(chunks)
    referenced = facts_referenced_anchors(facts)
    fig_set, tab_set = set(anchors["figures"]), set(anchors["tables"])
    uncovered_figures = _sorted_anchors(fig_set - referenced["figures"])
    uncovered_tables = _sorted_anchors(tab_set - referenced["tables"])
    detail_coverage = _figure_detail_coverage(fig_set, facts or [])
    return {
        "paper_figures": anchors["figures"],
        "paper_tables": anchors["tables"],
        "covered_figures": _sorted_anchors(fig_set & referenced["figures"]),
        "covered_tables": _sorted_anchors(tab_set & referenced["tables"]),
        "uncovered_figures": uncovered_figures,
        "uncovered_tables": uncovered_tables,
        "fully_covered": not uncovered_figures and not uncovered_tables,
        "figure_detail_coverage": detail_coverage,
        "fully_detailed": all(not item["missing_dimensions"] for item in detail_coverage),
    }


_DETAIL_PATTERNS: dict[str, re.Pattern[str]] = {
    "axes_metrics": re.compile(r"\b(x[- ]?axis|y[- ]?axis|versus|vs\.?|ber|ser|snr|rate|throughput|delay|cdf|accuracy|loss)\b", re.I),
    "methods_baselines": re.compile(r"\b(baseline|benchmark|scheme|method|algorithm|receiver|detector|compared?\s+with)\b", re.I),
    "parameters_regime": re.compile(r"\b(parameter|setting|regime|scenario|channel|antenna|user|snr|db|rho|alpha|beta|lambda)\b", re.I),
    "numeric_values": re.compile(r"(?<![A-Za-z])[-+]?\d+(?:\.\d+)?(?:e[-+]?\d+)?", re.I),
    "trend_claim": re.compile(r"\b(increas|decreas|outperform|higher|lower|better|worse|monotonic|saturat|gain|gap|trend)\w*\b", re.I),
}


def _figure_detail_coverage(figures: set[str], facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for anchor in _sorted_anchors(figures):
        blobs = [
            _fact_text_blob(fact)
            for fact in facts
            if anchor in _scan_figure_anchors(_fact_text_blob(fact))
        ]
        joined = " ".join(blobs)
        present = ["target_identity"] if blobs else []
        present.extend(name for name, pattern in _DETAIL_PATTERNS.items() if pattern.search(joined))
        dimensions = ["target_identity", *_DETAIL_PATTERNS]
        rows.append(
            {
                "figure": anchor,
                "present_dimensions": present,
                "missing_dimensions": [name for name in dimensions if name not in present],
            }
        )
    return rows


def merge_engineering_facts(
    base: dict[str, Any], addition: dict[str, Any]
) -> tuple[dict[str, Any], int]:
    """Semantic, provenance-preserving fact merge; the count is any effective delta."""
    return semantic_merge_engineering_facts(base, addition)


# Every reproducible experiment (a figure_claim fact) needs a finalized task, otherwise
# that figure/result is silently never reproduced.

# A figure counts as a reproducible EXPERIMENT only on positive evidence that it plots a
# measurable result (a metric on its axes). Ambiguous figures default to NOT-experiment so we
# never fabricate a task for a concept/geometry/system diagram ("宁可少也不要错"); the primary
# task LLM still covers the obvious result figures on its own.
_RESULT_KW = re.compile(
    r"\b(ber|ser|bler|fer|cdf|pdf|snr|throughput|capacity|outage|eigenvalue|achievable|"
    r"spectral\s+efficiency|energy\s+efficiency|sum[\s-]?rate|bit\s+error|symbol\s+error|"
    r"error\s+rate|mse|nmse|gain|accuracy|rate)\b"
    r"|heat\s?map|热图|和速率|误码率|误符号率|吞吐|频谱效率|增益",
    re.IGNORECASE,
)
_DIAGRAM_KW = re.compile(
    r"\b(system\s+model|architecture|framework|diagram|illustration|interpretation|"
    r"flowchart|schematic|scenario|overview|topology|block|concept|geometr)\b"
    r"|示意|概念|框图|几何|结构图",
    re.IGNORECASE,
)


def _is_experiment_blob(blob: str) -> bool:
    """Positive-evidence classifier: a figure is a reproducible experiment only if it shows a
    measurable result (a metric keyword) AND is not a clear concept/system diagram. Ambiguous
    (no positive result evidence) -> NOT an experiment, so we never fabricate a task for it.
    The primary task pass (a free LLM judgement) still covers obvious results regardless."""
    if _DIAGRAM_KW.search(blob):
        return False
    return bool(_RESULT_KW.search(blob))


def experiment_anchors_from_facts(facts: list[dict[str, Any]] | None) -> dict[str, set[str]]:
    """Figures/tables that figure_claim facts present as reproducible results."""
    figures: set[str] = set()
    tables: set[str] = set()
    for fact in facts or []:
        if not isinstance(fact, dict) or fact.get("type") != "figure_claim":
            continue
        blob = _fact_text_blob(fact)
        if not _is_experiment_blob(blob):
            continue
        figures |= _scan_figure_anchors(blob)
        tables |= _scan_anchors(blob, _TABLE_TOKEN)
    return {"figures": _prefer_subfigure_anchors(figures), "tables": tables}


def _task_anchors(tasks: list[dict[str, Any]] | None) -> dict[str, set[str]]:
    figures: set[str] = set()
    tables: set[str] = set()
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        blob = " ".join(str(task.get(k, "")) for k in ("figure_or_claim", "target"))
        figures |= _scan_figure_anchors(blob)
        tables |= _scan_anchors(blob, _TABLE_TOKEN)
    return {"figures": _prefer_subfigure_anchors(figures), "tables": tables}


def compute_task_coverage(facts: dict[str, Any] | None, tasks: dict[str, Any] | None) -> dict[str, Any]:
    """Which reproducible experiments (figure_claim facts) are NOT yet covered by a repro
    task. A missing task = that figure/result never gets reproduced downstream."""
    fact_list = facts.get("engineering_facts") if isinstance(facts, dict) else None
    task_list = tasks.get("repro_tasks") if isinstance(tasks, dict) else None
    exp = experiment_anchors_from_facts(fact_list if isinstance(fact_list, list) else [])
    cov = _task_anchors(task_list if isinstance(task_list, list) else [])
    uncovered_figures = _sorted_anchors(exp["figures"] - cov["figures"])
    uncovered_tables = _sorted_anchors(exp["tables"] - cov["tables"])
    specification = _task_specification_coverage(
        task_list if isinstance(task_list, list) else []
    )
    anchor_fully_covered = not uncovered_figures and not uncovered_tables
    no_silent_specification_gaps = all(
        not item["silent_missing_dimensions"] for item in specification
    )
    specification_complete = no_silent_specification_gaps and all(
        not item["explicitly_unresolved_dimensions"] for item in specification
    )
    return {
        "experiment_figures": _sorted_anchors(exp["figures"]),
        "experiment_tables": _sorted_anchors(exp["tables"]),
        "task_figures": _sorted_anchors(cov["figures"]),
        "uncovered_figures": uncovered_figures,
        "uncovered_tables": uncovered_tables,
        "anchor_fully_covered": anchor_fully_covered,
        "task_specification_coverage": specification,
        "no_silent_specification_gaps": no_silent_specification_gaps,
        "specification_complete": specification_complete,
        "fully_covered": anchor_fully_covered and specification_complete,
    }


_TASK_SPEC_DIMENSIONS = (
    "formula_chain",
    "parameter_matrix",
    "baseline_definitions",
    "statistical_protocol",
    "validation_anchors",
)


def _task_specification_coverage(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for task in tasks:
        if not isinstance(task, dict):
            continue
        declared: list[str] = []
        unresolved: list[str] = []
        missing: list[str] = []
        for dimension in _TASK_SPEC_DIMENSIONS:
            items = task.get(dimension)
            if not isinstance(items, list) or not items:
                missing.append(dimension)
                continue
            declared.append(dimension)
            if any(
                isinstance(item, dict) and item.get("status") == "unresolved"
                for item in items
            ):
                unresolved.append(dimension)
        rows.append(
            {
                "task_id": str(task.get("task_id") or ""),
                "declared_dimensions": declared,
                "explicitly_unresolved_dimensions": unresolved,
                "silent_missing_dimensions": missing,
            }
        )
    return rows
