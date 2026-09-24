"""Writable task-Writer environments layered over one shared science runtime."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .case_runtime_contracts import CaseRuntime
from .config import get_config_value
from .outputs import write_json


_MARKER = ".geng_writer_environment.json"
_LOCK = "writer_environment.lock.json"


def _python_in_venv(root: Path) -> Path:
    return root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def writer_environment_root(sandbox: Path, runtime: CaseRuntime) -> Path:
    configured = get_config_value("GENG_WRITER_ENVS_ROOT")
    parent = Path(configured).expanduser() if configured else runtime.lock_path.parent / "03c_writer_envs"
    identity = hashlib.sha256(
        (str(sandbox.resolve()) + "\n" + str(runtime.python_executable.resolve())).encode("utf-8")
    ).hexdigest()[:24]
    return parent / identity


def writer_python_path(sandbox: Path, runtime: CaseRuntime) -> Path:
    return _python_in_venv(writer_environment_root(sandbox, runtime))


def _site_packages(root: Path) -> Path:
    if os.name == "nt":
        return root / "Lib" / "site-packages"
    candidates = sorted((root / "lib").glob("python*/site-packages"))
    if len(candidates) != 1:
        raise RuntimeError(f"Writer environment has no unambiguous site-packages: {root}")
    return candidates[0]


def ensure_writer_environment(sandbox: Path, runtime: CaseRuntime) -> Path:
    """Give one Writer a private install target while reusing large base packages.

    The virtual environment lives in a separate writable root granted only to
    this Writer. Its local
    site-packages precedes the read-only shared directory on sys.path, allowing
    a task-specific version to shadow a base package without changing peers.
    """

    root = writer_environment_root(sandbox, runtime)
    python = writer_python_path(sandbox, runtime)
    shared_site = _site_packages(runtime.venv_dir)
    if not shared_site.is_dir():
        raise RuntimeError(f"Shared science packages are unavailable: {shared_site}")
    created = not python.is_file()
    if created:
        root.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [str(runtime.python_executable), "-m", "venv", str(root)],
            cwd=sandbox, check=True, capture_output=True, text=True, timeout=180,
        )
    local_site = _site_packages(root)
    marker_path = root / _MARKER
    expected = {
        "schema_version": 1,
        "shared_python": str(runtime.python_executable.resolve()),
        "shared_site_packages": str(shared_site.resolve()),
        "shared_environment_hash": runtime.environment_hash,
    }
    if marker_path.is_file():
        try:
            previous = json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"Writer environment marker is unreadable: {marker_path}") from exc
        if previous.get("shared_python") != expected["shared_python"]:
            raise RuntimeError("Writer environment base Python changed; preserve the existing task environment before rebuilding")
    # UTF-8 is needed for Windows user names; Python's site module reads .pth.
    (local_site / "geng_shared_science.pth").write_text(
        str(shared_site.resolve()) + "\n", encoding="utf-8",
    )
    write_json(marker_path, expected)
    if created:
        _restore_writer_additions(sandbox, runtime, python)
    return python


def _local_distributions(root: Path) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for distribution in importlib.metadata.distributions(path=[str(_site_packages(root))]):
        name = str(distribution.metadata.get("Name") or "").strip()
        version = str(distribution.version or "").strip()
        if name.casefold() in {"pip", "setuptools", "wheel"}:
            continue
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name) and re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._+!-]*", version
        ):
            result.append({"name": name, "version": version})
    return sorted(result, key=lambda item: item["name"].casefold())


def snapshot_writer_environment(sandbox: Path, runtime: CaseRuntime) -> Path:
    root = writer_environment_root(sandbox, runtime)
    path = sandbox / _LOCK
    write_json(path, {
        "schema_version": 1,
        "writer_environment": str(root.resolve()),
        "shared_python": str(runtime.python_executable.resolve()),
        "shared_environment_hash": runtime.environment_hash,
        "local_distributions": _local_distributions(root),
    })
    return path


def _restore_writer_additions(sandbox: Path, runtime: CaseRuntime, python: Path) -> None:
    path = sandbox / _LOCK
    if not path.is_file():
        return
    lock = json.loads(path.read_text(encoding="utf-8"))
    if lock.get("shared_python") != str(runtime.python_executable.resolve()):
        return
    raw_items = lock.get("local_distributions")
    requirements: list[str] = []
    for item in raw_items if isinstance(raw_items, list) else []:
        if not isinstance(item, dict):
            continue
        name, version = str(item.get("name") or ""), str(item.get("version") or "")
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name) and re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._+!-]*", version
        ):
            requirements.append(f"{name}=={version}")
    if requirements:
        subprocess.run(
            [str(python), "-m", "pip", "install", *requirements],
            cwd=sandbox, check=True, capture_output=True, text=True, timeout=1800,
        )


def cleanup_completed_writer_environments(
    *, output_dir: Path, task_records: list[dict[str, Any]],
) -> dict[str, Any]:
    """Retire only this completed case's recorded environments."""

    configured = get_config_value("GENG_WRITER_ENVS_ROOT")
    selected_parent = Path(configured).expanduser() if configured else output_dir / "03c_writer_envs"
    if not selected_parent.is_absolute() or selected_parent == Path(selected_parent.anchor):
        return {"removed": [], "skipped": ["Writer environment root is not a safe absolute directory"]}
    parent = selected_parent.resolve()
    removed: list[str] = []
    skipped: list[str] = []
    for raw in sorted({str(record.get("sandbox") or "") for record in task_records if isinstance(record, dict)}):
        if not raw:
            continue
        sandbox = Path(raw)
        lock_path = sandbox / _LOCK
        try:
            lock = json.loads(lock_path.read_text(encoding="utf-8"))
            root = Path(str(lock["writer_environment"]))
            shared_python = Path(str(lock["shared_python"]))
            identity = hashlib.sha256(
                (str(sandbox.resolve()) + "\n" + str(shared_python.resolve())).encode("utf-8")
            ).hexdigest()[:24]
            if (root.name != identity or root.is_symlink() or root.resolve().parent != parent
                    or root.resolve() == parent):
                raise ValueError("Writer environment path is outside its configured root")
            marker = json.loads((root / _MARKER).read_text(encoding="utf-8"))
            if marker.get("shared_python") != str(shared_python.resolve()):
                raise ValueError("Writer environment marker does not match its source")
            shutil.rmtree(root)
            removed.append(str(root))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            skipped.append(f"{raw}: {type(exc).__name__}: {exc}")
    return {"removed": removed, "skipped": skipped}


def writer_environment_prompt(python: Path) -> str:
    install_command = (
        f'& "{python}" -m pip install PACKAGE' if os.name == "nt"
        else f'"{python}" -m pip install PACKAGE'
    )
    return (
        f"The selected task Python is `{python}`. Common scientific libraries "
        "come from a shared read-only base. If you need another package, install "
        f"it yourself with `{install_command}` and continue. "
        "Install only into this task environment; never install into the shared "
        "base interpreter. Record packages you added and versions in your task "
        "notes and requirements.txt so the delivered project can recreate them."
    )
