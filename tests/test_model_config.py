import copy
from contextvars import copy_context
from dataclasses import FrozenInstanceError
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from concurrent.futures import ThreadPoolExecutor
import unittest

from geng_agent.model_config import (
    DEFAULT_MODEL,
    ROLES,
    get_current_model_config,
    load_model_config,
    model_config_scope,
    resolve_model_config,
)


class ModelConfigTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.path = self.directory / "models.json"

    def document(self):
        return {
            "schema_version": 1,
            "default": "writer",
            "profiles": {
                "writer": {
                    "provider": "deepseek",
                    "model": "example-coding-model",
                    "base_url": "https://provider.example/v1",
                    "env_key": "EXAMPLE_API_KEY",
                },
                "reviewer": {"provider": "openai", "model": DEFAULT_MODEL, "reasoning_effort": "high"},
            },
        }

    def write(self, document):
        self.path.write_text(json.dumps(document), encoding="utf-8")
        return self.path

    def load(self, document=None):
        return load_model_config(self.write(document if document is not None else self.document()), getter=lambda _: None)

    def test_no_file_uses_global_defaults_and_ignores_retired_role_overrides(self):
        configs = load_model_config(getter=lambda _: None)
        self.assertEqual(configs.model, DEFAULT_MODEL)
        self.assertEqual(configs.reasoning_effort, "medium")
        self.assertFalse(configs.managed)
        settings = {
            "GENG_CODEX_MODEL": "other-model",
            "GENG_CODEX_REASONING_EFFORT": "low",
            "GENG_CODEX_TASK_REPORTER_MODEL": "review-model",
            "GENG_CODEX_TASK_REPORTER_REASONING_EFFORT": "max",
        }
        configs = load_model_config(getter=settings.get)
        self.assertEqual(configs.model, "other-model")
        self.assertEqual(configs.reasoning_effort, "low")
        for role in ROLES:
            self.assertEqual(resolve_model_config(role, getter=settings.get), configs)

    def test_nondefault_model_does_not_inherit_medium(self):
        configs = load_model_config(getter={"GENG_CODEX_MODEL": "some-other-model"}.get)
        self.assertIsNone(configs.reasoning_effort)
        config = resolve_model_config("test", getter={"GENG_CODEX_MODEL": "some-other-model"}.get)
        self.assertIsNone(config.reasoning_effort)
        self.assertEqual(config.model, "some-other-model")

    def test_file_profiles_are_complete_and_do_not_mix_legacy_settings(self):
        path = self.write(self.document())
        settings = {"GENG_MODEL_CONFIG": str(path), "GENG_CODEX_MODEL": "unrelated", "GENG_CODEX_REASONING_EFFORT": "ultra"}
        configs = load_model_config(getter=settings.get)
        self.assertEqual(configs.provider, "deepseek")
        self.assertIsNone(configs.reasoning_effort)
        self.assertTrue(configs.managed)
        with self.assertRaises(FrozenInstanceError):
            configs.model = "mutated"

    def test_explicit_file_wins_over_environment_file(self):
        configs = load_model_config(self.write(self.document()), getter={"GENG_MODEL_CONFIG": "does-not-exist.json"}.get)
        self.assertEqual(configs.provider, "deepseek")

    def test_every_role_and_auxiliary_worker_share_one_instance(self):
        document = self.document()
        document["default"] = "reviewer"
        configs = self.load(document)
        with model_config_scope(configs):
            for role in (*ROLES, "test"):
                self.assertIs(resolve_model_config(role), configs)
                self.assertEqual(resolve_model_config(role).provider, "openai")

    def test_role_configurations_fail_with_migration_message(self):
        for retired in ({}, {"task_reporter": "reviewer"}):
            document = self.document()
            document["roles"] = retired
            with self.assertRaisesRegex(ValueError, "已取消 roles"):
                self.load(document)
        with self.assertRaises(ValueError):
            with model_config_scope({role: self.load() for role in ROLES}):
                pass

    def test_omit_is_different_from_explicit_none_reasoning_level(self):
        document = self.document()
        profile = document["profiles"]["writer"]
        profile["reasoning_effort"] = "none"
        self.assertEqual(self.load(document).reasoning_effort, "none")
        profile["reasoning_effort"] = "omit"
        self.assertIsNone(self.load(document).reasoning_effort)
        configs = self.load(document)
        with model_config_scope(configs):
            self.assertIsNone(resolve_model_config("analysis").reasoning_effort)

    def test_declared_reasoning_levels_reject_unsupported_selection(self):
        document = self.document()
        profile = document["profiles"]["writer"]
        profile.update(reasoning_effort="max", supported_reasoning_efforts=["high", "max"])
        configs = self.load(document)
        with model_config_scope(configs):
            self.assertEqual(resolve_model_config("analysis").reasoning_effort, "max")
            with self.assertRaises(TypeError):
                resolve_model_config("analysis", reasoning_effort="high")
        profile["reasoning_effort"] = "medium"
        with self.assertRaises(ValueError):
            self.load(document)

    def test_urls_cannot_carry_credentials_or_non_http_schemes(self):
        for address in (
            "https://secret-marker@provider.example/v1", "https://user:secret-marker@provider.example",
            "https://provider.example/v1?token=secret-marker", "https://provider.example/#secret-marker",
            "file:///secret-marker", "http://", "https://provider.example:bad", "https://provider.example/?",
        ):
            with self.subTest(address=address):
                document = self.document()
                document["profiles"]["writer"]["base_url"] = address
                with self.assertRaises(ValueError) as error:
                    self.load(document)
                self.assertNotIn("secret-marker", str(error.exception))

    def test_openai_custom_endpoint_requires_explicit_credential_source(self):
        document = self.document()
        document["default"] = "reviewer"
        profile = document["profiles"]["reviewer"]
        profile["base_url"] = "https://gateway.example/v1"
        with self.assertRaises(ValueError):
            self.load(document)
        profile["env_key"] = "GATEWAY_API_KEY"
        self.assertEqual(self.load(document).env_key, "GATEWAY_API_KEY")
        del profile["base_url"]
        self.assertIsNone(self.load(document).base_url)

    def test_invalid_fields_and_types_fail_without_echoing_values(self):
        bad_fields = (
            ("api_key", "secret-marker"), ("bearer", "secret-marker"), ("managed", False),
            ("provider", "ollama"), ("provider", "lmstudio"), ("provider", "amazon-bedrock"), ("provider", "bad.provider"),
            ("model", ""), ("wire_api", "chat"), ("supports_images", "false"),
            ("supports_tools", 1), ("supports_json_schema", None), ("web_search", []),
            ("reasoning_effort", "secret-marker"), ("supported_reasoning_efforts", ["omit"]),
            ("supported_reasoning_efforts", ["high", "high"]),
            ("env_key", "secret-marker"), ("env_key", "PATH"), ("env_key", "codex_home"),
            ("base_url", None), ("env_key", None),
        )
        for field, value in bad_fields:
            with self.subTest(field=field, value=value):
                document = self.document()
                document["profiles"]["writer"][field] = value
                with self.assertRaises(ValueError) as error:
                    self.load(document)
                self.assertNotIn("secret-marker", str(error.exception))

    def test_document_shape_and_references_are_strict(self):
        documents = []
        for key, value in (("schema_version", True), ("schema_version", 2), ("default", "missing"),
                           ("profiles", {}), ("roles", {"unknown-role": "writer"}),
                           ("roles", {"task_writer": "missing"}), ("extra", "secret-marker")):
            document = self.document()
            document[key] = value
            documents.append(document)
        documents.extend([[], {"schema_version": 1}])
        for document in documents:
            with self.subTest(document=document):
                with self.assertRaises(ValueError) as error:
                    self.load(document)
                self.assertNotIn("secret-marker", str(error.exception))

    def test_invalid_json_and_missing_file_do_not_echo_content(self):
        self.path.write_text('{"secret-marker":', encoding="utf-8")
        with self.assertRaises(ValueError) as error:
            load_model_config(self.path, getter=lambda _: None)
        self.assertNotIn("secret-marker", str(error.exception))
        self.path.write_text('{"schema_version": 1, "schema_version": 1}', encoding="utf-8")
        with self.assertRaises(ValueError):
            load_model_config(self.path, getter=lambda _: None)
        with self.assertRaises(ValueError) as error:
            load_model_config(self.directory / "secret-marker.json", getter=lambda _: None)
        self.assertNotIn("secret-marker", str(error.exception))

    def test_catalog_content_is_frozen_and_relative_to_config(self):
        catalog = self.directory / "catalog.json"
        catalog.write_text('{"models": []}', encoding="utf-8")
        document = self.document()
        document["profiles"]["writer"]["model_catalog_json"] = "catalog.json"
        config = self.load(document)
        self.assertEqual(config.model_catalog_json, catalog.resolve())
        original_identity = config.identity()
        config.validate_catalog_unchanged()
        catalog.write_text('{"models": [{"slug": "changed"}]}', encoding="utf-8")
        self.assertEqual(config.identity(), original_identity)
        with self.assertRaises(ValueError):
            config.validate_catalog_unchanged()
        self.assertNotEqual(self.load(document).identity(), original_identity)
        catalog.unlink()
        with self.assertRaises(ValueError):
            self.load(document)

    def test_identity_distinguishes_provider_endpoint_capability_and_parameters(self):
        document = self.document()
        baseline = self.load(document).identity()
        serialized = json.dumps(baseline)
        self.assertNotIn("https://", serialized)
        self.assertIn("EXAMPLE_API_KEY", serialized)
        for field, value in (("provider", "other"), ("base_url", "https://other.example/v1"),
                             ("model", "another-model"), ("reasoning_effort", "low"),
                             ("supports_images", False), ("supports_tools", False),
                             ("supports_json_schema", False), ("web_search", "disabled")):
            with self.subTest(field=field):
                modified = copy.deepcopy(document)
                modified["profiles"]["writer"][field] = value
                self.assertNotEqual(self.load(modified).identity(), baseline)

    def test_loading_config_does_not_read_provider_credentials(self):
        path = self.write(self.document())
        looked_up = []

        def getter(name):
            looked_up.append(name)
            return str(path) if name == "GENG_MODEL_CONFIG" else "secret-marker"

        configs = load_model_config(getter=getter)
        self.assertEqual(looked_up, ["GENG_MODEL_CONFIG"])
        self.assertNotIn("secret-marker", json.dumps(configs.identity()))

    def test_scopes_restore_and_cannot_be_mutated(self):
        configs = self.load()
        original = configs
        alternate = load_model_config(getter=lambda _: None)
        self.assertIsNone(get_current_model_config())
        with model_config_scope(configs):
            snapshot = get_current_model_config()
            with self.assertRaises(FrozenInstanceError):
                snapshot.model = "changed"
            self.assertEqual(resolve_model_config("analysis"), original)
            with self.assertRaises(RuntimeError):
                with model_config_scope(alternate):
                    self.assertEqual(resolve_model_config("analysis").model, DEFAULT_MODEL)
                    raise RuntimeError("leave nested scope")
            self.assertEqual(resolve_model_config("analysis"), original)
        self.assertIsNone(get_current_model_config())

    def test_copied_context_holds_selection_across_worker_thread(self):
        configs = self.load()
        with model_config_scope(configs):
            context = copy_context()
            with ThreadPoolExecutor(max_workers=1) as pool:
                actual = pool.submit(context.run, resolve_model_config, "task_writer").result()
            self.assertEqual(actual, configs)
        self.assertIsNone(get_current_model_config())


if __name__ == "__main__":
    unittest.main()
