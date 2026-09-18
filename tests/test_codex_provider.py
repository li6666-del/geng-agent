"""Offline coverage of model selection at the actual Codex transport boundary."""
from __future__ import annotations

from dataclasses import replace
import gzip
import json
from pathlib import Path
import subprocess
import tomllib
from unittest.mock import patch

import pytest

from geng_agent.codex_provider import ACTIVE_CREDENTIAL_ENV
from geng_agent.codex_runner import _clear_ephemeral_capability_cache, run_codex_subprocess
from geng_agent.model_config import ROLES, load_model_config, model_config_scope


FAKE_CREDENTIAL = "1234567890abcdef.glm-example-credential"
HELP_TEXT = "Usage: codex exec [OPTIONS]\n  --ephemeral\n  --ignore-user-config"


def _configs(directory: Path, **changes):
    profile = {
        "provider": "glm", "model": "example-agent-model",
        "base_url": "https://provider.example/v1", "env_key": "GLM_API_KEY",
        "reasoning_effort": "high", "supported_reasoning_efforts": ["high"],
        "web_search": "disabled",
    }
    profile.update(changes)
    path = directory / "models.json"
    path.write_text(json.dumps({"schema_version": 1, "default": "custom",
                               "profiles": {"custom": profile}}), encoding="utf-8")
    return load_model_config(path, getter=lambda _: None)


def _config_values(command):
    """Parse each argv setting exactly as TOML, without shell interpolation."""
    settings = {}
    for index, token in enumerate(command):
        if token != "--config":
            continue
        key, value = command[index + 1].split("=", 1)
        assert key not in settings
        settings[key] = tomllib.loads("value=" + value)["value"]
    return settings


def _invoke(directory, configs, *, env=None, help_text=HELP_TEXT,
            stdout="worker output", failure=None, role="task_writer", **arguments):
    _clear_ephemeral_capability_cache()
    calls = []
    if env is None:
        env = {"PATH": "C:\\tools", "GLM_API_KEY": FAKE_CREDENTIAL}

    def fake_run(command, **kwargs):
        calls.append((list(command), kwargs))
        if command[-2:] == ["exec", "--help"]:
            return subprocess.CompletedProcess(command, 0, help_text, "")
        if failure is not None:
            raise failure
        Path(command[command.index("--output-last-message") + 1]).write_text(stdout, encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout, "")

    with model_config_scope(configs), \
         patch("geng_agent.codex_runner.shutil.which", return_value="C:\\tools\\codex.exe"), \
         patch("geng_agent.codex_runner.codex_safe_env", return_value=dict(env)), \
         patch("geng_agent.codex_runner.get_config_value", return_value=None), \
         patch("geng_agent.codex_runner.subprocess.run", side_effect=fake_run):
        result = run_codex_subprocess(
            role=role, work_dir=directory, audit_dir=directory / "audit",
            prompt="Use only the supplied scientific evidence.", label="writer",
            sandbox="workspace-write", **arguments,
        )
    return result, calls


