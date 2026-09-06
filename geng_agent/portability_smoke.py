"""Relocated-copy and guarded offline smoke validation."""

from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from typing import Any, Mapping, Sequence

from .portability_contracts import (
    _PARENT_SEGMENT,
    _is_absolute_cross_platform,
    _issue,
    _warning,
)
from .portability_inventory import (
    build_source_inventory,
    _is_ignored_directory,
    _is_ignored_file,
    _source_inventory_issues,
)
from .portability_reference_scan import _looks_path_like

_MAX_SMOKE_TIMEOUT_SECONDS = 120.0
_LARGE_RELOCATION_COPY_BYTES = 2 * 1024 * 1024 * 1024

_SMOKE_GUARD_SOURCE = r'''from __future__ import annotations
import runpy
import sys

_PROCESS_EVENTS = (
    "os.exec", "os.posix_spawn", "os.spawn", "os.startfile",
)

def _audit(event, args):
    del args
    if event.startswith("socket."):
        raise PermissionError("portability smoke guard: network access is disabled")
    if (
        event == "subprocess.Popen"
        or event == "os.system"
        or event in {"os.fork", "os.forkpty", "pty.fork", "pty.spawn"}
        or any(event.startswith(prefix) for prefix in _PROCESS_EVENTS)
    ):
        raise PermissionError("portability smoke guard: process launch is disabled")

sys.addaudithook(_audit)
mode, target, *arguments = sys.argv[1:]
sys.argv = [target, *arguments]
if mode == "module":
    runpy.run_module(target, run_name="__main__", alter_sys=True)
else:
    runpy.run_path(target, run_name="__main__")
'''

