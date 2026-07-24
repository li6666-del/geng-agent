from __future__ import annotations

from copy import deepcopy
import re
from typing import Any, get_args

from .schemas import ValidationIssue
from .schema_models import FactType


_NORMALIZER_VERSION = 6
_BASIS_FIELDS = ("evidence_facts", "assumption_refs", "note")
_FACT_TYPES = set(get_args(FactType))
_PROTECTED_SCIENTIFIC_FIELDS: dict[str, tuple[str, ...]] = {
    "quantities": (
        "role",
        "dtype",
        "shape",
        "unit",
        "scale",
        "normalization",
        "scope",
        "default",
    ),
    "components": ("kind", "module", "callable", "execution"),
    # Binding identities and references may be repaired. Existing override
    # values remain protected because they change the numerical experiment.
    "bindings": ("overrides",),
}


def normalize_scientific_architecture_candidate(
    data: Any,
) -> tuple[dict[str, Any], list[ValidationIssue], list[ValidationIssue]]:
    """Canonicalize known architecture dialects without changing scientific values."""

    if not isinstance(data, dict):
        return {}, [], [ValidationIssue("$", "scientific architecture must be a JSON object")]

    document = deepcopy(data)
    meta = deepcopy(document.get("_meta")) if isinstance(document.get("_meta"), dict) else {}
    prior = meta.get("scientific_architecture_normalization")
    warnings = _issues_from_meta(prior, "warnings")
    errors = _issues_from_meta(prior, "errors")

    _move_alias(document, "bindings", "task_bindings", "$", warnings, errors)

    quantities = _dict_items(document.get("quantities"))
    for index, quantity in enumerate(quantities):
        base = f"$.quantities[{index}]"
        shape = quantity.get("shape")
        if isinstance(shape, str):
            quantity["shape"] = [shape]
            warnings.append(ValidationIssue(f"{base}.shape", "wrapped a scalar shape expression in a one-item array"))
        _normalize_basis(quantity, base, warnings, errors)

    components = _dict_items(document.get("components"))
    for index, component in enumerate(components):
        base = f"$.components[{index}]"
        if "callable" not in component:
            component["callable"] = ""
            warnings.append(ValidationIssue(f"{base}.callable", "missing optional callable name; preserved as empty"))
        execution = component.get("execution")
        if isinstance(execution, dict):
            framework = str(execution.get("primary_framework") or "").strip()
            framework_key = re.sub(r"[-_.\s]+", "", framework.casefold())
            if re.fullmatch(r"(?:c?python)(?:\d+)?", framework_key):
                execution["primary_framework"] = "standard_library"
                warnings.append(
                    ValidationIssue(
                        f"{base}.execution.primary_framework",
                        f"mapped Python runtime label {framework!r} to standard_library",
                    )
                )
        _normalize_basis(component, base, warnings, errors)

    bindings = _dict_items(document.get("bindings"))
    for index, binding in enumerate(bindings):
        base = f"$.bindings[{index}]"
        _move_alias(binding, "consistency_group", "consistency_group_id", base, warnings, errors)
        _move_alias(binding, "components", "component_ids", base, warnings, errors)
        _move_alias(binding, "outputs", "output_quantity_ids", base, warnings, errors)
        _move_alias(binding, "acceptance_bindings", "acceptance_mappings", base, warnings, errors)
        raw_acceptance = binding.get("acceptance_bindings")
        if raw_acceptance is None:
            binding["acceptance_bindings"] = []
            warnings.append(
                ValidationIssue(
                    f"{base}.acceptance_bindings",
                    "initialized missing optional acceptance output mappings as an empty array",
                )
            )
        elif isinstance(raw_acceptance, dict):
            binding["acceptance_bindings"] = [raw_acceptance]
            warnings.append(
                ValidationIssue(
                    f"{base}.acceptance_bindings",
                    "wrapped one optional acceptance output mapping in an array",
                )
            )
        elif not isinstance(raw_acceptance, list):
            binding["acceptance_bindings"] = []
            warnings.append(
                ValidationIssue(
                    f"{base}.acceptance_bindings",
                    "discarded malformed optional acceptance output mappings",
                )
            )
        raw_acceptance = binding.get("acceptance_bindings")
        acceptance_items = _dict_items(raw_acceptance)
        if isinstance(raw_acceptance, list) and len(acceptance_items) != len(raw_acceptance):
            binding["acceptance_bindings"] = acceptance_items
            warnings.append(
                ValidationIssue(
                    f"{base}.acceptance_bindings",
                    "discarded non-object optional acceptance output mappings",
                )
            )
        for acceptance_index, acceptance in enumerate(acceptance_items):
            acceptance_base = f"{base}.acceptance_bindings[{acceptance_index}]"
            had_claim_id = "claim_id" in acceptance
            had_target_id = "target_id" in acceptance
            _move_alias(acceptance, "criterion_id", "claim_id", acceptance_base, warnings, errors)
            _move_alias(acceptance, "criterion_id", "target_id", acceptance_base, warnings, errors)
            _move_alias(acceptance, "criterion_kind", "criterion_type", acceptance_base, warnings, errors)
            _move_alias(acceptance, "criterion_kind", "kind", acceptance_base, warnings, errors)
            _move_alias(acceptance, "output_quantity_ids", "outputs", acceptance_base, warnings, errors)
            _move_alias(acceptance, "output_quantity_ids", "output_ids", acceptance_base, warnings, errors)
            if "criterion_kind" not in acceptance:
                if had_claim_id:
                    acceptance["criterion_kind"] = "core_conclusion"
                    warnings.append(
                        ValidationIssue(
                            f"{acceptance_base}.criterion_kind",
                            "inferred core_conclusion from claim_id",
                        )
                    )
                elif had_target_id:
                    acceptance["criterion_kind"] = "key_numeric_target"
                    warnings.append(
                        ValidationIssue(
                            f"{acceptance_base}.criterion_kind",
                            "inferred key_numeric_target from target_id",
                        )
                    )
            kind_aliases = {
                "claim": "core_conclusion",
                "core_claim": "core_conclusion",
                "conclusion": "core_conclusion",
                "numeric": "key_numeric_target",
                "numeric_target": "key_numeric_target",
                "key_numeric": "key_numeric_target",
            }
            criterion_id = acceptance.get("criterion_id")
            if not isinstance(criterion_id, str):
                acceptance["criterion_id"] = (
                    str(criterion_id)
                    if isinstance(criterion_id, (int, float, bool))
                    else ""
                )
                warnings.append(
                    ValidationIssue(
                        f"{acceptance_base}.criterion_id",
                        "normalized a non-string optional criterion id",
                    )
                )
            kind = str(acceptance.get("criterion_kind") or "").strip().casefold()
            if kind in kind_aliases:
                acceptance["criterion_kind"] = kind_aliases[kind]
                warnings.append(
                    ValidationIssue(
                        f"{acceptance_base}.criterion_kind",
                        f"mapped criterion kind alias {kind!r} to {kind_aliases[kind]!r}",
                    )
                )
            elif kind in {"core_conclusion", "key_numeric_target"}:
                acceptance["criterion_kind"] = kind
            else:
                fallback_kind = (
                    "key_numeric_target"
                    if had_target_id and not had_claim_id
                    else "core_conclusion"
                )
                acceptance["criterion_kind"] = fallback_kind
                warnings.append(
                    ValidationIssue(
                        f"{acceptance_base}.criterion_kind",
                        f"normalized unsupported optional criterion kind to {fallback_kind!r}",
                    )
                )
            output_ids = acceptance.get("output_quantity_ids")
            if isinstance(output_ids, str):
                acceptance["output_quantity_ids"] = [output_ids] if output_ids.strip() else []
                warnings.append(
                    ValidationIssue(
                        f"{acceptance_base}.output_quantity_ids",
                        "wrapped a scalar output quantity id in an array",
                    )
                )
            elif isinstance(output_ids, list):
                normalized_output_ids = [
                    output_id
                    for output_id in output_ids
                    if isinstance(output_id, str) and output_id.strip()
                ]
                if normalized_output_ids != output_ids:
                    acceptance["output_quantity_ids"] = normalized_output_ids
                    warnings.append(
                        ValidationIssue(
                            f"{acceptance_base}.output_quantity_ids",
                            "discarded malformed optional output quantity ids",
                        )
                    )
            else:
                acceptance["output_quantity_ids"] = []
                warnings.append(
                    ValidationIssue(
                        f"{acceptance_base}.output_quantity_ids",
                        "initialized malformed optional output quantity ids as an empty array",
                    )
                )
            known_fields = {"criterion_id", "criterion_kind", "output_quantity_ids"}
            for field in sorted(set(acceptance) - known_fields):
                acceptance.pop(field, None)
                warnings.append(
                    ValidationIssue(
                        f"{acceptance_base}.{field}",
                        "discarded an unknown optional acceptance mapping field",
                    )
                )

    _normalize_bookkeeping(document, quantities, components, bindings, warnings)
    _normalize_invariants(document, warnings, errors)

    warnings = _group_warnings(warnings)
    errors = _dedupe(errors)
    meta["scientific_architecture_normalization"] = {
        "normalizer_version": _NORMALIZER_VERSION,
        "warnings": [issue.as_dict() for issue in warnings],
        "errors": [issue.as_dict() for issue in errors],
    }
    document["_meta"] = meta
    return document, warnings, errors


