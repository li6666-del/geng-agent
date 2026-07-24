import json
from pathlib import Path
import unittest

from geng_agent.schema_models import SCHEMA_FILENAMES, SCHEMA_MODELS, response_format_for_stage
from geng_agent.schemas import validate_stage, validate_task_fact_refs


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

    def test_exported_json_schemas_match_pydantic_models(self) -> None:
        schema_dir = Path(__file__).resolve().parents[1] / "schemas"
        for stage, model in SCHEMA_MODELS.items():
            with self.subTest(stage=stage):
                exported = json.loads((schema_dir / SCHEMA_FILENAMES[stage]).read_text(encoding="utf-8"))
                self.assertEqual(exported, model.model_json_schema())

    def test_scientific_architecture_schema_exposes_v11_execution_contract(self) -> None:
        schema = SCHEMA_MODELS["scientific_architecture"].model_json_schema()

        self.assertEqual(
            schema["properties"]["schema_version"]["enum"],
            ["1.0", "1.1"],
        )
        self.assertIn("schema_version", schema["required"])
        self.assertNotIn("default", schema["properties"]["schema_version"])
        component = schema["$defs"]["ArchitectureComponent"]
        self.assertNotIn("enum", component["properties"]["kind"])
        self.assertEqual(component["properties"]["kind"]["minLength"], 1)
        execution = schema["$defs"]["ArchitectureExecutionContract"]
        self.assertNotIn("enum", execution["properties"]["execution_kind"])
        self.assertNotIn("enum", execution["properties"]["primary_framework"])
        self.assertEqual(
            execution["properties"]["device_policy"]["enum"],
            [
                "cpu",
                "framework_default",
                "accelerator_preferred",
                "accelerator_required",
                "external_runtime",
            ],
        )
        self.assertEqual(
            set(execution["required"]),
            {
                "execution_kind",
                "primary_framework",
                "supporting_libraries",
                "device_policy",
                "precision",
                "trainable",
                "gradient_mode",
                "checkpoint_policy",
                "shared_implementation",
                "required_capabilities",
                "rationale",
            },
        )
        self.assertIn(
            "execution",
            schema["$defs"]["ArchitectureComponent"]["properties"],
        )

        v11_rule = schema["allOf"][0]
        self.assertEqual(
            v11_rule["if"]["properties"]["schema_version"]["const"],
            "1.1",
        )
        component_rule = v11_rule["then"]["properties"]["components"]["items"]
        self.assertEqual(set(component_rule["required"]), {"callable", "execution"})
        self.assertEqual(component_rule["properties"]["callable"]["minLength"], 1)
        self.assertEqual(component_rule["properties"]["callable"]["pattern"], r"\S")
        self.assertEqual(
            component_rule["properties"]["execution"],
            {"not": {"type": "null"}},
        )

    def test_task_verification_schemas_expose_structured_rerun_contract(self) -> None:
        isolated = SCHEMA_MODELS["task_verification_result"].model_json_schema()
        aggregate = SCHEMA_MODELS["verification_result"].model_json_schema()
        schema_nodes = [
            isolated,
            aggregate["$defs"]["TaskVerificationResult"],
        ]

        for schema in schema_nodes:
            properties = schema["properties"]
            required = set(schema.get("required", []))
            with self.subTest(title=properties["rerun_reason"]["title"]):
                self.assertEqual(
                    properties["rerun_reason"]["enum"],
                    [
                        "none",
                        "core_conclusion_failed",
                        "key_numeric_ratio_ge_10",
                        "invalid_run",
                    ],
                )
                self.assertEqual(properties["rerun_reason"]["default"], "none")
                self.assertIsNone(properties["run_valid"]["default"])
                self.assertTrue(
                    {
                        "rerun_reason", "run_valid", "core_conclusions",
                        "key_numeric_comparisons", "max_key_numeric_ratio",
                    }.isdisjoint(required)
                )
                self.assertNotIn("default", properties["core_conclusions"])
                self.assertIn("$ref", properties["core_conclusions"]["items"])
                self.assertIn("$ref", properties["key_numeric_comparisons"]["items"])
                self.assertEqual(
                    properties["max_key_numeric_ratio"]["anyOf"][0]["minimum"],
                    1,
                )

    def test_response_format_uses_json_schema(self) -> None:
        response_format = response_format_for_stage("repro_tasks")

        self.assertEqual(response_format["type"], "json_schema")
        self.assertEqual(response_format["json_schema"]["name"], "repro_tasks")
        self.assertTrue(response_format["json_schema"]["strict"])
        self.assertEqual(response_format["json_schema"]["schema"], SCHEMA_MODELS["repro_tasks"].model_json_schema())

    def test_targeted_backfill_schema_requires_field_resolutions(self) -> None:
        document = {
            "paper_domain": "communication",
            "paper_repro_type": "signal_chain",
            "engineering_facts": [],
            "missing_information": [],
        }

        issues = validate_stage("targeted_fact_backfill", document)

        self.assertTrue(any("request_resolutions" in issue.path for issue in issues))


if __name__ == "__main__":
    unittest.main()
