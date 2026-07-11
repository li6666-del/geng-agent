from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from geng_agent.outputs import write_json
from geng_agent.status import inspect_case_status


class StatusTests(unittest.TestCase):
    def test_status_reports_resume_from_generate_project(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            case = root / "case"
            case.mkdir()
            write_json(case / "paper_chunks.json", {"source_path": str(root / "paper.md"), "chunks": [{"chunk_id": "text_c1", "text": "AWGN"}]})
            write_json(
                case / "paper_memory.json",
                {
                    "schema_version": "2.0",
                    "source": {"path": str(root / "paper.md"), "format": "md", "sha256": None, "page_count": None},
                    "entities": [
                        {
                            "entity_id": "section:text_c1",
                            "kind": "section",
                            "label": "text_c1",
                            "number": None,
                            "subfigure": None,
                            "page": None,
                            "chunk_ids": ["text_c1"],
                            "text": "AWGN",
                            "parent_id": None,
                        }
                    ],
                    "cross_references": [],
                    "metadata": {"builder": "test", "chunk_count": 1, "entity_count": 1},
                    "memory_hash": "test-hash",
                },
            )
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
                            "status": "ready",
                            "limitations": [],
                        }
                    ]
                },
            )

            status = inspect_case_status(case)

            self.assertEqual(status["next_stage"], "repro_project_manifest")
            self.assertEqual(status["resume_from"], "03c_task_writer_workflow")
            self.assertIn("python -m geng_agent review", status["suggested_command"])
            self.assertIn(str(root / "paper.md"), status["suggested_command"])
            self.assertIn("--run-repro", status["suggested_command"])

    def test_status_accepts_task_writer_manifest_required_files(self) -> None:
        with TemporaryDirectory() as temp_dir:
            case = Path(temp_dir) / "case"
            case.mkdir()
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
                case / "reporter_error.json",
                {"enabled": True, "passed": False, "reason": "task writer report assembly failed", "error": "empty report"},
            )

            stage = next(item for item in inspect_case_status(case)["stages"] if item["stage"] == "result_review")

            self.assertFalse(stage["ok"])
            self.assertIn("task writer report assembly failed", stage["reason"])

    def test_status_accepts_markdown_result_review(self) -> None:
        with TemporaryDirectory() as temp_dir:
            case = Path(temp_dir) / "case"
            case.mkdir()
            (case / "result_review.md").write_text("# result review\n\nhuman readable report\n", encoding="utf-8")

            stage = next(item for item in inspect_case_status(case)["stages"] if item["stage"] == "result_review")

            self.assertTrue(stage["ok"], stage)
            self.assertEqual(stage["reason"], "present")


if __name__ == "__main__":
    unittest.main()
