"""One dispatch record for each final task chosen by the planner."""
from pathlib import Path
from .execution_plan import compile_execution_plan
from .paper_evidence import safe_label

def _execution_unit_work_items(task_pairs, execution_plan=None):
    plan = compile_execution_plan({"repro_tasks": [task for task, entry in task_pairs]})
    return [{**unit, "unit_index": index, "members": [(index, task, entry)]}
            for index, (unit, (task, entry)) in enumerate(zip(plan["execution_units"], task_pairs), 1)]

def _execution_unit_sandbox(task_root: Path, unit_id: str) -> Path:
    # Historical directory lookup; new tasks use their own task directory.
    return task_root / f"unit_{safe_label(unit_id)}"

def _public_execution_unit(unit):
    return {key: value for key, value in unit.items() if key not in {"members", "unit_index"}}
