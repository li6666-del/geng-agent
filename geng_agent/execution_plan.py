"""Address planner-final tasks without regrouping or judging their science.

Execution-unit keys remain a disk adapter for existing delivery and resume;
each entry always addresses exactly one final task. Historical relationships
remain available in the original planning document as context only.
"""
from typing import Any
from .paper_evidence import safe_label

class ExecutionPlanError(ValueError):
    def __init__(self, message, *, code="unreadable_tasks", path="$"):
        super().__init__(message)
        self.code, self.path, self.message = code, path, message

def compile_execution_plan(repro_tasks_document: Any, relationships=None) -> dict:
    if hasattr(repro_tasks_document, "model_dump"):
        repro_tasks_document = repro_tasks_document.model_dump()
    raw = repro_tasks_document.get("repro_tasks", repro_tasks_document.get("tasks", []))
    units, addresses = [], {}
    for index, task in enumerate(raw, 1):
        task_id = str(task.get("task_id") or f"task_{index:02d}")
        unit_id = f"task_{index:02d}_{safe_label(task_id)}"
        units.append({"unit_id": unit_id, "task_ids": [task_id], "mode": "singleton",
                      "depends_on": task.get("depends_on", []),
                      "relationships": [], "dependencies": [], "artifact_ids": []})
        addresses[task_id] = unit_id
    return {"schema_version": "2.0", "task_policy": "planner_final_tasks",
            "logical_task_count": len(units), "execution_unit_count": len(units),
            "execution_units": units, "task_to_execution_unit": addresses}
