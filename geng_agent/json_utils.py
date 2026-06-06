from __future__ import annotations

import json
import re
from typing import Any


FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL | re.IGNORECASE)
ANY_JSON_FENCE_RE = re.compile(r"```json\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)
THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
FILE_ITEM_RE = re.compile(
    r'\{\s*"path"\s*:\s*"(?P<path>[^"]+)"\s*,\s*"content"\s*:\s*"(?P<content>.*?)(?="\s*\}\s*(?:,\s*\{\s*"path"|\]\s*\}))',
    re.DOTALL,
)


def pretty_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def parse_json_object(text: str, *, allow_loose_manifest: bool = False) -> dict[str, Any]:
    candidate = _prepare_json_candidate(text)

    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        if not allow_loose_manifest:
            raise
        parsed = _parse_json_object_fallback(candidate)

    if not isinstance(parsed, dict):
        raise ValueError("LLM response must be a JSON object.")
    return parsed


def prepare_json_candidate(text: str) -> str:
    """Strip <think> blocks and Markdown code fences, returning the JSON-ish core.

    Public so that tolerant recovery paths (e.g. truncated-output salvage) can reuse
    the exact same pre-processing the strict parser applies.
    """
    candidate = THINK_BLOCK_RE.sub("", text).strip()
    fence_match = FENCE_RE.match(candidate)
    if fence_match:
        candidate = fence_match.group(1).strip()

    json_fence_match = ANY_JSON_FENCE_RE.search(candidate)
    if json_fence_match:
        candidate = json_fence_match.group(1).strip()

    return candidate


def _prepare_json_candidate(text: str) -> str:
    return prepare_json_candidate(text)


def _parse_json_object_fallback(candidate: str) -> dict[str, Any]:
    first = candidate.find("{")
    last = candidate.rfind("}")
    if first < 0 or last <= first:
        raise json.JSONDecodeError("No JSON object found", candidate, 0)

    sliced = candidate[first : last + 1]
    try:
        return json.loads(sliced)
    except json.JSONDecodeError as original_error:
        if '"files"' in sliced and '"content"' in sliced:
            manifest = parse_loose_file_manifest(sliced)
            if manifest["files"]:
                manifest["_meta"] = {"loose_recovery_used": True}
                return manifest
        raise original_error


def parse_loose_file_manifest(text: str) -> dict[str, Any]:
    """Recover file manifests only when explicitly allowed by the caller."""
    files = []
    for match in FILE_ITEM_RE.finditer(text):
        files.append(
            {
                "path": _unescape_jsonish_string(match.group("path")),
                "content": _unescape_jsonish_string(match.group("content")),
            }
        )
    return {"files": files}


def _unescape_jsonish_string(value: str) -> str:
    replacements = (
        ("\\r\\n", "\n"),
        ("\\n", "\n"),
        ("\\r", "\n"),
        ("\\t", "\t"),
        ('\\"', '"'),
        ("\\\\", "\\"),
    )
    result = value
    for old, new in replacements:
        result = result.replace(old, new)
    return result
