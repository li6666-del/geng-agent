from __future__ import annotations

from copy import deepcopy
import re
from typing import Any

from .schemas import ValidationIssue


_NORMALIZER_VERSION = 3
_BASIS_FIELDS = ("evidence_facts", "assumption_refs", "note")


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
        _normalize_basis(component, base, warnings, errors)

    bindings = _dict_items(document.get("bindings"))
    for index, binding in enumerate(bindings):
        base = f"$.bindings[{index}]"
        _move_alias(binding, "consistency_group", "consistency_group_id", base, warnings, errors)
        _move_alias(binding, "components", "component_ids", base, warnings, errors)
        _move_alias(binding, "outputs", "output_quantity_ids", base, warnings, errors)

    invariants = _dict_items(document.get("invariants"))
    for index, invariant in enumerate(invariants):
        base = f"$.invariants[{index}]"
        if "kind" not in invariant:
            invariant["kind"] = "other"
            warnings.append(ValidationIssue(f"{base}.kind", "missing machine-check kind; treated as descriptive invariant"))
        if "subjects" not in invariant:
            invariant["subjects"] = []
            warnings.append(ValidationIssue(f"{base}.subjects", "missing machine-check subjects; treated as descriptive invariant"))
        invariant.setdefault("description", "")
        invariant.setdefault("expression", "")
        _normalize_basis(invariant, base, warnings, errors)

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
    """Reject schema-repair output that changes canonical scientific content."""

    issues: list[ValidationIssue] = []
    if _architecture_projection(before) == _architecture_projection(after):
        return issues
    for field in (
        "schema_version",
        "workflow_version",
        "quantities",
        "components",
        "consistency_groups",
        "bindings",
        "invariants",
    ):
        if _architecture_projection(before).get(field) != _architecture_projection(after).get(field):
            issues.append(ValidationIssue(f"$.{field}", "format repair changed frozen scientific content"))
    return issues


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


def _architecture_projection(document: dict[str, Any]) -> dict[str, Any]:
    fields = {
        "quantities": ("id", "role", "dtype", "shape", "unit", "scale", "normalization", "scope", "default", "basis"),
        "components": (
            "id",
            "kind",
            "module",
            "callable",
            "execution",
            "inputs",
            "outputs",
            "parameters",
            "depends_on",
            "basis",
        ),
        "consistency_groups": ("id", "task_ids", "shared_quantity_ids"),
        "bindings": ("task_id", "experiment_id", "consistency_group", "components", "allowed_overrides", "overrides", "outputs"),
        "invariants": ("id", "kind", "subjects", "task_ids", "severity", "description", "expression", "basis"),
    }
    projection: dict[str, Any] = {
        "schema_version": deepcopy(document.get("schema_version")),
        "workflow_version": deepcopy(document.get("workflow_version")),
    }
    for list_name, item_fields in fields.items():
        raw_items = document.get(list_name)
        projection[list_name] = [
            {field: deepcopy(item.get(field)) for field in item_fields if field in item}
            for item in raw_items
            if isinstance(item, dict)
        ] if isinstance(raw_items, list) else deepcopy(raw_items)
    return projection


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
