from __future__ import annotations

import json
from pathlib import PurePosixPath
from typing import Any

from .schemas import ValidationIssue


def validate_scientific_architecture(
    architecture: dict[str, Any],
    *,
    facts: dict[str, Any],
    tasks: dict[str, Any],
    experiment_index: dict[str, Any],
) -> list[ValidationIssue]:
    """Validate cross-document references and shared-model consistency.

    Pydantic checks the JSON shape.  This second gate makes the contract useful:
    every task/experiment must bind to existing shared components, dependency and
    quantity references must resolve, and task-local overrides may not silently
    redefine a global quantity.
    """

    issues: list[ValidationIssue] = []
    quantities = _dict_items(architecture.get("quantities"))
    components = _dict_items(architecture.get("components"))
    consistency_groups = _dict_items(architecture.get("consistency_groups"))
    bindings = _dict_items(architecture.get("bindings"))
    invariants = _dict_items(architecture.get("invariants"))

    quantity_ids = _unique_ids(quantities, "$.quantities", issues, required=True)
    component_ids = _unique_ids(components, "$.components", issues, required=True)
    consistency_group_ids = _unique_ids(consistency_groups, "$.consistency_groups", issues)
    _unique_ids(invariants, "$.invariants", issues)
    schema_version = str(architecture.get("schema_version") or "")
    component_indexes = {
        str(component.get("id")): index
        for index, component in enumerate(components)
        if component.get("id")
    }
    component_task_ids: dict[str, set[str]] = {}

    task_ids = {
        str(item.get("task_id"))
        for item in _dict_items(tasks.get("repro_tasks"))
        if item.get("task_id")
    }
    experiment_pairs = {
        (str(item.get("task_id")), str(item.get("experiment_id")))
        for item in _dict_items(experiment_index.get("experiments"))
        if item.get("task_id") and item.get("experiment_id")
    }
    fact_keys = {
        (str(item.get("type")), str(item.get("name")))
        for item in _dict_items(facts.get("engineering_facts"))
        if item.get("type") and item.get("name")
    }
    assumption_names = {
        str(assumption.get("name"))
        for task in _dict_items(tasks.get("repro_tasks"))
        for assumption in _dict_items(task.get("assumptions"))
        if assumption.get("name")
    }

    global_quantities = {
        str(item.get("id"))
        for item in quantities
        if item.get("id") and item.get("scope") == "global"
    }
    groups_by_id = {
        str(group.get("id")): group
        for group in consistency_groups
        if group.get("id")
    }
    for index, group in enumerate(consistency_groups):
        base = f"$.consistency_groups[{index}]"
        for task_index, task_id in enumerate(group.get("task_ids", []) if isinstance(group.get("task_ids"), list) else []):
            if str(task_id) not in task_ids:
                issues.append(ValidationIssue(f"{base}.task_ids[{task_index}]", "must refer to a finalized task"))
        for quantity_index, quantity_id in enumerate(group.get("shared_quantity_ids", []) if isinstance(group.get("shared_quantity_ids"), list) else []):
            if str(quantity_id) not in quantity_ids:
                issues.append(ValidationIssue(f"{base}.shared_quantity_ids[{quantity_index}]", "must refer to a declared quantity"))

    for index, component in enumerate(components):
        base = f"$.components[{index}]"
        if schema_version == "1.1":
            if not str(component.get("callable") or "").strip():
                issues.append(ValidationIssue(f"{base}.callable", "must be non-empty in architecture 1.1"))
            if not isinstance(component.get("execution"), dict):
                issues.append(
                    ValidationIssue(f"{base}.execution", "is required in architecture 1.1")
                )
        module = str(component.get("module") or "")
        if not module:
            issues.append(ValidationIssue(f"{base}.module", "must name a relative Python module under src/"))
        elif not _safe_foundation_module(module):
            issues.append(ValidationIssue(f"{base}.module", "must be a safe relative Python path under src/"))
        for field in ("inputs", "outputs", "parameters"):
            for ref_index, ref in enumerate(component.get(field, []) if isinstance(component.get(field), list) else []):
                if str(ref) not in quantity_ids:
                    issues.append(ValidationIssue(f"{base}.{field}[{ref_index}]", "must refer to a declared quantity"))
        for ref_index, ref in enumerate(component.get("depends_on", []) if isinstance(component.get("depends_on"), list) else []):
            if str(ref) not in component_ids:
                issues.append(ValidationIssue(f"{base}.depends_on[{ref_index}]", "must refer to a declared component"))
        issues.extend(_basis_issues(component.get("basis"), f"{base}.basis", fact_keys, assumption_names))

    bound_task_ids: set[str] = set()
    group_overrides: dict[tuple[str, str], str] = {}
    for index, binding in enumerate(bindings):
        base = f"$.bindings[{index}]"
        task_id = str(binding.get("task_id") or "")
        experiment_id = str(binding.get("experiment_id") or "")
        if task_id not in task_ids:
            issues.append(ValidationIssue(f"{base}.task_id", "must refer to a finalized reproduction task"))
        elif task_id in bound_task_ids:
            issues.append(ValidationIssue(f"{base}.task_id", "each task may have only one architecture binding"))
        else:
            bound_task_ids.add(task_id)
        if (task_id, experiment_id) not in experiment_pairs:
            issues.append(ValidationIssue(f"{base}.experiment_id", "must match the experiment assigned to this task"))
        group_id = str(binding.get("consistency_group") or "")
        group_reference_must_resolve = schema_version == "1.1" or bool(consistency_group_ids)
        if group_reference_must_resolve and group_id not in consistency_group_ids:
            issues.append(ValidationIssue(f"{base}.consistency_group", "must refer to a declared consistency group"))
        elif group_id in groups_by_id:
            declared_tasks = groups_by_id[group_id].get("task_ids")
            if isinstance(declared_tasks, list) and task_id not in {str(item) for item in declared_tasks}:
                issues.append(ValidationIssue(f"{base}.consistency_group", "binding task must belong to the declared consistency group"))
        for ref_index, ref in enumerate(binding.get("components", []) if isinstance(binding.get("components"), list) else []):
            if str(ref) not in component_ids:
                issues.append(ValidationIssue(f"{base}.components[{ref_index}]", "must refer to a declared component"))
            elif task_id in task_ids:
                component_task_ids.setdefault(str(ref), set()).add(task_id)
        for ref_index, ref in enumerate(binding.get("outputs", []) if isinstance(binding.get("outputs"), list) else []):
            if str(ref) not in quantity_ids:
                issues.append(ValidationIssue(f"{base}.outputs[{ref_index}]", "must refer to a declared quantity"))
        allowed_raw = binding.get("allowed_overrides")
        allowed_overrides = {
            str(quantity_id)
            for quantity_id in allowed_raw
        } if isinstance(allowed_raw, list) else set()
        for ref_index, quantity_id in enumerate(allowed_raw if isinstance(allowed_raw, list) else []):
            if str(quantity_id) not in quantity_ids:
                issues.append(ValidationIssue(f"{base}.allowed_overrides[{ref_index}]", "must refer to a declared quantity"))
        overrides = binding.get("overrides") if isinstance(binding.get("overrides"), dict) else {}
        for quantity_id, value in overrides.items():
            if quantity_id not in quantity_ids:
                issues.append(ValidationIssue(f"{base}.overrides.{quantity_id}", "must refer to a declared quantity"))
                continue
            if quantity_id not in allowed_overrides:
                issues.append(ValidationIssue(f"{base}.overrides.{quantity_id}", "quantity is not listed in allowed_overrides"))
            if quantity_id in global_quantities:
                issues.append(ValidationIssue(f"{base}.overrides.{quantity_id}", "global quantities cannot be overridden per task"))
            group = str(binding.get("consistency_group") or "")
            if group:
                key = (group, str(quantity_id))
                encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                previous = group_overrides.get(key)
                if previous is not None and previous != encoded:
                    issues.append(ValidationIssue(f"{base}.overrides.{quantity_id}", "override conflicts inside one consistency_group"))
                group_overrides[key] = encoded

    if schema_version == "1.1":
        for component_id, bound_tasks in component_task_ids.items():
            if len(bound_tasks) <= 1:
                continue
            component_index = component_indexes.get(component_id)
            if component_index is None:
                continue
            execution = components[component_index].get("execution")
            if isinstance(execution, dict) and execution.get("shared_implementation") is True:
                continue
            issues.append(
                ValidationIssue(
                    f"$.components[{component_index}].execution.shared_implementation",
                    "must be true when one component is bound to multiple tasks in architecture 1.1",
                )
            )

    for task_id in sorted(task_ids - bound_task_ids):
        issues.append(ValidationIssue("$.bindings", f"missing architecture binding for task: {task_id}"))

    for index, quantity in enumerate(quantities):
        issues.extend(_basis_issues(quantity.get("basis"), f"$.quantities[{index}].basis", fact_keys, assumption_names))
    for index, invariant in enumerate(invariants):
        issues.extend(_basis_issues(invariant.get("basis"), f"$.invariants[{index}].basis", fact_keys, assumption_names))
        valid_subjects = quantity_ids | component_ids
        for subject_index, subject in enumerate(invariant.get("subjects", []) if isinstance(invariant.get("subjects"), list) else []):
            if str(subject) not in valid_subjects:
                issues.append(ValidationIssue(f"$.invariants[{index}].subjects[{subject_index}]", "must refer to a declared quantity or component"))
        for task_index, task_id in enumerate(invariant.get("task_ids", []) if isinstance(invariant.get("task_ids"), list) else []):
            if str(task_id) not in task_ids:
                issues.append(ValidationIssue(f"$.invariants[{index}].task_ids[{task_index}]", "must refer to a finalized task"))

    return _dedupe(issues)