def finalize_scientific_architecture(data: Any) -> dict[str, Any]:
    document, _, _ = normalize_scientific_architecture_candidate(data)
    return document


def scientific_architecture_normalization_warnings(document: dict[str, Any]) -> list[ValidationIssue]:
    meta = document.get("_meta") if isinstance(document, dict) else None
    payload = meta.get("scientific_architecture_normalization") if isinstance(meta, dict) else None
    return _issues_from_meta(payload, "warnings")


def scientific_architecture_normalization_errors(document: dict[str, Any]) -> list[ValidationIssue]:
    meta = document.get("_meta") if isinstance(document, dict) else None
    payload = meta.get("scientific_architecture_normalization") if isinstance(meta, dict) else None
    return _issues_from_meta(payload, "errors")


def validate_scientific_architecture_repair_preservation(
    before: dict[str, Any],
    after: dict[str, Any],
) -> list[ValidationIssue]:
    """Protect existing scientific values while allowing genuine repair."""

    issues: list[ValidationIssue] = []
    for field in ("schema_version", "workflow_version"):
        prior = before.get(field)
        if not _empty(prior) and not _scientifically_equivalent(prior, after.get(field)):
            issues.append(
                ValidationIssue(
                    f"$.{field}",
                    "repair changed an existing architecture identity value",
                )
            )
    for list_name, protected_fields in _PROTECTED_SCIENTIFIC_FIELDS.items():
        issues.extend(
            _protected_item_issues(
                list_name,
                before.get(list_name),
                after.get(list_name),
                protected_fields,
            )
        )
    return _dedupe(issues)


