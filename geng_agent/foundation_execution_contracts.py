"""Top-level Foundation execution-contract validation."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from .foundation_architecture import (
    architecture_components as _architecture_components,
    architecture_dependency_names as _architecture_dependency_names,
    architecture_requires_execution_contracts as _architecture_requires_execution_contracts,
    declared_requirement_keys as _declared_requirement_keys,
    library_keys as _library_keys,
    requirement_name_for_library as _requirement_name_for_library,
)
from .foundation_bindings import (
    _binding_is_trivial_external_stub,
    _component_framework_import_keys,
    _declared_callable_binding,
    _foundation_project_import_keys,
    _foundation_source_trees,
    _framework_is_external,
    _validate_declared_callable,
)
from .foundation_capability_evidence import (
    _capability_test_passed,
    _contract_values_equal,
    _execution_requires_trusted_capability_probe,
    _framework_has_trusted_capability_probe,
    _required_component_capabilities,
)
from .foundation_execution_policy import (
    CAPABILITY_GROUPS as _CAPABILITY_GROUPS,
    MATERIAL_EXECUTION_FIELDS as _MATERIAL_EXECUTION_FIELDS,
    TRUSTED_EXTERNAL_RUNTIME_ADAPTERS as _TRUSTED_EXTERNAL_RUNTIME_ADAPTERS,
)
from .foundation_test_catalog import (
    _capability_matches_group,
    _capability_status_passed,
    _delivered_test_references,
    _normalized_capability,
)


def _external_runtime_command(execution: dict[str, Any]) -> str:
    framework = str(execution.get("primary_framework") or "").strip()
    tokens = set(_normalized_capability(framework).split("-"))
    token_commands = {
        "julia": "julia",
        "matlab": "matlab",
        "octave": "octave",
        "rscript": "Rscript",
        "wolfram": "wolframscript",
    }
    for token, command in token_commands.items():
        if token in tokens:
            return command
    if tokens == {"r"}:
        return "R"
    return framework


def _external_runtime_available(execution: dict[str, Any]) -> tuple[str, str | None]:
    command = _external_runtime_command(execution)
    if not command:
        return "", None
    try:
        return command, shutil.which(command)
    except (OSError, TypeError, ValueError):
        return command, None


def _trusted_external_runtime_adapter(execution: dict[str, Any]) -> str | None:
    framework = _normalized_capability(execution.get("primary_framework"))
    return _TRUSTED_EXTERNAL_RUNTIME_ADAPTERS.get(framework)


def validate_foundation_execution_contracts(
    *,
    sandbox: Path,
    architecture: dict[str, Any],
    result: dict[str, Any],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Statically enforce schema 1.1 execution contracts without importing generated code."""

    if not _architecture_requires_execution_contracts(architecture):
        return [], []

    issues: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    components = _architecture_components(architecture)
    trees, tree_issues = _foundation_source_trees(sandbox)
    issues.extend(tree_issues)
    project_keys = _foundation_project_import_keys(sandbox)
    declared_keys = _declared_requirement_keys(sandbox) | _architecture_dependency_names(architecture)

    for component in components:
        component_id = str(component.get("id") or "<unknown>")
        execution = component.get("execution")
        if not isinstance(execution, dict):
            issues.append(
                {
                    "file": "scientific_architecture.json",
                    "message": f"component {component_id} has no schema 1.1 execution contract",
                }
            )
            continue
        callable_issues, callable_warnings = _validate_declared_callable(
            component=component,
            trees=trees,
        )
        issues.extend(callable_issues)
        warnings.extend(callable_warnings)
        if str(execution.get("device_policy") or "").strip().casefold() == "external_runtime":
            declared_capabilities = (
                execution.get("required_capabilities")
                if isinstance(execution.get("required_capabilities"), list)
                else []
            )
            for label in (
                "external runtime availability",
                "external runtime invocation interface",
            ):
                if any(
                    _capability_matches_group(
                        capability,
                        _CAPABILITY_GROUPS[label],
                    )
                    for capability in declared_capabilities
                ):
                    continue
                issues.append(
                    {
                        "file": "scientific_architecture.json",
                        "message": (
                            f"component {component_id} external_runtime contract must declare "
                            f"{label} in required_capabilities"
                        ),
                    }
                )
            runtime_command, resolved_runtime = _external_runtime_available(execution)
            if resolved_runtime is None:
                issues.append(
                    {
                        "file": "scientific_architecture.json",
                        "message": (
                            f"component {component_id} has an external runtime host capability gap: "
                            f"command {runtime_command or '<unspecified>'!r} was not found by the "
                            "trusted host executable probe"
                        ),
                    }
                )
            if _trusted_external_runtime_adapter(execution) is None:
                issues.append(
                    {
                        "file": "scientific_architecture.json",
                        "message": (
                            f"component {component_id} environment_extension_required: "
                            "no trusted host invocation adapter is registered for external runtime "
                            f"{execution.get('primary_framework')!r}"
                        ),
                    }
                )
            callable_binding = _declared_callable_binding(
                component=component,
                trees=trees,
            )
            if _binding_is_trivial_external_stub(callable_binding):
                issues.append(
                    {
                        "file": str(component.get("module") or "src"),
                        "message": (
                            f"component {component_id} external runtime callable is a "
                            "constant/identity stub and cannot prove runtime invocation"
                        ),
                    }
                )
            continue
        framework = execution.get("primary_framework")
        if (
            _execution_requires_trusted_capability_probe(execution)
            and not _framework_has_trusted_capability_probe(framework)
        ):
            issues.append(
                {
                    "file": "scientific_architecture.json",
                    "message": (
                        f"component {component_id} environment_extension_required: no trusted "
                        "training/gradient/checkpoint/device capability probe is registered for "
                        f"framework {framework!r}"
                    ),
                }
            )
        if not _framework_is_external(framework, project_keys):
            continue
        if _requirement_name_for_library(framework) is None:
            issues.append(
                {
                    "file": "requirements.txt",
                    "message": (
                        f"component {component_id} environment_extension_required: framework "
                        f"{framework!r} is not a safe ordinary PEP 508 distribution request"
                    ),
                }
            )
            continue
        framework_keys = _library_keys(framework)
        if not framework_keys & declared_keys:
            issues.append(
                {
                    "file": "requirements.txt",
                    "message": (
                        f"component {component_id} primary framework {framework!r} is not declared "
                        "in requirements.txt or architecture dependency metadata"
                    ),
                }
            )
        component_import_keys = _component_framework_import_keys(
            trees,
            str(component.get("module") or "").strip().replace(chr(92), "/"),
        )
        if not framework_keys & component_import_keys:
            issues.append(
                {
                    "file": str(component.get("module") or "src"),
                    "message": (
                        f"component {component_id} primary framework {framework!r} is never "
                        "imported by Foundation-owned source"
                    ),
                }
            )

    raw_contracts = result.get("execution_contracts")
    contracts = (
        [item for item in raw_contracts if isinstance(item, dict)]
        if isinstance(raw_contracts, list)
        else []
    )
    if not isinstance(raw_contracts, list):
        issues.append(
            {
                "file": "foundation_result.json",
                "message": "schema 1.1 Foundation hand-off must contain an execution_contracts array",
            }
        )
    contracts_by_component: dict[str, list[dict[str, Any]]] = {}
    for contract in contracts:
        contracts_by_component.setdefault(
            str(contract.get("component_id") or ""),
            [],
        ).append(contract)

    for component in components:
        component_id = str(component.get("id") or "")
        matches = contracts_by_component.get(component_id, [])
        if not matches:
            issues.append(
                {
                    "file": "foundation_result.json",
                    "message": (
                        "missing execution_contracts record for component "
                        f"{component_id or '<unknown>'}"
                    ),
                }
            )
            continue
        if len(matches) > 1:
            issues.append(
                {
                    "file": "foundation_result.json",
                    "message": f"duplicate execution_contracts records for component {component_id}",
                }
            )
        contract = matches[0]
        for field in ("module", "callable"):
            if not _contract_values_equal(component.get(field), contract.get(field)):
                issues.append(
                    {
                        "file": "foundation_result.json",
                        "message": (
                            f"component {component_id} execution contract does not match "
                            f"architecture {field}"
                        ),
                    }
                )
        expected_execution = component.get("execution")
        actual_execution = contract.get("execution")
        if not isinstance(expected_execution, dict) or not isinstance(actual_execution, dict):
            issues.append(
                {
                    "file": "foundation_result.json",
                    "message": (
                        f"component {component_id} execution contract must contain an execution object"
                    ),
                }
            )
            continue
        for field in _MATERIAL_EXECUTION_FIELDS:
            if not _contract_values_equal(
                expected_execution.get(field),
                actual_execution.get(field),
            ):
                issues.append(
                    {
                        "file": "foundation_result.json",
                        "message": (
                            f"component {component_id} execution contract weakens or changes {field}"
                        ),
                    }
                )
        if not _contract_values_equal(
            expected_execution.get("rationale"),
            actual_execution.get("rationale"),
        ):
            warnings.append(
                {
                    "file": "foundation_result.json",
                    "message": f"component {component_id} execution rationale was not copied exactly",
                    "severity": "warning",
                }
            )

    known_ids = {str(component.get("id") or "") for component in components}
    for component_id in sorted(set(contracts_by_component) - known_ids):
        warnings.append(
            {
                "file": "foundation_result.json",
                "message": (
                    f"execution_contracts contains unknown component {component_id or '<empty>'}"
                ),
                "severity": "warning",
            }
        )

    raw_capability_tests = result.get("capability_tests")
    capability_tests = (
        [item for item in raw_capability_tests if isinstance(item, dict)]
        if isinstance(raw_capability_tests, list)
        else []
    )
    delivered_tests = _delivered_test_references(sandbox)
    components_by_id = {
        str(component.get("id") or ""): component
        for component in components
    }
    for item in capability_tests:
        component = components_by_id.get(str(item.get("component_id") or ""))
        if _capability_status_passed(item) and not _capability_test_passed(
            item,
            delivered_tests,
            component=component,
        ):
            issues.append(
                {
                    "file": "foundation_result.json",
                    "message": (
                        "passing capability_tests record is not bound to its declared "
                        "component module/callable and a substantive delivered unittest method"
                    ),
                }
            )
    expected_capability_count = sum(
        len(_required_component_capabilities(component))
        for component in components
    )
    if expected_capability_count and not isinstance(raw_capability_tests, list):
        issues.append(
            {
                "file": "foundation_result.json",
                "message": "required execution capabilities need a capability_tests array",
            }
        )

    for component in components:
        component_id = str(component.get("id") or "")
        component_tests = [
            item
            for item in capability_tests
            if str(item.get("component_id") or "") == component_id
        ]
        for label, accepted, group_match in _required_component_capabilities(component):
            matching = [
                item
                for item in component_tests
                if (
                    _capability_matches_group(item.get("capability"), accepted)
                    if group_match
                    else _normalized_capability(item.get("capability")) in accepted
                )
            ]
            if not any(
                _capability_test_passed(
                    item,
                    delivered_tests,
                    component=component,
                    label=label,
                )
                for item in matching
            ):
                issues.append(
                    {
                        "file": "foundation_result.json",
                        "message": (
                            f"component {component_id} lacks passing capability_tests "
                            f"evidence for {label}"
                        ),
                    }
                )

    for item in capability_tests:
        component_id = str(item.get("component_id") or "")
        if component_id not in known_ids:
            warnings.append(
                {
                    "file": "foundation_result.json",
                    "message": (
                        f"capability_tests contains unknown component {component_id or '<empty>'}"
                    ),
                    "severity": "warning",
                }
            )
    return issues, warnings
