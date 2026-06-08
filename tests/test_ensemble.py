from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from geng_agent.config import build_secondary_extraction_client
from geng_agent.pipeline import ReviewPipeline
from tests.test_pipeline import FakeLLM, schema_name


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


class _SecondaryLLM:
    """Secondary extraction model: returns one extra fact the primary doesn't have."""

    def __init__(self) -> None:
        self.calls: list = []

    def complete(self, prompt: str, *, system=None, response_format=None) -> str:
        self.calls.append(schema_name(response_format))
        return json.dumps({
            "paper_domain": "communication",
            "paper_repro_type": "signal_chain",
            "engineering_facts": [{
                "type": "modulation",
                "name": "QPSK",
                "value": {"order": 4},
                "source": {"source_kind": "text", "chunk_id": "text_c1", "page": 1,
                           "section": "Simulation", "quote": "QPSK modulation", "figure_ref": ""},
                "confidence": "high",
                "used_for_reproduction": True,
            }],
            "missing_information": [],
        })


class EnsembleIntegrationTests(unittest.TestCase):
    def test_two_models_union_into_engineering_facts(self) -> None:
        with TemporaryDirectory() as d:
            temp = Path(d)
            paper = temp / "paper.md"
            paper.write_text("Simulation Results\nAWGN channel, QPSK, BER vs SNR.", encoding="utf-8")
            main = FakeLLM()              # primary -> AWGN fact (+ all later stages)
            sec = _SecondaryLLM()         # secondary -> QPSK fact

            # facts_gap_rounds=0 keeps the test focused on the base ensemble (no gap loop).
            ReviewPipeline(client=main, extraction_client_2=sec).run(
                paper, temp / "case", run_repro=False, facts_gap_rounds=0
            )

            facts = json.loads((temp / "case" / "engineering_facts.json").read_text(encoding="utf-8"))
            names = {f["name"] for f in facts["engineering_facts"]}
            self.assertIn("AWGN", names)   # from primary
            self.assertIn("QPSK", names)   # unioned from secondary
            self.assertEqual(facts["_meta"]["ensemble"]["added_by_secondary"], 1)
            self.assertEqual(facts["_meta"]["ensemble"]["secondary_fact_count"], 1)
            # secondary client is only used for the base fact extraction, nothing else
            self.assertEqual(sec.calls, ["engineering_facts"])
            # the secondary's own per-model output is also persisted
            self.assertTrue((temp / "case" / "engineering_facts_model2.json").exists())

    def test_single_model_path_writes_no_model2_file(self) -> None:
        with TemporaryDirectory() as d:
            temp = Path(d)
            paper = temp / "paper.md"
            paper.write_text("Simulation Results\nAWGN channel, BER vs SNR.", encoding="utf-8")
            ReviewPipeline(client=FakeLLM()).run(paper, temp / "case", run_repro=False, facts_gap_rounds=0)
            self.assertFalse((temp / "case" / "engineering_facts_model2.json").exists())


if __name__ == "__main__":
    unittest.main()
