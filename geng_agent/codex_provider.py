"""Translate project model settings into Codex options, without storing credentials."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Callable

from .model_config import CodexModelConfig
from .security_env import SENSITIVE_ENV_KEYS

ACTIVE_CREDENTIAL_ENV = "GENG_ACTIVE_PROVIDER_CREDENTIAL"
_RUNTIME_ENV = (
    "GENG_PYTHON", "GENG_PYTHON_EXECUTABLE", "GENG_EXECUTION_BROKER", "PYTHON",
    "VIRTUAL_ENV", "CONDA_PREFIX", "PYTHONDONTWRITEBYTECODE", "PYTHONIOENCODING",
    "MPLBACKEND", "MPLCONFIGDIR", "TEMP", "TMP",
)


def validate_model_use(config: CodexModelConfig, *, images: bool, schema: bool,
                       command_prefix: list[str], work_dir: Path) -> None:
    if not config.supports_tools:
        raise ValueError("所选模型未声明支持工具调用，不能运行项目智能体。")
    if images and not config.supports_images:
        raise ValueError("当前阶段包含图片，所选模型未声明支持图片输入；请为项目选择支持图片的模型。")
    if schema and not config.supports_json_schema:
        raise ValueError("当前阶段需要 JSON Schema 输出，所选模型未声明支持该能力。")
    if config.managed and any(
        arg.split("=", 1)[0] in {"--profile", "--config", "--oss", "--local-provider", "--model", "--enable", "--disable"}
        or (not arg.startswith("--") and arg.startswith(("-c", "-p", "-m")))
        for arg in command_prefix[1:]
    ):
        raise ValueError("使用项目模型配置时，Codex 命令不能再指定 profile/config 或本地模型覆盖。")
    if config.managed and (work_dir / ".codex" / "config.toml").exists():
        raise ValueError("智能体工作目录包含额外的 .codex/config.toml，无法保证所选模型配置独立生效。")
    config.validate_catalog_unchanged()


def provider_environment(config: CodexModelConfig, env: dict[str, str], *,
                         getter: Callable[[str], str | None]) -> tuple[dict[str, str], tuple[str, ...]]:
    if not config.managed:
        return env, ()
    env = dict(env)
    # The selected provider alone receives a credential. Generated shell commands
    # receive a core environment and explicit runtime variables, never this alias.
    key_name = config.env_key
    native_openai = config.provider == "openai" and not config.base_url and not key_name
    if native_openai:
        key_name = "OPENAI_API_KEY"
    credential = (env.get(key_name) or getter(key_name)) if key_name else None
    if config.env_key and not credential:
        raise ValueError(f"缺少所选服务商的认证环境变量：{config.env_key}。")
    for key in list(env):
        if (key.upper() in SENSITIVE_ENV_KEYS - {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"}
                or re.search(r"(?:API_?KEY|TOKEN|SECRET|PASSWORD)$", key, re.I)
                or key == ACTIVE_CREDENTIAL_ENV or key == config.env_key):
            env.pop(key, None)
    if credential:
        env["OPENAI_API_KEY" if native_openai else ACTIVE_CREDENTIAL_ENV] = credential
    return env, (credential,) if credential else ()


def model_cli_options(config: CodexModelConfig, env: dict[str, str]) -> list[str]:
    options: list[str] = []

    def toml(value: object) -> str:
        if isinstance(value, dict):
            return "{ " + ", ".join(f"{json.dumps(key)} = {toml(item)}" for key, item in value.items()) + " }"
        return json.dumps(value, ensure_ascii=False)

    def setting(key: str, value: object) -> None:
        # JSON string escaping is valid for TOML basic strings here. These are
        # argv elements, never shell source or interpolated command text.
        options.extend(["--config", f"{key}={toml(value)}"])

    if config.reasoning_effort is not None:
        setting("model_reasoning_effort", config.reasoning_effort)
    if not config.managed:
        return options
    options.append("--ignore-user-config")
    if sys.platform == "win32":
        # Ignoring personal config also hides [windows]. Without an explicit
        # native sandbox Codex can downgrade workspace-write to read-only.
        # Keep the stronger OS sandbox; never use an unsandboxed fallback.
        setting("windows.sandbox", "elevated")
    # No ancestor project configuration is needed inside a staged Worker. Do
    # not override system/organization policies or disable their enforcement.
    setting("project_root_markers", [])
    native_openai = config.provider == "openai" and not config.base_url and not config.env_key
    provider_id = "openai" if native_openai else f"geng_{config.provider}"
    setting("model_provider", provider_id)
    if not native_openai:
        setting(f"model_providers.{provider_id}", {
            "name": config.provider,
            "base_url": config.base_url or "https://api.openai.com/v1",
            "wire_api": config.wire_api,
            "env_key": ACTIVE_CREDENTIAL_ENV,
            "requires_openai_auth": False,
        })
        # env_key + requires_openai_auth=False authenticate this provider.
        # forced_login_method is account-wide: Codex may log the user out when
        # their existing ChatGPT login conflicts with it, even in ephemeral mode.
    if config.model_catalog_json is not None:
        setting("model_catalog_json", str(config.model_catalog_json))
    if config.web_search is not None:
        setting("web_search", config.web_search)
    setting("shell_environment_policy", {
        "inherit": "core", "ignore_default_excludes": False,
        "filters": {name: "exclude" for name in (ACTIVE_CREDENTIAL_ENV, "OPENAI_API_KEY")},
        "set": {name: env[name] for name in _RUNTIME_ENV if name in env},
    })
    return options


def redact_provider_secrets(text: str, secrets: tuple[str, ...]) -> str:
    for secret in secrets:
        text = text.replace(secret, "[REDACTED]")
    return text
