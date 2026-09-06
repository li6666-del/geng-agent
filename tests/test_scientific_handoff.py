"""Counterexamples for lossless scientific evidence and final task publication."""

from __future__ import annotations

import copy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from geng_agent.semantic_merge import semantic_merge_engineering_facts, semantic_merge_repro_tasks
from geng_agent.task_acceptance_normalize import normalize_scientific_acceptance
from geng_agent.task_reporter_validation import normalize_reporter_observation_evidence
from geng_agent.verification_result import normalize_task_verification, writer_revision_allowed


def _fact(value: object, *, explicit: bool = False, name: str = "noise variance") -> dict:
    return {
        "type": "simulation_parameter", "name": name, "value": {"value": value},
        "confidence": "high" if explicit else "low",
        "evidence_kind": "paper_explicit" if explicit else "visual_estimate",
        "source": {"chunk_id": "paper-equation" if explicit else None, "page": 2},
    }


def _task(task_id: str = "experiment_a") -> dict:
    return {
        "task_id": task_id,
        "figure_or_claim": task_id,
        "metric": "bit_error_rate",
        "metric_formula": "errors / total_bits",
        "scientific_acceptance": {
            "core_conclusions": [{"claim_id": "ordering", "statement": "A outperforms B"}],
            "key_numeric_targets": [], "information_gaps": [],
        },
    }


class ScientificValueMergeTests(unittest.TestCase):
    def test_material_value_differences_are_conflicts_without_evidence_upgrade(self) -> None:
        pairs = [
            (1.0, 10), (-1, 1), ("1e-3", "1e3"), ("h*x+n", "h*x-n"),
            ({"value": [0.1, -2]}, {"value": [1, 2]}),
            ("H", "h"), (True, 1), ("α", "β"),
        ]
        for before, after in pairs:
            with self.subTest(before=before, after=after):
                base = {"engineering_facts": [_fact(before)]}
                addition = {"engineering_facts": [_fact(after, explicit=True)]}
                original = copy.deepcopy(base)
                merged, _ = semantic_merge_engineering_facts(base, addition)
                self.assertEqual(base, original)
                retained = merged["engineering_facts"][0]
                self.assertEqual(retained["value"]["value"], before)
                self.assertEqual(retained["evidence_kind"], "visual_estimate")
                self.assertEqual(retained["confidence"], "low")
                conflict = merged["_meta"]["semantic_merge"]["fact_conflicts"][0]
                self.assertEqual(conflict["candidate_value"], after)
                self.assertEqual(conflict["candidate_source"], addition["engineering_facts"][0]["source"])

    def test_unicode_and_signed_identifier_names_remain_distinct(self) -> None:
        names = [
            "噪声功率", "信号功率", "channel α", "channel β", "method+", "method-", "H", "h",
            "H matrix", "h matrix", "power in mW", "power in MW",
        ]
        merged, _ = semantic_merge_engineering_facts(
            {"engineering_facts": []},
            {"engineering_facts": [_fact(index, name=name) for index, name in enumerate(names)]},
        )
        self.assertEqual([item["name"] for item in merged["engineering_facts"]], names)

    def test_descriptive_label_aliases_merge_but_conflicting_values_do_not(self) -> None:
        merged, _ = semantic_merge_engineering_facts(
            {"engineering_facts": [_fact(1.0, name="SNR range")]},
            {"engineering_facts": [_fact(10, name="snr  range", explicit=True)]},
        )
        self.assertEqual(len(merged["engineering_facts"]), 1)
        self.assertEqual(merged["engineering_facts"][0]["value"]["value"], 1.0)
        self.assertEqual(merged["engineering_facts"][0]["evidence_kind"], "visual_estimate")
        self.assertEqual(len(merged["_meta"]["semantic_merge"]["fact_conflicts"]), 1)

    def test_equal_typed_values_and_duplicate_ingestion_remain_idempotent(self) -> None:
        fact = _fact({"coefficients": [1, -0.25], "equation": "h*x+n"})
        merged, _ = semantic_merge_engineering_facts(
            {"engineering_facts": [fact]}, {"engineering_facts": [copy.deepcopy(fact)]}
        )
        again, delta = semantic_merge_engineering_facts(merged, {"engineering_facts": [fact]})
        self.assertEqual(delta, 0)
        self.assertEqual(len(again["engineering_facts"]), 1)
        self.assertEqual(again["_meta"]["semantic_merge"]["fact_conflicts"], [])


