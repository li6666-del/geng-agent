"""Deterministic coverage check + merge helpers for round-1 fact extraction.

The single-pass extractor tends to skim long papers and silently drop facts -- and a
missed fact at this, the highest-leverage stage, diverges everything downstream (the
Fig.7 threshold case). This module adds two pure, deterministic pieces that let a
targeted LLM gap-finder re-query only the omissions:

1. anchor coverage -- enumerate the figures/tables the paper *references* (from chunk
   text) and compare against the anchors the extracted facts actually *cover*, so the
   gap-finder gets concrete targets instead of "look again".
2. stable merge/dedup -- accumulate gap-pass facts into the base by (type, name) so
   repeated rounds converge and a resume re-merge adds zero.

No I/O, no LLM, no randomness here -- everything is unit-testable in isolation.
"""

from __future__ import annotations

import json
import re
from typing import Any


# A figure reference: "Fig 7", "Fig. 7", "Figure 7", "Figs 3", "Fig 7a" (subfigure letter
# dropped for coverage). Two-digit cap avoids matching years ("Fig. 2020" -> no match).
_FIG_TOKEN = re.compile(r"\bfig(?:ure)?s?\.?\s*(\d{1,2})[a-z]?\b", re.IGNORECASE)
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


def _sorted_anchors(anchors: set[str]) -> list[str]:
    return sorted(anchors, key=lambda a: (0, int(a)) if a.isdigit() else (1, a))


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
        figures |= _scan_anchors(text, _FIG_TOKEN)
        tables |= _scan_anchors(text, _TABLE_TOKEN)
    return {"figures": _sorted_anchors(figures), "tables": _sorted_anchors(tables)}


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
        figures |= _scan_anchors(blob, _FIG_TOKEN)
        tables |= _scan_anchors(blob, _TABLE_TOKEN)
    return {"figures": figures, "tables": tables}


def compute_fact_coverage(
    chunks: list[dict[str, Any]] | None, facts: list[dict[str, Any]] | None
) -> dict[str, Any]:
    """Coverage report: which paper figures/tables are (un)covered by the current facts."""
    anchors = enumerate_paper_anchors(chunks)
    referenced = facts_referenced_anchors(facts)
    fig_set, tab_set = set(anchors["figures"]), set(anchors["tables"])
    uncovered_figures = _sorted_anchors(fig_set - referenced["figures"])
    uncovered_tables = _sorted_anchors(tab_set - referenced["tables"])
    return {
        "paper_figures": anchors["figures"],
        "paper_tables": anchors["tables"],
        "covered_figures": _sorted_anchors(fig_set & referenced["figures"]),
        "covered_tables": _sorted_anchors(tab_set & referenced["tables"]),
        "uncovered_figures": uncovered_figures,
        "uncovered_tables": uncovered_tables,
        "fully_covered": not uncovered_figures and not uncovered_tables,
    }


def _norm_name(value: Any) -> str:
    return re.sub(r"[\s\-_]+", "", str(value).strip().lower())


def _fact_key(fact: dict[str, Any]) -> tuple[str, str]:
    return (str(fact.get("type", "")).strip().lower(), _norm_name(fact.get("name", "")))


def merge_engineering_facts(
    base: dict[str, Any], addition: dict[str, Any]
) -> tuple[dict[str, Any], int]:
    """Append non-duplicate facts + missing_information from ``addition`` into a copy of
    ``base``. Dedup key = (type, normalized name). Returns (merged_doc, added_fact_count).
    Stable + idempotent: merging the same addition twice adds zero the second time."""
    merged = dict(base) if isinstance(base, dict) else {}

    base_facts_raw = merged.get("engineering_facts")
    base_facts = list(base_facts_raw) if isinstance(base_facts_raw, list) else []
    seen = {_fact_key(f) for f in base_facts if isinstance(f, dict)}
    added = 0
    add_facts = addition.get("engineering_facts") if isinstance(addition, dict) else None
    for fact in add_facts if isinstance(add_facts, list) else []:
        if not isinstance(fact, dict):
            continue
        key = _fact_key(fact)
        if key in seen:
            continue
        seen.add(key)
        base_facts.append(fact)
        added += 1
    merged["engineering_facts"] = base_facts

    base_missing_raw = merged.get("missing_information")
    base_missing = list(base_missing_raw) if isinstance(base_missing_raw, list) else []
    miss_seen = {_norm_name(m.get("name", "")) for m in base_missing if isinstance(m, dict)}
    add_missing = addition.get("missing_information") if isinstance(addition, dict) else None
    for item in add_missing if isinstance(add_missing, list) else []:
        if not isinstance(item, dict):
            continue
        key_name = _norm_name(item.get("name", ""))
        if not key_name or key_name in miss_seen:
            continue
        miss_seen.add(key_name)
        base_missing.append(item)
    merged["missing_information"] = base_missing

    return merged, added
