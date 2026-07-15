from __future__ import annotations

from geng_agent.progress import PHASES, phase_for_step


DISPLAY_STAGES: list[tuple[str, str]] = [
    (phase_id, label) for phase_id, label, _steps in PHASES
]


def stage_label(stage_id: str) -> str:
    for phase_id, label, steps in PHASES:
        if stage_id == phase_id or stage_id in steps:
            return label
    return stage_id


def build_stage_progress(inspect: dict) -> list[dict]:
    """Fold filesystem checkpoints into the five user-facing Web phases."""
    by_name = {
        item["stage"]: item
        for item in inspect.get("stages", [])
        if isinstance(item, dict) and item.get("stage")
    }
    next_stage = inspect.get("next_stage")
    try:
        active_phase = phase_for_step(next_stage) if next_stage else None
    except KeyError:
        active_phase = None

    rows: list[dict] = []
    for index, (phase_id, label, steps) in enumerate(PHASES, start=1):
        statuses = []
        for step, status in by_name.items():
            try:
                if phase_for_step(step) == phase_id:
                    statuses.append(status)
            except KeyError:
                continue
        completed = sum(bool(item.get("ok")) for item in statuses)
        if phase_id == active_phase:
            state = "running"
        elif statuses and completed == len(statuses):
            state = "success"
        elif completed:
            state = "partial"
        else:
            state = "waiting"
        rows.append(
            {
                "id": phase_id,
                "index": index,
                "label": label,
                "state": state,
                "ok": state == "success",
                "steps": list(steps),
                "completed_steps": completed,
            }
        )
    return rows