class FinalTaskSnapshotTests(unittest.TestCase):
    def test_final_snapshot_replaces_science_removes_relationship_and_preserves_coverage(self) -> None:
        first = _task()
        first.update({
            "metric_formula": "errors / symbols",
            "assumptions": [{"name": "old normalization", "default_value": 2}],
            "parameter_matrix": [{"name": "variance", "value": 10}],
            "baseline_definitions": [{"name": "B", "value": "old implementation"}],
        })
        omitted = _task("experiment_b")
        relationship = {"relationship_id": "old_coupling", "task_ids": ["experiment_a", "experiment_b"]}
        base = {"repro_tasks": [first, omitted], "execution_relationships": [relationship]}
        before = copy.deepcopy(base)
        updated = _task()
        updated.update({
            "assumptions": [], "parameter_matrix": [{"name": "variance", "value": 1.0}],
            "baseline_definitions": [{"name": "B", "value": "paper implementation"}],
        })
        merged, _ = semantic_merge_repro_tasks(
            base, {"repro_tasks": [updated], "execution_relationships": []}, merge_mode="snapshot"
        )
        self.assertEqual(base, before)
        self.assertEqual(merged["repro_tasks"], [updated, omitted])
        self.assertEqual(merged["execution_relationships"], [])
        changes = merged["_meta"]["semantic_merge"]
        self.assertEqual(changes["preserved_task_ids"], ["experiment_b"])
        self.assertEqual(changes["removed_relationship_ids"], ["old_coupling"])

    def test_incremental_supplement_does_not_revoke_omitted_evidence(self) -> None:
        first = _task()
        first["assumptions"] = [{"name": "retained evidence"}]
        relationship = {"relationship_id": "shared", "task_ids": ["experiment_a", "experiment_b"]}
        merged, _ = semantic_merge_repro_tasks(
            {"repro_tasks": [first], "execution_relationships": [relationship]},
            {"repro_tasks": [{**_task(), "assumptions": []}], "execution_relationships": []},
        )
        self.assertEqual(merged["repro_tasks"][0]["assumptions"], first["assumptions"])
        self.assertEqual(merged["execution_relationships"], [relationship])

    def test_final_snapshot_does_not_duplicate_current_named_specification(self) -> None:
        first = {**_task(), "formula_chain": [{"name": "NMSE", "note": "old wording"}]}
        latest = {**_task(), "formula_chain": [{"name": "NMSE", "note": "clearer wording"}]}
        result, _ = semantic_merge_repro_tasks(
            {"repro_tasks": [first]}, {"repro_tasks": [latest]}, merge_mode="snapshot"
        )
        self.assertEqual(result["repro_tasks"][0]["formula_chain"], latest["formula_chain"])

    def test_recovered_task_identity_is_applied_to_relationship_references(self) -> None:
        original = {**_task("old_id"), "figure_or_claim": "Fig. 2"}
        updated = {**_task("new_id"), "figure_or_claim": "Fig. 2"}
        other = _task("other")
        result, _ = semantic_merge_repro_tasks(
            {"repro_tasks": [original, other]},
            {"repro_tasks": [updated, other], "execution_relationships": [{
                "relationship_id": "flow", "task_ids": ["new_id", "other"],
                "producer_task_id": "new_id", "consumer_task_ids": ["other"],
            }]},
            merge_mode="snapshot",
        )
        self.assertEqual(result["repro_tasks"][0]["task_id"], "old_id")
        self.assertEqual(result["execution_relationships"][0]["task_ids"], ["old_id", "other"])
        self.assertEqual(result["execution_relationships"][0]["producer_task_id"], "old_id")


