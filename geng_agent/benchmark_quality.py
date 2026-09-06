"""Compare blind review labels against independent, versioned quality baselines."""
from __future__ import annotations
import json
from pathlib import Path
from typing import Any

SUCCESS = frozenset({"reproduced", "reproduced_with_assumptions", "matched"})
SCIENTIFIC_OUTCOMES = ("reproduced", "reproduced_with_assumptions", "inconclusive_missing_information",
                       "not_reproduced", "execution_failed")


def _read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def assess_quality_baseline(case_dir: Path) -> dict[str, Any]:
    baseline = _read(case_dir / "quality_baseline.json")
    actual = _read(case_dir / "quality_results.json")
    rows = actual.get("tasks", [])
    observed = {str(item.get("task_id")): item for item in rows if isinstance(item, dict)}
    for path in (case_dir / "audit" / "04a_task_reporters").glob("*/status.json"):
        status = _read(path)
        note = status.get("task_verification")
        if isinstance(note, dict):
            observed[str(note.get("task_id"))] = note
    comparisons = []
    for expected in baseline.get("tasks", []):
        if not isinstance(expected, dict):
            continue
        task_id = str(expected.get("task_id") or "")
        note = observed.get(task_id, {})
        outcome = note.get("outcome") or note.get("scientific_outcome")
        target = expected.get("expected_outcome")
        comparisons.append({"task_id": task_id, "paper_family": expected.get("paper_family", baseline.get("paper_family")),
                            "pair_id": expected.get("pair_id"), "failure_mode": expected.get("failure_mode"),
                            "expected_outcome": target, "actual_outcome": outcome,
                            "assessed": bool(outcome), "correct": outcome == target if outcome else None,
                            "false_success": bool(outcome in SUCCESS and target not in SUCCESS),
                            "false_failure": bool(outcome and outcome not in SUCCESS and target in SUCCESS),
                            "rerun_correct": (note.get("host_action") == "rerun_writer") == expected["expected_rerun_allowed"]
                            if outcome and "expected_rerun_allowed" in expected else None})
    return {"available": bool(baseline), "baseline_version": baseline.get("schema_version"),
            "tasks": comparisons, **quality_counts(comparisons)}


def scientific_outcome_counts(case_dir: Path, tasks: dict[str, Any] | None) -> dict[str, int]:
    """Keep scientific conclusions distinct from process success and legacy labels."""
    planned = {str(item["task_id"]) for item in (tasks or {}).get("repro_tasks", [])
               if isinstance(item, dict) and item.get("task_id")}
    observed = {}
    final = _read(case_dir / "verification_result.json")
    rows = final.get("tasks", [])
    if not rows:
        rows = [_read(path).get("task_verification", {})
                for path in (case_dir / "audit" / "04a_task_reporters").glob("*/status.json")]
    for row in rows:
        if isinstance(row, dict) and row.get("task_id"):
            observed[str(row["task_id"])] = row.get("outcome") or row.get("scientific_outcome") or row.get("terminal_outcome")
    scope = planned or set(observed)
    counts = {name: sum(observed.get(task_id) == name for task_id in scope) for name in SCIENTIFIC_OUTCOMES}
    counts["unassessed"] = sum(observed.get(task_id) not in SCIENTIFIC_OUTCOMES for task_id in scope)
    return counts


def quality_counts(rows: list[dict[str, Any]]) -> dict[str, Any]:
    assessed = sum(bool(row.get("assessed")) for row in rows)
    return {"labeled": len(rows), "assessed": assessed, "unassessed": len(rows) - assessed,
            "correct": sum(row.get("correct") is True for row in rows),
            "false_success": sum(bool(row.get("false_success")) for row in rows),
            "false_failure": sum(bool(row.get("false_failure")) for row in rows),
            "rerun_errors": sum(row.get("rerun_correct") is False for row in rows)}
