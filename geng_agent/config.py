from __future__ import annotations

import os
from pathlib import Path

from .llm import OpenAICompatibleClient

_MAX_CASE_DIR_NAME_LENGTH = 128
_ALLOWED_CASE_DIR_PUNCTUATION = frozenset("._- ()")
_WINDOWS_RESERVED_CASE_DIR_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)


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
    return (Path.home() / "Desktop" / "耿同学agent_cases").resolve()


def validate_case_output_dir(path: str | Path) -> Path:
    """Resolve a case path and reject prose or generated text as its leaf name."""
    candidate = Path(path).expanduser()
    name = candidate.name
    safe = (
        bool(name)
        and name == name.strip()
        and len(name) <= _MAX_CASE_DIR_NAME_LENGTH
        and name[0].isalnum()
        and not name.endswith(".")
        and name.split(".", 1)[0].upper() not in _WINDOWS_RESERVED_CASE_DIR_NAMES
        and all(char.isalnum() or char in _ALLOWED_CASE_DIR_PUNCTUATION for char in name)
    )
    if not safe:
        raise ValueError(
            "不安全的 case 输出目录名："
            f"{name!r}。目录名必须以字母或数字开头，长度不超过 "
            f"{_MAX_CASE_DIR_NAME_LENGTH}，且只能包含字母、数字、空格、点、下划线、连字符或圆括号。"
        )
    return candidate.resolve()


def resolve_case_dir(path: str | Path) -> Path:
    """Put relative case names below the configured root; honor explicit absolute paths."""
    candidate = Path(path).expanduser()
    if candidate.is_absolute():
        return validate_case_output_dir(candidate)
    if len(candidate.parts) != 1 or candidate.name in {"", ".", ".."}:
        raise ValueError(
            "相对 case 路径只能是一个目录名；请使用例如 case_001，"
            "或显式传入一个绝对路径。"
        )
    return validate_case_output_dir(get_cases_root() / candidate.name)


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