def _run_relocated_smoke(
    root: Path,
    *,
    inventory: Mapping[str, Any],
    python_executable: str | Path | None,
    smoke_command: Sequence[str] | None,
    timeout_s: float,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    issues: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    declared_command = list(smoke_command) if smoke_command is not None and not isinstance(smoke_command, str) else None
    command_source = "explicit"
    if declared_command is None:
        declared_command = _discover_smoke_command(root)
        command_source = "manifest"
    if not declared_command or not all(isinstance(item, str) and item.strip() for item in declared_command):
        return (
            {"requested": True, "ran": False, "command_source": command_source, "status": "failed"},
            [_issue("smoke_command_missing", ".", "no explicit lightweight smoke command is declared")],
            warnings,
        )

    executable = str(Path(python_executable).resolve()) if python_executable is not None else sys.executable
    if not Path(executable).is_file():
        return (
            {"requested": True, "ran": False, "command_source": command_source, "status": "failed"},
            [_issue("smoke_python_missing", ".", "selected Python interpreter does not exist")],
            warnings,
        )
    try:
        timeout = min(max(float(timeout_s), 1.0), _MAX_SMOKE_TIMEOUT_SECONDS)
    except (TypeError, ValueError):
        return (
            {"requested": True, "ran": False, "command_source": command_source, "status": "failed"},
            [_issue("smoke_timeout_invalid", ".", "smoke timeout must be a positive number")],
            warnings,
        )
    inventory_files = inventory.get("files")
    file_count = len(inventory_files) if isinstance(inventory_files, list) else 0
    copy_bytes = sum(
        int(item.get("size") or 0)
        for item in inventory_files or []
        if isinstance(item, Mapping) and isinstance(item.get("size"), int)
    )
    copy_audit = {
        "copy_file_count": file_count,
        "copy_bytes": copy_bytes,
        "copy_large": copy_bytes >= _LARGE_RELOCATION_COPY_BYTES,
        "environment_policy": "isolated_minimal",
    }
    with TemporaryDirectory(prefix="geng-project-portability-") as temporary:
        copied_root = Path(temporary) / "project"
        isolated_home = Path(temporary) / "runtime-home"
        isolated_home.mkdir()
        try:
            shutil.copytree(root, copied_root, symlinks=False, ignore=_copy_ignore)
        except OSError as exc:
            smoke = {
                "requested": True,
                "ran": False,
                "command_source": command_source,
                "status": "inconclusive",
                "verified": False,
                "infrastructure_reason": "relocation_copy_failed",
                "infrastructure_detail": str(exc),
                **copy_audit,
            }
            warnings.append(
                _warning(
                    "relocation_copy_inconclusive",
                    ".",
                    "relocation copy could not be completed in this validation environment",
                    detail=str(exc),
                )
            )
            return smoke, issues, warnings
        copied_inventory = build_source_inventory(copied_root)
        copied_inventory_issues = _source_inventory_issues(copied_root, copied_inventory)
        if (
            copied_inventory.get("inventory_sha256") != inventory.get("inventory_sha256")
            or copied_inventory_issues
        ):
            return (
                {
                    "requested": True,
                    "ran": False,
                    "command_source": command_source,
                    "status": "failed",
                    **copy_audit,
                },
                [_issue("relocation_copy_mismatch", ".", "copied project does not match the source inventory")],
                warnings,
            )

        argv, command_issue = _safe_python_command(copied_root, declared_command, executable)
        if command_issue is not None:
            return (
                {
                    "requested": True,
                    "ran": False,
                    "command_source": command_source,
                    "status": "failed",
                    **copy_audit,
                },
                [command_issue],
                warnings,
            )
        guard_path = copied_root / ".geng_portability_smoke_guard.py"
        guard_path.write_text(_SMOKE_GUARD_SOURCE, encoding="utf-8", newline="\n")
        guarded_argv = _guarded_smoke_command(argv, guard_path, executable)
        environment = _offline_smoke_environment(isolated_home)
        try:
            completed = subprocess.run(
                guarded_argv,
                cwd=copied_root,
                env=environment,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            smoke = {
                "requested": True,
                "ran": True,
                "command_source": command_source,
                "command": _display_command(argv, executable),
                "timeout_s": timeout,
                "timed_out": True,
                "status": "inconclusive",
                "verified": False,
                "infrastructure_reason": "timeout",
                **copy_audit,
            }
            warnings.append(
                _warning(
                    "smoke_timeout_inconclusive",
                    ".",
                    f"relocated smoke run exceeded the audit budget of {timeout:g} seconds",
                )
            )
            return smoke, issues, warnings
        except OSError as exc:
            return (
                {
                    "requested": True,
                    "ran": False,
                    "command_source": command_source,
                    "command": _display_command(argv, executable),
                    "status": "failed",
                    **copy_audit,
                },
                [_issue("smoke_launch_failed", ".", f"relocated smoke command could not start: {exc}")],
                warnings,
            )
        smoke = {
            "requested": True,
            "ran": True,
            "command_source": command_source,
            "command": _display_command(argv, executable),
            "timeout_s": timeout,
            "returncode": completed.returncode,
            "stdout_tail": completed.stdout[-4000:],
            "stderr_tail": completed.stderr[-4000:],
            "status": "passed" if completed.returncode == 0 else "failed",
            "verified": completed.returncode == 0,
            **copy_audit,
        }
        if completed.returncode != 0:
            aggregate_summary = _read_smoke_aggregate_summary(copied_root)
            if aggregate_summary is not None:
                smoke["aggregate_summary"] = aggregate_summary
            issues.append(
                _issue(
                    "relocated_smoke_failed",
                    ".",
                    f"relocated smoke command exited with status {completed.returncode}",
                )
            )
        return smoke, issues, warnings

def _discover_smoke_command(root: Path) -> list[str] | None:
    for name in (
        "reproducibility_manifest.json",
        "package_manifest.json",
        "project_manifest.json",
        "project_portability_manifest.json",
        "repro_project_manifest.json",
    ):
        path = root / name
        if not path.is_file() or path.is_symlink():
            continue
        try:
            document = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        candidates = [
            document.get("smoke_command") if isinstance(document, Mapping) else None,
            _nested_value(document, "commands", "smoke"),
            _nested_value(document, "validation", "smoke_command"),
            _nested_value(document, "_meta", "smoke_command"),
        ]
        for candidate in candidates:
            if isinstance(candidate, Mapping):
                candidate = candidate.get("argv")
            if isinstance(candidate, list) and all(isinstance(item, str) for item in candidate):
                return list(candidate)
    return None

def _nested_value(value: Any, *keys: str) -> Any:
    current = value
    for key in keys:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current

def _safe_python_command(
    copied_root: Path,
    declared: Sequence[str],
    executable: str,
) -> tuple[list[str], dict[str, Any] | None]:
    argv = list(declared)
    first = Path(argv[0]).name.casefold()
    if argv[0] in {"{python}", "${python}"} or first in {"python", "python.exe", "python3", "python3.exe"}:
        argv[0] = executable
    elif argv[0].endswith(".py"):
        argv.insert(0, executable)
    elif Path(argv[0]).resolve() != Path(executable).resolve():
        return [], _issue("unsafe_smoke_command", ".", "smoke command must use the selected Python interpreter")

    lowered = [item.casefold() for item in argv[1:]]
    if "-c" in lowered or any(item in {"pip", "conda"} for item in lowered):
        return [], _issue("unsafe_smoke_command", ".", "inline code and installers are not allowed in a smoke command")
    if not any("smoke" in item for item in lowered):
        return [], _issue("unsafe_smoke_command", ".", "smoke command must explicitly select a smoke script or configuration")

    root_resolved = copied_root.resolve()
    module_index = 2 if len(argv) > 2 and argv[1] == "-m" else -1
    for index, item in enumerate(argv[1:], start=1):
        if index == module_index:
            continue
        if not item or item.startswith("-") or not _looks_path_like(item):
            continue
        if _is_absolute_cross_platform(item) or _PARENT_SEGMENT.search(item.replace("\\", "/")):
            return [], _issue("unsafe_smoke_command", ".", f"smoke argument escapes the copied project: {item}")
        candidate = (copied_root / PurePosixPath(item.replace("\\", "/"))).resolve(strict=False)
        try:
            candidate.relative_to(root_resolved)
        except ValueError:
            return [], _issue("unsafe_smoke_command", ".", f"smoke argument escapes the copied project: {item}")
        if candidate.suffix and not candidate.exists():
            return [], _issue("smoke_input_missing", ".", f"smoke command references a missing file: {item}")
    return argv, None

def _guarded_smoke_command(argv: Sequence[str], guard_path: Path, executable: str) -> list[str]:
    arguments = list(argv[1:])
    if arguments and arguments[0] == "-m":
        return [executable, str(guard_path), "module", arguments[1], *arguments[2:]]
    return [executable, str(guard_path), "path", arguments[0], *arguments[1:]]

def _offline_smoke_environment(isolated_home: Path) -> dict[str, str]:
    """Return a deliberately minimal environment with no host cache injection."""

    environment: dict[str, str] = {}
    # Keep only values needed to start CPython on common platforms.  In
    # particular PATH, PYTHONPATH, DATA_ROOT and arbitrary application variables
    # are not inherited.
    for key in ("COMSPEC", "LANG", "LC_ALL", "PATHEXT", "SYSTEMROOT", "TZ", "WINDIR"):
        value = os.environ.get(key)
        if value:
            environment[key] = value

    cache_home = isolated_home / "cache"
    config_home = isolated_home / "config"
    data_home = isolated_home / "data"
    temp_home = isolated_home / "tmp"
    for directory in (cache_home, config_home, data_home, temp_home):
        directory.mkdir(parents=True, exist_ok=True)
    environment.update(
        {
            "ALL_PROXY": "http://127.0.0.1:9",
            "GENG_AGENT_OFFLINE": "1",
            "HF_HOME": str(cache_home / "huggingface"),
            "HF_HUB_OFFLINE": "1",
            "HOME": str(isolated_home),
            "HTTP_PROXY": "http://127.0.0.1:9",
            "HTTPS_PROXY": "http://127.0.0.1:9",
            "LNAME": "geng-case-runtime",
            "LOGNAME": "geng-case-runtime",
            "MPLCONFIGDIR": str(cache_home / "matplotlib"),
            "NO_PROXY": "",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONNOUSERSITE": "1",
            "PYTHONUTF8": "1",
            "TEMP": str(temp_home),
            "TMP": str(temp_home),
            "TMPDIR": str(temp_home),
            "TORCH_HOME": str(cache_home / "torch"),
            "TORCHINDUCTOR_CACHE_DIR": str(cache_home / "torchinductor"),
            "TRANSFORMERS_OFFLINE": "1",
            # Some standard-library and scientific-library probes call
            # getpass.getuser().  Give them a synthetic isolated identity rather
            # than leaking the host account or making Windows fall through to
            # the unavailable POSIX ``pwd`` module.
            "USER": "geng-case-runtime",
            "USERNAME": "geng-case-runtime",
            "USERPROFILE": str(isolated_home),
            "WANDB_MODE": "offline",
            "XDG_CACHE_HOME": str(cache_home),
            "XDG_CONFIG_HOME": str(config_home),
            "XDG_DATA_HOME": str(data_home),
        }
    )
    return environment


def _read_smoke_aggregate_summary(copied_root: Path) -> dict[str, Any] | None:
    """Preserve task-level smoke failures before the relocated copy is removed."""

    summary_path = copied_root / "outputs" / "summary.json"
    if not summary_path.is_file() or summary_path.is_symlink():
        return None
    try:
        document = json.loads(summary_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return dict(document) if isinstance(document, Mapping) else None

def _copy_ignore(directory: str, names: list[str]) -> set[str]:
    del directory
    return {
        name
        for name in names
        if _is_ignored_directory(name) or _is_ignored_file(Path(name))
    }

def _display_command(argv: Sequence[str], executable: str) -> list[str]:
    return ["{python}" if Path(item) == Path(executable) else item for item in argv]
