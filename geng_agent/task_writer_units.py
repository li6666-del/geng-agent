"""Execution-unit projection helpers shared by dispatch, prompts, and sandboxes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .paper_evidence import safe_label


def _execution_unit_work_items(
    task_pairs: list[tuple[dict[str, Any], dict[str, Any]]],
    execution_plan: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    by_task_id = {
        str(task.get("task_id") or entry.get("task_id") or f"task_{index}"): (
            index,
            task,
            entry,
        )
        for index, (task, entry) in enumerate(task_pairs, start=1)
    }
    raw_units = execution_plan.get("execution_units") if isinstance(execution_plan, dict) else None
    units: list[dict[str, Any]] = []
    assigned: set[str] = set()
    for unit_index, raw_unit in enumerate(
        raw_units if isinstance(raw_units, list) else [],
        start=1,
    ):
        if not isinstance(raw_unit, dict):
            continue
        members = []
        for raw_task_id in raw_unit.get("task_ids") if isinstance(raw_unit.get("task_ids"), list) else []:
            task_id = str(raw_task_id)
            member = by_task_id.get(task_id)
            if member is not None and task_id not in assigned:
                members.append(member)
                assigned.add(task_id)
        if not members:
            continue
        units.append(
            {
                **json.loads(json.dumps(raw_unit, ensure_ascii=False)),
                "unit_index": unit_index,
                "unit_id": str(raw_unit.get("unit_id") or f"unit_{unit_index:02d}"),
                "members": members,
            }
        )
    for task_id, member in by_task_id.items():
        if task_id in assigned:
            continue
        index, _task, _entry = member
        units.append(
            {
                "unit_index": len(units) + 1,
                "unit_id": f"unit_task_{index:02d}_{safe_label(task_id)}",
                "mode": "singleton",
                "task_ids": [task_id],
                "relationships": [],
                "dependencies": [],
                "artifact_ids": [],
                "members": [member],
            }
        )
    return units

def _execution_unit_sandbox(task_root: Path, unit_id: str) -> Path:
    return task_root / f"unit_{safe_label(unit_id)}"

def _public_execution_unit(unit: dict[str, Any]) -> dict[str, Any]:
    return {
        key: json.loads(json.dumps(unit[key], ensure_ascii=False))
        for key in (
            "unit_id",
            "mode",
            "task_ids",
            "relationships",
            "dependencies",
            "artifact_ids",
        )
        if key in unit
    }
