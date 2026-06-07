from __future__ import annotations

import json
import shutil
from contextlib import contextmanager
from pathlib import Path
from typing import Any

"""Leaf utilities for the review pipeline: client-timeout context manager, small JSON/
path IO, untrusted-data wrapping, prompt-context selection, and error classification.
No dependencies on other pipeline modules (keeps the import graph acyclic)."""

from .json_utils import pretty_json
from .llm import LLMClient


@contextmanager
def _temporary_client_timeout(client: LLMClient, timeout: float | None):
    if timeout is None or not hasattr(client, "timeout"):
        yield
        return

    timeout_value = float(timeout)
    if timeout_value <= 0:
        raise ValueError("request timeout must be positive")

    original_timeout = getattr(client, "timeout")
    try:
        setattr(client, "timeout", timeout_value)
        yield
    finally:
        setattr(client, "timeout", original_timeout)


def _read_json_file(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def _remove_path_inside(root: Path, target: Path) -> None:
    if not target.exists():
        return
    root_resolved = root.resolve()
    target_resolved = target.resolve()
    if target_resolved != root_resolved and root_resolved not in target_resolved.parents:
        raise ValueError(f"Refusing to remove path outside {root}: {target}")
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()


def _paper_context_for_prompt(chunks: list[dict[str, Any]], max_chars: int = 60000) -> str:
    scored = sorted(enumerate(chunks), key=lambda item: _chunk_priority(item[1], item[0]), reverse=True)
    selected = []
    selected_ids: set[str] = set()
    total = 0
    for _, chunk in scored:
        text = str(chunk.get("text", ""))
        if not text:
            continue
        if total + len(text) > max_chars and selected:
            continue
        selected.append(chunk)
        selected_ids.add(str(chunk.get("chunk_id")))
        total += len(text)
        if total >= max_chars:
            break

    for chunk in chunks[:3]:
        chunk_id = str(chunk.get("chunk_id"))
        text = str(chunk.get("text", ""))
        if chunk_id in selected_ids or not text:
            continue
        if total + len(text) > max_chars and selected:
            continue
        selected.insert(0, chunk)
        selected_ids.add(chunk_id)
        total += len(text)

    return pretty_json(selected)


def _chunk_priority(chunk: dict[str, Any], index: int) -> tuple[int, int]:
    text = " ".join(str(chunk.get(key, "")) for key in ("section", "text")).lower()
    keywords = {
        "simulation": 8,
        "experiment": 8,
        "result": 8,
        "figure": 7,
        "table": 7,
        "baseline": 7,
        "parameter": 7,
        "snr": 6,
        "ber": 6,
        "ser": 6,
        "bler": 6,
        "throughput": 6,
        "channel": 5,
        "modulation": 5,
        "mimo": 5,
        "ofdm": 5,
        "dataset": 5,
        "seed": 4,
        "metric": 4,
    }
    score = sum(weight for token, weight in keywords.items() if token in text)
    return score, -index


def wrap_untrusted(label: str, text: str) -> str:
    return f"BEGIN UNTRUSTED DATA: {label}\n{text}\nEND UNTRUSTED DATA: {label}"


def summarize_bad_output(text: str, limit: int = 12000) -> str:
    if len(text) <= limit:
        return text
    return text[: limit // 2] + "\n...[truncated invalid output]...\n" + text[-limit // 2 :]


def _is_non_retryable_llm_error(error: str) -> bool:
    lowered = error.lower()
    return any(token in lowered for token in ("http 401", "http 403", "unauthorized", "forbidden", "invalid api key"))