def partition_scientific_architecture_issues(
    architecture: dict[str, Any],
    *,
    facts: dict[str, Any],
    tasks: dict[str, Any],
    experiment_index: dict[str, Any],
) -> tuple[list[ValidationIssue], list[ValidationIssue]]:
    """Split cross-document findings into execution blockers and advice.

    The architecture gate is deliberately narrow: it blocks only references or
    bindings that the Foundation/Task Writers cannot implement consistently.
    Evidence sufficiency and descriptive consistency diagnostics remain visible
    in the audit trail without interrupting reproduction.
    """

    blockers: list[ValidationIssue] = []
    warnings: list[ValidationIssue] = []
    for issue in validate_scientific_architecture(
        architecture,
        facts=facts,
        tasks=tasks,
        experiment_index=experiment_index,
    ):
        target = (
            blockers
            if _is_execution_blocker(
                issue,
                schema_version=str(architecture.get("schema_version") or ""),
            )
            else warnings
        )
        target.append(issue)
    return blockers, warnings


def scientific_architecture_execution_blockers(
    architecture: dict[str, Any],
    *,
    facts: dict[str, Any],
    tasks: dict[str, Any],
    experiment_index: dict[str, Any],
) -> list[ValidationIssue]:
    """Return only cross-document defects that make execution ambiguous."""

    blockers, _warnings = partition_scientific_architecture_issues(
        architecture,
        facts=facts,
        tasks=tasks,
        experiment_index=experiment_index,
    )
    return blockers


