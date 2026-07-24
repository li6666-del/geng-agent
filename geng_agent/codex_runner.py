from __future__ import annotations

import json
import math
import os
import site
import shlex
import shutil
import subprocess
import sys
import sysconfig
import threading
import time
from pathlib import Path
from typing import Any

from .config import get_config_value
from .outputs import write_json, write_text
from .security import build_safe_env, codex_safe_env, redact_text


MAX_TRANSCRIPT_CHARS = 200_000
CODEX_CAPABILITY_TIMEOUT_SECONDS = 5.0
DEFAULT_CODEX_TIMEOUT_SECONDS = 1800.0
DEFAULT_GENG_CODEX_MODEL = "gpt-5.6-sol"
DEFAULT_GENG_CODEX_REASONING_EFFORT = {
    "analysis": "xhigh",
    "foundation_writer": "xhigh",
    "task_writer": "xhigh",
    "task_reporter": "xhigh",
    "report_editor": "xhigh",
}

_EPHEMERAL_CAPABILITY_CACHE: dict[tuple[str, ...], dict[str, Any]] = {}
_EPHEMERAL_CAPABILITY_LOCK = threading.Lock()

_FOUNDATION_UNITTEST_GUARD = r"""
import builtins
import dis
import json
import os
import sys

import threading

_PATH_STATE = threading.local()
_CONFIG = json.loads(sys.argv[1])


def _real_path(value):
    try:
        path = os.fspath(value)
    except TypeError:
        return None
    if isinstance(path, bytes):
        path = os.fsdecode(path)
    if not isinstance(path, str) or not path:
        return None
    if not os.path.isabs(path):
        path = os.path.join(os.getcwd(), path)
    previous = getattr(_PATH_STATE, "resolving", False)
    _PATH_STATE.resolving = True
    try:
        return os.path.normcase(os.path.realpath(path))
    finally:
        _PATH_STATE.resolving = previous


def _lexical_path(value):
    try:
        path = os.fspath(value)
    except TypeError:
        return None
    if isinstance(path, bytes):
        path = os.fsdecode(path)
    if not isinstance(path, str) or not path:
        return None
    if not os.path.isabs(path):
        path = os.path.join(os.getcwd(), path)
    return os.path.normcase(os.path.abspath(os.path.normpath(path)))


def _inside(path, root):
    try:
        return os.path.commonpath((path, root)) == root
    except (TypeError, ValueError):
        return False


_WORK_DIR = _real_path(_CONFIG["work_dir"])
_SENSITIVE_ROOTS = tuple(
    path
    for path in (_real_path(item) for item in _CONFIG["sensitive_roots"])
    if path is not None
)
_TRUSTED_READ_ROOTS = tuple(
    path
    for path in (_real_path(item) for item in _CONFIG["trusted_read_roots"])
    if path is not None
)


def _deny(message):
    raise PermissionError("Foundation runtime guard: " + message)


def _is_sensitive(path):
    if path is None:
        return False
    return any(_inside(path, root) for root in _SENSITIVE_ROOTS)


def _require_allowed_read(path, event):
    if path is None and event in {"os.listdir", "os.scandir"}:
        path = "."
    if isinstance(path, int):
        if path not in (0, 1, 2):
            _deny(f"{event} via an external file descriptor")
        return
    lexical = _lexical_path(path)
    resolved = _real_path(path)
    if lexical is None or resolved is None:
        _deny(f"{event} with an unresolved path: {path!r}")
    if _inside(lexical, _WORK_DIR) and not _inside(resolved, _WORK_DIR):
        _deny(f"{event} follows a case symlink outside the sandbox: {path!r}")
    if _is_sensitive(resolved):
        _deny(f"host credential read blocked: {path!r}")
    if _inside(resolved, _WORK_DIR):
        return
    if any(_inside(resolved, root) for root in _TRUSTED_READ_ROOTS):
        return
    _deny(f"{event} outside case and trusted runtime roots: {path!r}")


def _require_case_write(path, event):
    if isinstance(path, int):
        if path not in (0, 1, 2):
            _deny(f"{event} via an external file descriptor")
        return
    resolved = _real_path(path)
    if resolved is None or not _inside(resolved, _WORK_DIR):
        _deny(f"{event} outside the case sandbox: {path!r}")


def _require_case_chdir(path, event):
    lexical = _lexical_path(path)
    resolved = _real_path(path)
    if lexical is None or resolved is None:
        _deny(f"{event} with an unresolved path: {path!r}")
    if not _inside(lexical, _WORK_DIR) or not _inside(resolved, _WORK_DIR):
        _deny(f"{event} outside the case sandbox: {path!r}")


def _open_is_write(mode, flags):
    if isinstance(mode, str) and any(marker in mode for marker in ("w", "a", "x", "+")):
        return True
    if not isinstance(flags, int):
        return False
    write_flags = (
        os.O_WRONLY
        | os.O_RDWR
        | os.O_APPEND
        | os.O_CREAT
        | os.O_TRUNC
    )
    tmpfile_flag = getattr(os, "O_TMPFILE", 0)
    return bool(flags & write_flags) or bool(tmpfile_flag and flags & tmpfile_flag == tmpfile_flag)


_MUTATING_PATH_ARGUMENTS = {
    "os.chmod": (0,),
    "os.chown": (0,),
    "os.link": (0, 1),
    "os.mkdir": (0,),
    "os.remove": (0,),
    "os.rename": (0, 1),
    "os.replace": (0, 1),
    "os.rmdir": (0,),
    "os.symlink": (1,),
    "os.truncate": (0,),
    "os.utime": (0,),
    "shutil.copyfile": (1,),
    "shutil.copymode": (1,),
    "shutil.copystat": (1,),
    "shutil.copytree": (1,),
    "shutil.move": (0, 1),
    "shutil.rmtree": (0,),
}

_READ_PATH_EVENTS = {
    "os.listdir",
    "os.scandir",
    "glob.glob",
    "glob.glob/2",
}


def _glob_anchor(args):
    pattern = args[0] if args else "."
    root_dir = args[2] if len(args) > 2 else None
    try:
        pattern = os.fspath(pattern)
        if root_dir is not None and not os.path.isabs(pattern):
            pattern = os.path.join(os.fspath(root_dir), pattern)
    except TypeError:
        return pattern
    separators = {os.sep}
    if os.altsep:
        separators.add(os.altsep)
    first_magic = min(
        (pattern.find(marker) for marker in ("*", "?", "[") if marker in pattern),
        default=-1,
    )
    if first_magic < 0:
        return pattern
    prefix = pattern[:first_magic]
    while prefix and prefix[-1] not in separators:
        prefix = prefix[:-1]
    return prefix or "."


def _is_case_frame(frame):
    if frame is None:
        return False
    code_filename = frame.f_code.co_filename
    if (
        isinstance(code_filename, str)
        and code_filename.startswith("<")
        and code_filename.endswith(">")
    ):
        return False
    filename = _real_path(code_filename)
    return filename is not None and _inside(filename, _WORK_DIR)


def _caller_opcode(frame):
    if frame is None:
        return ""
    current = ""
    try:
        for instruction in dis.get_instructions(frame.f_code):
            if instruction.offset > frame.f_lasti:
                break
            current = instruction.opname
    except (TypeError, ValueError):
        return ""
    return current

def _is_import_statement(frame):
    return _caller_opcode(frame).endswith("IMPORT_NAME")



_ORIGINAL_EVAL = builtins.eval
_ORIGINAL_EXEC = builtins.exec
_ORIGINAL_COMPILE = builtins.compile
_ORIGINAL_IMPORT = builtins.__import__
_ORIGINAL_STAT = os.stat
_ORIGINAL_LSTAT = os.lstat
_ORIGINAL_ACCESS = os.access
_ORIGINAL_READLINK = os.readlink


def _guarded_eval(*args, **kwargs):
    if _is_case_frame(sys._getframe(1)):
        _deny("eval called directly by case code")
    return _ORIGINAL_EVAL(*args, **kwargs)


def _guarded_exec(*args, **kwargs):
    if _is_case_frame(sys._getframe(1)):
        _deny("exec called directly by case code")
    return _ORIGINAL_EXEC(*args, **kwargs)


def _guarded_compile(*args, **kwargs):
    if _is_case_frame(sys._getframe(1)):
        _deny("compile called directly by case code")
    return _ORIGINAL_COMPILE(*args, **kwargs)


def _guarded_import(*args, **kwargs):
    caller = sys._getframe(1)
    if _is_case_frame(caller) and not _is_import_statement(caller):
        _deny("__import__ called directly by case code")
    return _ORIGINAL_IMPORT(*args, **kwargs)


def _guarded_stat(path, *args, **kwargs):
    if getattr(_PATH_STATE, "resolving", False):
        return _ORIGINAL_STAT(path, *args, **kwargs)
    _require_allowed_read(path, "os.stat")
    return _ORIGINAL_STAT(path, *args, **kwargs)


def _guarded_lstat(path, *args, **kwargs):
    if getattr(_PATH_STATE, "resolving", False):
        return _ORIGINAL_LSTAT(path, *args, **kwargs)
    _require_allowed_read(path, "os.lstat")
    return _ORIGINAL_LSTAT(path, *args, **kwargs)


def _guarded_access(path, *args, **kwargs):
    if getattr(_PATH_STATE, "resolving", False):
        return _ORIGINAL_ACCESS(path, *args, **kwargs)
    _require_allowed_read(path, "os.access")
    return _ORIGINAL_ACCESS(path, *args, **kwargs)


def _guarded_readlink(path, *args, **kwargs):
    if getattr(_PATH_STATE, "resolving", False):
        return _ORIGINAL_READLINK(path, *args, **kwargs)
    _require_allowed_read(path, "os.readlink")
    return _ORIGINAL_READLINK(path, *args, **kwargs)


def _direct_audit_case_caller():
    try:
        return _is_case_frame(sys._getframe(2))
    except ValueError:
        return False


def _audit(event, args):
    if event.startswith("socket."):
        _deny(f"network operation blocked ({event})")
    if (
        event == "subprocess.Popen"
        or event == "os.system"
        or event.startswith("os.spawn")
        or event.startswith("os.posix_spawn")
        or event.startswith("os.exec")
        or event in {"os.fork", "os.forkpty", "pty.fork", "pty.spawn"}
        or event.startswith("os.startfile")
    ):
        _deny(f"process operation blocked ({event})")
    if event in {"compile", "exec"} and _direct_audit_case_caller():
        _deny(f"dynamic code execution blocked ({event})")
    if (
        event == "import"
        and _direct_audit_case_caller()
        and not _is_import_statement(sys._getframe(1))
    ):
        _deny("dynamic import blocked")
    if event == "open" and args:
        path = args[0]
        mode = args[1] if len(args) > 1 else None
        flags = args[2] if len(args) > 2 else None
        if _open_is_write(mode, flags):
            _require_case_write(path, event)
        else:
            _require_allowed_read(path, event)
        return
    path_indexes = _MUTATING_PATH_ARGUMENTS.get(event)
    if path_indexes:
        for index in path_indexes:
            if index < len(args):
                _require_case_write(args[index], event)
        return
    if event == "os.chdir" and args:
        _require_case_chdir(args[0], event)
        return
    if event in _READ_PATH_EVENTS and args:
        path = _glob_anchor(args) if event.startswith("glob.") else args[0]
        _require_allowed_read(path, event)


sys.addaudithook(_audit)
builtins.eval = _guarded_eval
builtins.exec = _guarded_exec
builtins.compile = _guarded_compile
builtins.__import__ = _guarded_import
os.stat = _guarded_stat
os.lstat = _guarded_lstat
os.access = _guarded_access
os.readlink = _guarded_readlink
sys.path.insert(0, _WORK_DIR)

import unittest

_START_DIR = _real_path(os.path.join(_WORK_DIR, _CONFIG["start_dir"]))
if _START_DIR is None or not _inside(_START_DIR, _WORK_DIR):
    _deny("unittest discovery path escapes the case sandbox")
_SUITE = unittest.defaultTestLoader.discover(
    start_dir=_START_DIR,
    top_level_dir=_WORK_DIR,
)
_RESULT = unittest.TextTestRunner(verbosity=2).run(_SUITE)
raise SystemExit(0 if _RESULT.wasSuccessful() else 1)
"""


