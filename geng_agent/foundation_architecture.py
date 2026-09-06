"""Architecture and dependency mapping for the Foundation Writer."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .case_environment import EnvironmentPolicyError, normalize_requirement
from .case_runtime import CaseRuntime, requirements_from_scientific_architecture
from .foundation_execution_policy import FRAMEWORK_EXEMPTIONS, LIBRARY_CANONICAL_NAMES


def architecture_requires_execution_contracts(architecture: dict[str, Any]) -> bool:
    match = re.fullmatch(
        r"\s*(\d+)(?:\.(\d+))?\s*",
        str(architecture.get("schema_version") or ""),
    )
    if match is None:
        return False
    return (int(match.group(1)), int(match.group(2) or 0)) >= (1, 1)


def architecture_components(architecture: dict[str, Any]) -> list[dict[str, Any]]:
    raw = architecture.get("components")
    return [component for component in raw if isinstance(component, dict)] if isinstance(raw, list) else []


def normalized_library_name(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip().casefold()
    text = re.split(r"[<>=!~;\[]", text, maxsplit=1)[0].strip()
    return re.sub(r"[-_.\s]+", "-", text)


def library_keys(value: Any) -> set[str]:
    normalized = normalized_library_name(value)
    if not normalized:
        return set()
    keys = {normalized}
    raw = str(value).strip().casefold()
    raw = re.split(r"[<>=!~;\[]", raw, maxsplit=1)[0].strip()
    if "." in raw:
        keys.add(normalized_library_name(raw.split(".", 1)[0]))
    canonical = LIBRARY_CANONICAL_NAMES.get(normalized)
    if canonical:
        keys.add(canonical)
    for alias, target in LIBRARY_CANONICAL_NAMES.items():
        if normalized == target:
            keys.add(alias)
    return keys


def requirement_name_for_library(value: Any) -> str | None:
    """Return a safe distribution name without imposing a package allow-list."""

    normalized = normalized_library_name(value)
    if not normalized or normalized in FRAMEWORK_EXEMPTIONS:
        return None
    candidate = LIBRARY_CANONICAL_NAMES.get(normalized, normalized)
    try:
        return normalize_requirement(candidate).distribution
    except EnvironmentPolicyError:
        return None


def initial_foundation_requirements(
    architecture: dict[str, Any],
    *,
    case_runtime: CaseRuntime | None = None,
) -> str:
    if case_runtime is not None:
        requirements = {
            str(item.get("requirement") or "").strip()
            for item in case_runtime.lock.get("requirements", [])
            if isinstance(item, dict)
            and item.get("applicable") is True
            and item.get("satisfied") is True
            and str(item.get("requirement") or "").strip()
        }
    else:
        requirements = {
            request.requirement
            for request in requirements_from_scientific_architecture(architecture)
        }
    return "".join(f"{requirement}\n" for requirement in sorted(requirements))


def dependency_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for child in value for item in dependency_strings(child)]
    if isinstance(value, dict):
        result = list(value)
        for child in value.values():
            result.extend(dependency_strings(child))
        return result
    return []


def architecture_dependency_names(architecture: dict[str, Any]) -> set[str]:
    dependency_fields = (
        "dependencies",
        "dependency_declarations",
        "libraries",
        "requirements",
        "supporting_libraries",
    )
    values: list[str] = []
    for container in [architecture, *architecture_components(architecture)]:
        for field in dependency_fields:
            values.extend(dependency_strings(container.get(field)))
        execution = container.get("execution")
        if isinstance(execution, dict):
            values.extend(dependency_strings(execution.get("supporting_libraries")))
            for field in ("dependencies", "dependency_declarations", "requirements"):
                values.extend(dependency_strings(execution.get(field)))
    return {key for value in values for key in library_keys(value)}


def declared_requirement_keys(sandbox: Path) -> set[str]:
    path = sandbox / "requirements.txt"
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return set()
    result: set[str] = set()
    for raw in lines:
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        match = re.match(r"([A-Za-z0-9_.-]+)", line)
        if match:
            result.update(library_keys(match.group(1)))
    return result
