from __future__ import annotations

import json
import gzip
import hashlib
import os
import site
import shlex
import shutil
import subprocess
import sys
import sysconfig
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

from .config import get_config_value
from .agent_activity import session_finished, session_started
from .outputs import write_json, write_text
from .security import build_safe_env, codex_safe_env, redact_text
from .model_config import DEFAULT_MODEL, resolve_model_config
from .codex_provider import (
    ACTIVE_CREDENTIAL_ENV, model_cli_options, provider_environment,
    redact_provider_secrets, validate_model_use,
)


MAX_TRANSCRIPT_CHARS = 200_000
CODEX_CLI_HELP_PROBE_TIMEOUT_SECONDS = 5.0
DEFAULT_GENG_CODEX_MODEL = DEFAULT_MODEL

_EPHEMERAL_CAPABILITY_CACHE: dict[tuple[str, ...], dict[str, Any]] = {}
_EPHEMERAL_CAPABILITY_LOCK = threading.Lock()

def run_codex_subprocess(
    *,
    role: str,
    work_dir: Path,
    prompt: str,
    audit_dir: Path,
    label: str,
    sandbox: str,
    command_override: str | None = None,
    output_schema: Path | None = None,
    image_paths: list[Path] | None = None,
    extra_env: dict[str, str] | None = None,
    path_prepend: list[Path | str] | None = None,
    workspace_network_access: bool = False,
    workspace_writable_roots: list[Path] | None = None,
) -> dict[str, Any]:
    raw_cmd = command_override or get_config_value("GENG_CODEX_CMD") or "codex"
    model_config = resolve_model_config(role, getter=get_config_value)
    model = model_config.model
    resolved_reasoning_effort = model_config.reasoning_effort
    argv = split_command(raw_cmd)
    resolved = shutil.which(argv[0]) if argv else None
    status: dict[str, Any] = {
        "ok": False,
        "role": role,
        "backend": "codex",
        "session_persistence": "ephemeral",
        "execution_policy": "unbounded_until_exit_or_user_stop",
        "ephemeral_capability": None,
        "model": model,
        "provider": model_config.provider if model_config.managed else "inherited",
        "model_config": model_config.identity(),
        "reasoning_effort": resolved_reasoning_effort,
        "command": None,
        "returncode": None,
        "error_kind": None,
        "blocked_reason": None,
        "error": None,
        "last_message_path": None,
        "transcript": None,
        "duration_s": None,
        "observation_errors": [],
    }
    def observe(label, operation, fallback=None):
        try:
            return operation()
        except Exception as exc:
            from .observations import record_error
            record_error(audit_dir / label, exc)
            status["observation_errors"].append(redact_text(f"{label}: {type(exc).__name__}: {exc}"))
            return fallback
    # Record at the transport boundary, after every caller's dynamic additions.
    # Immutable per-invocation copies retain history when a stage label is reused.
    invocation_id = uuid.uuid4().hex
    input_dir = audit_dir / "prompt_inputs" / invocation_id
    observe("prompt_directory", lambda: input_dir.mkdir(parents=True, exist_ok=True))
    prompt_bytes = prompt.encode("utf-8")
    observe("prompt_snapshot", lambda: (input_dir / "brief.md").write_bytes(prompt_bytes))
    observe("prompt_brief", lambda: (audit_dir / f"{label}_brief.md").write_bytes(prompt_bytes))
    from .prompt_identity import file_identity
    input_manifest = {
        "invocation_id": invocation_id, "role": role, "label": label,
        "work_dir": str(work_dir.resolve()), "sandbox": sandbox,
        "model": model, "reasoning_effort": resolved_reasoning_effort,
        "model_config": model_config.identity(),
        "prompt_path": str(input_dir / "brief.md"),
        "prompt_sha256": hashlib.sha256(prompt_bytes).hexdigest(),
        "prompt_characters": len(prompt), "prompt_utf8_bytes": len(prompt_bytes),
        "images": [observe("image_identity", lambda path=path: file_identity(Path(path)), {"path": str(path)}) for path in image_paths or []],
        "output_schema": observe("schema_identity", lambda: file_identity(output_schema)) if output_schema is not None else None,
        "context_boundary": "Records project-provided stdin and attachments; CLI system instructions, tools and dynamic reads are not implied by this manifest.",
    }
    observe("input_snapshot", lambda: write_json(input_dir / "input.json", input_manifest))
    observe("input_manifest", lambda: write_json(audit_dir / f"{label}_input.json", input_manifest))
    status.update(label=label, invocation_id=invocation_id, input_manifest=str(input_dir / "input.json"))
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

    try:
        validate_model_use(model_config, images=bool(image_paths), schema=output_schema is not None,
                           command_prefix=command_prefix, work_dir=work_dir)
        env, provider_secrets = provider_environment(model_config, env, getter=get_config_value)
    except ValueError as exc:
        status.update(error_kind="invalid_model_config", error=str(exc), blocked_reason=str(exc))
        write_json(audit_dir / f"{label}.json", status)
        return status

    probe_env = env
    if model_config.managed:
        probe_env = {key: value for key, value in env.items()
                     if key.upper() not in {ACTIVE_CREDENTIAL_ENV, "OPENAI_API_KEY"}}
    capability = _ephemeral_capability(command_prefix, probe_env, work_dir)
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

    if model_config.managed and not capability.get("ignore_user_config_supported"):
        status.update(error_kind="unsupported_cli_feature",
                      error="项目模型配置需要支持 --ignore-user-config 的 Codex CLI；请升级 CLI 后重试。")
        write_json(audit_dir / f"{label}.json", status)
        return status

    last_message_path = audit_dir / f"{label}_last_message.txt"
    command = [
        *command_prefix,
        "exec",
        "--ephemeral",
        "--json",
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
    if workspace_network_access and sandbox == "workspace-write":
        command.extend(["--config", "sandbox_workspace_write.network_access=true"])
    if workspace_writable_roots and sandbox == "workspace-write":
        command.extend(["--config", "sandbox_workspace_write.writable_roots=" +
                        json.dumps([str(path.resolve()) for path in workspace_writable_roots])])
    command.extend(model_cli_options(model_config, env))
    if output_schema is not None:
        command.extend(["--output-schema", str(output_schema)])
    for image_path in image_paths or []:
        command.extend(["--image", str(image_path)])
    command.append("-")
    status["command"] = [redact_text(redact_provider_secrets(arg, provider_secrets))
                         for arg in command[:-1]] + ["<brief via stdin>"]
    status["last_message_path"] = str(last_message_path)

    started = time.monotonic()
    invocation_started_at = time.time()
    activity = observe("activity_started", lambda: session_started(invocation_id=invocation_id, role=role, label=label, work_dir=work_dir))
    cancelled = None
    from .supervisor import current_supervisor
    from .progress import PipelineCancelled
    supervisor = current_supervisor()
    try:
        from .supervisor_process import run_observed_process
        completed = run_observed_process(command, supervisor=supervisor, label=f"{role}:{label}",
            cwd=work_dir, env=env, input=prompt) if supervisor is not None else subprocess.run(
            command,
            cwd=work_dir,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            input=prompt,
        )
        status["observation_errors"].extend(getattr(completed, "observation_errors", []))
        status["returncode"] = completed.returncode
        status["ok"] = completed.returncode == 0
        transcript = redact_provider_secrets(
            (completed.stdout or "") + ("\n--- stderr ---\n" + completed.stderr if completed.stderr else ""),
            provider_secrets,
        )
        if completed.returncode != 0:
            _annotate_codex_failure(status, transcript)
            status["error"] = f"codex exited with status {completed.returncode}"
    except PipelineCancelled as exc:
        cancelled = exc
        status.update(ok=False, error_kind="cancelled", error="用户停止运行",
                      returncode=getattr(exc, "returncode", None))
        transcript = redact_provider_secrets(str(getattr(exc, "stdout", "") or "") + "\n" +
                                            str(getattr(exc, "stderr", "") or ""), provider_secrets)
    except Exception as exc:
        status["error_kind"] = "subprocess_error"
        status["error"] = redact_text(redact_provider_secrets(f"{type(exc).__name__}: {exc}", provider_secrets))
        transcript = ""
    finally:
        observe("activity_finished", lambda: session_finished(activity, ok=bool(status["ok"]), error_kind=status.get("error_kind")))
    status["duration_s"] = round(time.monotonic() - started, 1)
    if provider_secrets and last_message_path.is_file() and not last_message_path.is_symlink():
        last_text = last_message_path.read_text(encoding="utf-8")
        safe_last_text = redact_provider_secrets(last_text, provider_secrets)
        if safe_last_text != last_text:
            write_text(last_message_path, safe_last_text)
    from .codex_cost import record_codex_invocation
    try:
        status["cost_event"] = record_codex_invocation(
            audit_dir, status, transcript, started_at=invocation_started_at,
        )
    except Exception as exc:
        status["cost_warning"] = f"Invocation accounting unavailable: {type(exc).__name__}"
    transcript_path = audit_dir / f"{label}_transcript.txt"
    redacted_transcript = redact_text(transcript)
    observe("transcript_tail", lambda: write_text(transcript_path, redacted_transcript[-MAX_TRANSCRIPT_CHARS:]))
    full_transcript_path = input_dir / "transcript.txt.gz"
    def save_transcript():
        with gzip.open(full_transcript_path, "wt", encoding="utf-8", newline="") as stream:
            stream.write(redacted_transcript)
    observe("transcript_archive", save_transcript)
    status["full_transcript"] = str(full_transcript_path)
    status["transcript_characters"] = len(redacted_transcript)
    status["transcript_tail_truncated"] = len(redacted_transcript) > MAX_TRANSCRIPT_CHARS
    status["transcript"] = str(transcript_path)
    observe("session_status", lambda: write_json(audit_dir / f"{label}.json", status))
    if cancelled is not None:
        raise cancelled
    return status






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
        "probe_timeout_s": CODEX_CLI_HELP_PROBE_TIMEOUT_SECONDS,
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
            timeout=CODEX_CLI_HELP_PROBE_TIMEOUT_SECONDS,
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
    result["ignore_user_config_supported"] = completed.returncode == 0 and "--ignore-user-config" in help_text
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
