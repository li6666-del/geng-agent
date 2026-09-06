from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

from .schemas import ValidationIssue


def validate_scientific_architecture(
    architecture: dict[str, Any],
    *,
    facts: dict[str, Any],
    tasks: dict[str, Any],
    experiment_index: dict[str, Any],
    execution_plan: dict[str, Any] | None = None,
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

    task_items = _dict_items(tasks.get("repro_tasks"))
    task_ids = {
        str(item.get("task_id"))
        for item in task_items
        if item.get("task_id")
    }
    task_acceptance_criteria = {
        str(item.get("task_id")): _task_acceptance_criteria(item)
        for item in task_items
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
    group_ids_by_task: dict[str, set[str]] = {}
    for index, group in enumerate(consistency_groups):
        base = f"$.consistency_groups[{index}]"
        for task_index, task_id in enumerate(group.get("task_ids", []) if isinstance(group.get("task_ids"), list) else []):
            group_ids_by_task.setdefault(str(task_id), set()).add(str(group.get("id") or ""))
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
    bound_experiment_pairs: set[tuple[str, str]] = set()
    group_overrides: dict[tuple[str, str], Any] = {}
    for index, binding in enumerate(bindings):
        base = f"$.bindings[{index}]"
        task_id = str(binding.get("task_id") or "")
        experiment_id = str(binding.get("experiment_id") or "")
        if task_id not in task_ids:
            issues.append(ValidationIssue(f"{base}.task_id", "must refer to a finalized reproduction task"))
        elif (task_id, experiment_id) in bound_experiment_pairs:
            issues.append(ValidationIssue(f"{base}.task_id", "each task/experiment pair may have only one architecture binding"))
        else:
            bound_task_ids.add(task_id)
            bound_experiment_pairs.add((task_id, experiment_id))
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
        binding_outputs = {
            str(ref)
            for ref in binding.get("outputs", [])
        } if isinstance(binding.get("outputs"), list) else set()
        for ref_index, ref in enumerate(binding.get("outputs", []) if isinstance(binding.get("outputs"), list) else []):
            if str(ref) not in quantity_ids:
                issues.append(ValidationIssue(f"{base}.outputs[{ref_index}]", "must refer to a declared quantity"))
        acceptance_items = _dict_items(binding.get("acceptance_bindings"))
        expected_acceptance = task_acceptance_criteria.get(task_id, set())
        mapped_acceptance: set[tuple[str, str]] = set()
        seen_acceptance: set[tuple[str, str]] = set()
        if "acceptance_bindings" in binding and not isinstance(binding.get("acceptance_bindings"), list):
            issues.append(
                ValidationIssue(
                    f"{base}.acceptance_bindings",
                    "optional acceptance output mappings should be an array; malformed mappings are ignored",
                )
            )
        for acceptance_index, acceptance_binding in enumerate(acceptance_items):
            acceptance_base = f"{base}.acceptance_bindings[{acceptance_index}]"
            criterion_id = str(acceptance_binding.get("criterion_id") or "")
            criterion_kind = str(acceptance_binding.get("criterion_kind") or "")
            criterion_key = (criterion_id, criterion_kind)
            expected_kinds = {kind for expected_id, kind in expected_acceptance if expected_id == criterion_id}
            expected_kind = next(iter(expected_kinds)) if len(expected_kinds) == 1 else None
            mapping_is_active = False
            if not criterion_id:
                issues.append(
                    ValidationIssue(
                        f"{acceptance_base}.criterion_id",
                        "empty optional criterion mapping is ignored",
                    )
                )
            elif criterion_key in seen_acceptance:
                issues.append(
                    ValidationIssue(
                        f"{acceptance_base}.criterion_id",
                        "duplicate optional criterion mapping is ignored after the first occurrence",
                    )
                )
            else:
                seen_acceptance.add(criterion_key)
                if not expected_kinds:
                    issues.append(
                        ValidationIssue(
                            f"{acceptance_base}.criterion_id",
                            "does not match this task's scientific_acceptance contract; mapping is ignored",
                        )
                    )
                elif criterion_kind in expected_kinds:
                    mapping_is_active = True
                    mapped_acceptance.add(criterion_key)
                elif expected_kind is not None:
                    mapping_is_active = True
                    mapped_acceptance.add((criterion_id, expected_kind))
                    issues.append(
                        ValidationIssue(
                            f"{acceptance_base}.criterion_kind",
                            f"expected {expected_kind!r} for {criterion_id!r}; output mapping remains advisory",
                        )
                    )
                else:
                    issues.append(
                        ValidationIssue(
                            f"{acceptance_base}.criterion_kind",
                            f"{criterion_id!r} is used by multiple criterion kinds; this ambiguous mapping is ignored",
                        )
                    )
            if mapping_is_active:
                raw_output_ids = acceptance_binding.get("output_quantity_ids")
                output_ids = raw_output_ids if isinstance(raw_output_ids, list) else []
                if not output_ids:
                    issues.append(
                        ValidationIssue(
                            f"{acceptance_base}.output_quantity_ids",
                            "optional criterion mapping has no measurable output quantities",
                        )
                    )
                for output_index, output_id in enumerate(output_ids):
                    output_id = str(output_id)
                    output_path = f"{acceptance_base}.output_quantity_ids[{output_index}]"
                    if output_id not in quantity_ids:
                        issues.append(
                            ValidationIssue(
                                output_path,
                                "must refer to a declared quantity so the Foundation can implement the interface",
                            )
                        )
                    elif output_id not in binding_outputs:
                        issues.append(
                            ValidationIssue(
                                output_path,
                                "must also be listed in this task binding's outputs",
                            )
                        )
        for criterion_id, criterion_kind in sorted(expected_acceptance - mapped_acceptance):
            issues.append(
                ValidationIssue(
                    f"{base}.acceptance_bindings",
                    f"no measurable output mapping was supplied for {criterion_kind} {criterion_id!r}; mapping is optional and execution may continue",
                )
            )
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
            primary_group = str(binding.get("consistency_group") or "")
            memberships = set(group_ids_by_task.get(task_id, set()))
            if primary_group:
                memberships.add(primary_group)
            for group in memberships:
                group_definition = groups_by_id.get(group, {})
                shared_quantity_ids = {
                    str(item)
                    for item in group_definition.get("shared_quantity_ids", [])
                } if isinstance(group_definition.get("shared_quantity_ids"), list) else set()
                if quantity_id not in shared_quantity_ids:
                    continue
                key = (group, str(quantity_id))
                previous = group_overrides.get(key)
                if previous is not None and not _scientifically_equivalent(previous, value):
                    issues.append(ValidationIssue(f"{base}.overrides.{quantity_id}", "override conflicts inside one consistency_group"))
                group_overrides[key] = value

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

    issues.extend(
        _task_relationship_architecture_issues(
            tasks=tasks,
            consistency_groups=consistency_groups,
            bindings=bindings,
            declared_quantity_ids=quantity_ids,
            material_relationship_ids=_material_weak_relationship_ids(
                execution_plan
            ),
        )
    )

    return _dedupe(issues)


def partition_scientific_architecture_issues(
    architecture: dict[str, Any],
    *,
    facts: dict[str, Any],
    tasks: dict[str, Any],
    experiment_index: dict[str, Any],
    execution_plan: dict[str, Any] | None = None,
) -> tuple[list[ValidationIssue], list[ValidationIssue]]:
    """Split cross-document findings into execution blockers and advice.

    The architecture gate is deliberately narrow: it blocks only references or
    bindings that the Foundation/Task Writers cannot implement consistently.
    Evidence sufficiency and descriptive consistency diagnostics remain visible
    in the audit trail without interrupting reproduction.
    """

    blockers: list[ValidationIssue] = []
    warnings: list[ValidationIssue] = []
    material_relationship_ids = _material_weak_relationship_ids(execution_plan)
    material_relationship_indexes = {
        index
        for index, relationship in enumerate(
            _dict_items(tasks.get("execution_relationships"))
        )
        if str(relationship.get("relationship_id") or "")
        in material_relationship_ids
    }
    for issue in validate_scientific_architecture(
        architecture,
        facts=facts,
        tasks=tasks,
        experiment_index=experiment_index,
        execution_plan=execution_plan,
    ):
        target = (
            blockers
            if _is_execution_blocker(
                issue,
                schema_version=str(architecture.get("schema_version") or ""),
                material_relationship_indexes=material_relationship_indexes,
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
    execution_plan: dict[str, Any] | None = None,
) -> list[ValidationIssue]:
    """Return only cross-document defects that make execution ambiguous."""

    blockers, _warnings = partition_scientific_architecture_issues(
        architecture,
        facts=facts,
        tasks=tasks,
        experiment_index=experiment_index,
        execution_plan=execution_plan,
    )
    return blockers


def foundation_module_paths(
    architecture: dict[str, Any],
    execution_plan: dict[str, Any] | None = None,
) -> set[str]:
    """Return normalized shared Python paths owned by the Foundation Writer."""

    if execution_plan is not None:
        from .foundation_scope import scoped_foundation_architecture
        architecture = scoped_foundation_architecture(architecture, execution_plan)
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
    material_relationship_indexes: set[int],
) -> bool:
    del schema_version  # classification is based on material execution impact.
    path = issue.path
    if ".basis" in path or path.startswith("$.invariants["):
        return False
    if path.startswith("$.consistency_groups["):
        return False
    if path.startswith("$.execution_relationships["):
        try:
            relationship_index = int(path.split("[", 1)[1].split("]", 1)[0])
        except (IndexError, ValueError):
            return False
        return relationship_index in material_relationship_indexes
    if any(
        marker in path
        for marker in (
            ".acceptance_bindings",
            ".allowed_overrides",
            ".execution.shared_implementation",
        )
    ):
        return False
    if path == "$.bindings":
        return True
    if path.startswith("$.bindings["):
        if ".consistency_group" in path:
            return False
        if ".overrides." in path:
            # Unknown/global/shared-conflicting values change executable
            # science. A missing whitelist entry is only bookkeeping debt.
            return "not listed in allowed_overrides" not in issue.message
        return any(
            marker in path
            for marker in (".task_id", ".experiment_id", ".components[", ".outputs[")
        )
    if path.startswith("$.quantities["):
        return path.endswith(".id")
    if path.startswith("$.components["):
        return any(
            marker in path
            for marker in (
                ".id", ".module", ".callable", ".execution",
                ".inputs[", ".outputs[", ".parameters[", ".depends_on[",
            )
        )
    return False


def _task_relationship_architecture_issues(
    *,
    tasks: dict[str, Any],
    consistency_groups: list[dict[str, Any]],
    bindings: list[dict[str, Any]],
    declared_quantity_ids: set[str],
    material_relationship_ids: set[str],
) -> list[ValidationIssue]:
    """Check only shared-science omissions that would change execution semantics."""

    relationships = _dict_items(tasks.get("execution_relationships"))
    if not relationships:
        return []
    group_records: list[tuple[set[str], set[str]]] = []
    for group in consistency_groups:
        members = {
            str(item)
            for item in group.get("task_ids", [])
        } if isinstance(group.get("task_ids"), list) else set()
        shared_quantities = {
            str(item)
            for item in group.get("shared_quantity_ids", [])
            if str(item) in declared_quantity_ids
        } if isinstance(group.get("shared_quantity_ids"), list) else set()
        group_records.append((members, shared_quantities))
    components_by_task: dict[str, set[str]] = {}
    quantities_by_task: dict[str, set[str]] = {}
    for binding in bindings:
        task_id = str(binding.get("task_id") or "")
        components = binding.get("components")
        if task_id and isinstance(components, list):
            components_by_task.setdefault(task_id, set()).update(map(str, components))
        outputs = binding.get("outputs")
        if task_id and isinstance(outputs, list):
            quantities_by_task.setdefault(task_id, set()).update(
                str(value) for value in outputs if str(value) in declared_quantity_ids
            )

    issues: list[ValidationIssue] = []
    for index, relationship in enumerate(relationships):
        members = {
            str(item)
            for item in relationship.get("task_ids", [])
            if str(item)
        } if isinstance(relationship.get("task_ids"), list) else set()
        if len(members) < 2:
            continue
        relationship_id = str(relationship.get("relationship_id") or "")
        material = relationship_id in material_relationship_ids
        covering_groups = [
            shared_quantities
            for group_members, shared_quantities in group_records
            if (
                group_members == members
                if material
                else members.issubset(group_members)
            )
        ]
        base = f"$.execution_relationships[{index}]"
        if not covering_groups:
            issues.append(
                ValidationIssue(
                    f"{base}.task_ids",
                    (
                        "architecture must keep this cross-Writer weak relationship "
                        "in an exact shared consistency group"
                        if material
                        else "architecture should keep this task relationship in one shared consistency group"
                    ),
                )
            )
            continue
        shared_components: set[str] | None = None
        for task_id in members:
            task_components = components_by_task.get(task_id, set())
            shared_components = (
                set(task_components)
                if shared_components is None
                else shared_components.intersection(task_components)
            )
        common_quantities: set[str] | None = None
        for task_id in members:
            task_quantities = quantities_by_task.get(task_id, set())
            common_quantities = (
                set(task_quantities)
                if common_quantities is None
                else common_quantities.intersection(task_quantities)
            )
        has_shared_quantities = any(
            bool(shared_quantities.intersection(common_quantities or set()))
            for shared_quantities in covering_groups
        )
        if not has_shared_quantities and not shared_components:
            issues.append(
                ValidationIssue(
                    f"{base}.shared_science",
                    "architecture exposes neither a shared quantity nor a shared component for this relationship",
                )
            )
    return issues


def _material_weak_relationship_ids(
    execution_plan: dict[str, Any] | None,
) -> set[str]:
    if not isinstance(execution_plan, dict):
        return set()
    groups = execution_plan.get("weak_consistency_groups")
    material: set[str] = set()
    for group in groups if isinstance(groups, list) else []:
        if not isinstance(group, dict):
            continue
        unit_ids = {
            str(unit_id)
            for unit_id in group.get("execution_unit_ids", [])
            if str(unit_id)
        } if isinstance(group.get("execution_unit_ids"), list) else set()
        if len(unit_ids) <= 1:
            continue
        relationship_ids = group.get("relationship_ids")
        if isinstance(relationship_ids, list):
            material.update(
                str(relationship_id)
                for relationship_id in relationship_ids
                if str(relationship_id)
            )
    return material


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
    )


def _scientifically_equivalent(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return left is right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return float(left) == float(right)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            _scientifically_equivalent(left_item, right_item)
            for left_item, right_item in zip(left, right)
        )
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(
            _scientifically_equivalent(left[key], right[key]) for key in left
        )
    return left == right


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


def _task_acceptance_criteria(task: dict[str, Any]) -> set[tuple[str, str]]:
    """Return criterion IDs that may be mapped to measurable outputs.

    The task contract remains the scientific authority. Architecture mappings are
    only implementation hints, so malformed task entries stay the task validator's
    responsibility and never become a second acceptance authority here.
    """

    acceptance = task.get("scientific_acceptance")
    if not isinstance(acceptance, dict):
        return set()
    result: set[tuple[str, str]] = set()
    for item in _dict_items(acceptance.get("core_conclusions")):
        criterion_id = str(item.get("claim_id") or "")
        if criterion_id:
            result.add((criterion_id, "core_conclusion"))
    for item in _dict_items(acceptance.get("key_numeric_targets")):
        criterion_id = str(item.get("target_id") or "")
        if criterion_id:
            result.add((criterion_id, "key_numeric_target"))
    return result


def _dedupe(issues: list[ValidationIssue]) -> list[ValidationIssue]:
    result: list[ValidationIssue] = []
    seen: set[tuple[str, str]] = set()
    for issue in issues:
        key = (issue.path, issue.message)
        if key not in seen:
            seen.add(key)
            result.append(issue)
    return result
