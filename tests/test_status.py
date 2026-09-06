from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from geng_agent.outputs import write_json
from geng_agent.status import inspect_case_status


def valid_host_environment_lock() -> dict:
    return {
        "kind": "geng.case_environment.lock",
        "ready": True,
        "runtime_mode": "host_shared",
        "capabilities_ok": True,
        "environment_hash": "f" * 64,
        "host_provenance": {
            "kind": "geng.host_shared_runtime",
            "runtime_mode": "host_shared",
            "selected_launcher": "/trusted/bin/python",
            "resolved_executable": "/trusted/bin/python3",
            "prefix": "/trusted",
            "mutex_identity_sha256": "e" * 64,
        },
        "source_policy": {
            "trusted": True,
            "binary_wheels_only": True,
            "host_runtime_verified": True,
            "artifact_report_verified": False,
            "artifact_evidence": {},
        },
        "index": {
            "fingerprint": "a" * 64,
            "artifact_hosts": ["files.pythonhosted.org"],
        },
        "requirements": [
            {
                "requirement": "numpy",
                "distribution": "numpy",
                "applicable": True,
                "installed_version": "2.4.6",
                "version_satisfied": True,
                "imports_ok": True,
                "satisfied": True,
                "resolution_source": "host_runtime",
            }
        ],
    }



