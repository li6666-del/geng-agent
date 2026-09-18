"""Project-owned Codex model profiles and immutable, run-local selections.

Profiles contain credential environment variable *names*, never credentials.
The Codex runner is responsible for transporting the selected credential.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Callable, Iterator
from urllib.parse import urlsplit


ROLES = ("analysis", "foundation_writer", "task_writer", "task_reporter", "report_editor")
DEFAULT_MODEL = "gpt-6-astra"
REASONING_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"})
_IDENTIFIER = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_RESERVED_ENV_NAMES = frozenset({
    "PATH", "PATHEXT", "HOME", "USERPROFILE", "CODEX_HOME", "SHELL", "COMSPEC",
    "TEMP", "TMP", "PWD", "PYTHONPATH", "NODE_PATH", "LD_PRELOAD", "LD_LIBRARY_PATH",
    "SYSTEMROOT", "WINDIR", "APPDATA", "LOCALAPPDATA", "PROGRAMFILES",
})
_PROFILE_FIELDS = frozenset({
    "provider", "model", "reasoning_effort", "base_url", "env_key", "wire_api",
    "supports_images", "supports_tools", "supports_json_schema", "model_catalog_json",
    "web_search", "supported_reasoning_efforts",
})
ConfigGetter = Callable[[str], str | None]


def _fail(message: str) -> ValueError:
    # Never echo values: malformed input can itself contain a credential.
    return ValueError(f"模型配置无效：{message}")


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or any(ord(char) < 32 for char in value):
        raise _fail(f"{field} 必须是非空文本。")
    return value.strip()


def _reasoning(value: object) -> str | None:
    if value is None:
        return None
    normalized = _text(value, "reasoning_effort").lower()
    if normalized == "omit":
        return None
    if normalized not in REASONING_EFFORTS:
        raise _fail("reasoning_effort 不受支持；使用受支持的推理等级，或 omit 表示不显式覆盖 Codex 的模型推理设置。")
    return normalized


def _catalog_digest(path: Path) -> str:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        raise _fail("model_catalog_json 必须指向可读取的文件。") from None


@dataclass(frozen=True)
class CodexModelConfig:
    provider: str
    model: str
    reasoning_effort: str | None = None
    base_url: str | None = None
    env_key: str | None = None
    wire_api: str = "responses"
    supports_images: bool = True
    supports_tools: bool = True
    supports_json_schema: bool = True
    model_catalog_json: Path | None = None
    web_search: str | None = None
    supported_reasoning_efforts: tuple[str, ...] | None = None
    model_catalog_sha256: str | None = None
    managed: bool = False

    def identity(self) -> dict[str, object]:
        """Return a stable, secret-free identity suitable for caches and audit."""
        return {
            "config_contract": "codex-model-config-v1",
            "managed": self.managed,
            "provider": self.provider if self.managed else "inherited",
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "endpoint_sha256": hashlib.sha256(self.base_url.encode("utf-8")).hexdigest() if self.base_url else None,
            "env_key": self.env_key,
            "wire_api": self.wire_api,
            "supports_images": self.supports_images,
            "supports_tools": self.supports_tools,
            "supports_json_schema": self.supports_json_schema,
            "model_catalog_sha256": self.model_catalog_sha256,
            "web_search": self.web_search,
            "supported_reasoning_efforts": list(self.supported_reasoning_efforts) if self.supported_reasoning_efforts is not None else None,
        }

    def validate_catalog_unchanged(self) -> None:
        """Reject a mutable catalog that no longer matches the run snapshot."""
        if self.model_catalog_json is not None and _catalog_digest(self.model_catalog_json) != self.model_catalog_sha256:
            raise _fail("model_catalog_json 在本次配置加载后已更改；请重新开始运行以使用新配置。")


_CURRENT_CONFIG: ContextVar[CodexModelConfig | None] = ContextVar("geng_codex_model_config", default=None)


def get_current_model_config() -> CodexModelConfig | None:
    return _CURRENT_CONFIG.get()


@contextmanager
def model_config_scope(config: CodexModelConfig) -> Iterator[None]:
    """Bind one immutable model for every worker in this run."""
    if not isinstance(config, CodexModelConfig):
        raise _fail("一次运行只能使用一个全局模型配置，不能按智能体分配。")
    token = _CURRENT_CONFIG.set(config)
    try:
        yield
    finally:
        _CURRENT_CONFIG.reset(token)


def _get(getter: ConfigGetter | None) -> ConfigGetter:
    if getter is not None:
        return getter
    from .config import get_config_value
    return get_config_value


def _profile(value: object, config_dir: Path) -> CodexModelConfig:
    if not isinstance(value, dict) or set(value) - _PROFILE_FIELDS:
        raise _fail("profile 必须是对象，且只能包含已声明的字段；密钥请通过 env_key 引用。")
    provider = _text(value.get("provider"), "provider")
    if not _IDENTIFIER.fullmatch(provider) or provider in {"ollama", "lmstudio", "amazon-bedrock"}:
        raise _fail("provider 必须是安全的小写标识，且不能使用 Codex 保留的本地或 AWS 服务标识。")
    model = _text(value.get("model"), "model")
    base_url = value.get("base_url")
    if base_url is not None:
        base_url = _text(base_url, "base_url")
        try:
            parsed = urlsplit(base_url)
            valid_url = (
                parsed.scheme in {"http", "https"} and bool(parsed.hostname)
                and parsed.username is None and parsed.password is None
                and not parsed.query and not parsed.fragment
                and "?" not in base_url and "#" not in base_url
                and not any(char.isspace() for char in base_url)
            )
            parsed.port  # Validate malformed/out-of-range ports without echoing them.
        except ValueError:
            valid_url = False
        if not valid_url:
            raise _fail("base_url 必须是 HTTP(S) 地址，且不能含用户凭据、查询参数或片段。")
        base_url = base_url.rstrip("/")
    env_key = value.get("env_key")
    if env_key is not None:
        env_key = _text(env_key, "env_key")
        if not _ENV_NAME.fullmatch(env_key) or env_key.upper() in _RESERVED_ENV_NAMES:
            raise _fail("env_key 必须是专用凭据环境变量的合法名称，不能引用系统配置变量。")
    if provider != "openai" and (base_url is None or env_key is None):
        raise _fail("自定义 provider 必须显式指定 base_url 和 env_key。")
    if provider == "openai" and base_url is not None and env_key is None:
        raise _fail("openai 使用自定义 base_url 时也必须指定 env_key；内建账户认证仅用于默认端点。")
    wire_api = value.get("wire_api", "responses")
    if wire_api != "responses":
        raise _fail("wire_api 目前仅支持 responses。")
    capabilities: dict[str, bool] = {}
    for field in ("supports_images", "supports_tools", "supports_json_schema"):
        flag = value.get(field, True)
        if not isinstance(flag, bool):
            raise _fail(f"{field} 必须是布尔值。")
        capabilities[field] = flag
    reasoning = _reasoning(value.get("reasoning_effort"))
    supported = value.get("supported_reasoning_efforts")
    if supported is not None:
        if not isinstance(supported, list):
            raise _fail("supported_reasoning_efforts 必须是推理等级数组。")
        normalized = tuple(_reasoning(item) for item in supported)
        if any(item is None for item in normalized) or len(set(normalized)) != len(normalized):
            raise _fail("supported_reasoning_efforts 不能包含空值、omit 或重复等级。")
        supported = tuple(sorted(normalized))
        if reasoning is not None and reasoning not in supported:
            raise _fail("reasoning_effort 不在该 profile 声明的 supported_reasoning_efforts 中。")
    web_search = value.get("web_search")
    if web_search is not None and (not isinstance(web_search, str) or web_search not in {"disabled", "cached", "live"}):
        raise _fail("web_search 只能是 disabled、cached 或 live。")
    catalog_path = None
    catalog_hash = None
    if value.get("model_catalog_json") is not None:
        raw_catalog = _text(value["model_catalog_json"], "model_catalog_json")
        try:
            catalog_path = Path(raw_catalog).expanduser()
            if not catalog_path.is_absolute():
                catalog_path = config_dir / catalog_path
            catalog_path = catalog_path.resolve()
        except (OSError, RuntimeError, ValueError):
            raise _fail("model_catalog_json 路径无效。") from None
        catalog_hash = _catalog_digest(catalog_path)
    return CodexModelConfig(
        provider=provider, model=model, reasoning_effort=reasoning,
        base_url=base_url, env_key=env_key, wire_api=wire_api,
        model_catalog_json=catalog_path, model_catalog_sha256=catalog_hash,
        web_search=web_search, supported_reasoning_efforts=supported, managed=True, **capabilities,
    )


def _json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _fail("JSON 不允许重复字段。")
        result[key] = value
    return result


def load_model_config(path: Path | None = None, *, getter: ConfigGetter | None = None) -> CodexModelConfig:
    getter = _get(getter)
    configured_path = path if path is not None else getter("GENG_MODEL_CONFIG")
    if configured_path:
        try:
            config_path = Path(configured_path).expanduser().resolve()
            document = json.loads(config_path.read_text(encoding="utf-8-sig"), object_pairs_hook=_json_object)
        except (OSError, UnicodeError, ValueError, RuntimeError):
            raise _fail("无法读取配置文件，或文件不是有效的 UTF-8 JSON。") from None
        if isinstance(document, dict) and "roles" in document:
            raise _fail("已取消 roles 分配；请删除 roles，并用 default 为整个项目选择一种模型。")
        if not isinstance(document, dict) or set(document) - {"schema_version", "default", "profiles"}:
            raise _fail("配置顶层必须是对象，且只能包含 schema_version、default、profiles。")
        if type(document.get("schema_version")) is not int or document["schema_version"] != 1:
            raise _fail("schema_version 必须是整数 1。")
        raw_profiles = document.get("profiles")
        if not isinstance(raw_profiles, dict) or not raw_profiles:
            raise _fail("profiles 必须是非空对象。")
        profiles = {}
        for name, value in raw_profiles.items():
            if not isinstance(name, str) or not _IDENTIFIER.fullmatch(name):
                raise _fail("profile 名称必须是安全的小写标识。")
            profiles[name] = _profile(value, config_path.parent)
        default_name = document.get("default")
        if not isinstance(default_name, str) or default_name not in profiles:
            raise _fail("default 必须引用已定义的 profile。")
        return profiles[default_name]

    # Legacy environment settings apply only when no profile file is selected.
    default_model = _text(getter("GENG_CODEX_MODEL") or DEFAULT_MODEL, "GENG_CODEX_MODEL")
    common_effort = getter("GENG_CODEX_REASONING_EFFORT")
    default_effort = _reasoning(common_effort) if common_effort else ("medium" if default_model == DEFAULT_MODEL else None)
    return CodexModelConfig(provider="openai", model=default_model, reasoning_effort=default_effort)


def resolve_model_config(role: str, *, getter: ConfigGetter | None = None) -> CodexModelConfig:
    # Role names remain useful in audit records and prompt identities; they
    # never select a different model, provider, or reasoning effort.
    del role
    return _CURRENT_CONFIG.get() or load_model_config(getter=getter)
