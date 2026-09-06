"""Launch scientific processes inside Codex's OS sandbox without calling a model.

This preserves native write isolation. Broad OS read access remains compatible
with the existing Windows backend; the Python guard still restricts reads and
network operations. It is not a claim of native-code read isolation.
"""
from __future__ import annotations

from functools import lru_cache
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any, Mapping, Sequence

from .security_env import build_safe_env


class ScientificSandboxUnavailable(RuntimeError):
    pass


def _codex_executable(explicit: str | Path | None) -> Path:
    candidate = str(explicit or os.environ.get("GENG_CODEX_SANDBOX_EXE") or shutil.which("codex.exe") or shutil.which("codex") or "")
    path = Path(candidate)
    if os.name == "nt" and path.suffix.lower() in {".cmd", ".ps1"}:
        package = path.parent / "node_modules" / "@openai" / "codex"
        architecture = "arm64" if "arm" in os.environ.get("PROCESSOR_ARCHITECTURE", "").lower() else "x64"
        target = "aarch64" if architecture == "arm64" else "x86_64"
        candidates = [package / "node_modules" / "@openai" / f"codex-win32-{architecture}" / "vendor" / f"{target}-pc-windows-msvc" / "bin" / "codex.exe",
                      package / "vendor" / f"{target}-pc-windows-msvc" / "bin" / "codex.exe"]
        path = next((item for item in candidates if item.is_file()), path)
    if not candidate or not path.is_file() or (os.name == "nt" and path.suffix.lower() != ".exe"):
        raise ScientificSandboxUnavailable("Native Codex sandbox CLI is unavailable; set GENG_CODEX_SANDBOX_EXE to its executable. Scientific execution was not started.")
    return path.resolve()


@lru_cache(maxsize=8)
def _sandbox_prefix(executable: str, modified_ns: int, platform: str) -> tuple[str, ...]:
    prefix = [executable, "sandbox"]
    try:
        result = subprocess.run([*prefix, "--help"], capture_output=True, text=True,
                                env=build_safe_env(), timeout=15, check=True)
        help_text = result.stdout + result.stderr
        if "Commands:" in help_text:
            subcommand = "windows" if platform == "win32" else "macos" if platform == "darwin" else "linux"
            prefix.append(subcommand)
            result = subprocess.run([*prefix, "--help"], capture_output=True, text=True,
                                    env=build_safe_env(), timeout=15, check=True)
            help_text = result.stdout + result.stderr
        if "-P," not in help_text or "--config" not in help_text or "--cd" not in help_text:
            raise ScientificSandboxUnavailable("Codex sandbox does not support explicit filesystem permission profiles; update the CLI before scientific execution.")
        return tuple(prefix)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ScientificSandboxUnavailable(f"Codex sandbox capability check failed: {exc}") from exc


def scientific_sandbox_launch(command: Sequence[str], *, work_dir: Path,
                              write_roots: Sequence[Path], env: Mapping[str, str],
                              codex_executable: str | Path | None = None) -> dict[str, Any]:
    """Return command/env/policy for Popen; there is no unsandboxed fallback."""
    root = work_dir.resolve(strict=True)
    roots = []
    for item in write_roots:
        path = item.resolve(strict=True)
        if path == root or not path.is_relative_to(root) or item.is_symlink():
            raise ValueError("Scientific write roots must be specific directories inside the task workspace")
        roots.append(str(path))
    if not roots or not command:
        raise ValueError("Scientific execution needs a command and explicit write directories")
    executable = _codex_executable(codex_executable)
    prefix = _sandbox_prefix(str(executable), executable.stat().st_mtime_ns, sys.platform)
    filesystem = {":root": "read", **{path: "write" for path in sorted(set(roots))}}
    inline_table = "{" + ",".join(json.dumps(key) + "=" + json.dumps(value) for key, value in filesystem.items()) + "}"
    args = [*prefix, "-C", str(root), "-P", "geng-science",
            "-c", "permissions.geng-science.filesystem=" + inline_table,
            "-c", "permissions.geng-science.network.enabled=false"]
    if os.name == "nt":
        # Keep the existing native write boundary without initiating OS account
        # setup or changing any user configuration on first scientific use.
        args += ["-c", 'windows.sandbox="unelevated"']
    safe_env = build_safe_env()
    allowed = {"PATH", "SYSTEMROOT", "WINDIR", "HOME", "USERPROFILE", "TEMP", "TMP", "TMPDIR", "XDG_CACHE_HOME",
               "MPLCONFIGDIR", "MPLBACKEND", "TORCH_HOME", "CUDA_VISIBLE_DEVICES", "CUDA_PATH", "CUDA_HOME",
               "LD_LIBRARY_PATH", "PYTHONIOENCODING", "USER", "LOGNAME", "LNAME", "USERNAME",
               "TORCHINDUCTOR_CACHE_DIR", "TORCH_EXTENSIONS_DIR"}
    safe_env.update({key: value for key, value in env.items() if key.upper() in allowed})
    child_command = list(map(str, command))
    if os.name == "nt":
        # Windows protects the CLI's HOME root. Pointing the CLI itself at the
        # scientific runtime directory silently removes that write capability.
        # Only trusted Python bootstrap code sees the launcher home; restore the
        # isolated case home before installing the guard or importing task code.
        if "-c" not in child_command or child_command.index("-c") + 1 >= len(child_command):
            raise ScientificSandboxUnavailable("The Windows scientific sandbox requires the trusted Python -c bootstrap.")
        restore = {key: safe_env[key] for key in ("HOME", "USERPROFILE") if key in safe_env}
        if restore:
            index = child_command.index("-c") + 1
            prelude = "import os as _geng_bootstrap_os\n" + "".join(
                f"_geng_bootstrap_os.environ[{json.dumps(key)}] = {json.dumps(value)}\n" for key, value in restore.items())
            child_command[index] = prelude + "del _geng_bootstrap_os\n" + child_command[index]
        for key in ("HOME", "USERPROFILE"):
            safe_env[key] = os.environ.get(key) or str(Path.home())
    return {"command": [*args, "--", *child_command], "env": safe_env,
            "policy": {"provider": "codex_sandbox", "model_invocation": False,
                       "native_write_roots": sorted(set(roots)), "native_read_scope": "global_read",
                       "network_enabled": False, "python_guard_required": True,
                       "native_read_isolation": False, "executable": str(executable)}}
