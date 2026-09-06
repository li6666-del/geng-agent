"""Derive the immutable scientific code shared by different execution units.

Source reuse and runtime-state reuse are distinct: this scope owns code only.
Checkpoints, datasets, and random realizations belong to execution-plan flows.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any


def _objects(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def derive_foundation_scope(
    architecture: dict[str, Any],
    execution_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Use every binding and transitive component dependency, never figure names."""

    components = {
        str(item["id"]): item
        for item in _objects(architecture.get("components"))
        if item.get("id")
    }
    dependencies = {
        component_id: {
            str(value) for value in component.get("depends_on", [])
            if str(value) in components
        }
        for component_id, component in components.items()
    }

    def closure(values: set[str]) -> set[str]:
        result = set(values) & components.keys()
        pending = list(result)
        while pending:
            for dependency in dependencies[pending.pop()] - result:
                result.add(dependency)
                pending.append(dependency)
        return result

    direct_by_task: dict[str, set[str]] = {}
    for binding in _objects(architecture.get("bindings")):
        task_id = str(binding.get("task_id") or "")
        if task_id:
            direct_by_task.setdefault(task_id, set()).update(
                str(value) for value in binding.get("components", [])
                if str(value) in components
            )
    task_components = {
        task_id: closure(values) for task_id, values in direct_by_task.items()
    }
    plan = execution_plan if isinstance(execution_plan, dict) else {}
    task_to_unit = plan.get("task_to_execution_unit") or {}
    consumers = {
        component_id: {task_id for task_id, values in task_components.items() if component_id in values}
        for component_id in components
    }
    unit_consumers = {
        component_id: {str(task_to_unit.get(task_id) or task_id) for task_id in task_ids}
        for component_id, task_ids in consumers.items()
    }
    shared = closure({key for key, units in unit_consumers.items() if len(units) > 1})
    # A Python source file is the smallest immutable unit. If two declared
    # components share it, retain both interfaces and their dependencies.
    while True:
        modules = {str(components[key].get("module") or "") for key in shared}
        expanded = closure(shared | {
            key for key, component in components.items()
            if str(component.get("module") or "") in modules
        })
        if expanded == shared:
            break
        shared = expanded
    return {
        "policy_version": "cross-unit-component-closure-v1",
        "component_ids": sorted(shared),
        "module_paths": sorted({str(components[key].get("module") or "") for key in shared}),
        "private_component_ids": sorted(components.keys() - shared),
        "private_module_paths": sorted({str(components[key].get("module") or "") for key in components.keys() - shared}),
        "task_component_ids": {key: sorted(value) for key, value in sorted(task_components.items())},
        "component_task_ids": {key: sorted(value) for key, value in sorted(consumers.items())},
        "component_execution_unit_ids": {key: sorted(value) for key, value in sorted(unit_consumers.items())},
        "task_to_execution_unit": {key: str(task_to_unit.get(key) or key) for key in sorted(task_components)},
    }


def scoped_foundation_architecture(
    architecture: dict[str, Any],
    execution_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Restrict implementation ownership while retaining all task/experiment context."""

    result = deepcopy(architecture)
    scope = derive_foundation_scope(architecture, execution_plan)
    result["_foundation_scope"] = scope
    shared = set(scope["component_ids"])
    result["components"] = [
        item for item in _objects(result.get("components")) if str(item.get("id")) in shared
    ]
    return result


def affected_foundation_consumers(
    architecture: dict[str, Any],
    component_ids: list[str],
    execution_plan: dict[str, Any] | None = None,
) -> dict[str, list[str]]:
    """Compute affected consumers from the same dependency closure as freezing."""

    scope = derive_foundation_scope(architecture, execution_plan)
    changed = set(component_ids)
    task_ids = sorted(
        task_id for task_id, values in scope["task_component_ids"].items()
        if changed.intersection(values)
    )
    return {
        "task_ids": task_ids,
        "execution_unit_ids": sorted({scope["task_to_execution_unit"][key] for key in task_ids}),
    }
