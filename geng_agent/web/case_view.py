"""Read stored task and report records for the UI without making scientific decisions."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .artifacts import LocalArtifactStore, UnsafeArtifactPath


def _read(store: LocalArtifactStore, name: str, warnings: list[str]) -> dict[str, Any]:
    try:
        path = store.resolve(name)
        if path.stat().st_size > 8 * 1024 * 1024:
            raise ValueError("record exceeds preview limit")
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("expected an object")
        return value
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, UnsafeArtifactPath):
        warnings.append(f"暂时无法读取 {name}，可在产物区核查原文件。")
        return {}


def case_research_view(case_dir: Path) -> dict[str, Any]:
    store = LocalArtifactStore(case_dir)
    warnings: list[str] = []
    plan = _read(store, "repro_tasks.json", warnings)
    verification = _read(store, "verification_result.json", warnings)
    risk = _read(store, "risk_report.json", warnings)
    editor = _read(store, "audit/04b_report_editor_status.json", warnings)

    def rows(document: dict, key: str) -> list[dict]:
        value = document.get(key)
        return [item for item in value if isinstance(item, dict) and isinstance(item.get("task_id"), str)] if isinstance(value, list) else []

    planned = {item["task_id"]: item for item in rows(plan, "repro_tasks")}
    verified = {item["task_id"]: item for item in rows(verification, "tasks")}
    tasks = []
    for task_id in dict.fromkeys([*planned, *verified]):
        task = planned.get(task_id, {})
        review = verified.get(task_id, {})
        tasks.append({
            "task_id": task_id,
            "title": review.get("report_title") or task.get("title") or task.get("target") or task_id,
            "target": task.get("figure_or_claim") or task.get("target"),
            "outcome": review.get("outcome"),
            "engineering_status": review.get("engineering_status"),
            "decision_reason": review.get("decision_reason"),
            "remaining_uncertainties": review.get("remaining_uncertainties") or [],
            "host_action": review.get("host_action"),
        })
    # Expose recorded statuses separately from file availability and job completion.
    # In particular, job success is never used to infer a scientific outcome.
    editor_summary = risk.get("report_editor")
    if not editor and isinstance(editor_summary, dict):
        editor = editor_summary
    return {
        "tasks": tasks,
        "verification_available": bool(verification),
        "all_terminal": verification.get("all_terminal"),
        "all_successful": verification.get("all_successful"),
        "editor_ok": editor.get("ok"),
        "editor_mode": editor.get("mode"),
        "warnings": warnings,
    }