def resolve_codex_timeout(timeout: float | None) -> float:
    """Resolve every inference Worker to a finite per-session wall-clock limit."""

    value = float(timeout or DEFAULT_CODEX_TIMEOUT_SECONDS)
    if not math.isfinite(value):
        raise ValueError("Codex session timeout must be finite")
    return max(1.0, value)


def run_codex_subprocess(
    *,
    role: str,
    work_dir: Path,
    prompt: str,
    audit_dir: Path,
    label: str,
    sandbox: str,
    timeout: float | None,
    command_override: str | None = None,
    output_schema: Path | None = None,
    image_paths: list[Path] | None = None,
    extra_env: dict[str, str] | None = None,
    path_prepend: list[Path | str] | None = None,
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    effective_timeout = resolve_codex_timeout(timeout)
    raw_cmd = command_override or get_config_value("GENG_CODEX_CMD") or "codex"
    model = get_config_value("GENG_CODEX_MODEL") or DEFAULT_GENG_CODEX_MODEL
    resolved_reasoning_effort = _resolve_reasoning_effort(role, reasoning_effort)
    argv = split_command(raw_cmd)
    resolved = shutil.which(argv[0]) if argv else None
    status: dict[str, Any] = {
        "ok": False,
        "role": role,
        "backend": "codex",
        "session_persistence": "ephemeral",
        "ephemeral_capability": None,
        "model": model,
        "reasoning_effort": resolved_reasoning_effort,
        "timeout_s": effective_timeout,
        "command": None,
        "returncode": None,
        "timed_out": False,
        "error_kind": None,
        "blocked_reason": None,
        "error": None,
        "last_message_path": None,
        "transcript": None,
        "duration_s": None,
        "active_duration_s": None,
        "excluded_duration_s": None,
    }
    if not argv or resolved is None:
        status["error_kind"] = "missing_cli"
        status["error"] = f"codex CLI not found: {raw_cmd!r} (install it or set GENG_CODEX_CMD)"
        write_json(audit_dir / f"{label}.json", status)
        return status

    # Every current project Worker is one-shot: project-owned transcript,
    # last-message, JSON and case artifacts are its durable state. A future
    # feature that genuinely needs resume must use a case-local subprocess-only
    # state design, an explicit session UUID, and never --last. CODEX_SQLITE_HOME
    # alone does not isolate sessions/logs/config/auth; zero writes to personal
    # Codex data also requires a separately designed CODEX_HOME and auth flow.
    command_prefix = [resolved, *(arg for arg in argv[1:] if arg != "--ephemeral")]
    env = codex_safe_env()
    if extra_env:
        env.update({str(key): str(value) for key, value in extra_env.items()})
    if path_prepend:
        _prepend_path(env, path_prepend)

    capability = _ephemeral_capability(command_prefix, env, work_dir)
    status["ephemeral_capability"] = capability
    if not capability["supported"]:
        detail = str(capability.get("error") or "--ephemeral is absent from codex exec --help")
        status["error_kind"] = "unsupported_cli_feature"
        status["blocked_reason"] = "Codex CLI does not support required ephemeral Worker sessions"
        status["error"] = (
            "Codex CLI must support codex exec --ephemeral; run codex update "
            f"or upgrade the CLI, restart the project process, and retry. Detail: {detail}"
        )
        write_json(audit_dir / f"{label}.json", status)
        return status

    last_message_path = audit_dir / f"{label}_last_message.txt"
    command = [
        *command_prefix,
        "exec",
        "--ephemeral",
        "--skip-git-repo-check",
        "--sandbox",
        sandbox,
        "--cd",
        str(work_dir),
        "--output-last-message",
        str(last_message_path),
        "--model",
        model,
    ]
    if resolved_reasoning_effort:
        command.extend(["--config", f'model_reasoning_effort="{resolved_reasoning_effort}"'])
    if output_schema is not None:
        command.extend(["--output-schema", str(output_schema)])
    for image_path in image_paths or []:
        command.extend(["--image", str(image_path)])
    command.append("-")
    status["command"] = command[:-1] + ["<brief via stdin>"]
    status["last_message_path"] = str(last_message_path)

    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=work_dir,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=effective_timeout,
            check=False,
            input=prompt,
        )
        status["active_duration_s"] = round(time.monotonic() - started, 1)
        status["excluded_duration_s"] = 0.0
        status["returncode"] = completed.returncode
        status["ok"] = completed.returncode == 0
        transcript = (completed.stdout or "") + ("\n--- stderr ---\n" + completed.stderr if completed.stderr else "")
        if completed.returncode != 0:
            _annotate_codex_failure(status, transcript)
            status["error"] = f"codex exited with status {completed.returncode}"
    except subprocess.TimeoutExpired as exc:
        status["timed_out"] = True
        status["error_kind"] = "timeout"
        timeout_label = f"{effective_timeout:.0f}s"
        status["blocked_reason"] = f"agent session timed out after {timeout_label}"
        status["error"] = f"agent session timed out after {timeout_label}"
        out = exc.stdout or b""
        err = exc.stderr or b""
        transcript = (out.decode("utf-8", "replace") if isinstance(out, bytes) else str(out)) + (
            "\n--- stderr ---\n" + (err.decode("utf-8", "replace") if isinstance(err, bytes) else str(err))
        )
        status["active_duration_s"] = round(float(getattr(exc, "active_elapsed", effective_timeout)), 1)
        status["excluded_duration_s"] = round(float(getattr(exc, "excluded_elapsed", 0.0)), 1)
    except Exception as exc:
        status["error_kind"] = "subprocess_error"
        status["error"] = f"{type(exc).__name__}: {exc}"
        transcript = ""
    status["duration_s"] = round(time.monotonic() - started, 1)
    transcript_path = audit_dir / f"{label}_transcript.txt"
    write_text(transcript_path, redact_text(transcript)[-MAX_TRANSCRIPT_CHARS:])
    status["transcript"] = str(transcript_path)
    write_json(audit_dir / f"{label}.json", status)
    return status