class ReporterObservationTests(unittest.TestCase):
    def test_post_execution_image_cannot_support_claim_without_observed_data(self) -> None:
        with TemporaryDirectory() as temp:
            workspace = Path(temp)
            output = workspace / "inputs" / "writer_output" / "outputs"
            output.mkdir(parents=True)
            (output / "figure.png").write_bytes(b"new plot")
            (output / "raw.csv").write_text("A,B\n0.02,0.03\n")
            raw = {"core_conclusions": [{"claim_id": "ordering", "status": "supported",
                "local_observation": "The plot claims A wins.",
                "evidence_files": ["inputs/writer_output/outputs/figure.png"]}]}
            host = {"passed": True, "receipt": {"task_id": "experiment_a", "output_subdir": "experiment_a"},
                    "unobserved_artifacts": ["outputs/experiment_a/figure.png"]}
            checked, _ = normalize_reporter_observation_evidence(raw, workspace, host_execution=host)
            self.assertEqual(checked["core_conclusions"][0]["status"], "unassessable_missing_information")
            raw["core_conclusions"][0]["evidence_files"].append("inputs/writer_output/outputs/raw.csv")
            checked, _ = normalize_reporter_observation_evidence(raw, workspace, host_execution=host)
            self.assertEqual(checked["core_conclusions"][0]["status"], "supported")

    def test_status_spelling_cannot_bypass_local_evidence_check(self) -> None:
        with TemporaryDirectory() as temp:
            for status_fields in ({"status": " supported "}, {"status": "pass", "supported": True}):
                with self.subTest(status_fields=status_fields):
                    raw = {"core_conclusions": [{
                        "claim_id": "ordering", **status_fields,
                        "local_observation": "A supposedly wins without any verifiable local file.",
                    }]}
                    checked, warnings = normalize_reporter_observation_evidence(raw, Path(temp))
                    result = normalize_task_verification(
                        checked, "experiment_a", task=_task(), run_valid_hint=True
                    )
                    self.assertTrue(warnings)
                    self.assertEqual(result["outcome"], "inconclusive_missing_information")

    def test_missing_claim_local_evidence_cannot_certify_success(self) -> None:
        with TemporaryDirectory() as temp:
            workspace = Path(temp)
            paper = workspace / "paper_evidence" / "paper.txt"
            paper.parent.mkdir()
            paper.write_text("A has lower BER than B", encoding="utf-8")
            raw = {
                "core_conclusions": [{
                    "claim_id": "ordering", "status": "supported",
                    "local_observation": "The result is claimed to agree.",
                    "evidence_files": ["inputs/writer_output/outputs/missing.csv"],
                }],
                "evidence_files": ["paper_evidence/paper.txt"],
            }
            checked, warnings = normalize_reporter_observation_evidence(raw, workspace)
            result = normalize_task_verification(checked, "experiment_a", task=_task(), run_valid_hint=True)
            self.assertEqual(result["outcome"], "inconclusive_missing_information")
            self.assertEqual(result["host_action"], "complete")
            self.assertTrue(any("missing.csv" in warning for warning in warnings))
            self.assertEqual(raw["core_conclusions"][0]["status"], "supported")

    def test_real_source_proof_preserves_method_failure_without_outputs(self) -> None:
        with TemporaryDirectory() as temp:
            workspace = Path(temp)
            source = workspace / "inputs" / "writer_output" / "source" / "tasks" / "experiment.py"
            source.parent.mkdir(parents=True)
            source.write_text("prediction = constant_curve\n", encoding="utf-8")
            raw = {
                "core_conclusions": [{
                    "claim_id": "reported_method_identity", "status": "unsupported",
                    "local_observation": "The implementation returns a constant instead of the learned method.",
                    "evidence_files": [source.relative_to(workspace).as_posix()],
                }],
                "rerun_evidence": {
                    "rerun_reason": "core_conclusion_failed",
                    "contract_item_ids": ["reported_method_identity"],
                    "paper_evidence_files": ["paper_evidence/algorithm.txt"],
                    "causal_change": "Call the trained model instead of returning a constant.",
                    "change_targets": ["tasks/experiment.py"],
                    "predicted_effect": "Evaluate the actual method on the same held-out data.",
                },
            }
            checked, _ = normalize_reporter_observation_evidence(raw, workspace)
            result = normalize_task_verification(checked, "experiment_a", task=_task(), run_valid_hint=True)
            self.assertEqual(result["outcome"], "not_reproduced")
            self.assertTrue(writer_revision_allowed(result, "experiment_a"))

    def test_top_level_real_output_can_support_a_claim_without_duplicate_paths(self) -> None:
        with TemporaryDirectory() as temp:
            workspace = Path(temp)
            output = workspace / "inputs" / "writer_output" / "outputs" / "results.csv"
            output.parent.mkdir(parents=True)
            output.write_text("A,B\n0.02,0.03\n", encoding="utf-8")
            checked, warnings = normalize_reporter_observation_evidence({
                "core_conclusions": [{"claim_id": "ordering", "status": "supported", "local_observation": "A has lower BER."}],
                "evidence_files": [str(output)],
            }, workspace)
            result = normalize_task_verification(checked, "experiment_a", task=_task(), run_valid_hint=True)
            self.assertEqual(result["outcome"], "reproduced")
            self.assertEqual(warnings, [])

    def test_unsupported_missing_evidence_is_retained_without_authorizing_rerun(self) -> None:
        with TemporaryDirectory() as temp:
            workspace = Path(temp)
            checked, warnings = normalize_reporter_observation_evidence({
                "core_conclusions": [{"claim_id": "ordering", "status": "unsupported", "local_observation": "The Reporter describes an ordering reversal.", "evidence_files": ["inputs/writer_output/outputs/missing.csv"]}],
                "rerun_evidence": {"rerun_reason": "core_conclusion_failed", "contract_item_ids": ["ordering"]},
            }, workspace)
            result = normalize_task_verification(checked, "experiment_a", task=_task(), run_valid_hint=True)
            self.assertEqual(result["core_conclusions"][0]["status"], "unsupported")
            self.assertEqual(result["outcome"], "not_reproduced")
            self.assertEqual(result["host_action"], "complete")
            self.assertTrue(warnings)

    def test_uncontracted_failure_survives_and_prevents_false_success(self) -> None:
        raw = {
            "core_conclusions": [
                {"claim_id": "ordering", "status": "supported", "local_observation": "A has lower BER."},
                {"claim_id": "reported_training", "status": "unsupported", "local_observation": "The predictions bypass the learned model and use a constant array."},
            ],
        }
        result = normalize_task_verification(raw, "experiment_a", task=_task(), run_valid_hint=True)
        self.assertEqual(result["outcome"], "not_reproduced")
        self.assertEqual(result["core_conclusions"][1]["claim_id"], "reported_training")
        self.assertEqual(result["host_action"], "complete")

    def test_supported_label_without_observation_cannot_use_fallback_as_evidence(self) -> None:
        for item in ({"claim_id": "ordering", "status": "supported"}, {"supported": True}):
            with self.subTest(item=item):
                result = normalize_task_verification(
                    {"core_conclusions": [item]}, "experiment_a", task=_task(), run_valid_hint=True
                )
                self.assertEqual(result["outcome"], "inconclusive_missing_information")
                self.assertEqual(result["core_conclusions"][0]["status"], "unassessable_missing_information")
                self.assertEqual(result["evidence_files"], [])
                self.assertFalse(writer_revision_allowed(result, "experiment_a"))

    def test_duplicate_claim_cannot_overwrite_an_unsupported_observation(self) -> None:
        failed = {"claim_id": "ordering", "status": "unsupported", "local_observation": "A is worse at low SNR."}
        supported = {"claim_id": "ordering", "status": "supported", "local_observation": "A is better at high SNR."}
        for items in ([failed, supported], [supported, failed]):
            with self.subTest(items=items):
                result = normalize_task_verification(
                    {"core_conclusions": items}, "experiment_a", task=_task(), run_valid_hint=True
                )
                self.assertEqual(result["outcome"], "not_reproduced")
                self.assertEqual(len(result["core_conclusions"]), 1)
                self.assertIn("low SNR", result["core_conclusions"][0]["local_observation"])
                self.assertIn("high SNR", result["core_conclusions"][0]["local_observation"])

    def test_unicode_claim_ids_and_negative_exponent_are_preserved(self) -> None:
        task = {"task_id": "experiment_a", "scientific_acceptance": {
            "core_conclusions": [{"claim_id": "排序_α", "statement": "A outperforms B"}],
            "key_numeric_targets": [{"target_id": "偏差", "name": "signed error", "paper_magnitude": "−1.2×10^−3", "unit": "V", "evidence_quality": "paper_explicit"}],
            "information_gaps": [{"gap_id": "几何", "description": "geometry unknown", "affects_claim_ids": ["排序_α"]}],
        }}
        normalize_scientific_acceptance(task, 0, [])
        acceptance = task["scientific_acceptance"]
        self.assertEqual(acceptance["core_conclusions"][0]["claim_id"], "排序_α")
        self.assertEqual(acceptance["information_gaps"][0]["affects_claim_ids"], ["排序_α"])
        self.assertAlmostEqual(acceptance["key_numeric_targets"][0]["paper_magnitude"], -0.0012)


if __name__ == "__main__":
    unittest.main()