def test_custom_provider_argv_is_valid_toml_and_preserves_execution_inputs(tmp_path):
    configs = _configs(tmp_path)
    image = tmp_path / "paper.png"
    image.write_bytes(b"image input")
    schema = tmp_path / "response.schema.json"
    schema.write_text('{"type":"object"}', encoding="utf-8")
    runtime = {
        "GENG_PYTHON": r"C:\实验 环境\python.exe",
        "GENG_EXECUTION_BROKER": '{"command":"run", "path":"C:\\\\case"}',
    }
    env = {"PATH": "C:\\tools", "GLM_API_KEY": FAKE_CREDENTIAL,
           "OPENAI_API_KEY": "unused-openai-credential", "OTHER_TOKEN": "unused-token"}
    result, calls = _invoke(tmp_path, configs, env=env, extra_env=runtime,
                            output_schema=schema, image_paths=[image])
    assert result["ok"]
    assert len(calls) == 2
    assert ACTIVE_CREDENTIAL_ENV not in calls[0][1]["env"]
    assert "OPENAI_API_KEY" not in calls[0][1]["env"]
    command, arguments = calls[1]
    settings = _config_values(command)
    assert command[command.index("--model") + 1] == "example-agent-model"
    assert command[command.index("--output-schema") + 1] == str(schema)
    assert command[command.index("--image") + 1] == str(image)
    assert "--ignore-user-config" in command and "--ephemeral" in command
    assert settings["model_provider"] == "geng_glm"
    assert settings["model_providers.geng_glm"] == {
        "name": "glm", "base_url": "https://provider.example/v1", "wire_api": "responses",
        "env_key": ACTIVE_CREDENTIAL_ENV, "requires_openai_auth": False,
    }
    assert settings["model_reasoning_effort"] == "high"
    assert "forced_login_method" not in settings
    assert settings["web_search"] == "disabled"
    assert settings["project_root_markers"] == []
    policy = settings["shell_environment_policy"]
    assert policy["inherit"] == "core"
    assert policy["ignore_default_excludes"] is False
    assert policy["filters"][ACTIVE_CREDENTIAL_ENV] == "exclude"
    assert policy["filters"]["OPENAI_API_KEY"] == "exclude"
    assert policy["set"] == runtime
    worker_env = arguments["env"]
    assert worker_env[ACTIVE_CREDENTIAL_ENV] == FAKE_CREDENTIAL
    assert not {"GLM_API_KEY", "OPENAI_API_KEY", "OTHER_TOKEN"} & worker_env.keys()
    assert FAKE_CREDENTIAL not in json.dumps(command)
    assert FAKE_CREDENTIAL not in json.dumps(result)
    manifest = json.loads(Path(result["input_manifest"]).read_text(encoding="utf-8"))
    assert manifest["model_config"] == configs.identity()
    assert manifest["images"][0]["bytes"] == len(b"image input")


def test_all_workers_use_identical_provider_model_and_reasoning(tmp_path):
    config = _configs(tmp_path, reasoning_effort="max", supported_reasoning_efforts=["high", "max"])
    selections = []
    for role in (*ROLES, "auxiliary"):
        result, calls = _invoke(tmp_path, config, role=role)
        assert result["ok"]
        command = calls[-1][0]
        selections.append((command[command.index("--model") + 1], _config_values(command)))
        assert result["model_config"] == config.identity()
    assert all(selection == selections[0] for selection in selections)


def test_single_worker_cannot_override_global_reasoning(tmp_path):
    with pytest.raises(TypeError):
        _invoke(tmp_path, _configs(tmp_path), reasoning_effort="low")


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_managed_windows_worker_explicitly_keeps_native_sandbox(tmp_path, platform):
    with patch("geng_agent.codex_provider.sys.platform", platform):
        result, calls = _invoke(tmp_path, _configs(tmp_path))
    assert result["ok"]
    settings = _config_values(calls[-1][0])
    assert settings.get("windows.sandbox") == ("elevated" if platform == "win32" else None)


def test_bare_non_openai_credential_is_redacted_from_persisted_transcripts(tmp_path):
    result, _ = _invoke(tmp_path, _configs(tmp_path), stdout=f"provider error: {FAKE_CREDENTIAL}")
    transcript = Path(result["transcript"]).read_text(encoding="utf-8")
    with gzip.open(result["full_transcript"], "rt", encoding="utf-8") as stream:
        full_transcript = stream.read()
    assert "[REDACTED]" in transcript
    assert FAKE_CREDENTIAL not in transcript + full_transcript
    assert FAKE_CREDENTIAL not in Path(result["last_message_path"]).read_text(encoding="utf-8")
    for path in (tmp_path / "audit").rglob("*.json"):
        assert FAKE_CREDENTIAL not in path.read_text(encoding="utf-8")


def test_subprocess_exception_does_not_persist_bare_provider_credential(tmp_path):
    result, _ = _invoke(tmp_path, _configs(tmp_path),
                        failure=RuntimeError(f"transport failure: {FAKE_CREDENTIAL}"))
    assert result["error_kind"] == "subprocess_error"
    assert FAKE_CREDENTIAL not in json.dumps(result)
    assert "[REDACTED]" in result["error"]


@pytest.mark.parametrize("field,inputs", [
    ("supports_tools", {}),
    ("supports_images", {"image_paths": [Path("paper.png")]}),
    ("supports_json_schema", {"output_schema": Path("response.json")}),
])
def test_missing_required_model_capability_stops_before_any_subprocess(tmp_path, field, inputs):
    result, calls = _invoke(tmp_path, _configs(tmp_path, **{field: False}), **inputs)
    assert not result["ok"]
    assert result["error_kind"] == "invalid_model_config"
    assert calls == []


