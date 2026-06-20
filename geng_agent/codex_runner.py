from __future__ import annotations

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


def run_codex_subprocess(
    *,
    role: str,
    work_dir: Path,
    prompt: str,
    audit_dir: Path,
    label: str,
    sandbox: str,
    timeout: float,
    command_override: str | None = None,
    output_schema: Path | None = None,
    image_paths: list[Path] | None = None,
) -> dict[str, Any]:
    raw_cmd = command_override or get_config_value("GENG_CODEX_CMD") or "codex"
    argv = split_command(raw_cmd)
    resolved = shutil.which(argv[0]) if argv else None
    status: dict[str, Any] = {
        "ok": False,
        "role": role,
        "backend": "codex",
        "command": None,
        "returncode": None,
        "timed_out": False,
        "error": None,
        "last_message_path": None,
        "transcript": None,
        "duration_s": None,
    }
    if not argv or resolved is None:
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
    ]
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
            env=codex_safe_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            input=prompt,
        )
        status["returncode"] = completed.returncode
        status["ok"] = completed.returncode == 0
        transcript = (completed.stdout or "") + ("\n--- stderr ---\n" + completed.stderr if completed.stderr else "")
        if completed.returncode != 0:
            status["error"] = f"codex exited with status {completed.returncode}"
    except subprocess.TimeoutExpired as exc:
        status["timed_out"] = True
        status["error"] = f"agent session timed out after {timeout:.0f}s"
        out = exc.stdout or b""
        err = exc.stderr or b""
        transcript = (out.decode("utf-8", "replace") if isinstance(out, bytes) else str(out)) + (
            "\n--- stderr ---\n" + (err.decode("utf-8", "replace") if isinstance(err, bytes) else str(err))
        )
    except Exception as exc:
        status["error"] = f"{type(exc).__name__}: {exc}"
        transcript = ""
    status["duration_s"] = round(time.monotonic() - started, 1)
    transcript_path = audit_dir / f"{label}_transcript.txt"
    write_text(transcript_path, redact_text(transcript)[-MAX_TRANSCRIPT_CHARS:])
    status["transcript"] = str(transcript_path)
    write_json(audit_dir / f"{label}.json", status)
    return status


def split_command(raw: str) -> list[str]:
    return [token.strip('"') for token in shlex.split(raw, posix=False) if token.strip('"')]
