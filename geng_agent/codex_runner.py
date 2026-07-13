from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from .config import get_config_value
from .outputs import write_json, write_text
from .security import codex_safe_env, redact_text


MAX_TRANSCRIPT_CHARS = 200_000
DEFAULT_GENG_CODEX_MODEL = "gpt-5.5"
DEFAULT_GENG_CODEX_REASONING_EFFORT = {
    "analysis": "high",
    "task_writer": "medium",
    "task_reporter": "high",
    "report_editor": "medium",
}


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
    raw_cmd = command_override or get_config_value("GENG_CODEX_CMD") or "codex"
    model = get_config_value("GENG_CODEX_MODEL") or DEFAULT_GENG_CODEX_MODEL
    resolved_reasoning_effort = _resolve_reasoning_effort(role, reasoning_effort)
    argv = split_command(raw_cmd)
    resolved = shutil.which(argv[0]) if argv else None
    status: dict[str, Any] = {
        "ok": False,
        "role": role,
        "backend": "codex",
        "model": model,
        "reasoning_effort": resolved_reasoning_effort,
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

    last_message_path = audit_dir / f"{label}_last_message.txt"
    command = [
        resolved,
        *argv[1:],
        "exec",
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
        env = codex_safe_env()
        if extra_env:
            env.update({str(key): str(value) for key, value in extra_env.items()})
        if path_prepend:
            _prepend_path(env, path_prepend)
        completed = subprocess.run(
            command,
            cwd=work_dir,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
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
        timeout_label = f"{timeout:.0f}s" if timeout is not None else "the configured limit"
        status["blocked_reason"] = f"agent session timed out after {timeout_label}"
        status["error"] = f"agent session timed out after {timeout_label}"
        out = exc.stdout or b""
        err = exc.stderr or b""
        transcript = (out.decode("utf-8", "replace") if isinstance(out, bytes) else str(out)) + (
            "\n--- stderr ---\n" + (err.decode("utf-8", "replace") if isinstance(err, bytes) else str(err))
        )
        status["active_duration_s"] = round(float(getattr(exc, "active_elapsed", timeout or 0.0)), 1)
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
