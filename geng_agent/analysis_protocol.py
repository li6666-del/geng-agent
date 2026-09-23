"""The small transport contract needed to hand analysis to its next owner.

Scientific wording and completeness belong to the supervisor.  This module
only checks addresses and containers that the host actually dereferences.
It never changes a paper fact, task, assumption or acceptance criterion.
"""
from __future__ import annotations

from pathlib import PurePosixPath, PureWindowsPath
from typing import Any


ANALYSIS_STAGES = frozenset({
    "paper_understanding", "engineering_facts", "repro_tasks", "experiment_plan",
    "targeted_fact_backfill", "paper_thesis", "scientific_architecture", "experiment_index",
})


def analysis_protocol_issues(stage: str, data: Any) -> list:
    from .schemas import ValidationIssue
    from .execution_plan import ExecutionPlanError, compile_execution_plan

    issues: list[ValidationIssue] = []
    if not isinstance(data, dict):
        return [ValidationIssue("$", "the handoff must be a JSON object")]

    def object_at(value: Any, path: str) -> bool:
        if isinstance(value, dict):
            return True
        issues.append(ValidationIssue(path, "must be a JSON object to address the next stage"))
        return False

    def list_at(value: Any, path: str) -> bool:
        if isinstance(value, list):
            return True
        issues.append(ValidationIssue(path, "must be a JSON array to enumerate the handoff"))
        return False

    def facts_at(value: Any, path: str) -> None:
        if object_at(value, path):
            list_at(value.get("engineering_facts"), path + ".engineering_facts")

    def tasks_at(value: Any, path: str) -> None:
        if not object_at(value, path):
            return
        try:
            compile_execution_plan(value)
        except ExecutionPlanError as exc:
            issues.append(ValidationIssue(path + exc.path.removeprefix("$"), str(exc)))
            return
        handoff = value.get("backfill_handoff")
        if handoff is None:
            return  # Absence stays unknown; the host does not invent readiness.
        if not object_at(handoff, path + ".backfill_handoff"):
            return
        ready = handoff.get("ready_for_writer")
        if ready is not None and not isinstance(ready, bool):
            issues.append(ValidationIssue(path + ".backfill_handoff.ready_for_writer", "must be a boolean when used for dispatch"))
        if ready is not False:
            return
        requests: dict[str, list[str]] = {}
        for task in value.get("repro_tasks", []):
            raw_requests = task.get("missing_fact_requests")
            for request in raw_requests if isinstance(raw_requests, list) else []:
                if isinstance(request, dict) and isinstance(request.get("request_id"), str):
                    requests.setdefault(request["request_id"], []).append(task["task_id"])
        selected = handoff.get("blocking_request_ids")
        if not list_at(selected, path + ".backfill_handoff.blocking_request_ids"):
            return
        if not selected:
            issues.append(ValidationIssue(path + ".backfill_handoff.blocking_request_ids", "a backfill dispatch needs at least one explicitly addressed request"))
        for index, request_id in enumerate(selected):
            if not isinstance(request_id, str) or len(requests.get(request_id, [])) != 1:
                issues.append(ValidationIssue(f"{path}.backfill_handoff.blocking_request_ids[{index}]", "must address exactly one declared missing-fact request"))

    def architecture_at(value: Any, path: str, task_ids: set[str] | None = None) -> None:
        if value is None or not object_at(value, path):
            return
        for name in ("components", "quantities", "bindings", "consistency_groups", "invariants"):
            items = value.get(name)
            if items is None:
                continue
            if not list_at(items, path + "." + name):
                continue
            seen: set[str] = set()
            for index, item in enumerate(items):
                at = f"{path}.{name}[{index}]"
                if not object_at(item, at):
                    continue
                identifier = item.get("id")
                if name != "bindings" and identifier is not None:
                    if not isinstance(identifier, str) or not identifier.strip() or identifier in seen:
                        issues.append(ValidationIssue(at + ".id", "must identify one item unambiguously"))
                    else:
                        seen.add(identifier)
                if name == "bindings" and task_ids is not None and (not isinstance(item.get("task_id"), str) or item.get("task_id") not in task_ids):
                    issues.append(ValidationIssue(at + ".task_id", "must address a declared task"))
                module = item.get("module") if name == "components" else None
                if module:
                    text = str(module).replace("\\", "/")
                    if PureWindowsPath(text).is_absolute() or PureWindowsPath(text).drive or PurePosixPath(text).is_absolute() or ".." in PurePosixPath(text).parts:
                        issues.append(ValidationIssue(at + ".module", "must remain inside the delivered project"))

    if stage == "paper_understanding":
        facts_at(data.get("facts"), "$.facts")
    elif stage in {"engineering_facts", "targeted_fact_backfill"}:
        facts_at(data, "$")
    elif stage == "repro_tasks":
        tasks_at(data, "$")
    elif stage == "experiment_plan":
        tasks_at(data.get("tasks"), "$.tasks")
        tasks = data.get("tasks") if isinstance(data.get("tasks"), dict) else {}
        ids = {str(item.get("task_id")) for item in tasks.get("repro_tasks", []) if isinstance(item, dict)} if isinstance(tasks.get("repro_tasks"), list) else set()
        architecture_at(data.get("scientific_architecture"), "$.scientific_architecture", ids)
    elif stage == "scientific_architecture":
        architecture_at(data, "$")
    elif stage == "experiment_index":
        list_at(data.get("experiments"), "$.experiments")
    return issues
