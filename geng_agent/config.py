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


def build_code_review_client(
    *,
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    temperature: float = 0.1,
    timeout: float = 120.0,
    thinking: str | None = None,
    reasoning_effort: str | None = None,
) -> OpenAICompatibleClient | None:
    """Build a separate (heterogeneous) reviewer client for the code-faithfulness stage.

    Returns ``None`` when no review model is configured, so the caller falls back to the
    main generator client (same-model review). Key/base-url fall back to the main
    ``GENG_LLM_*`` values only when the ``GENG_CODE_REVIEW_*`` ones are unset -- so a
    cross-provider reviewer (e.g. deepseek-v4-pro on api.deepseek.com) must set its own
    ``GENG_CODE_REVIEW_API_KEY`` and ``GENG_CODE_REVIEW_BASE_URL``.
    """
    resolved_model = model or get_config_value("GENG_CODE_REVIEW_MODEL")
    if not resolved_model:
        return None
    resolved_key = api_key or get_config_value("GENG_CODE_REVIEW_API_KEY") or get_config_value("GENG_LLM_API_KEY")
    resolved_base = (
        base_url
        or get_config_value("GENG_CODE_REVIEW_BASE_URL")
        or get_config_value("GENG_LLM_BASE_URL")
        or "https://api.openai.com/v1"
    )
    if not resolved_key:
        raise ValueError("缺少代码审查 API key：请设置 GENG_CODE_REVIEW_API_KEY（或回退用 GENG_LLM_API_KEY）。")
    return OpenAICompatibleClient(
        api_key=resolved_key,
        base_url=resolved_base,
        model=resolved_model,
        temperature=temperature,
        timeout=timeout,
        thinking=thinking,
        reasoning_effort=reasoning_effort,
    )


def build_generation_client(
    *,
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    temperature: float = 0.1,
    timeout: float = 120.0,
    thinking: str | None = None,
    reasoning_effort: str | None = None,
) -> OpenAICompatibleClient | None:
    """Optional separate client for round-3 CODE generation + Phase-D science repair only.

    The main client must stay multimodal (round-4 result review hard-requires
    complete_multimodal, and facts/thesis/plan read page images), but per-file code
    generation needs no images -- so a stronger, possibly text-only coder (e.g.
    mimo-v2.5-pro) can be pointed here to write the hard science files while the
    multimodal model keeps doing extraction/review. Returns ``None`` when unconfigured
    (-> generation falls back to the main client; behavior unchanged). Key/base-url fall
    back to the main ``GENG_LLM_*`` values, so same-endpoint variants (pro) just need the
    model name.
    """
    resolved_model = model or get_config_value("GENG_GEN_MODEL")
    if not resolved_model:
        return None
    resolved_key = api_key or get_config_value("GENG_GEN_API_KEY") or get_config_value("GENG_LLM_API_KEY")
    resolved_base = (
        base_url
        or get_config_value("GENG_GEN_BASE_URL")
        or get_config_value("GENG_LLM_BASE_URL")
        or "https://api.openai.com/v1"
    )
    if not resolved_key:
        raise ValueError("缺少生成模型 API key：请设置 GENG_GEN_API_KEY（或回退用 GENG_LLM_API_KEY）。")
    return OpenAICompatibleClient(
        api_key=resolved_key,
        base_url=resolved_base,
        model=resolved_model,
        temperature=temperature,
        timeout=timeout,
        thinking=thinking,
        reasoning_effort=reasoning_effort,
    )


def build_secondary_extraction_client(
    *,
    temperature: float = 0.1,
    timeout: float = 120.0,
) -> OpenAICompatibleClient | None:
    """Optional second (heterogeneous, multimodal) extraction model for the round-1
    cross-model fact ensemble.

    Reads ``GENG_LLM2_API_KEY`` / ``GENG_LLM2_BASE_URL`` / ``GENG_LLM2_MODEL`` and returns
    ``None`` when any is unset -- an unconfigured environment keeps single-model behavior
    unchanged. The model MUST be multimodal: round-1 sends rendered page images alongside
    the prompt, and a text-only second model would silently miss every figure-sourced fact.
    """
    model = get_config_value("GENG_LLM2_MODEL")
    api_key = get_config_value("GENG_LLM2_API_KEY")
    base_url = get_config_value("GENG_LLM2_BASE_URL")
    if not (model and api_key and base_url):
        return None
    # Always run the second model WITHOUT reasoning/thinking for speed: MiniMax-M3 honors
    # {"thinking": {"type": "disabled"}} on the OpenAI-compatible endpoint (fast mode). A full
    # thinking pass on a paper took ~7.5 min; disabled is far faster at no accuracy cost for
    # straight fact extraction. (M2.x can't disable thinking; use M3.)
    return OpenAICompatibleClient(
        api_key=api_key,
        base_url=base_url,
        model=model,
        temperature=temperature,
        timeout=timeout,
        thinking="disabled",
    )