def foundation_module_paths(architecture: dict[str, Any]) -> set[str]:
    """Return normalized shared Python paths owned by the Foundation Writer."""

    paths: set[str] = set()
    for component in _dict_items(architecture.get("components")):
        module = str(component.get("module") or "").replace("\\", "/")
        if _safe_foundation_module(module):
            paths.add(module)
    return paths


def _is_execution_blocker(
    issue: ValidationIssue,
    *,
    schema_version: str,
) -> bool:
    path = issue.path
    if ".basis" in path or path.startswith("$.invariants["):
        return False
    if path == "$.bindings" or path.startswith("$.bindings["):
        return True
    if path.startswith("$.quantities["):
        return path.endswith(".id")
    if schema_version == "1.1" and path.startswith("$.consistency_groups["):
        return path.endswith(".id")
    if path.startswith("$.components["):
        return any(
            marker in path
            for marker in (
                ".id",
                ".module",
                ".callable",
                ".execution",
                ".inputs[",
                ".outputs[",
                ".parameters[",
                ".depends_on[",
            )
        )
    return False


def _basis_issues(
    raw: Any,
    base: str,
    fact_keys: set[tuple[str, str]],
    assumption_names: set[str],
) -> list[ValidationIssue]:
    if not isinstance(raw, dict):
        return []
    issues: list[ValidationIssue] = []
    for index, ref in enumerate(_dict_items(raw.get("evidence_facts"))):
        key = (str(ref.get("type") or ""), str(ref.get("name") or ""))
        if key not in fact_keys:
            issues.append(ValidationIssue(f"{base}.evidence_facts[{index}]", "must refer to an extracted engineering fact"))
    for index, name in enumerate(raw.get("assumption_refs", []) if isinstance(raw.get("assumption_refs"), list) else []):
        if str(name) not in assumption_names:
            issues.append(ValidationIssue(f"{base}.assumption_refs[{index}]", "must refer to a declared task assumption"))
    if raw.get("status") == "assumed" and not raw.get("assumption_refs"):
        issues.append(ValidationIssue(f"{base}.assumption_refs", "assumed architecture entries need an assumption reference"))
    return issues


def _safe_foundation_module(value: str) -> bool:
    path = PurePosixPath(value.replace("\\", "/"))
    return (
        bool(value)
        and not path.is_absolute()
        and ".." not in path.parts
        and len(path.parts) >= 2
        and path.parts[0] == "src"
        and path.suffix == ".py"
        and all(part not in {"", "."} for part in path.parts)
        and path.name not in {"_io.py", "_backend.py"}
    )


def _unique_ids(
    items: list[dict[str, Any]],
    base: str,
    issues: list[ValidationIssue],
    *,
    required: bool = False,
) -> set[str]:
    seen: set[str] = set()
    for index, item in enumerate(items):
        value = str(item.get("id") or "")
        if not value:
            if required:
                issues.append(ValidationIssue(f"{base}[{index}].id", "id is required"))
            continue
        if value in seen:
            issues.append(ValidationIssue(f"{base}[{index}].id", "duplicate id"))
        seen.add(value)
    return seen


def _dict_items(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _dedupe(issues: list[ValidationIssue]) -> list[ValidationIssue]:
    result: list[ValidationIssue] = []
    seen: set[tuple[str, str]] = set()
    for issue in issues:
        key = (issue.path, issue.message)
        if key not in seen:
            seen.add(key)
            result.append(issue)
    return result
