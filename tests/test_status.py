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
            self.assertEqual(status["resume_from"], "03_generate_repro_project")
            self.assertIn("python -m geng_agent review", status["suggested_command"])
            self.assertIn(str(root / "paper.md"), status["suggested_command"])
            self.assertIn("--run-repro", status["suggested_command"])


if __name__ == "__main__":
    unittest.main()