def test_text_only_stage_can_use_profile_without_images_or_json_schema(tmp_path):
    result, calls = _invoke(tmp_path, _configs(tmp_path, supports_images=False,
                                              supports_json_schema=False, reasoning_effort="omit"))
    assert result["ok"]
    assert "model_reasoning_effort" not in _config_values(calls[-1][0])


def test_cli_without_configuration_isolation_does_not_start_worker(tmp_path):
    result, calls = _invoke(tmp_path, _configs(tmp_path), help_text="Usage: codex exec\n --ephemeral")
    assert not result["ok"]
    assert result["error_kind"] == "unsupported_cli_feature"
    assert len(calls) == 1
    assert calls[0][0][-2:] == ["exec", "--help"]


@pytest.mark.parametrize("override", [
    "codex --profile alternate", "codex --profile=alternate", "codex -palternate",
    "codex -p alternate", "codex -cmodel_provider=other", "codex -c model=other",
    "codex --config=model_provider=other", "codex --model other", "codex -mother",
    "codex --oss", "codex --local-provider=ollama", "codex --enable feature",
])
def test_competing_model_overrides_are_rejected_before_launch(tmp_path, override):
    result, calls = _invoke(tmp_path, _configs(tmp_path), command_override=override)
    assert result["error_kind"] == "invalid_model_config"
    assert calls == []


def test_missing_credential_fails_without_falling_back_to_openai(tmp_path):
    result, calls = _invoke(tmp_path, _configs(tmp_path),
                            env={"PATH": "C:\\tools", "OPENAI_API_KEY": "other-account"})
    assert result["error_kind"] == "invalid_model_config"
    assert "GLM_API_KEY" in result["error"]
    assert calls == []


def test_changed_catalog_is_rejected_before_launch(tmp_path):
    catalog = tmp_path / "catalog.json"
    catalog.write_text('{"models":[]}', encoding="utf-8")
    configs = _configs(tmp_path, model_catalog_json=str(catalog))
    catalog.write_text('{"models":[{"slug":"changed"}]}', encoding="utf-8")
    result, calls = _invoke(tmp_path, configs)
    assert result["error_kind"] == "invalid_model_config"
    assert calls == []


def test_staged_project_cannot_add_another_codex_configuration(tmp_path):
    configs = _configs(tmp_path)
    directory = tmp_path / ".codex"
    directory.mkdir()
    (directory / "config.toml").write_text('model="unexpected"', encoding="utf-8")
    result, calls = _invoke(tmp_path, configs)
    assert result["error_kind"] == "invalid_model_config"
    assert calls == []


def test_native_openai_keeps_account_auth_without_custom_provider(tmp_path):
    native = replace(_configs(tmp_path), provider="openai", model="gpt-6-astra",
                     base_url=None, env_key=None)
    result, calls = _invoke(tmp_path, native,
                            env={"PATH": "C:\\tools", "OPENAI_API_KEY": "openai-account-key"})
    assert result["ok"]
    command, arguments = calls[-1]
    settings = _config_values(command)
    assert settings["model_provider"] == "openai"
    assert not any(key.startswith("model_providers.") for key in settings)
    assert "forced_login_method" not in settings
    assert arguments["env"]["OPENAI_API_KEY"] == "openai-account-key"
    assert ACTIVE_CREDENTIAL_ENV not in arguments["env"]
    assert "openai-account-key" not in json.dumps(result)


def test_models_cli_checks_configuration_offline_without_requesting_credentials(tmp_path, capsys):
    from geng_agent.cli import main

    _configs(tmp_path)
    with patch("geng_agent.model_config._get", return_value=lambda _: None), \
         patch("geng_agent.codex_runner.subprocess.run") as runner:
        code = main(["models", "--config", str(tmp_path / "models.json")])
    assert code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["provider"] == "glm"
    assert result["env_key"] == "GLM_API_KEY"
    assert not set(ROLES) & set(result)
    assert FAKE_CREDENTIAL not in json.dumps(result)
    runner.assert_not_called()