def _normalize_basis(
    item: dict[str, Any],
    base: str,
    warnings: list[ValidationIssue],
    errors: list[ValidationIssue],
) -> None:
    raw_basis = item.get("basis")
    if isinstance(raw_basis, str):
        basis: dict[str, Any] = {"status": raw_basis}
        item["basis"] = basis
        warnings.append(ValidationIssue(f"{base}.basis", "nested the flattened evidence basis object"))
    elif isinstance(raw_basis, dict):
        basis = deepcopy(raw_basis)
        item["basis"] = basis
    elif raw_basis is None:
        basis = {"status": "unresolved"}
        item["basis"] = basis
        warnings.append(ValidationIssue(f"{base}.basis", "missing basis marked unresolved"))
    else:
        return

    for field in _BASIS_FIELDS:
        if field not in item:
            continue
        flattened = item.pop(field)
        if field not in basis:
            basis[field] = flattened
            warnings.append(ValidationIssue(f"{base}.{field}", f"moved flattened {field} into basis"))
        elif basis[field] == flattened:
            warnings.append(ValidationIssue(f"{base}.{field}", f"removed duplicate flattened {field}"))
        else:
            warnings.append(ValidationIssue(f"{base}.{field}", f"conflicts with {base}.basis.{field}; kept canonical basis value"))
    basis.setdefault("evidence_facts", [])
    basis.setdefault("assumption_refs", [])
    basis.setdefault("note", "")


