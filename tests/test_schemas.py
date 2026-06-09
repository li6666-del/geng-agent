import base64
import json
from pathlib import Path
import unittest

from geng_agent.schema_models import SCHEMA_FILENAMES, SCHEMA_MODELS, response_format_for_stage
from geng_agent.schemas import validate_stage, validate_task_fact_refs


REVIEW_DIMENSIONS = [
    "artifact_coverage",
    "reproduction_logic",
    "trend_shape",
    "metric_axis_scale",
    "baseline_comparison",
    "statistical_reliability",
    "conclusion_support",
]


def dimension_reviews() -> list[dict]:
    return [
        {
            "dimension": dimension,
            "rating": "acceptable",
            "finding": f"{dimension} 维度有基础证据。",
            "evidence": ["outputs/results.csv"],
        }
        for dimension in REVIEW_DIMENSIONS
    ]


class SchemaTests(unittest.TestCase):
    def test_repro_project_manifest_requires_smoke_config(self) -> None:
        manifest = {
            "files": [
                {"path": "README.md", "content": ""},
                {"path": "requirements.txt", "content": ""},
                {"path": "config.json", "content": "{}"},
                {"path": "run_experiment.py", "content": ""},
                {"path": "src/channel.py", "content": ""},
                {"path": "src/modulation.py", "content": ""},
                {"path": "src/metrics.py", "content": ""},
                {"path": "src/simulation.py", "content": ""},
            ]
        }

        issues = validate_stage("repro_project_manifest", manifest)

        self.assertTrue(any("config_smoke.json" in issue.message for issue in issues))

    def test_manifest_rejects_duplicate_and_bad_content_shape(self) -> None:
        manifest = {
            "files": [
                {"path": "README.md", "content": "", "content_lines": []},
                {"path": "README.md", "content": ""},
            ]
        }

        issues = validate_stage("repair_manifest", {"reason": "x", "touched_files": ["README.md"], "scientific_changes": [], **manifest})

        self.assertTrue(any("exactly one" in issue.message for issue in issues))
        self.assertTrue(any("duplicate" in issue.message for issue in issues))

    def test_manifest_accepts_content_b64_and_repair_metadata(self) -> None:
        manifest = {
            "reason": "Fix smoke runner.",
            "touched_files": ["run_experiment.py"],
            "scientific_changes": [],
            "files": [
                {
                    "path": "run_experiment.py",
                    "content_b64": base64.b64encode(b"print('ok')\n").decode(),
                }
            ],
        }

        issues = validate_stage("repair_manifest", manifest)

        self.assertEqual(issues, [])

    def test_task_fact_refs_must_exist(self) -> None:
        tasks = {
            "repro_tasks": [
                {
                    "task_id": "t",
                    "target": "BER",
                    "metric": "bit_error_rate",
                    "metric_formula": "bit_error_rate = bit_errors / total_bits",
                    "figure_or_claim": "Fig. 1",
                    "expected_artifacts": ["outputs/results.csv"],
                    "output_columns": ["snr_db", "bit_error_rate"],
                    "expected_trend": {},
                    "comparison": {"baselines": ["x"], "curve_groups": [], "tolerance": "qualitative"},
                    "required_facts": [{"type": "channel_model", "name": "Missing"}],
                    "assumptions": [],
                    "risk_if_unreproducible": "risk",
                }
            ]
        }
        facts = {"engineering_facts": [{"type": "channel_model", "name": "AWGN"}]}

        issues = validate_task_fact_refs(tasks, facts)

        self.assertTrue(issues)

    def test_repro_tasks_requires_metric_formula(self) -> None:
        tasks = {
            "repro_tasks": [
                {
                    "task_id": "t",
                    "target": "BER",
                    "metric": "bit_error_rate",
                    "figure_or_claim": "Fig. 1",
                    "expected_artifacts": ["outputs/results.csv"],
                    "output_columns": ["snr_db", "bit_error_rate"],
                    "expected_trend": {
                        "x_axis": "snr_db",
                        "y_axis": "bit_error_rate",
                        "direction": "decreasing",
                        "reason": "higher SNR lowers BER",
                    },
                    "comparison": {"baselines": ["AWGN"], "curve_groups": [], "tolerance": "qualitative"},
                    "required_facts": [{"type": "channel_model", "name": "AWGN"}],
                    "assumptions": [],
                    "risk_if_unreproducible": "risk",
                }
            ]
        }

        issues = validate_stage("repro_tasks", tasks)

        self.assertTrue(any("metric_formula" in issue.path for issue in issues))

    def test_result_review_experiment_requires_all_scientific_dimensions(self) -> None:
        reviews = dimension_reviews()
        reviews[-1] = {
            "dimension": "artifact_coverage",
            "rating": "acceptable",
            "finding": "重复产物覆盖维度。",
            "evidence": ["outputs/results.csv"],
        }
        issues = validate_stage(
            "result_review_experiment",
            {
                "task_id": "reproduce_fig_1",
                "local_result_credibility": "medium",
                "paper_alignment": "partial_match",
                "scientific_verdict": "partially_supports_paper_claim",
                "dimension_reviews": reviews,
                "paper_result_summary": "论文显示 BER 随 SNR 下降。",
                "local_result_summary": "本地 CSV 显示 BER 下降。",
                "differences": ["趋势一致但样本少。"],
                "possible_causes": ["smoke 配置样本量较小。"],
                "evidence": ["outputs/results.csv"],
                "limitations": ["未精确读图。"],
                "confidence": "medium",
            },
        )

        issue_text = "\n".join(f"{issue.path}: {issue.message}" for issue in issues)
        self.assertIn("dimension_reviews", issue_text)
        self.assertIn("conclusion_support", issue_text)

    def test_exported_json_schemas_match_pydantic_models(self) -> None:
        schema_dir = Path(__file__).resolve().parents[1] / "schemas"
        for stage, model in SCHEMA_MODELS.items():
            with self.subTest(stage=stage):
                exported = json.loads((schema_dir / SCHEMA_FILENAMES[stage]).read_text(encoding="utf-8"))
                self.assertEqual(exported, model.model_json_schema())

    def test_response_format_uses_json_schema(self) -> None:
        response_format = response_format_for_stage("repro_tasks")

        self.assertEqual(response_format["type"], "json_schema")
        self.assertEqual(response_format["json_schema"]["name"], "repro_tasks")
        self.assertTrue(response_format["json_schema"]["strict"])
        self.assertEqual(response_format["json_schema"]["schema"], SCHEMA_MODELS["repro_tasks"].model_json_schema())


if __name__ == "__main__":
    unittest.main()
