import json
import unittest

from geng_agent.facts_normalize import (
    engineering_facts_floor_issues,
    finalize_engineering_facts,
    normalize_engineering_facts_candidate,
    recover_truncated_engineering_facts,
    select_valid_engineering_facts,
)
from geng_agent.schemas import validate_stage

CHUNK_IDS = {"text_c1", "text_c2", "text_c3"}


def good_fact(**overrides):
    fact = {
        "type": "channel_model",
        "name": "AWGN",
        "value": {"snr_db": [0, 2, 4]},
        "source": {"chunk_id": "text_c1", "page": 3, "section": "Simulation", "quote": "AWGN channel"},
        "confidence": "high",
        "used_for_reproduction": True,
    }
    fact.update(overrides)
    return fact


class NormalizeTests(unittest.TestCase):
    def test_maps_fact_type_synonym_and_unknown(self) -> None:
        data = {
            "paper_domain": "communication",
            "paper_repro_type": "signal_chain",
            "engineering_facts": [
                good_fact(type="parameter"),
                good_fact(type="totally_made_up"),
            ],
            "missing_information": [],
        }
        out, coercions = normalize_engineering_facts_candidate(data)
        self.assertEqual(out["engineering_facts"][0]["type"], "simulation_parameter")
        self.assertEqual(out["engineering_facts"][1]["type"], "other")
        self.assertTrue(any("type" in c for c in coercions))

    def test_maps_paper_repro_type_synonym_and_fills_domain(self) -> None:
        out, coercions = normalize_engineering_facts_candidate(
            {"paper_repro_type": "phy_simulation", "engineering_facts": [], "missing_information": []}
        )
        self.assertEqual(out["paper_repro_type"], "signal_chain")
        self.assertEqual(out["paper_domain"], "communication")
        self.assertTrue(any("paper_domain" in c for c in coercions))

    def test_fixes_null_section_missing_page_and_scalar_value(self) -> None:
        fact = good_fact()
        fact["source"]["section"] = None
        del fact["source"]["page"]
        fact["value"] = "BPSK"
        out, _ = normalize_engineering_facts_candidate(
            {"paper_repro_type": "signal_chain", "engineering_facts": [fact], "missing_information": []}
        )
        src = out["engineering_facts"][0]["source"]
        self.assertEqual(src["section"], "")
        self.assertIsNone(src["page"])
        self.assertEqual(out["engineering_facts"][0]["value"], {"value": "BPSK"})

    def test_coerces_page_string_and_confidence_synonym(self) -> None:
        fact = good_fact(confidence="med")
        fact["source"]["page"] = "page 12"
        out, _ = normalize_engineering_facts_candidate(
            {"paper_repro_type": "signal_chain", "engineering_facts": [fact], "missing_information": []}
        )
        self.assertEqual(out["engineering_facts"][0]["source"]["page"], 12)
        self.assertEqual(out["engineering_facts"][0]["confidence"], "medium")

    def test_strips_unknown_keys_on_fact_and_source(self) -> None:
        fact = good_fact(explanation="prose the model added")
        fact["source"]["confidence_note"] = "extra"
        out, coercions = normalize_engineering_facts_candidate(
            {"paper_repro_type": "signal_chain", "engineering_facts": [fact], "missing_information": []}
        )
        self.assertNotIn("explanation", out["engineering_facts"][0])
        self.assertNotIn("confidence_note", out["engineering_facts"][0]["source"])
        self.assertTrue(any("unknown keys" in c for c in coercions))

    def test_drops_malformed_missing_information(self) -> None:
        out, _ = normalize_engineering_facts_candidate(
            {
                "paper_repro_type": "signal_chain",
                "engineering_facts": [],
                "missing_information": [
                    {"name": "seed", "why_needed": "RNG seed", "impact": "med"},
                    {"name": "", "why_needed": "no name"},
                    "not even an object",
                ],
            }
        )
        self.assertEqual(len(out["missing_information"]), 1)
        self.assertEqual(out["missing_information"][0]["impact"], "medium")


