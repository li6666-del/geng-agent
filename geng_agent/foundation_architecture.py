"""Read declared Foundation components and the resolved dependency lock."""
from __future__ import annotations
from typing import Any
from .case_runtime import CaseRuntime, requirements_from_scientific_architecture


def architecture_components(architecture: dict[str, Any]) -> list[dict[str, Any]]:
    raw = architecture.get("components")
    return [component for component in raw if isinstance(component, dict)] if isinstance(raw, list) else []


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
