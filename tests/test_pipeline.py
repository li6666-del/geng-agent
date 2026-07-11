from __future__ import annotations

import inspect
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from geng_agent.agentic_analysis import CODEX_ANALYSIS_BACKEND
from geng_agent.pipeline import ReviewPipeline


def fact_doc(*facts: dict) -> dict:
    return {
        "paper_domain": "communication",
        "paper_repro_type": "signal_chain",
        "engineering_facts": list(facts),
        "missing_information": [],
    }


def fact(fact_type: str, name: str) -> dict:
    return {
        "type": fact_type,
        "name": name,
        "value": {},
        "source": {
            "source_kind": "text",
            "chunk_id": "text_c1",
            "page": 1,
            "section": "Simulation",
            "quote": name,
            "figure_ref": "",
        },
        "confidence": "high",
        "used_for_reproduction": True,
    }


def task_doc(*tasks: dict) -> dict:
    return {"repro_tasks": list(tasks)}


def task(task_id: str, figure_or_claim: str) -> dict:
    return {
        "task_id": task_id,
        "target": f"Reproduce {figure_or_claim}",
        "metric": "bit_error_rate",
        "metric_formula": "bit_error_rate = errors / bits",
        "figure_or_claim": figure_or_claim,
        "expected_artifacts": ["results.csv", "figure.png", "summary.json"],
        "output_columns": ["snr_db", "bit_error_rate"],
        "expected_trend": {
            "x_axis": "snr_db",
            "y_axis": "bit_error_rate",
            "direction": "decreasing",
            "reason": "Higher SNR reduces BER.",
        },
        "comparison": {
            "baselines": ["paper baseline"],
            "curve_groups": ["proposed"],
            "tolerance": "qualitative",
        },
        "required_facts": [{"type": "figure_claim", "name": figure_or_claim}],
        "assumptions": [],
        "risk_if_unreproducible": "The paper figure cannot be checked.",
    }


class PipelineTests(unittest.TestCase):
    def test_analysis_width_and_round_caps_are_not_public_pipeline_options(self) -> None:
        run_params = inspect.signature(ReviewPipeline.run).parameters
        stage_params = inspect.signature(ReviewPipeline.run_stage).parameters
        for name in ("facts_gap_rounds", "tasks_gap_rounds", "analysis_agent_width", "codex_agent_rounds", "result_review"):
            self.assertNotIn(name, run_params)
            self.assertNotIn(name, stage_params)

    def test_codex_analysis_uses_one_fact_specialist(self) -> None:
        candidate = fact_doc(fact("simulation_parameter", "SNR range"), fact("metric", "BER"))

        def fake_stage(**kwargs):
            self.assertEqual(kwargs["stage_label"], "01_extract_engineering_facts")
            return candidate

        with TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            audit = base / "audit"
            audit.mkdir()
            pipe = ReviewPipeline(client=None)
            with patch.object(pipe, "_load_or_create_stage_json", side_effect=fake_stage) as mocked:
                merged = pipe._load_or_create_analysis_stage_json(
                    output_path=base / "engineering_facts.json",
                    output_dir=base,
                    audit_dir=audit,
                    prompt="extract facts",
                    stage_label="01_extract_engineering_facts",
                    cleanup_stage="facts",
                    schema_stage="engineering_facts",
                    max_attempts=1,
                    resume=False,
                    backend=CODEX_ANALYSIS_BACKEND,
                )

            self.assertEqual(mocked.call_count, 1)
            self.assertEqual([f["name"] for f in merged["engineering_facts"]], ["SNR range", "BER"])

    def test_single_specialist_resume_is_forwarded(self) -> None:
        calls: list[dict] = []

        def fake_stage(**kwargs):
            calls.append(kwargs)
            self.assertTrue(kwargs["resume"])
            return fact_doc(fact("metric", "BER"))

        with TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            audit = base / "audit"
            audit.mkdir()
            pipe = ReviewPipeline(client=None)
            with patch.object(pipe, "_load_or_create_stage_json", side_effect=fake_stage):
                merged = pipe._load_or_create_analysis_stage_json(
                    output_path=base / "engineering_facts.json",
                    output_dir=base,
                    audit_dir=audit,
                    prompt="extract facts",
                    stage_label="01_extract_engineering_facts",
                    cleanup_stage="facts",
                    schema_stage="engineering_facts",
                    max_attempts=1,
                    resume=True,
                    backend=CODEX_ANALYSIS_BACKEND,
                )

            self.assertEqual(len(calls), 1)
            self.assertEqual(merged["engineering_facts"][0]["name"], "BER")

    def test_codex_analysis_uses_one_task_design_specialist(self) -> None:
        candidate = task_doc(task("reproduce_fig_4", "Fig. 4"), task("reproduce_fig_7", "Fig. 7"))

        def fake_stage(**kwargs):
            self.assertEqual(kwargs["stage_label"], "02_build_repro_tasks")
            return candidate

        with TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            audit = base / "audit"
            audit.mkdir()
            pipe = ReviewPipeline(client=None)
            with patch.object(pipe, "_load_or_create_stage_json", side_effect=fake_stage):
                merged = pipe._load_or_create_analysis_stage_json(
                    output_path=base / "repro_tasks.json",
                    output_dir=base,
                    audit_dir=audit,
                    prompt="build tasks",
                    stage_label="02_build_repro_tasks",
                    cleanup_stage="tasks",
                    schema_stage="repro_tasks",
                    max_attempts=1,
                    resume=False,
                    backend=CODEX_ANALYSIS_BACKEND,
                )

            self.assertEqual(
                [t["figure_or_claim"] for t in merged["repro_tasks"]],
                ["Fig. 4", "Fig. 7"],
            )

    def test_pipeline_api_is_codex_only_and_thesis_is_mandatory(self) -> None:
        run_params = inspect.signature(ReviewPipeline.run).parameters
        stage_params = inspect.signature(ReviewPipeline.run_stage).parameters
        self.assertNotIn("project_backend", run_params)
        self.assertNotIn("project_backend", stage_params)
        self.assertNotIn("science_loop", run_params)
        source = inspect.getsource(ReviewPipeline.run)
        self.assertIn("paper_thesis = self._load_or_create_paper_thesis(", source)
        self.assertNotIn("if science_loop", source)

    def test_single_reporter_runs_after_all_task_writers(self) -> None:
        source = inspect.getsource(ReviewPipeline.run)
        self.assertLess(
            source.index("run_codex_task_writer_workflow("),
            source.index("run_codex_reporter_workflow("),
        )
        self.assertNotIn("render_review_markdown(", source)

    def test_analysis_agent_width_is_not_a_pipeline_option(self) -> None:
        self.assertNotIn("analysis_agent_width", inspect.signature(ReviewPipeline.run).parameters)

    def test_report_renderer_creates_all_three_word_reports(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for name in ("review.md", "reproduction_report.md", "result_review.md"):
                (root / name).write_text("## task_1\n\n报告正文。\n", encoding="utf-8")

            result = ReviewPipeline()._generate_docx_reports(
                output_dir=root,
                result_review_result={"passed": True},
            )

            self.assertTrue(result["review_docx"]["passed"])
            self.assertTrue(result["reproduction_report_docx"]["passed"])
            self.assertTrue(result["result_review_docx"]["passed"])
            for name in ("review.docx", "reproduction_report.docx", "result_review.docx"):
                self.assertTrue((root / name).exists())


if __name__ == "__main__":
    unittest.main()
