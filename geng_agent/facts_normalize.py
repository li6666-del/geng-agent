"""Lossless compatibility helpers for paper fact documents.

Scientific aliases, source confidence and missing information are never guessed
or deleted here. Invalid transport is repaired by the original analysis owner.
"""
from copy import deepcopy
from typing import Any
from .json_utils import parse_json_object
from .analysis_protocol import analysis_protocol_issues


def normalize_engineering_facts_candidate(data: Any) -> tuple[dict, list[str]]:
    if not isinstance(data, dict):
        raise ValueError("engineering facts must be a JSON object")
    return deepcopy(data), []


def finalize_engineering_facts(data: Any, valid_chunk_ids=None, valid_pages=None) -> dict:
    return normalize_engineering_facts_candidate(data)[0]


def select_valid_engineering_facts(data: dict, valid_chunk_ids=None, valid_pages=None) -> tuple[list, list]:
    facts = data.get("engineering_facts")
    if not isinstance(facts, list):
        raise ValueError("engineering_facts must be an array")
    return deepcopy(facts), []


def engineering_facts_floor_issues(data: dict) -> list:
    return analysis_protocol_issues("engineering_facts", data)


def recover_truncated_engineering_facts(raw: str) -> dict | None:
    # A prefix is not a completed extraction and must never replace it.
    try:
        return parse_json_object(raw)
    except (TypeError, ValueError):
        return None