def run_python_unittest_subprocess(
    *,
    work_dir: Path,
    start_dir: str = "tests",
    timeout: float = 120.0,
) -> dict[str, Any]:
    """Run generated contract tests through an isolated, host-owned Python."""

    env = build_safe_env()
    resolved_work_dir = work_dir.resolve()
    runtime_home = resolved_work_dir / ".runtime_home"
    runtime_cache = runtime_home / ".cache"
    runtime_tmp = runtime_home / "tmp"
    runtime_cache.mkdir(parents=True, exist_ok=True)
    runtime_tmp.mkdir(parents=True, exist_ok=True)
    env["HOME"] = str(runtime_home)
    env["USERPROFILE"] = str(runtime_home)
    env["XDG_CACHE_HOME"] = str(runtime_cache)
    env["MPLCONFIGDIR"] = str(runtime_cache / "matplotlib")
    env["TEMP"] = str(runtime_tmp)
    env["TMP"] = str(runtime_tmp)
    env["TMPDIR"] = str(runtime_tmp)
    for key in ("CUDA_HOME", "CUDA_PATH", "CUDA_VISIBLE_DEVICES", "LD_LIBRARY_PATH"):
        value = os.environ.get(key)
        if value:
            env[key] = value
    python_dir = str(Path(sys.executable).resolve().parent)
    env["PATH"] = os.pathsep.join([python_dir, env.get("PATH", "")])
    if os.name == "nt":
        env["Path"] = env["PATH"]
    guard_config = _foundation_unittest_guard_config(
        work_dir=resolved_work_dir,
        start_dir=start_dir,
    )
    command = [
        sys.executable,
        "-I",
        "-B",
        "-c",
        _FOUNDATION_UNITTEST_GUARD,
        json.dumps(guard_config, ensure_ascii=True, separators=(",", ":")),
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=resolved_work_dir,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        return {
            "passed": completed.returncode == 0,
            "returncode": completed.returncode,
            "stdout": completed.stdout[-20_000:],
            "stderr": completed.stderr[-20_000:],
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "passed": False,
            "timed_out": True,
            "stdout": str(exc.stdout or "")[-20_000:],
            "stderr": str(exc.stderr or "")[-20_000:],
        }


def _foundation_unittest_guard_config(*, work_dir: Path, start_dir: str) -> dict[str, Any]:
    """Build path-only guard input without exposing host environment values."""

    home_candidates: list[Path] = [Path.home()]
    for name in ("HOME", "USERPROFILE"):
        value = os.environ.get(name)
        if value:
            home_candidates.append(Path(value))
    sensitive_roots = {
        str((home / leaf).resolve())
        for home in home_candidates
        for leaf in (".ssh", ".codex", ".config")
    }

    trusted_read_roots = {
        str(work_dir.resolve()),
        str(Path(sys.prefix).resolve()),
        str(Path(sys.base_prefix).resolve()),
        str(Path(sys.executable).resolve().parent),
    }
    try:
        trusted_read_roots.update(str(Path(path).resolve()) for path in site.getsitepackages())
    except AttributeError:
        pass
    trusted_read_roots.update(
        str(Path(path).resolve())
        for path in sysconfig.get_paths().values()
        if path
    )
    if os.name == "nt":
        system_root = os.environ.get("SystemRoot")
        if system_root:
            trusted_read_roots.add(str(Path(system_root).resolve()))
    else:
        trusted_read_roots.update(
            str(path.absolute())
            for path in (
                Path("/usr/lib"),
                Path("/usr/lib64"),
                Path("/lib"),
                Path("/lib64"),
                Path("/usr/share"),
                Path("/etc/ssl/certs"),
                Path("/dev/urandom"),
                Path("/proc/cpuinfo"),
                Path("/proc/meminfo"),
                Path("/proc/self/status"),
                Path("/proc/driver/nvidia"),
                Path("/sys/bus/pci/devices"),
                Path("/sys/devices/system/cpu"),
                Path("/sys/devices/system/node"),
            )
            if path.exists()
        )

    return {
        "work_dir": str(work_dir.resolve()),
        "start_dir": str(start_dir),
        "sensitive_roots": sorted(sensitive_roots),
        "trusted_read_roots": sorted(trusted_read_roots),
    }


def _ephemeral_capability(
    command_prefix: list[str],
    env: dict[str, str],
    work_dir: Path,
) -> dict[str, Any]:
    """Fail closed unless this exact Codex command supports one-shot sessions.

    The lock makes the first probe single-flight. Other Worker threads wait for
    that short probe only, then launch concurrently using the cached result.
    """
    key = tuple(command_prefix)
    with _EPHEMERAL_CAPABILITY_LOCK:
        cached = _EPHEMERAL_CAPABILITY_CACHE.get(key)
        if cached is not None:
            return {**cached, "cached": True}
        result = _probe_ephemeral_capability(command_prefix, env, work_dir)
        _EPHEMERAL_CAPABILITY_CACHE[key] = result
        return {**result, "cached": False}


def _probe_ephemeral_capability(
    command_prefix: list[str],
    env: dict[str, str],
    work_dir: Path,
) -> dict[str, Any]:
    command = [*command_prefix, "exec", "--help"]
    result: dict[str, Any] = {
        "supported": False,
        "cached": False,
        "command": command,
        "returncode": None,
        "timeout_s": CODEX_CAPABILITY_TIMEOUT_SECONDS,
        "error": None,
    }
    try:
        completed = subprocess.run(
            command,
            cwd=work_dir,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=CODEX_CAPABILITY_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        result["error"] = "codex exec --help timed out"
        return result
    except Exception as exc:
        result["error"] = f"capability check failed: {type(exc).__name__}: {exc}"
        return result

    help_text = (completed.stdout or "") + "\n" + (completed.stderr or "")
    result["returncode"] = completed.returncode
    result["supported"] = completed.returncode == 0 and "--ephemeral" in help_text
    if not result["supported"]:
        result["error"] = (
            "codex exec --help did not advertise --ephemeral"
            if completed.returncode == 0
            else f"codex exec --help exited with status {completed.returncode}"
        )
    return result


def _clear_ephemeral_capability_cache() -> None:
    """Reset process-local capability state for deterministic tests."""
    with _EPHEMERAL_CAPABILITY_LOCK:
        _EPHEMERAL_CAPABILITY_CACHE.clear()


def _annotate_codex_failure(status: dict[str, Any], transcript: str) -> None:
    lowered = transcript.lower()
    if "usage limit" in lowered and (
        "hit your usage limit" in lowered or "purchase more credits" in lowered or "try again" in lowered
    ):
        status["error_kind"] = "codex_usage_limit"
        status["blocked_reason"] = "Codex CLI usage limit exhausted"
        return
    if (
        "rate limit" in lowered
        or "too many requests" in lowered
        or "model is at capacity" in lowered
        or "selected model is at capacity" in lowered
    ):
        status["error_kind"] = "codex_rate_limit"
        status["blocked_reason"] = "Codex CLI rate limit or model capacity"
        return
    status["error_kind"] = "codex_nonzero_exit"


def _prepend_path(env: dict[str, str], entries: list[Path | str]) -> None:
    existing = env.get("PATH") or env.get("Path") or ""
    parts = [str(entry) for entry in entries if str(entry)]
    parts.extend(item for item in existing.split(os.pathsep) if item)
    env["PATH"] = os.pathsep.join(parts)
    if os.name == "nt":
        env["Path"] = env["PATH"]


def split_command(raw: str) -> list[str]:
    return [token.strip('"') for token in shlex.split(raw, posix=False) if token.strip('"')]


def _resolve_reasoning_effort(role: str, explicit: str | None) -> str | None:
    value = explicit
    if not value:
        role_name = (
            "TASK_WRITER"
            if role == "task_writer"
            else "FOUNDATION_WRITER"
            if role == "foundation_writer"
            else "TASK_REPORTER"
            if role == "task_reporter"
            else "REPORT_EDITOR"
            if role == "report_editor"
            else "ANALYSIS"
            if role == "analysis"
            else ""
        )
        if role_name:
            value = get_config_value(f"GENG_CODEX_{role_name}_REASONING_EFFORT")
    if not value:
        value = get_config_value("GENG_CODEX_REASONING_EFFORT")
    if not value:
        value = DEFAULT_GENG_CODEX_REASONING_EFFORT.get(role)
    normalized = str(value or "").strip().lower()
    return normalized if normalized in {"minimal", "low", "medium", "high", "xhigh"} else None