def _move_alias(
    container: dict[str, Any],
    canonical: str,
    alias: str,
    base: str,
    warnings: list[ValidationIssue],
    errors: list[ValidationIssue],
) -> None:
    if alias not in container:
        return
    alias_value = container.pop(alias)
    alias_path = f"{base}.{alias}" if base != "$" else f"$.{alias}"
    canonical_path = f"{base}.{canonical}" if base != "$" else f"$.{canonical}"
    if canonical not in container:
        container[canonical] = alias_value
        warnings.append(ValidationIssue(alias_path, f"mapped alias to {canonical_path}"))
    elif container[canonical] == alias_value:
        warnings.append(ValidationIssue(alias_path, f"removed duplicate alias for {canonical_path}"))
    else:
        warnings.append(ValidationIssue(alias_path, f"conflicts with canonical field {canonical_path}; kept canonical value"))


def _normalize_bookkeeping(
    document: dict[str, Any],
    quantities: list[dict[str, Any]],
    components: list[dict[str, Any]],
    bindings: list[dict[str, Any]],
    warnings: list[ValidationIssue],
) -> None:
    raw_groups = document.get("consistency_groups")
    groups = _dict_items(raw_groups)
    if isinstance(raw_groups, list) and len(groups) != len(raw_groups):
        warnings.append(
            ValidationIssue(
                "$.consistency_groups",
                "ignored non-object consistency bookkeeping entries",
            )
        )
    elif raw_groups is not None and not isinstance(raw_groups, list):
        groups = []
        warnings.append(
            ValidationIssue(
                "$.consistency_groups",
                "replaced malformed consistency bookkeeping with an empty list",
            )
        )

    normalized_groups: list[dict[str, Any]] = []
    groups_by_id: dict[str, dict[str, Any]] = {}
    for index, group in enumerate(groups):
        base = f"$.consistency_groups[{index}]"
        group_id = str(group.get("id") or "").strip()
        if not group_id:
            group_id = f"consistency_group_{index + 1}"
            group["id"] = group_id
            warnings.append(
                ValidationIssue(f"{base}.id", f"assigned bookkeeping id {group_id!r}")
            )
        group["task_ids"] = _string_list(group.get("task_ids"))
        group["shared_quantity_ids"] = _string_list(group.get("shared_quantity_ids"))
        for field in sorted(set(group) - {"id", "task_ids", "shared_quantity_ids"}):
            group.pop(field, None)
            warnings.append(
                ValidationIssue(f"{base}.{field}", "ignored unknown consistency bookkeeping")
            )
        existing = groups_by_id.get(group_id)
        if existing is not None:
            _extend_unique(existing["task_ids"], group["task_ids"])
            _extend_unique(existing["shared_quantity_ids"], group["shared_quantity_ids"])
            warnings.append(
                ValidationIssue(f"{base}.id", f"merged duplicate bookkeeping group {group_id!r}")
            )
            continue
        groups_by_id[group_id] = group
        normalized_groups.append(group)

    quantity_scopes = {
        str(quantity.get("id") or ""): str(quantity.get("scope") or "")
        for quantity in quantities
        if str(quantity.get("id") or "")
    }
    component_tasks: dict[str, set[str]] = {}
    for index, binding in enumerate(bindings):
        base = f"$.bindings[{index}]"
        task_id = str(binding.get("task_id") or "").strip()
        group_id = str(binding.get("consistency_group") or "").strip()
        if not group_id:
            suffix = re.sub(r"[^A-Za-z0-9_.-]+", "_", task_id).strip("_")
            group_id = f"consistency_{suffix or index + 1}"
            binding["consistency_group"] = group_id
            warnings.append(
                ValidationIssue(
                    f"{base}.consistency_group",
                    f"created bookkeeping group {group_id!r} for this binding",
                )
            )
        group = groups_by_id.get(group_id)
        if group is None:
            group = {"id": group_id, "task_ids": [], "shared_quantity_ids": []}
            groups_by_id[group_id] = group
            normalized_groups.append(group)
            warnings.append(
                ValidationIssue(
                    f"{base}.consistency_group",
                    f"declared missing bookkeeping group {group_id!r}",
                )
            )
        if task_id:
            _extend_unique(group["task_ids"], [task_id])

        for field in ("components", "outputs", "allowed_overrides"):
            normalized_refs = _string_list(binding.get(field))
            if normalized_refs != binding.get(field):
                binding[field] = normalized_refs
                warnings.append(
                    ValidationIssue(f"{base}.{field}", "normalized reference bookkeeping")
                )
        overrides = binding.get("overrides")
        if isinstance(overrides, dict):
            allowed = binding.setdefault("allowed_overrides", [])
            for quantity_id in overrides:
                quantity_id = str(quantity_id)
                if (
                    quantity_scopes.get(quantity_id)
                    and quantity_scopes.get(quantity_id) != "global"
                    and quantity_id not in allowed
                ):
                    allowed.append(quantity_id)
                    warnings.append(
                        ValidationIssue(
                            f"{base}.allowed_overrides",
                            f"listed existing non-global override {quantity_id!r}",
                        )
                    )
        for component_id in binding.get("components", []):
            if task_id:
                component_tasks.setdefault(str(component_id), set()).add(task_id)

    for index, component in enumerate(components):
        execution = component.get("execution")
        if not isinstance(execution, dict):
            continue
        component_id = str(component.get("id") or "")
        shared = len(component_tasks.get(component_id, set())) > 1
        if "shared_implementation" not in execution or (
            shared and execution.get("shared_implementation") is not True
        ):
            execution["shared_implementation"] = shared
            warnings.append(
                ValidationIssue(
                    f"$.components[{index}].execution.shared_implementation",
                    f"derived shared implementation flag as {shared}",
                )
            )

    document["consistency_groups"] = normalized_groups


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if not isinstance(value, list):
        return []
    return list(
        dict.fromkeys(
            str(item).strip()
            for item in value
            if isinstance(item, (str, int, float)) and str(item).strip()
        )
    )