class PartialAcceptanceTests(unittest.TestCase):
    def test_drops_only_invalid_facts(self) -> None:
        data = {
            "engineering_facts": [
                good_fact(name="AWGN"),
                good_fact(name="ghost", source={"chunk_id": "text_unknown", "page": 1, "section": "x", "quote": "q"}),
                good_fact(name="noquote", source={"chunk_id": "text_c1", "page": 1, "section": "x", "quote": ""}),
            ]
        }
        kept, dropped = select_valid_engineering_facts(data, CHUNK_IDS)
        self.assertEqual([f["name"] for f in kept], ["AWGN"])
        reasons = {d["name"]: d["reason"] for d in dropped}
        self.assertIn("chunk_id", reasons["ghost"])
        self.assertEqual(len(dropped), 2)

    def test_finalize_produces_schema_clean_document(self) -> None:
        messy = {
            "paper_repro_type": "phy_simulation",
            "engineering_facts": [
                good_fact(type="parameter", explanation="extra"),
                good_fact(name="bad", source={"chunk_id": "nope", "page": None, "section": None, "quote": "q"}),
            ],
            "missing_information": [{"name": "seed", "why_needed": "rng", "impact": "high"}],
        }
        doc = finalize_engineering_facts(messy, CHUNK_IDS)
        self.assertEqual(validate_stage("engineering_facts", doc), [])
        self.assertEqual(len(doc["engineering_facts"]), 1)
        self.assertEqual(doc["paper_repro_type"], "signal_chain")
        self.assertTrue(doc["_meta"]["normalization_used"])
        self.assertTrue(doc["_meta"]["partial_acceptance_used"])
        self.assertEqual(doc["_meta"]["dropped_fact_count"], 1)

    def test_clean_input_is_left_untouched_without_meta(self) -> None:
        clean = {
            "paper_domain": "communication",
            "paper_repro_type": "signal_chain",
            "engineering_facts": [good_fact()],
            "missing_information": [],
        }
        doc = finalize_engineering_facts(clean, CHUNK_IDS)
        self.assertEqual(validate_stage("engineering_facts", doc), [])
        self.assertNotIn("_meta", doc)
        self.assertEqual(doc["engineering_facts"][0]["name"], "AWGN")


class FloorTests(unittest.TestCase):
    def test_floor_flags_zero_facts(self) -> None:
        self.assertTrue(engineering_facts_floor_issues({"engineering_facts": []}))

    def test_floor_passes_with_one_fact(self) -> None:
        self.assertEqual(engineering_facts_floor_issues({"engineering_facts": [good_fact()]}), [])


class TruncationRecoveryTests(unittest.TestCase):
    def test_salvages_complete_prefix_of_truncated_array(self) -> None:
        first = json.dumps(good_fact(name="AWGN"))
        second = json.dumps(good_fact(name="Rayleigh"))
        raw = (
            '{"paper_domain":"communication","paper_repro_type":"signal_chain",'
            '"engineering_facts":[' + first + "," + second + ',{"type":"metric","name":"BER","va'
        )
        recovered = recover_truncated_engineering_facts(raw)
        self.assertIsNotNone(recovered)
        self.assertEqual(len(recovered["engineering_facts"]), 2)
        self.assertTrue(recovered["_meta"]["truncation_recovered"])

        doc = finalize_engineering_facts(recovered, CHUNK_IDS)
        self.assertEqual(validate_stage("engineering_facts", doc), [])
        self.assertEqual(len(doc["engineering_facts"]), 2)

    def test_returns_none_when_no_facts_array(self) -> None:
        self.assertIsNone(recover_truncated_engineering_facts('{"repro_tasks": [trunc'))

    def test_strips_think_block_before_salvage(self) -> None:
        first = json.dumps(good_fact())
        raw = "<think>reasoning that never closes the json</think>\n```json\n" '{"engineering_facts":[' + first + ",{trunc"
        recovered = recover_truncated_engineering_facts(raw)
        self.assertIsNotNone(recovered)
        self.assertEqual(len(recovered["engineering_facts"]), 1)


if __name__ == "__main__":
    unittest.main()
