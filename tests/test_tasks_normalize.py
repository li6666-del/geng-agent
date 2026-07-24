import json
import unittest

from geng_agent.heuristic_fallbacks import build_fallback_repro_tasks
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

    def test_moves_soft_backfill_handoff_into_meta(self) -> None:
        document = {
            "backfill_handoff": {
                "ready_for_writer": False,
                "blocking_request_ids": ["backfill_a", "backfill_a", ""],
                "reason": "a newly exposed formula is still missing",
            },
            "repro_tasks": [good_task()],
        }

        normalized = finalize_repro_tasks(document, FACTS)

        self.assertNotIn("backfill_handoff", normalized)
        self.assertEqual(
            normalized["_meta"]["backfill_handoff"],
            {
                "ready_for_writer": False,
                "blocking_request_ids": ["backfill_a"],
                "reason": "a newly exposed formula is still missing",
            },
        )
        self.assertEqual(validate_stage("repro_tasks", normalized), [])

    def test_preserves_task_when_only_legacy_description_fields_are_missing(self) -> None:
        doc = finalize_repro_tasks(
            {
                "repro_tasks": [
                    good_task(required_facts=["F-CH-AWGN"]),
                    {
                        "task_id": "minimal",
                        "metric": "BER",
                        "scientific_acceptance": {
                            "core_conclusions": [
                                {"claim_id": "ordering", "statement": "Method A outperforms baseline B."}
                            ]
                        },
                    },
                ]
            },
            FACTS,
        )

        self.assertEqual(validate_stage("repro_tasks", doc), [])
        self.assertEqual(len(doc["repro_tasks"]), 2)
        minimal = doc["repro_tasks"][1]
        self.assertEqual(minimal["task_id"], "minimal")
        self.assertEqual(
            minimal["scientific_acceptance"]["core_conclusions"][0]["statement"],
            "Method A outperforms baseline B.",
        )
        self.assertTrue(minimal["metric_formula"])

    def test_empty_task_set_gets_one_honest_minimum_handoff(self) -> None:
        doc = finalize_repro_tasks({"repro_tasks": []}, FACTS)

        self.assertEqual(validate_stage("repro_tasks", doc), [])
        self.assertEqual(len(doc["repro_tasks"]), 1)

    def test_clean_input_remains_schema_valid(self) -> None:
        clean = {"repro_tasks": [good_task()]}

        doc = finalize_repro_tasks(clean, FACTS)

        self.assertEqual(validate_stage("repro_tasks", doc), [])
        self.assertEqual(doc["repro_tasks"][0]["task_id"], "reproduce_fig_11")
        self.assertEqual(len(doc["repro_tasks"]), 1)

    def test_preserves_field_requests_specs_and_linked_sensitivity(self) -> None:
        request = {
            "request_id": "fig11_trials",
            "type": "simulation_parameter",
            "name": "Fig. 11 trial count",
            "why_needed": "controls statistical reliability",
            "impact": "high",
            "search_targets": ["Fig. 11 caption"],
            "required_fields": [
                {
                    "field_id": "trial_count",
                    "description": "number of Monte Carlo trials",
                    "affects": ["statistical_protocol"],
                }
            ],
        }
        spec = {
            "name": "BER formula",
            "value": "errors / bits",
            "status": "evidenced",
            "evidence_facts": [{"type": "metric", "name": "Bit Error Rate (BER)"}],
            "note": "paper metric",
        }
        task = good_task(
            missing_fact_requests=[request],
            formula_chain=[spec],
            parameter_matrix=[{**spec, "name": "SNR grid"}],
            baseline_definitions=[{**spec, "name": "AWGN baseline"}],
            statistical_protocol=[{**spec, "name": "trial protocol", "status": "unresolved"}],
            validation_anchors=[{**spec, "name": "BER ordering"}],
            assumptions=[
                {
                    "name": "trial count",
                    "default_value": 1000,
                    "reason": "not disclosed",
                    "risk": "medium",
                    "request_id": "backfill_x",
                    "field_ids": ["trial_count"],
                    "sensitivity_check": "repeat with 500 and 2000 trials",
                }
            ],
        )

        doc = finalize_repro_tasks({"repro_tasks": [task]}, FACTS)
        normalized = doc["repro_tasks"][0]

        self.assertEqual(
            normalized["missing_fact_requests"][0]["required_fields"][0]["field_id"],
            "trial_count",
        )
        self.assertEqual(normalized["formula_chain"][0]["status"], "evidenced")
        self.assertEqual(
            normalized["assumptions"][0]["sensitivity_check"],
            "repeat with 500 and 2000 trials",
        )

    def test_missing_contract_gets_minimal_nonblocking_semantics(self) -> None:
        doc = finalize_repro_tasks({"repro_tasks": [good_task()]}, FACTS)

        acceptance = doc["repro_tasks"][0]["scientific_acceptance"]
        self.assertEqual(acceptance["contract_version"], "1.0")
        self.assertEqual(len(acceptance["core_conclusions"]), 1)
        self.assertEqual(acceptance["core_conclusions"][0]["kind"], "trend")
        self.assertIn("decreasing", acceptance["core_conclusions"][0]["statement"])
        self.assertEqual(
            acceptance["information_gaps"][0]["disposition"],
            "assume_and_disclose",
        )
        self.assertEqual(validate_stage("repro_tasks", doc), [])

    def test_contract_repairs_ids_and_ignores_presentation_only_targets(self) -> None:
        contract = {
            "contract_version": "draft",
            "core_conclusions": [
                {
                    "claim_id": "Main Trend",
                    "statement": "BER decreases as SNR increases.",
                    "kind": "trend",
                    "regime": "AWGN",
                    "paper_anchor": "Fig. 11",
                },
                {
                    "claim_id": "Main Trend",
                    "statement": "The learned method ranks above the baseline.",
                    "kind": "ordering",
                    "regime": "AWGN",
                    "paper_anchor": "Fig. 11",
                },
                {
                    "claim_id": "plot_style",
                    "statement": "pixel-perfect line colors and font size",
                    "kind": "other",
                    "regime": "plot",
                    "paper_anchor": "Fig. 11",
                },
            ],
            "key_numeric_targets": [
                {
                    "target_id": "BER@10dB",
                    "name": "BER at 10 dB",
                    "paper_magnitude": "0.02",
                    "unit": "probability",
                    "regime": "AWGN",
                    "evidence_quality": "visual_estimate",
                },
                {
                    "target_id": "pixel_delta",
                    "name": "pixel-perfect layout delta",
                    "paper_magnitude": 0,
                    "unit": "pixels",
                    "regime": "plot",
                    "evidence_quality": "paper_explicit",
                },
            ],
            "information_gaps": [
                {
                    "gap_id": "Missing Trials",
                    "description": "The trial count is not stated.",
                    "affects_claim_ids": ["Main Trend"],
                    "disposition": "single_sensitivity_if_core",
                }
            ],
            "model_commentary": "drop",
        }

        doc = finalize_repro_tasks(
            {"repro_tasks": [good_task(scientific_acceptance=contract)]}, FACTS
        )
        acceptance = doc["repro_tasks"][0]["scientific_acceptance"]

        self.assertEqual(acceptance["contract_version"], "1.0")
        claim_ids = [item["claim_id"] for item in acceptance["core_conclusions"]]
        self.assertEqual(len(claim_ids), 2)
        self.assertEqual(len(set(claim_ids)), 2)
        self.assertNotIn("plot_style", claim_ids)
        self.assertEqual(len(acceptance["key_numeric_targets"]), 1)
        self.assertEqual(
            acceptance["key_numeric_targets"][0]["paper_magnitude"], 0.02
        )
        self.assertEqual(
            acceptance["information_gaps"][0]["affects_claim_ids"],
            [claim_ids[0]],
        )
        self.assertEqual(validate_stage("repro_tasks", doc), [])
    def test_pixel_level_image_science_is_not_mistaken_for_plot_styling(self) -> None:
        contract = {
            "contract_version": "1.0",
            "core_conclusions": [
                {
                    "claim_id": "reconstruction",
                    "statement": "Pixel-perfect image reconstruction improves SSIM.",
                    "kind": "trend",
                    "regime": "test images",
                    "paper_anchor": "Fig. 3",
                },
                {
                    "claim_id": "style",
                    "statement": "Pixel-perfect line colors and font size.",
                    "kind": "other",
                    "regime": "plot",
                    "paper_anchor": "Fig. 3",
                },
            ],
            "key_numeric_targets": [
                {
                    "target_id": "mse",
                    "name": "Pixel-perfect image reconstruction MSE",
                    "paper_magnitude": "1\u00d710^-3 MSE",
                    "unit": "",
                    "regime": "test images",
                    "evidence_quality": "paper_explicit",
                },
                {
                    "target_id": "ber",
                    "name": "BER threshold",
                    "paper_magnitude": "10^-3 BER",
                    "unit": "",
                    "regime": "test channel",
                    "evidence_quality": "paper_explicit",
                },
                {
                    "target_id": "style_delta",
                    "name": "pixel-perfect layout delta",
                    "paper_magnitude": "0 pixels",
                    "unit": "pixels",
                    "regime": "plot",
                    "evidence_quality": "paper_explicit",
                },
            ],
            "information_gaps": [],
        }

        doc = finalize_repro_tasks(
            {"repro_tasks": [good_task(scientific_acceptance=contract)]}, FACTS
        )
        acceptance = doc["repro_tasks"][0]["scientific_acceptance"]

        self.assertEqual(
            [item["claim_id"] for item in acceptance["core_conclusions"]],
            ["reconstruction"],
        )
        self.assertEqual(len(acceptance["key_numeric_targets"]), 2)
        targets = {item["target_id"]: item for item in acceptance["key_numeric_targets"]}
        self.assertEqual(targets["mse"]["paper_magnitude"], 0.001)
        self.assertEqual(targets["mse"]["unit"], "MSE")
        self.assertEqual(targets["ber"]["paper_magnitude"], 0.001)
        self.assertEqual(targets["ber"]["unit"], "BER")
        self.assertEqual(validate_stage("repro_tasks", doc), [])

    def test_local_fallback_declares_a_non_visual_scientific_contract(self) -> None:
        fallback = build_fallback_repro_tasks(
            facts=FACTS,
            paper={"chunks": []},
            reason="task designer unavailable",
        )

        acceptance = fallback["repro_tasks"][0]["scientific_acceptance"]
        self.assertEqual(
            acceptance["core_conclusions"][0]["claim_id"],
            "fallback_primary_trend",
        )
        self.assertEqual(acceptance["core_conclusions"][0]["kind"], "trend")
        self.assertEqual(acceptance["key_numeric_targets"], [])
        self.assertEqual(
            acceptance["information_gaps"][0]["disposition"],
            "assume_and_disclose",
        )
        normalized = finalize_repro_tasks(fallback, FACTS)
        self.assertEqual(validate_stage("repro_tasks", normalized), [])

    def test_text_shorthand_in_scientific_acceptance_is_recovered(self) -> None:
        contract = {
            "contract_version": "1.0",
            "core_conclusions": ["Method A outperforms Method B at high SNR."],
            "key_numeric_targets": ["10^-3 BER"],
            "information_gaps": ["The block length is not reported."],
        }

        doc = finalize_repro_tasks(
            {"repro_tasks": [good_task(scientific_acceptance=contract)]}, FACTS
        )
        acceptance = doc["repro_tasks"][0]["scientific_acceptance"]

        self.assertEqual(
            acceptance["core_conclusions"][0]["statement"],
            "Method A outperforms Method B at high SNR.",
        )
        self.assertEqual(
            acceptance["key_numeric_targets"][0]["paper_magnitude"], 0.001
        )
        self.assertEqual(
            acceptance["information_gaps"][0]["description"],
            "The block length is not reported.",
        )
        self.assertEqual(validate_stage("repro_tasks", doc), [])

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