def _extend_unique(target: list[str], additions: list[str]) -> None:
    for item in additions:
        if item not in target:
            target.append(item)


def _normalize_invariants(
    document: dict[str, Any],
    warnings: list[ValidationIssue],
    errors: list[ValidationIssue],
) -> None:
    raw_invariants = document.get("invariants")
    if raw_invariants is None:
        document["invariants"] = []
        return
    if not isinstance(raw_invariants, list):
        document["invariants"] = []
        warnings.append(
            ValidationIssue(
                "$.invariants",
                "ignored malformed advisory invariant collection",
            )
        )
        return
    invariants = _dict_items(raw_invariants)
    if len(invariants) != len(raw_invariants):
        document["invariants"] = invariants
        warnings.append(
            ValidationIssue(
                "$.invariants",
                "ignored non-object advisory invariants",
            )
        )
    allowed_kinds = {
        "reference",
        "shape",
        "unit",
        "normalization",
        "global_override",
        "consistency",
        "foundation_ownership",
        "other",
    }
    known_fields = {
        "id",
        "kind",
        "subjects",
        "task_ids",
        "severity",
        "description",
        "expression",
        "basis",
    }
    for index, invariant in enumerate(invariants):
        base = f"$.invariants[{index}]"
        if not str(invariant.get("id") or "").strip():
            invariant["id"] = f"invariant_{index + 1}"
            warnings.append(ValidationIssue(f"{base}.id", "assigned an advisory invariant id"))
        kind = str(invariant.get("kind") or "other").strip().casefold()
        if kind not in allowed_kinds:
            kind = "other"
            warnings.append(
                ValidationIssue(f"{base}.kind", "treated unknown invariant kind as advisory")
            )
        invariant["kind"] = kind
        for field in ("subjects", "task_ids"):
            normalized_refs = _string_list(invariant.get(field))
            if normalized_refs != invariant.get(field):
                warnings.append(
                    ValidationIssue(f"{base}.{field}", "normalized advisory references")
                )
            invariant[field] = normalized_refs
        if invariant.get("severity") not in {"error", "warning"}:
            invariant["severity"] = "warning"
            warnings.append(
                ValidationIssue(f"{base}.severity", "defaulted advisory severity to warning")
            )
        for field in ("description", "expression"):
            value = invariant.get(field)
            invariant[field] = value if isinstance(value, str) else str(value or "")
        if invariant.get("basis") is not None and not isinstance(
            invariant.get("basis"), (str, dict)
        ):
            invariant["basis"] = None
            warnings.append(
                ValidationIssue(f"{base}.basis", "marked malformed invariant basis unresolved")
            )
        _normalize_basis(invariant, base, warnings, errors)
        _normalize_advisory_basis(invariant["basis"], base, warnings)
        for field in sorted(set(invariant) - known_fields):
            invariant.pop(field, None)
            warnings.append(
                ValidationIssue(f"{base}.{field}", "ignored unknown advisory metadata")
            )