class StatusTests(unittest.TestCase):
    def test_optional_v2_stages_are_advisory_not_resume_blockers(self) -> None:
        with TemporaryDirectory() as temp_dir:
            case = Path(temp_dir) / "case"
            case.mkdir()
            write_json(case / "workflow.json", {"workflow_version": "2"})

            status = inspect_case_status(case)
            by_name = {item["stage"]: item for item in status["stages"]}

            self.assertEqual(status["next_stage"], "paper")
            for name in (
                "paper_thesis", "scientific_architecture", "foundation_manifest",
                "review_docx", "reproduction_report_docx", "result_review_docx",
            ):
                self.assertFalse(by_name[name]["required"])
                self.assertTrue(by_name[name]["advisory"])

    def test_status_reports_resume_from_environment_resolver(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            case = root / "case"
            case.mkdir()
            write_json(case / "workflow.json", {"workflow_version": "2"})
            write_json(case / "paper_chunks.json", {"source_path": str(root / "paper.md"), "chunks": [{"chunk_id": "text_c1", "text": "AWGN"}]})
            write_json(
                case / "engineering_facts.json",
                {
                    "paper_domain": "communication",
                    "paper_repro_type": "signal_chain",
                    "engineering_facts": [
                        {
                            "type": "channel_model",
                            "name": "AWGN",
                            "value": {},
                            "source": {"source_kind": "text", "chunk_id": "text_c1", "page": None, "section": "", "quote": "AWGN", "figure_ref": ""},
                            "confidence": "high",
                            "used_for_reproduction": True,
                        }
                    ],
                    "missing_information": [],
                },
            )
            write_json(
                case / "paper_thesis.json",
                {
                    "central_claim": "AWGN 下 BER 随 SNR 增加而下降。",
                    "proposed_method": "基准通信链路",
                    "mechanism": "噪声相对功率降低会减少判决错误。",
                    "comparisons": [],
                    "headline_shape": "BER 单调下降。",
                    "caveats": [],
                },
            )
            write_json(
                case / "repro_tasks.json",
                {
                    "repro_tasks": [
                        {
                            "task_id": "reproduce_fig_1",
                            "target": "BER vs SNR",
                            "metric": "bit_error_rate",
                            "metric_formula": "bit_error_rate = bit_errors / total_bits",
                            "figure_or_claim": "Fig. 1",
                            "expected_artifacts": ["outputs/results.csv", "outputs/plot.png", "outputs/summary.json"],
                            "output_columns": ["snr_db", "bit_error_rate"],
                            "expected_trend": {
                                "x_axis": "snr_db",
                                "y_axis": "bit_error_rate",
                                "direction": "decreasing",
                                "reason": "Higher SNR should reduce BER.",
                            },
                            "comparison": {"baselines": ["AWGN"], "curve_groups": ["simulated"], "tolerance": "qualitative"},
                            "required_facts": [{"type": "channel_model", "name": "AWGN"}],
                            "assumptions": [],
                            "risk_if_unreproducible": "Core result cannot be checked.",
                        }
                    ]
                },
            )
            write_json(
                case / "experiment_index.json",
                {
                    "experiments": [
                        {
                            "experiment_id": "exp_reproduce_fig_1",
                            "title": "BER vs SNR",
                            "figure_or_table": "Fig. 1",
                            "task_id": "reproduce_fig_1",
                            "metric": "bit_error_rate",
                            "source_pages": [],
                            "source_chunk_ids": ["text_c1"],
                            "required_facts": [{"type": "channel_model", "name": "AWGN"}],
                            "limitations": [],
                        }
                    ]
                },
            )

            status = inspect_case_status(case)

            self.assertEqual(status["next_stage"], "execution_plan")
            self.assertEqual(status["resume_from"], "02e_compile_execution_plan")

            write_json(
                case / "execution_plan.json",
                {
                    "schema_version": "1.0",
                    "logical_task_count": 1,
                    "execution_unit_count": 1,
                    "task_to_execution_unit": {
                        "reproduce_fig_1": "unit_reproduce_fig_1"
                    },
                    "execution_units": [
                        {
                            "unit_id": "unit_reproduce_fig_1",
                            "mode": "singleton",
                            "task_ids": ["reproduce_fig_1"],
                            "relationships": [],
                            "dependencies": [],
                            "artifact_ids": [],
                        }
                    ],
                    "weak_consistency_groups": [],
                    "artifact_dependencies": [],
                    "task_order": ["reproduce_fig_1"],
                },
            )
            status = inspect_case_status(case)
            self.assertEqual(status["next_stage"], "environment_lock")
            self.assertEqual(status["resume_from"], "03a_environment_resolver")
            self.assertIn("python -m geng_agent review", status["suggested_command"])
            self.assertIn(str(root / "paper.md"), status["suggested_command"])
            self.assertIn("--run-repro", status["suggested_command"])

            write_json(case / "03a_environment.lock.json", valid_host_environment_lock())
            status = inspect_case_status(case)
            self.assertEqual(status["next_stage"], "repro_project_manifest")
            self.assertEqual(status["resume_from"], "03c_task_writer_workflow")


    def test_status_accepts_task_writer_manifest_required_files(self) -> None:
        with TemporaryDirectory() as temp_dir:
            case = Path(temp_dir) / "case"
            case.mkdir()
            write_json(case / "workflow.json", {"workflow_version": "2"})
            files = ["README.md", "requirements.txt", "config.json", "config_smoke.json", "src/channel.py"]
            write_json(
                case / "repro_project_manifest.json",
                {
                    "files": [{"path": path, "content_lines": ["x"]} for path in files],
                    "_meta": {"backend": "codex", "mode": "task_writers", "generated_paths": files},
                },
            )

            stage = next(item for item in inspect_case_status(case)["stages"] if item["stage"] == "repro_project_manifest")

            self.assertTrue(stage["ok"], stage)

    def test_status_prefers_newer_result_review_error_over_stale_json(self) -> None:
        with TemporaryDirectory() as temp_dir:
            case = Path(temp_dir) / "case"
            case.mkdir()
            write_json(case / "workflow.json", {"workflow_version": "2"})
            dims = [
                "artifact_coverage",
                "reproduction_logic",
                "trend_shape",
                "metric_axis_scale",
                "baseline_comparison",
                "statistical_reliability",
                "conclusion_support",
            ]
            write_json(
                case / "result_review.json",
                {
                    "overall_result_credibility": "medium",
                    "overall_alignment": "partial_match",
                    "experiment_reviews": [
                        {
                            "task_id": "t1",
                            "local_result_credibility": "medium",
                            "paper_alignment": "partial_match",
                            "scientific_verdict": "partially_supports_paper_claim",
                            "dimension_reviews": [
                                {"dimension": dim, "rating": "acceptable", "finding": "ok", "evidence": ["e"]}
                                for dim in dims
                            ],
                            "paper_result_summary": "paper",
                            "local_result_summary": "local",
                            "differences": [],
                            "possible_causes": [],
                            "evidence": ["e"],
                            "limitations": [],
                            "confidence": "medium",
                        }
                    ],
                    "cross_experiment_findings": [],
                    "recommended_human_checks": [],
                },
            )
            write_json(
                case / "report_editor_error.json",
                {"enabled": True, "passed": False, "reason": "task writer report assembly failed", "error": "empty report"},
            )

            stage = next(item for item in inspect_case_status(case)["stages"] if item["stage"] == "result_review")

            self.assertFalse(stage["ok"])
            self.assertIn("task writer report assembly failed", stage["reason"])

    def test_status_accepts_markdown_result_review(self) -> None:
        with TemporaryDirectory() as temp_dir:
            case = Path(temp_dir) / "case"
            case.mkdir()
            write_json(case / "workflow.json", {"workflow_version": "2"})
            (case / "result_review.md").write_text("# result review\n\nhuman readable report\n", encoding="utf-8")

            stage = next(item for item in inspect_case_status(case)["stages"] if item["stage"] == "result_review")

            self.assertTrue(stage["ok"], stage)
            self.assertEqual(stage["reason"], "present")


if __name__ == "__main__":
    unittest.main()
