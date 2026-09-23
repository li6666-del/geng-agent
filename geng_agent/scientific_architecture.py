"""Mechanical enumeration of explicitly declared Foundation files.

Architecture meaning, scientific completeness and task binding suitability are
reviewed by the project supervisor. No host semantic validator remains here.
"""
from pathlib import PurePosixPath
from typing import Any


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



def _dict_items(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []
