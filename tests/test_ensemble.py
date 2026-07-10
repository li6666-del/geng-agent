from __future__ import annotations

import unittest
from unittest.mock import patch

from geng_agent.config import build_secondary_extraction_client
from geng_agent.pipeline import ReviewPipeline


class FakeLLM:
    model = "fake"
    usage_log: list[dict] = []

    def complete(self, prompt: str, *, system=None, response_format=None) -> str:
        return "{}"


class BuildSecondaryClientTests(unittest.TestCase):
    def test_returns_none_when_unconfigured(self) -> None:
        with patch("geng_agent.config.get_config_value", side_effect=lambda name: None):
            self.assertIsNone(build_secondary_extraction_client())

    def test_returns_none_when_partial(self) -> None:
        # only model set, key/base missing -> still None (all three required)
        cfg = {"GENG_LLM2_MODEL": "MiniMax-M3"}
        with patch("geng_agent.config.get_config_value", side_effect=lambda name: cfg.get(name)):
            self.assertIsNone(build_secondary_extraction_client())

    def test_builds_client_when_all_set(self) -> None:
        cfg = {
            "GENG_LLM2_MODEL": "MiniMax-M3",
            "GENG_LLM2_API_KEY": "sk-secret",
            "GENG_LLM2_BASE_URL": "https://api.minimaxi.com/v1",
        }
        with patch("geng_agent.config.get_config_value", side_effect=lambda name: cfg.get(name)):
            client = build_secondary_extraction_client(timeout=42.0)
        self.assertIsNotNone(client)
        self.assertEqual(client.model, "MiniMax-M3")
        self.assertEqual(client.base_url, "https://api.minimaxi.com/v1")
        self.assertEqual(client.timeout, 42.0)
        # second model always runs in fast (no-thinking) mode
        self.assertEqual(client.thinking, "disabled")


class LlmClientsRollupTests(unittest.TestCase):
    def test_secondary_extraction_client_included_once(self) -> None:
        main = FakeLLM()
        sec = FakeLLM()
        pipe = ReviewPipeline(client=main, extraction_client_2=sec)
        clients = pipe._llm_clients()
        self.assertIn(sec, clients)
        self.assertEqual(len(clients), 2)

    def test_same_object_not_duplicated(self) -> None:
        main = FakeLLM()
        pipe = ReviewPipeline(client=main, extraction_client_2=main)
        self.assertEqual(pipe._llm_clients(), [main])


if __name__ == "__main__":
    unittest.main()
