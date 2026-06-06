from __future__ import annotations

import json
import unittest

from geng_agent.template_project import build_template_repro_project_manifest, choose_template_name


class TemplateProjectTests(unittest.TestCase):
    def test_choose_template_name_for_ber_terms(self) -> None:
        tasks = {
            "repro_tasks": [
                {
                    "task_id": "ber_curve",
                    "metric": "BER",
                    "figure_or_claim": "BER versus SNR curve under AWGN",
                }
            ]
        }

        self.assertEqual(choose_template_name({}, tasks), "deterministic_ber_curve_v1")

    def test_choose_template_name_for_accuracy_terms(self) -> None:
        tasks = {
            "repro_tasks": [
                {
                    "task_id": "classifier_curve",
                    "metric": "classification_accuracy",
                    "figure_or_claim": "Precision and recall across thresholds",
                }
            ]
        }

        self.assertEqual(choose_template_name({}, tasks), "deterministic_accuracy_curve_v1")

    def test_choose_template_name_for_generic_metric(self) -> None:
        tasks = {
            "repro_tasks": [
                {
                    "task_id": "runtime_curve",
                    "metric": "latency",
                    "figure_or_claim": "Wall-clock runtime as sample size changes",
                }
            ]
        }

        self.assertEqual(choose_template_name({}, tasks), "deterministic_generic_metric_v1")

    def test_manifest_records_selected_template_name(self) -> None:
        tasks = {
            "repro_tasks": [
                {
                    "task_id": "accuracy_manifest",
                    "metric": "accuracy",
                    "figure_or_claim": "Classification accuracy curve",
                }
            ]
        }

        manifest = build_template_repro_project_manifest(facts={}, tasks=tasks, reason="unit test fallback")

        self.assertEqual(manifest["_meta"]["template_name"], "deterministic_accuracy_curve_v1")
        files = {item["path"]: "\n".join(item["content_lines"]) for item in manifest["files"]}
        self.assertIn("Selected template: `deterministic_accuracy_curve_v1`.", files["README.md"])
        config = json.loads(files["config.json"])
        assumptions = config["assumptions"]
        self.assertIn(
            {
                "name": "template_name",
                "value": "deterministic_accuracy_curve_v1",
                "risk": "low",
                "reason": "Records which deterministic fallback template was selected for this reproduction project.",
            },
            assumptions,
        )
        self.assertTrue(manifest["_meta"]["template_fallback_used"])


if __name__ == "__main__":
    unittest.main()
