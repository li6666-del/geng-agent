"""Low-level task-writer file and JSON helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _writer_delivery_path_is_fresh(path: Path, since: float | None) -> bool:
    try:
        return path.is_file() and (since is None or path.stat().st_mtime >= since)
    except OSError:
        return False

def _read_optional_json_object(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}

def _task_result_file_path(
    sandbox: Path,
    output_subdir: str,
    filename: str,
    *,
    allow_root_fallback: bool = True,
) -> tuple[Path, bool]:
    output_path = sandbox / "outputs" / output_subdir / filename
    if not allow_root_fallback:
        return output_path, True
    root_path = sandbox / filename
    if root_path.exists():
        return root_path, False
    if output_path.exists():
        return output_path, True
    return root_path, False

def _task_source_files(sandbox: Path) -> list[Path]:
    """Return every task-owned Python module, including transitive helpers.

    A writer sandbox contains exactly one task scaffold. Copying the complete
    task package is safer than guessing a single ``<module>_lib.py`` filename
    and keeps imports such as ``from tasks import _fig6_full_dd`` intact.
    """

    return [path for path in _task_owned_files(sandbox) if path.suffix.lower() == ".py"]

def _task_owned_files(sandbox: Path) -> list[Path]:
    """Return the complete task package dependency closure without following links."""

    task_root = sandbox / "tasks"
    if not task_root.is_dir():
        return []
    safe: list[Path] = []
    task_root_resolved = task_root.resolve()
    for path in sorted(task_root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(task_root).as_posix()
        if relative == "__init__.py" or "__pycache__" in path.parts or path.suffix.lower() in {".pyc", ".pyo"}:
            continue
        try:
            path.resolve().relative_to(task_root_resolved)
        except ValueError:
            continue
        safe.append(path)
    return safe
