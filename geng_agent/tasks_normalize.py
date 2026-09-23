"""Lossless task handoff helpers; the Planner owns the task specification."""
from copy import deepcopy
from typing import Any
from .json_utils import parse_json_object


def normalize_repro_tasks_candidate(data: Any, facts: Any) -> tuple[dict, list[str]]:
    if not isinstance(data, dict):
        raise ValueError("reproduction tasks must be a JSON object")
    return deepcopy(data), []


def finalize_repro_tasks(data: Any, facts: Any) -> dict:
    """Retain all owner fields, including unknown prose and unresolved references."""
    return normalize_repro_tasks_candidate(data, facts)[0]


def recover_truncated_repro_tasks(raw: str) -> dict | None:
    # A prefix could silently omit an entire task or its acceptance conditions.
    try:
        return parse_json_object(raw)
    except (TypeError, ValueError):
        return None
