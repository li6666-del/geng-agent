"""Lossless scientific views for analysis prompts; persisted documents stay intact."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

from .json_utils import pretty_json
from .pipeline_helpers import _paper_context_for_prompt, wrap_untrusted


_AUDIT_META_KEYS = {
    "cache", "analysis_backend", "analysis_stage_label", "analysis_attempt",
    "analysis_resume_source", "generated_at", "created_at", "updated_at", "duration_s", "cache_reused",
}


def scientific_prompt_value(value: Any, *, resolution_supplied: bool = False) -> Any:
    """Remove known audit bookkeeping, never arbitrary scientific dict/list fields."""
    if isinstance(value, list):
        return [scientific_prompt_value(item, resolution_supplied=resolution_supplied) for item in value]
    if not isinstance(value, dict):
        return deepcopy(value)
    result = {}
    for key, item in value.items():
        if key == "_meta" and isinstance(item, dict):
            meta = {name: entry for name, entry in item.items() if name not in _AUDIT_META_KEYS}
            # These exact status arrays are supplied in the resolution block.
            # Preserve the diagnostics and any unknown metadata around them.
            handoff = meta.get("fact_gap_handoff")
            if resolution_supplied and isinstance(handoff, dict):
                meta["fact_gap_handoff"] = {
                    name: entry for name, entry in handoff.items()
                    if name not in {"terminal_unresolved", "open"}
                }
            if meta:
                result[key] = scientific_prompt_value(meta, resolution_supplied=resolution_supplied)
        else:
            result[key] = scientific_prompt_value(item, resolution_supplied=resolution_supplied)
    return result


def resolution_for_prompt(resolution: dict[str, Any]) -> dict[str, Any]:
    result = scientific_prompt_value(resolution)
    combined = [*result.get("terminal_unresolved", []), *result.get("open", [])]
    if result.get("unresolved") == combined:
        result.pop("unresolved", None)
    return result


def ledger_for_prompt(ledger: dict[str, Any]) -> dict[str, Any]:
    result = scientific_prompt_value(ledger)
    latest = result.get("latest")
    entries = result.get("entries")
    if isinstance(latest, list) and isinstance(entries, list):
        # Keep every distinct earlier scientific search result, but do not
        # resend the current state once as entries and once as latest.
        previous = []
        for entry in entries:
            if entry not in latest and entry not in previous:
                previous.append(entry)
        result.pop("entries")
        if previous:
            result["previous_search_results"] = previous
    return result


def tasks_for_backfill(tasks: dict[str, Any], requests: list[dict[str, Any]]) -> dict[str, Any]:
    result = scientific_prompt_value(tasks)
    task_ids = {str(task_id) for request in requests for task_id in request.get("task_ids", [])}
    if task_ids:
        result["repro_tasks"] = [task for task in result.get("repro_tasks", []) if str(task.get("task_id")) in task_ids]
        # Relationship context remains relevant even when the other endpoint
        # is outside this request's task subset.
    return result


def analysis_paper_context(
    *, paper: dict[str, Any], figure_summary: dict[str, Any], paper_path: Path,
    chunks_path: Path, images: list[Any], backend: str,
    requests: list[dict[str, Any]] | None = None, max_chars: int = 60000,
) -> str:
    chunks = [item for item in paper.get("chunks", []) if isinstance(item, dict)]
    globally_selected = json.loads(_paper_context_for_prompt(chunks, max_chars=max_chars))
    selected_ids = {str(item.get("chunk_id")) for item in globally_selected}
    selection = "global_evidence_selection"
    matched_ids: list[str] = []
    if requests:
        # Search the complete loaded text again, not the first pass's excerpt.
        targets = [str(target) for request in requests for target in request.get("search_targets", [])]
        terms = [str(request.get("name", "")) for request in requests]
        terms.extend(str(field.get("description", "")) for request in requests for field in request.get("required_fields", []) if isinstance(field, dict))
        words = {word for word in re.findall(r"[a-z][a-z0-9_-]{3,}", " ".join(terms).lower()) if word not in {"paper", "exact", "figure", "required", "method", "parameter"}}
        pages = {int(number) for target in targets for number in re.findall(r"\b(?:page|p\.)\s*(\d+)\b", target.lower())}
        scores = []
        for index, chunk in enumerate(chunks):
            text = " ".join(str(chunk.get(key, "")) for key in ("section", "text")).lower()
            score = 100 * sum(target.lower() in text for target in targets if target.strip())
            score += 100 if chunk.get("page") in pages else 0
            score += sum(word in text for word in words)
            scores.append((score, index, chunk))
        ordered = sorted(scores, key=lambda row: (-row[0], row[1]))
        selected_ids = set()
        total = 0
        for score, _, chunk in ordered:
            text = str(chunk.get("text", ""))
            if not text or (total + len(text) > max_chars and selected_ids):
                continue
            selected_ids.add(str(chunk.get("chunk_id")))
            total += len(text)
            if score:
                matched_ids.append(str(chunk.get("chunk_id")))
            if total >= max_chars:
                break
        selection = "request_search_over_all_loaded_chunks"
    selected = [chunk for chunk in chunks if str(chunk.get("chunk_id")) in selected_ids]
    omitted = [str(chunk.get("chunk_id")) for chunk in chunks if str(chunk.get("chunk_id")) not in selected_ids]
    image_labels = [str(getattr(image, "label", "")) for image in images]
    inventory = {
        "selection": selection,
        "supplied_chunk_ids": [str(chunk.get("chunk_id")) for chunk in selected],
        "omitted_loaded_chunk_ids": omitted,
        "all_loaded_chunks_supplied": not omitted,
        "matched_request_chunk_ids": matched_ids,
        "supplied_image_labels": image_labels,
        "unseen_loaded_page_images": [f"paper_page:{page}" for page in sorted({chunk["page"] for chunk in chunks if isinstance(chunk.get("page"), int)})
                                       if f"paper_page:{page}" not in image_labels],
        "paper_source_path": str(paper_path.resolve()),
        "complete_loaded_chunks_path": str(chunks_path.resolve()),
        "local_sources_readable_by_worker": backend == "codex",
        "coverage_note": (
            "The supplied text may cover only loaded pages, and images may be absent. "
            "Paths are read-only source entry points for Codex; API workers can use only embedded evidence. "
            "An unobserved field is not proof that the paper omits it. Report the actual searched chunks/pages."
        ),
    }
    return wrap_untrusted("paper_chunks_json", pretty_json({
        "paper_source_sha256": paper.get("source_sha256"),
        "evidence_inventory": inventory,
        "paper_chunks": selected,
        "paper_figure_index": figure_summary,
    }))
