from __future__ import annotations

import os
from pathlib import Path

from .llm import OpenAICompatibleClient


def get_config_value(name: str) -> str | None:
    value = os.getenv(name)
    if value:
        return value

    if os.name != "nt":
        return None

    try:
        import winreg
    except ImportError:
        return None

    locations = (
        (winreg.HKEY_CURRENT_USER, "Environment"),
        (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
    )
    for root, path in locations:
        try:
            with winreg.OpenKey(root, path) as key:
                registry_value, _ = winreg.QueryValueEx(key, name)
        except OSError:
            continue
        if registry_value:
            return str(registry_value)
    return None


def get_cases_root() -> Path:
    raw = get_config_value("GENG_CASES_ROOT")
    if raw:
        return Path(raw).expanduser().resolve()
    return (Path.home() / "Documents" / "geng_cases").resolve()


def build_llm_client(
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    temperature: float = 0.1,
    timeout: float = 120.0,
    thinking: str | None = None,
    reasoning_effort: str | None = None,
) -> OpenAICompatibleClient:
    resolved_key = api_key or get_config_value("GENG_LLM_API_KEY")
    resolved_base = base_url or get_config_value("GENG_LLM_BASE_URL") or "https://api.openai.com/v1"
    resolved_model = model or get_config_value("GENG_LLM_MODEL")
    if not resolved_key:
        raise ValueError("缺少 API key：请设置 GENG_LLM_API_KEY。")
    if not resolved_model:
        raise ValueError("缺少模型名：请设置 GENG_LLM_MODEL。")
    return OpenAICompatibleClient(
        api_key=resolved_key,
        base_url=resolved_base,
        model=resolved_model,
        temperature=temperature,
        timeout=timeout,
        thinking=thinking,
        reasoning_effort=reasoning_effort,
    )
