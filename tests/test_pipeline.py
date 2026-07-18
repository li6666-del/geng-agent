from __future__ import annotations

import inspect
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from geng_agent.agentic_analysis import CODEX_ANALYSIS_BACKEND
from geng_agent.outputs import write_json
from geng_agent.pipeline import ReviewPipeline
from geng_agent.task_evidence_backfill import collect_missing_fact_requests


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
            self.assertEqual(kwargs["stage_label"], "02a_build_preliminary_repro_tasks")
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
                    stage_label="02a_build_preliminary_repro_tasks",
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

    def test_analysis_is_task_driven_and_has_no_open_ended_gap_loop(self) -> None:
        source = inspect.getsource(ReviewPipeline.run)
        self.assertNotIn("_augment_facts_with_gap_finder", source)
        self.assertNotIn("_augment_tasks_with_gap_finder", source)
        self.assertLess(source.index("engineering_facts_initial.json"), source.index("repro_tasks_preliminary.json"))
        self.assertLess(source.index("repro_tasks_preliminary.json"), source.index("targeted_fact_backfill.md"))
        self.assertLess(source.index("targeted_fact_backfill.md"), source.index("finalize_repro_tasks.md"))

    def test_isolated_task_reporters_and_final_editor_follow_task_writers(self) -> None:
        source = inspect.getsource(ReviewPipeline.run)
        self.assertLess(
            source.index("run_codex_task_writer_workflow("),
            source.index("task_review_callback=_review_one_task"),
        )
        self.assertLess(
            source.index('if not runtime_result.get("passed")'),
            source.index("run_codex_report_editor_workflow("),
        )
        self.assertNotIn("render_review_markdown(", source)
        self.assertIn("run_codex_task_reporter_workflow(", source)
        self.assertIn("revision_target", source)
        self.assertIn("apply_verified_result(", source)
        self.assertIn('not verification_result.get("all_accepted")', source)
        self.assertIn("run_codex_report_editor_workflow(", source)
        self.assertIn("writer_session_count", source)
        self.assertIn('report_editor_result.get("retryable")', source)
        self.assertIn("repair_context=report_editor_result", source)
        self.assertIn("allow_fallback=True", source)
        self.assertIn("report_editor_invocations += int(", source)

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

    def test_pipeline_runs_one_converged_backfill_round_and_refreshes_tasks(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paper_path = root / "paper.md"
            paper_path.write_text("# Results\nFig. 4 reports throughput versus SNR.", encoding="utf-8")
            output_dir = root / "case"
            calls: list[str] = []

            initial = fact_doc(
                fact("figure_claim", "Fig. 4 throughput versus SNR"),
                fact("metric", "throughput"),
            )
            draft_task = task("reproduce_fig_4", "Fig. 4")
            draft_task["missing_fact_requests"] = [
                {
                    "request_id": "fig4_normalization",
                    "type": "simulation_parameter",
                    "name": "Fig. 4 power normalization",
                    "why_needed": "sets the simulation x axis",
                    "impact": "high",
                    "search_targets": ["Fig. 4 caption"],
                }
            ]
            preliminary = task_doc(draft_task)
            preliminary["_meta"] = {
                "backfill_handoff": {
                    "ready_for_writer": False,
                    "blocking_request_ids": ["fig4_normalization"],
                    "reason": "power normalization changes the experiment",
                }
            }
            aggregate_request = collect_missing_fact_requests(preliminary)[0]
            backfill_fact = fact("simulation_parameter", "Fig. 4 power normalization")
            backfill_fact["evidence_kind"] = "paper_explicit"
            backfill = {
                **fact_doc(backfill_fact),
                "request_resolutions": [
                    {
                        "request_id": aggregate_request["request_id"],
                        "field_results": [
                            {
                                "field_id": "answer",
                                "status": "resolved_explicit",
                                "fact_refs": [
                                    {
                                        "type": "simulation_parameter",
                                        "name": "Fig. 4 power normalization",
                                    }
                                ],
                                "searched_locations": ["Fig. 4 caption"],
                                "note": "explicitly stated",
                            }
                        ],
                    }
                ],
            }
            finalized = task_doc({**draft_task, "missing_fact_requests": []})
            finalized["backfill_handoff"] = {
                "ready_for_writer": True,
                "blocking_request_ids": [],
                "reason": "the task is ready for writer implementation",
            }

            def fake_analysis_stage(**kwargs):
                label = kwargs["stage_label"]
                calls.append(label)
                documents = {
                    "01_extract_engineering_facts": initial,
                    "02a_build_preliminary_repro_tasks": preliminary,
                    "02b_round_01_targeted_fact_backfill": backfill,
                    "02c_round_01_refresh_repro_tasks": finalized,
                }
                document = documents[label]
                write_json(kwargs["output_path"], document)
                return document

            def fake_thesis(**kwargs):
                document = {
                    "central_claim": "throughput increases with SNR",
                    "proposed_method": "method",
                    "mechanism": "higher SNR improves decoding",
                    "comparisons": [],
                    "headline_shape": "increasing",
                    "caveats": [],
                }
                write_json(kwargs["output_dir"] / "paper_thesis.json", document)
                return document

            pipeline = ReviewPipeline()
            with (
                patch.object(pipeline, "_load_or_create_analysis_stage_json", side_effect=fake_analysis_stage),
                patch.object(pipeline, "_load_or_create_paper_thesis", side_effect=fake_thesis),
            ):
                result = pipeline.run(paper_path, output_dir, resume=False, analysis_only=True)

            self.assertEqual(
                calls,
                [
                    "01_extract_engineering_facts",
                    "02a_build_preliminary_repro_tasks",
                    "02b_round_01_targeted_fact_backfill",
                    "02c_round_01_refresh_repro_tasks",
                ],
            )
            final_facts = json.loads((output_dir / "engineering_facts.json").read_text(encoding="utf-8"))
            final_tasks = json.loads((output_dir / "repro_tasks.json").read_text(encoding="utf-8"))
            self.assertIn("Fig. 4 power normalization", [item["name"] for item in final_facts["engineering_facts"]])
            self.assertEqual(final_tasks["repro_tasks"][0]["missing_fact_requests"], [])
            self.assertIn(
                {"type": "simulation_parameter", "name": "Fig. 4 power normalization"},
                final_tasks["repro_tasks"][0]["required_facts"],
            )
            self.assertIsNone(result.runtime_passed)
            self.assertTrue((output_dir / "analysis_result.json").exists())
            analysis_result = json.loads(
                (output_dir / "analysis_result.json").read_text(encoding="utf-8")
            )
            self.assertEqual(analysis_result["analysis_stage_invocations"], 5)
            self.assertFalse((output_dir / "repro_project").exists())
            self.assertFalse((output_dir / "runtime_result.json").exists())

    def test_pipeline_runs_second_round_for_new_task_field(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paper_path = root / "paper.md"
            paper_path.write_text("# Results\nFig. 4 reports throughput versus SNR.", encoding="utf-8")
            output_dir = root / "case"
            calls: list[str] = []

            initial = fact_doc(
                fact("figure_claim", "Fig. 4 throughput versus SNR"),
                fact("metric", "throughput"),
            )
            draft = task("reproduce_fig_4", "Fig. 4")
            draft["missing_fact_requests"] = [
                {
                    "request_id": "fig4_setup",
                    "type": "simulation_parameter",
                    "name": "Fig. 4 simulation setup",
                    "why_needed": "controls the implementation",
                    "impact": "high",
                    "search_targets": ["Fig. 4"],
                    "required_fields": [
                        {
                            "field_id": "normalization",
                            "description": "power normalization",
                            "affects": ["formula_chain"],
                        }
                    ],
                }
            ]
            preliminary = task_doc(draft)
            preliminary["_meta"] = {
                "backfill_handoff": {
                    "ready_for_writer": False,
                    "blocking_request_ids": ["fig4_setup"],
                    "reason": "simulation setup changes the implementation",
                }
            }
            aggregate_id = collect_missing_fact_requests(preliminary)[0]["request_id"]

            setup_fact = fact("simulation_parameter", "Fig. 4 simulation setup")
            setup_fact["evidence_kind"] = "paper_explicit"
            round_1_backfill = {
                **fact_doc(setup_fact),
                "request_resolutions": [
                    {
                        "request_id": aggregate_id,
                        "field_results": [
                            {
                                "field_id": "normalization",
                                "status": "resolved_explicit",
                                "fact_refs": [
                                    {
                                        "type": "simulation_parameter",
                                        "name": "Fig. 4 simulation setup",
                                    }
                                ],
                                "searched_locations": ["Fig. 4"],
                                "note": "found normalization",
                            }
                        ],
                    }
                ],
            }
            round_1_task = json.loads(json.dumps(preliminary))
            round_1_task["repro_tasks"][0]["missing_fact_requests"][0]["required_fields"].append(
                {
                    "field_id": "trial_count",
                    "description": "Monte Carlo trial count",
                    "affects": ["statistical_protocol"],
                }
            )
            round_1_task["backfill_handoff"] = {
                "ready_for_writer": False,
                "blocking_request_ids": [aggregate_id],
                "reason": "trial count changes the statistical protocol",
            }
            round_2_backfill = {
                **fact_doc(),
                "request_resolutions": [
                    {
                        "request_id": aggregate_id,
                        "field_results": [
                            {
                                "field_id": "trial_count",
                                "status": "not_found_in_paper",
                                "fact_refs": [],
                                "searched_locations": ["Fig. 4", "Simulation Setup"],
                                "note": "paper does not disclose a trial count",
                            }
                        ],
                    }
                ],
            }
            round_2_task = json.loads(json.dumps(round_1_task))
            round_2_task["repro_tasks"][0]["assumptions"] = []
            round_2_task["backfill_handoff"] = {
                "ready_for_writer": True,
                "blocking_request_ids": [],
                "reason": "writer can choose and test an explicit trial-count assumption",
            }

            documents = {
                "01_extract_engineering_facts": initial,
                "02a_build_preliminary_repro_tasks": preliminary,
                "02b_round_01_targeted_fact_backfill": round_1_backfill,
                "02c_round_01_refresh_repro_tasks": round_1_task,
                "02b_round_02_targeted_fact_backfill": round_2_backfill,
                "02c_round_02_refresh_repro_tasks": round_2_task,
            }

            def fake_analysis_stage(**kwargs):
                label = kwargs["stage_label"]
                calls.append(label)
                document = documents[label]
                write_json(kwargs["output_path"], document)
                return document

            def fake_thesis(**kwargs):
                document = {
                    "central_claim": "throughput increases with SNR",
                    "proposed_method": "method",
                    "mechanism": "higher SNR improves decoding",
                    "comparisons": [],
                    "headline_shape": "increasing",
                    "caveats": [],
                }
                write_json(kwargs["output_dir"] / "paper_thesis.json", document)
                return document

            pipeline = ReviewPipeline()
            with (
                patch.object(pipeline, "_load_or_create_analysis_stage_json", side_effect=fake_analysis_stage),
                patch.object(pipeline, "_load_or_create_paper_thesis", side_effect=fake_thesis),
            ):
                pipeline.run(paper_path, output_dir, resume=False, analysis_only=True)

            self.assertEqual(len(calls), 6)
            summary = json.loads(
                (output_dir / "audit" / "02b_targeted_fact_backfill_summary.json").read_text(encoding="utf-8")
            )
            self.assertEqual(summary["round_count"], 2)
            self.assertEqual(summary["terminal_unresolved_count"], 1)
            self.assertEqual(summary["stop_reason"], "task_expert_handoff_ready")
            analysis_result = json.loads(
                (output_dir / "analysis_result.json").read_text(encoding="utf-8")
            )
            self.assertEqual(analysis_result["analysis_stage_invocations"], 7)
            diagnostics = json.loads(
                (output_dir / "audit" / "02c_terminal_gap_diagnostics.json").read_text(encoding="utf-8")
            )
            self.assertTrue(diagnostics["advisory"])
            self.assertFalse(diagnostics["passed"])
            self.assertGreaterEqual(diagnostics["issue_count"], 1)
            warnings = json.loads(
                (output_dir / "analysis_warnings.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertTrue(warnings["advisory_only"])
            self.assertGreaterEqual(warnings["warning_count"], 1)
            self.assertIn(
                "terminal_gap",
                {item["category"] for item in warnings["warnings"]},
            )
            final_tasks = json.loads((output_dir / "repro_tasks.json").read_text(encoding="utf-8"))
            self.assertEqual(
                final_tasks["_meta"]["fact_gap_handoff"]["stop_reason"],
                "task_expert_handoff_ready",
            )


if __name__ == "__main__":
    unittest.main()
