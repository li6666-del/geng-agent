import json
import unittest

from geng_agent.schemas import validate_stage, validate_task_fact_refs
from geng_agent.tasks_normalize import (
    finalize_repro_tasks,
    normalize_repro_tasks_candidate,
    recover_truncated_repro_tasks,
)


FACTS = {
    "engineering_facts": [
        {"type": "channel_model", "name": "AWGN channel"},
        {"type": "channel_model", "name": "Multipath Rayleigh Fading channel"},
        {"type": "modulation", "name": "16 PSK"},
        {"type": "metric", "name": "Bit Error Rate (BER)"},
        {"type": "figure_claim", "name": "Figure 11"},
        {"type": "simulation_parameter", "name": "SNR for BER plot", "value": {"value_dB": 10}},
        {"type": "simulation_parameter", "name": "Down sampling factor", "value": {"factor": 8}},
        {"type": "simulation_parameter", "name": "Pulse shaping filter", "value": {"roll_off_factor": 0.22}},
    ]
}


def good_task(**overrides):
    task = {
        "task_id": "reproduce_fig_11",
        "target": "Figure 11 BER comparison",
        "metric": "bit_error_rate",
        "metric_formula": "bit_error_rate = bit_errors / total_bits",
        "figure_or_claim": "Figure 11",
        "expected_artifacts": ["outputs/results.csv", "outputs/ber.png", "outputs/summary.json"],
        "output_columns": ["snr_db", "bit_error_rate"],
        "expected_trend": {
            "x_axis": "snr_db",
            "y_axis": "bit_error_rate",
            "direction": "decreasing",
            "reason": "Higher SNR lowers BER.",
        },
        "comparison": {"baselines": ["AWGN"], "curve_groups": ["Rayleigh"], "tolerance": "qualitative"},
        "required_facts": [{"type": "channel_model", "name": "AWGN channel"}],
        "assumptions": [{"name": "seed", "default_value": 0, "reason": "No seed specified.", "risk": "medium"}],
        "risk_if_unreproducible": "Core BER comparison cannot be checked.",
    }
    task.update(overrides)
    return task


class NormalizeReproTasksTests(unittest.TestCase):
    def test_repairs_near_miss_task_output_against_extracted_facts(self) -> None:
        messy = {
            "paper_id": "1404.2302",
            "venue": "extra top-level metadata",
            "repro_tasks": [
                good_task(
                    metric="BER",
                    expected_trend={
                        "x_axis": "channel_type",
                        "y_axis": "BER",
                        "direction": "mixed",
                        "reason": "Not monotonic across all modulation/channel groups.",
                        "extra": "drop me",
                    },
                    required_facts=[
                        "F-CH-AWGN",
                        "F-CH-RAYLEIGH-MULTIPATH",
                        "F-MOD-16PSK",
                        "F-METRIC-BER",
                        "F-FIG-11",
                        "F-SNR-10dB",
                        "F-RX-DOWNSAMPLE-8",
                        "F-RC-ROLL-0.22",
                    ],
                    unexpected="drop me too",
                )
            ],
        }

        doc = finalize_repro_tasks(messy, FACTS)

        self.assertEqual(validate_stage("repro_tasks", doc), [])
        self.assertEqual(validate_task_fact_refs(doc, FACTS), [])
        task = doc["repro_tasks"][0]
        self.assertEqual(task["metric"], "bit_error_rate")
        self.assertEqual(task["expected_trend"]["direction"], "unknown")
        self.assertNotIn("paper_id", doc)
        self.assertNotIn("unexpected", task)
        self.assertGreaterEqual(len(task["required_facts"]), 7)
        self.assertIn({"type": "figure_claim", "name": "Figure 11"}, task["required_facts"])
        self.assertTrue(doc["_meta"]["normalization_used"])

    def test_drops_only_irreparable_tasks(self) -> None:
        doc = finalize_repro_tasks(
            {
                "repro_tasks": [
                    good_task(required_facts=["F-CH-AWGN"]),
                    {"task_id": "bad", "metric": "BER"},
                ]
            },
            FACTS,
        )

        self.assertEqual(validate_stage("repro_tasks", doc), [])
        self.assertEqual(len(doc["repro_tasks"]), 1)
        self.assertTrue(doc["_meta"]["partial_acceptance_used"])
        self.assertEqual(doc["_meta"]["dropped_task_count"], 1)

    def test_clean_input_is_left_untouched_without_meta(self) -> None:
        clean = {"repro_tasks": [good_task()]}

        doc = finalize_repro_tasks(clean, FACTS)

        self.assertEqual(validate_stage("repro_tasks", doc), [])
        self.assertNotIn("_meta", doc)

    def test_truncation_recovery_salvages_complete_task_prefix(self) -> None:
        first = json.dumps(good_task(required_facts=["F-CH-AWGN"]))
        raw = '{"repro_tasks":[' + first + ',{"task_id":"cut"'

        recovered = recover_truncated_repro_tasks(raw)

        self.assertIsNotNone(recovered)
        doc = finalize_repro_tasks(recovered, FACTS)
        self.assertEqual(validate_stage("repro_tasks", doc), [])
        self.assertEqual(len(doc["repro_tasks"]), 1)
        self.assertTrue(doc["_meta"]["truncation_recovered"])

    def test_normalize_non_object_payload_to_empty_tasks(self) -> None:
        normalized, coercions = normalize_repro_tasks_candidate(["not", "object"], FACTS)

        self.assertEqual(normalized, {"repro_tasks": []})
        self.assertTrue(coercions)


if __name__ == "__main__":
    unittest.main()
