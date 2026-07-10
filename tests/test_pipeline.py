from __future__ import annotations

import inspect
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from geng_agent.agentic_analysis import CODEX_ANALYSIS_BACKEND
from geng_agent.facts_coverage import merge_engineering_facts, merge_repro_tasks
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
    def test_facts_gap_rounds_default_is_six(self) -> None:
        self.assertEqual(inspect.signature(ReviewPipeline.run).parameters["facts_gap_rounds"].default, 6)
        self.assertEqual(inspect.signature(ReviewPipeline.run_stage).parameters["facts_gap_rounds"].default, 6)
        self.assertEqual(inspect.signature(ReviewPipeline.run).parameters["analysis_agent_width"].default, 2)
        self.assertEqual(inspect.signature(ReviewPipeline.run_stage).parameters["analysis_agent_width"].default, 2)

    def test_codex_analysis_ensemble_merges_and_dedupes_facts(self) -> None:
        candidates = {
            "01_extract_engineering_facts_agent_1": fact_doc(
                fact("simulation_parameter", "SNR range"),
                fact("metric", "BER"),
            ),
            "01_extract_engineering_facts_agent_2": fact_doc(
                fact("simulation_parameter", "snr  range"),
                fact("channel_model", "Rayleigh"),
            ),
        }

        def fake_stage(**kwargs):
            return candidates[kwargs["stage_label"]]

        with TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            audit = base / "audit"
            audit.mkdir()
            pipe = ReviewPipeline(client=None)
            with patch.object(pipe, "_load_or_create_stage_json", side_effect=fake_stage) as mocked:
                merged = pipe._load_or_create_ensemble_stage_json(
                    output_path=base / "engineering_facts.json",
                    output_dir=base,
                    audit_dir=audit,
                    prompt="extract facts",
                    stage_label="01_extract_engineering_facts",
                    cleanup_stage="facts",
                    schema_stage="engineering_facts",
                    max_attempts=1,
                    resume=False,
                    agent_width=2,
                    merge_func=merge_engineering_facts,
                    backend=CODEX_ANALYSIS_BACKEND,
                )

            self.assertEqual(mocked.call_count, 2)
            prompts = [call.kwargs["prompt"] for call in mocked.call_args_list]
            self.assertNotEqual(prompts[0], prompts[1])
            self.assertIn("text/formula specialist", prompts[0])
            self.assertIn("visual/experiment specialist", prompts[1])
            self.assertEqual(
                [f["name"] for f in merged["engineering_facts"]],
                ["SNR range", "BER", "Rayleigh"],
            )
            self.assertEqual(merged["_meta"]["analysis_agent_width"], 2)
            summary = json.loads((audit / "01_extract_engineering_facts_ensemble_summary.json").read_text(encoding="utf-8"))
            self.assertTrue(summary["ok"])
            self.assertEqual(summary["added_by_agent"]["agent_1"], 2)
            self.assertEqual(summary["added_by_agent"]["agent_2"], 1)
            self.assertEqual(summary["total_items"], 3)

    def test_codex_analysis_ensemble_resume_clears_stale_candidate_cache(self) -> None:
        candidates = {
            "01_extract_engineering_facts_agent_1": fact_doc(fact("metric", "BER")),
            "01_extract_engineering_facts_agent_2": fact_doc(fact("channel_model", "Rayleigh")),
        }
        calls: list[dict] = []

        def fake_stage(**kwargs):
            calls.append(kwargs)
            self.assertFalse(kwargs["output_path"].exists())
            self.assertFalse(kwargs["resume"])
            return candidates[kwargs["stage_label"]]

        with TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            audit = base / "audit"
            audit.mkdir()
            stale_candidate = base / "engineering_facts_agent_1.json"
            stale_candidate.write_text(json.dumps(fact_doc(fact("metric", "STALE"))), encoding="utf-8")

            pipe = ReviewPipeline(client=None)
            with patch.object(pipe, "_load_or_create_stage_json", side_effect=fake_stage):
                merged = pipe._load_or_create_ensemble_stage_json(
                    output_path=base / "engineering_facts.json",
                    output_dir=base,
                    audit_dir=audit,
                    prompt="extract facts",
                    stage_label="01_extract_engineering_facts",
                    cleanup_stage="facts",
                    schema_stage="engineering_facts",
                    max_attempts=1,
                    resume=True,
                    agent_width=2,
                    merge_func=merge_engineering_facts,
                    backend=CODEX_ANALYSIS_BACKEND,
                )

            self.assertEqual(len(calls), 2)
            self.assertFalse(stale_candidate.exists())
            self.assertNotIn("STALE", {f["name"] for f in merged["engineering_facts"]})

    def test_codex_analysis_ensemble_merges_and_dedupes_tasks(self) -> None:
        candidates = {
            "02_build_repro_tasks_agent_1": task_doc(
                task("reproduce_fig_4", "Fig. 4"),
                task("reproduce_fig_7", "Fig. 7"),
            ),
            "02_build_repro_tasks_agent_2": task_doc(
                task("reproduce_fig_4b", "Fig. 4"),
                task("reproduce_fig_9a", "Fig. 9(a)"),
            ),
        }

        def fake_stage(**kwargs):
            return candidates[kwargs["stage_label"]]

        with TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            audit = base / "audit"
            audit.mkdir()
            pipe = ReviewPipeline(client=None)
            with patch.object(pipe, "_load_or_create_stage_json", side_effect=fake_stage):
                merged = pipe._load_or_create_ensemble_stage_json(
                    output_path=base / "repro_tasks.json",
                    output_dir=base,
                    audit_dir=audit,
                    prompt="build tasks",
                    stage_label="02_build_repro_tasks",
                    cleanup_stage="tasks",
                    schema_stage="repro_tasks",
                    max_attempts=1,
                    resume=False,
                    agent_width=2,
                    merge_func=merge_repro_tasks,
                    backend=CODEX_ANALYSIS_BACKEND,
                )

            self.assertEqual(
                [t["figure_or_claim"] for t in merged["repro_tasks"]],
                ["Fig. 4", "Fig. 7", "Fig. 9(a)"],
            )
            summary = json.loads((audit / "02_build_repro_tasks_ensemble_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["added_by_agent"], {"agent_1": 2, "agent_2": 1})

    def test_pipeline_api_is_codex_only_and_thesis_is_mandatory(self) -> None:
        run_params = inspect.signature(ReviewPipeline.run).parameters
        stage_params = inspect.signature(ReviewPipeline.run_stage).parameters
        self.assertNotIn("project_backend", run_params)
        self.assertNotIn("project_backend", stage_params)
        self.assertNotIn("science_loop", run_params)
        source = inspect.getsource(ReviewPipeline.run)
        self.assertIn("paper_thesis = self._load_or_create_paper_thesis(", source)
        self.assertNotIn("if science_loop", source)

    def test_analysis_agent_width_rejects_unbounded_parallelism(self) -> None:
        with TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(ValueError, "analysis_agent_width must be between 1 and 8"):
                ReviewPipeline(client=None).run(
                    Path(temp_dir) / "paper.md",
                    Path(temp_dir) / "case",
                    analysis_agent_width=9,
                )


if __name__ == "__main__":
    unittest.main()
