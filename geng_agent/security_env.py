"""Safe subprocess environments and secret redaction."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any


SENSITIVE_ENV_KEYS = {
    "GENG_LLM_API_KEY",
    "OPENAI_API_KEY",
    "DEEPSEEK_API_KEY",
    "ANTHROPIC_API_KEY",
    "HF_TOKEN",
    "HUGGINGFACE_HUB_TOKEN",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AZURE_OPENAI_API_KEY",
    "GOOGLE_API_KEY",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
}
SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{12,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9_\-\.]{12,}", re.IGNORECASE),
    re.compile(r"(?i)(api[_-]?key|token|secret|password)\s*[:=]\s*['\"]?[^'\"\s]+"),
]


def build_safe_env() -> dict[str, str]:
    keep = {
        "PATH",
        "SystemRoot",
        "WINDIR",
        "windir",
        "TEMP",
        "TMP",
        "PYTHONIOENCODING",
        "MPLBACKEND",
    }
    keep_lower = {key.lower() for key in keep}
    safe_env = {key: value for key, value in os.environ.items() if key.lower() in keep_lower}
    windows_root = os.environ.get("SystemRoot") or os.environ.get("WINDIR") or os.environ.get("windir")
    if windows_root:
        safe_env.setdefault("SystemRoot", windows_root)
        safe_env.setdefault("WINDIR", windows_root)
        safe_env.setdefault("windir", windows_root)
    safe_env["PYTHONIOENCODING"] = "utf-8"
    safe_env["MPLBACKEND"] = "Agg"
    safe_env["MPLCONFIGDIR"] = safe_env.get("TEMP") or safe_env.get("TMP") or "."
    for key in SENSITIVE_ENV_KEYS:
        safe_env.pop(key, None)
    return safe_env


def codex_safe_env() -> dict[str, str]:
    """Environment for a Codex subprocess: the inherited parent env MINUS geng's own LLM
    secrets, which codex never needs (it authenticates with its own credentials). Unlike
    :func:`build_safe_env` -- which strips the env to a minimal allowlist for running
    UNTRUSTED generated code -- codex is a trusted external tool needing a normal env (PATH,
    HOME, its own auth), so keep everything except the GENG_* keys it has no reason to read."""
    env = dict(os.environ)
    for key in ("GENG_LLM_API_KEY", "GENG_LLM2_API_KEY"):
        env.pop(key, None)
    _prefer_geng_python_for_codex(env)
    return env


def _prefer_geng_python_for_codex(env: dict[str, str]) -> None:
    raw_python = _select_geng_python(env.get("GENG_PYTHON"))
    if not raw_python:
        return
    python_path = Path(raw_python)
    python_dir = python_path.parent
    prefix = [
        python_dir,
        python_dir / "Scripts",
        python_dir / "Library" / "bin",
    ]
    existing = env.get("PATH") or env.get("Path") or ""
    seen: set[str] = set()
    path_parts: list[str] = []
    for item in [str(path) for path in prefix if path] + existing.split(os.pathsep):
        if not item:
            continue
        key = item.lower() if sys.platform == "win32" else item
        if key in seen:
            continue
        seen.add(key)
        path_parts.append(item)
    env["PATH"] = os.pathsep.join(path_parts)
    if sys.platform == "win32":
        env["Path"] = env["PATH"]
    env["PYTHON"] = str(python_path)
    env["GENG_PYTHON"] = str(python_path)
    if python_dir.parent.name == "envs":
        env.setdefault("CONDA_PREFIX", str(python_dir))


def _select_geng_python(explicit_python: str | None) -> str:
    for raw_python in (explicit_python, _default_geng_python()):
        python_path = _valid_python_path(raw_python)
        if python_path is not None:
            return str(python_path)
    return ""


def _valid_python_path(raw_python: str | None) -> Path | None:
    raw = (raw_python or "").strip().strip('"')
    if not raw:
        return None
    python_path = Path(raw).expanduser()
    if python_path.name.lower() not in {"python.exe", "python"}:
        return None
    if not python_path.exists() or not python_path.is_file():
        return None
    return python_path


def _default_geng_python() -> str:
    homes = [os.environ.get("USERPROFILE"), os.environ.get("HOME")]
    for home in homes:
        if not home:
            continue
        candidate = Path(home) / "miniconda3" / "envs" / "torch" / ("python.exe" if sys.platform == "win32" else "bin/python")
        if candidate.exists():
            return str(candidate)
    return ""


def redact_text(text: str) -> str:
    redacted = text
    for pattern in SECRET_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def redact_data(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [redact_data(item) for item in value]
    if isinstance(value, dict):
        return {key: redact_data(item) for key, item in value.items()}
    return value