def _normalize_advisory_basis(
    basis: dict[str, Any],
    base: str,
    warnings: list[ValidationIssue],
) -> None:
    if basis.get("status") not in {
        "paper_explicit",
        "paper_derived",
        "assumed",
        "unresolved",
    }:
        basis["status"] = "unresolved"
        warnings.append(
            ValidationIssue(f"{base}.basis.status", "marked advisory basis unresolved")
        )
    raw_facts = basis.get("evidence_facts")
    normalized_facts: list[dict[str, str]] = []
    for fact in (raw_facts if isinstance(raw_facts, list) else []):
        if not isinstance(fact, dict) or not str(fact.get("name") or "").strip():
            continue
        fact_type = str(fact.get("type") or "other")
        normalized_facts.append(
            {
                "type": fact_type if fact_type in _FACT_TYPES else "other",
                "name": str(fact.get("name")).strip(),
            }
        )
    if normalized_facts != raw_facts:
        basis["evidence_facts"] = normalized_facts
        warnings.append(
            ValidationIssue(
                f"{base}.basis.evidence_facts",
                "normalized advisory evidence references",
            )
        )
    assumption_refs = _string_list(basis.get("assumption_refs"))
    if assumption_refs != basis.get("assumption_refs"):
        basis["assumption_refs"] = assumption_refs
        warnings.append(
            ValidationIssue(
                f"{base}.basis.assumption_refs",
                "normalized advisory assumption references",
            )
        )
    if not isinstance(basis.get("note"), str):
        basis["note"] = str(basis.get("note") or "")
        warnings.append(
            ValidationIssue(f"{base}.basis.note", "normalized advisory basis note")
        )


