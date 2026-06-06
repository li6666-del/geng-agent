from __future__ import annotations

from enum import Enum


class RepairBackend(str, Enum):
    LLM = "llm"
    HYBRID = "hybrid"
    OPENHANDS = "openhands"


def normalize_repair_backend(value: RepairBackend | str) -> RepairBackend:
    if isinstance(value, RepairBackend):
        return value
    try:
        return RepairBackend(str(value).lower())
    except ValueError as exc:
        choices = ", ".join(item.value for item in RepairBackend)
        raise ValueError(f"unknown repair backend: {value!r}; expected one of: {choices}") from exc