def _protected_item_issues(
    list_name: str,
    before_raw: Any,
    after_raw: Any,
    protected_fields: tuple[str, ...],
) -> list[ValidationIssue]:
    before_items = _dict_items(before_raw)
    after_items = _dict_items(after_raw)
    after_by_id = {
        str(item.get("id") or ""): item
        for item in after_items
        if str(item.get("id") or "")
    }
    issues: list[ValidationIssue] = []
    after_by_task_id = {
        str(item.get("task_id") or ""): item
        for item in after_items
        if str(item.get("task_id") or "")
    }
    for index, prior_item in enumerate(before_items):
        prior_id = str(prior_item.get("id") or "")
        current_item: dict[str, Any] | None = None
        if list_name in {"quantities", "components"} and prior_id:
            current_item = after_by_id.get(prior_id)
            if current_item is None:
                issues.append(
                    ValidationIssue(
                        f"$.{list_name}[{index}].id",
                        f"repair removed or renamed existing {list_name[:-1]} id {prior_id!r}",
                    )
                )
                continue
        elif (
            list_name == "bindings"
            and str(prior_item.get("task_id") or "") in after_by_task_id
        ):
            current_item = after_by_task_id[str(prior_item.get("task_id") or "")]
        elif index < len(after_items):
            current_item = after_items[index]
        if current_item is None:
            issues.append(
                ValidationIssue(
                    f"$.{list_name}[{index}]",
                    "repair removed an item containing existing scientific values",
                )
            )
            continue
        for field in protected_fields:
            prior_value = prior_item.get(field)
            if _empty(prior_value):
                continue
            ignored = {"shared_implementation"} if field == "execution" else set()
            issues.extend(
                _existing_value_issues(
                    prior_value,
                    current_item.get(field),
                    f"$.{list_name}[{index}].{field}",
                    ignored_fields=ignored,
                )
            )
    return issues




def _existing_value_issues(
    before: Any,
    after: Any,
    path: str,
    *,
    ignored_fields: set[str] | None = None,
) -> list[ValidationIssue]:
    ignored = ignored_fields or set()
    if isinstance(before, dict):
        if not isinstance(after, dict):
            return [ValidationIssue(path, "repair replaced existing scientific values")]
        issues: list[ValidationIssue] = []
        for field, prior_value in before.items():
            if field in ignored or _empty(prior_value):
                continue
            field_path = f"{path}.{field}"
            if field not in after:
                issues.append(
                    ValidationIssue(field_path, "repair removed an existing scientific value")
                )
                continue
            issues.extend(
                _existing_value_issues(
                    prior_value,
                    after.get(field),
                    field_path,
                    ignored_fields=ignored,
                )
            )
        return issues
    if not _scientifically_equivalent(before, after):
        return [ValidationIssue(path, "repair changed an existing scientific value")]
    return []


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


def _empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _issues_from_meta(payload: Any, key: str) -> list[ValidationIssue]:
    if not isinstance(payload, dict) or payload.get("normalizer_version") != _NORMALIZER_VERSION:
        return []
    result: list[ValidationIssue] = []
    raw = payload.get(key)
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, dict) and isinstance(item.get("path"), str) and isinstance(item.get("message"), str):
            result.append(ValidationIssue(item["path"], item["message"]))
    return result


def _dict_items(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _group_warnings(issues: list[ValidationIssue]) -> list[ValidationIssue]:
    groups: dict[tuple[str, str], list[str]] = {}
    for issue in issues:
        template = re.sub(r"\[\d+\]", "[*]", issue.path)
        groups.setdefault((template, issue.message), []).append(issue.path)
    result: list[ValidationIssue] = []
    for (path, message), examples in groups.items():
        unique_examples = list(dict.fromkeys(examples))
        if len(unique_examples) > 1:
            sample = ", ".join(unique_examples[:3])
            message = f"{message} ({len(unique_examples)} occurrences; examples: {sample})"
        result.append(ValidationIssue(path, message))
    return _dedupe(result)


def _dedupe(issues: list[ValidationIssue]) -> list[ValidationIssue]:
    result: list[ValidationIssue] = []
    seen: set[tuple[str, str]] = set()
    for issue in issues:
        key = (issue.path, issue.message)
        if key not in seen:
            seen.add(key)
            result.append(issue)
    return result
