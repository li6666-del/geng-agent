from __future__ import annotations

import inspect
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from geng_agent.agentic_analysis import CODEX_ANALYSIS_BACKEND
from geng_agent.case_environment import RequirementRequest
from geng_agent.case_runtime import (
    CaseRuntime,
    EnvironmentRequestRequired,
    EnvironmentResolutionError,
)
from geng_agent.outputs import write_json
from geng_agent.supervisor import StageBlocked
from geng_agent.pipeline_analysis_flow import run_analysis_flow
from geng_agent.pipeline_execution_flow import run_execution_flow
from geng_agent.pipeline_report_flow import run_report_flow
from geng_agent.runtime_status import build_stage_cache_metadata
from geng_agent.scientific_materiality import SCIENTIFIC_POLICY_ID
from geng_agent.pipeline import ReviewPipeline
from geng_agent.schemas import validate_stage
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


def thesis_doc() -> dict:
    return {"central_claim": "BER decreases with SNR", "proposed_method": "test method",
            "mechanism": "higher SNR improves decoding", "comparisons": [],
            "headline_shape": "decreasing", "caveats": []}


def understanding_doc(facts: dict) -> dict:
    return {"facts": facts, "paper_thesis": thesis_doc(), "limitations": []}


def architecture_doc(output_dir: Path, tasks: dict | None = None) -> dict:
    index = ({"experiments": [{"task_id": item["task_id"], "experiment_id": f"exp_{item['task_id']}"}
                             for item in tasks["repro_tasks"]]} if tasks is not None
             else json.loads((output_dir / "experiment_index.json").read_text(encoding="utf-8")))
    basis = {"status": "unresolved", "evidence_facts": [], "assumption_refs": [], "note": "test fixture"}
    return {
        "schema_version": "1.1",
        "workflow_version": "2",
        "quantities": [],
        "components": [
            {
                "id": "system",
                "kind": "system",
                "module": "src/system.py",
                "callable": "build_system",
                "execution": {
                    "execution_kind": "deterministic_simulation",
                    "primary_framework": "standard_library",
                    "supporting_libraries": [],
                    "device_policy": "cpu",
                    "precision": "float64",
                    "trainable": False,
                    "gradient_mode": "not_applicable",
                    "checkpoint_policy": "not_applicable",
                    "shared_implementation": True,
                    "required_capabilities": ["deterministic_simulation"],
                    "rationale": "The fixture exercises a shared deterministic implementation.",
                },
                "inputs": [],
                "outputs": [],
                "parameters": [],
                "depends_on": [],
                "basis": basis,
            }
        ],
        "consistency_groups": [
            {
                "id": "test",
                "task_ids": [item["task_id"] for item in index["experiments"]],
                "shared_quantity_ids": [],
            }
        ],
        "bindings": [
            {
                "task_id": item["task_id"], "experiment_id": item["experiment_id"],
                "consistency_group": "test", "components": ["system"], "overrides": {}, "outputs": [],
            }
            for item in index["experiments"]
        ],
        "invariants": [],
    }


def case_runtime_fixture(output_dir: Path, environment_hash: str) -> CaseRuntime:
    runtime_dir = output_dir / "audit" / "03a_case_environment"
    return CaseRuntime(
        venv_dir=runtime_dir / "venv",
        python_executable=runtime_dir / "venv" / "bin" / "python",
        request_path=output_dir / "03a_environment_request.json",
        lock_path=output_dir / "03a_environment.lock.json",
        report_path=output_dir / "03a_environment_report.json",
        environment_hash=environment_hash,
        manifest={"schema_version": 1, "requirements": []},
        lock={"schema_version": 1, "ready": True, "environment_hash": environment_hash},
        report={"ready": True, "status": "ready"},
        trusted_read_roots=(output_dir.resolve(),),
    )


def _run_to_task_writer_boundary(
    root: Path,
    *,
    resume: bool,
    environment_mock: Mock,
    foundation_mock: Mock,
    task_writer_mock: Mock,
    tasks_document: dict | None = None,
    final_tasks_candidate: dict | None = None,
):
    paper_path = root / "paper.md"
    paper_path.write_text(
        "# Results\nFig. 4 reports bit error rate versus SNR.",
        encoding="utf-8",
    )
    output_dir = root / "case"
    if resume:
        output_dir.mkdir()
        write_json(
            output_dir / "workflow.json",
            {"workflow_version": "2", "architecture_contract": "scientific_architecture/1.1"},
        )
    initial = fact_doc(
        fact("figure_claim", "Fig. 4"),
        fact("metric", "bit_error_rate"),
    )
    preliminary = (
        json.loads(json.dumps(tasks_document))
        if isinstance(tasks_document, dict)
        else task_doc(task("reproduce_fig_4", "Fig. 4"))
    )
    preliminary["backfill_handoff"] = {
        "ready_for_writer": True,
        "blocking_request_ids": [],
        "reason": "fixture has no missing facts",
    }

    def fake_analysis_stage(**kwargs):
        if kwargs["stage_label"] == "01_understand_paper":
            document = understanding_doc(initial)
        else:
            document = {"tasks": preliminary, "scientific_architecture": architecture_doc(output_dir, preliminary)}
        document = json.loads(json.dumps(document))
        write_json(kwargs["output_path"], document)
        return document


    def fake_experiment_index(**kwargs):
        document = {
            "experiments": [
                {
                    "task_id": str(item["task_id"]),
                    "experiment_id": f"exp_{item['task_id']}",
                }
                for item in preliminary.get("repro_tasks", [])
                if isinstance(item, dict) and item.get("task_id")
            ]
        }
        write_json(kwargs["output_dir"] / "experiment_index.json", document)
        return document


    mineru_result = {
        "ok": True,
        "cached": False,
        "fallback_used": False,
        "duration_s": 0.0,
        "figure_count": 0,
        "figure_index": {"figures": [], "unmatched_visuals": []},
    }
    pipeline = ReviewPipeline()
    with (
        patch.object(pipeline, "_render_paper_images", return_value=[]),
        patch("geng_agent.pipeline.run_mineru_layout_stage", return_value=mineru_result),
        patch.object(
            pipeline,
            "_load_or_create_analysis_stage_json",
            side_effect=fake_analysis_stage,
        ),
        patch.object(
            pipeline,
            "_load_or_create_experiment_index",
            side_effect=fake_experiment_index,
        ),
        patch("geng_agent.case_runtime.ensure_case_runtime", new=environment_mock),
        patch(
            "geng_agent.agentic_foundation.run_codex_foundation_writer_workflow",
            new=foundation_mock,
        ),
        patch(
            "geng_agent.agentic_task_writers.run_codex_task_writer_workflow",
            new=task_writer_mock,
        ),
    ):
        return pipeline.run(paper_path, output_dir, resume=resume, analysis_only=False)


def _run_minimal_full_pipeline(
    root: Path,
    *,
    report_editor_error: Exception | None = None,
    report_editor_result: dict | None = None,
    report_mode: str = "model",
):
    paper_path = root / "paper.md"
    paper_path.write_text(
        "# Results\nFig. 4 reports bit error rate versus SNR.",
        encoding="utf-8",
    )
    output_dir = root / "case"
    initial = fact_doc(
        fact("figure_claim", "Fig. 4"),
        fact("metric", "bit_error_rate"),
    )
    preliminary = task_doc(task("reproduce_fig_4", "Fig. 4"))
    preliminary["backfill_handoff"] = {
        "ready_for_writer": True,
        "blocking_request_ids": [],
        "reason": "fixture has no missing facts",
    }

    def fake_analysis_stage(**kwargs):
        document = (understanding_doc(initial) if kwargs["stage_label"] == "01_understand_paper"
                    else {"tasks": preliminary, "scientific_architecture": None})
        document = json.loads(json.dumps(document))
        write_json(kwargs["output_path"], document)
        return document


    def fake_experiment_index(**kwargs):
        document = {
            "experiments": [
                {
                    "task_id": "reproduce_fig_4",
                    "experiment_id": "exp_reproduce_fig_4",
                }
            ]
        }
        write_json(kwargs["output_dir"] / "experiment_index.json", document)
        return document

    def fake_task_writer(**kwargs):
        kwargs["repro_project_dir"].mkdir(parents=True, exist_ok=True)
        return {
            "manifest": {"files": [], "_meta": {}},
            "written_files": [],
            "runtime_result": {"enabled": True, "passed": True, "coverage": {}},
            "task_records": [
                {
                    "task_id": "reproduce_fig_4",
                    "writer_session_count": 1,
                    "execution_summary": {"full_run_count": 1, "last_returncode": 0},
                }
            ],
            "status": {},
        }

    mineru_result = {
        "ok": True,
        "cached": False,
        "fallback_used": False,
        "duration_s": 0.0,
        "figure_count": 0,
        "figure_index": {"figures": [], "unmatched_visuals": []},
    }
    editor_patch = (
        patch(
            "geng_agent.agentic_report_editor.run_codex_report_editor_workflow",
            side_effect=report_editor_error,
        )
        if report_editor_error is not None
        else patch(
            "geng_agent.agentic_report_editor.run_codex_report_editor_workflow",
            return_value=report_editor_result,
        )
    )
    pipeline = ReviewPipeline()
    with (
        patch.object(pipeline, "_render_paper_images", return_value=[]),
        patch("geng_agent.pipeline.run_mineru_layout_stage", return_value=mineru_result),
        patch.object(
            pipeline,
            "_load_or_create_analysis_stage_json",
            side_effect=fake_analysis_stage,
        ),
        patch.object(
            pipeline,
            "_load_or_create_experiment_index",
            side_effect=fake_experiment_index,
        ),
        patch(
            "geng_agent.case_runtime.ensure_case_runtime",
            return_value=case_runtime_fixture(output_dir, "0" * 64),
        ),
        patch(
            "geng_agent.agentic_task_writers.run_codex_task_writer_workflow",
            side_effect=fake_task_writer,
        ),
        patch(
            "geng_agent.agentic_task_writers.apply_verified_result",
            return_value={"enabled": True, "passed": True, "coverage": {}},
        ),
        editor_patch,
        patch.dict("os.environ", {"GENG_REPORT_MODE": report_mode}),
        patch.object(
            pipeline,
            "_inspect_editor_word_reports",
            return_value={"enabled": False, "ok": True},
        ),
        patch("geng_agent.pipeline.build_automation_provenance", return_value={}),
    ):
        result = pipeline.run(
            paper_path,
            output_dir,
            resume=False,
            analysis_only=False,
        )
    return result, output_dir

class PipelineTests(unittest.TestCase):
    def test_required_foundation_cancel_does_not_start_task_writers(self) -> None:
        from geng_agent.progress import PipelineCancelled

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            writer = Mock()
            foundation = Mock(side_effect=PipelineCancelled("user stopped the run"))
            tasks_document = task_doc(task("t0", "Claim 0"), task("t1", "Claim 1"))
            tasks_document["execution_relationships"] = [{
                "relationship_id": "shared_science", "kind": "shared_definition",
                "strength": "weak", "task_ids": ["t0", "t1"],
                "producer_task_id": None, "consumer_task_ids": [], "artifact_ids": [],
            }]
            with self.assertRaisesRegex(PipelineCancelled, "user stopped"):
                _run_to_task_writer_boundary(
                    root, resume=False,
                    environment_mock=Mock(return_value=case_runtime_fixture(root / "case", "0" * 64)),
                    foundation_mock=foundation, task_writer_mock=writer,
                    tasks_document=tasks_document,
                )
            foundation.assert_called_once()
            writer.assert_not_called()
            self.assertFalse((root / "case/audit/03b_foundation_fallback.json").exists())

    def test_preliminary_task_cache_survives_snapshot_publication(self) -> None:
        expected_cache = {
            "stage_label": "02a_build_preliminary_repro_tasks",
            "fingerprint": "cache-fingerprint",
        }
        downstream_tasks: dict = {}
        initial = fact_doc(
            fact("figure_claim", "Fig. 4"),
            fact("metric", "bit_error_rate"),
        )
        preliminary = task_doc(task("reproduce_fig_4", "Fig. 4"))
        preliminary["_meta"] = {
            "cache": expected_cache,
            "untrusted": True,
        }
        preliminary["backfill_handoff"] = {
            "ready_for_writer": True,
            "blocking_request_ids": [],
            "reason": "fixture has no missing facts",
            "inferred": False,
        }

        def fake_analysis_stage(**kwargs):
            document = (
                understanding_doc(initial)
                if kwargs["stage_label"] == "01_understand_paper"
                else {"tasks": preliminary, "scientific_architecture": None}
            )
            document = json.loads(json.dumps(document))
            write_json(kwargs["output_path"], document)
            return document

        def stop_after_preliminary(**kwargs):
            downstream_tasks.update(kwargs["preliminary_tasks"])
            raise RuntimeError("stop after preliminary")

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paper_path = root / "paper.md"
            paper_path.write_text(
                "# Results\nFig. 4 reports bit error rate versus SNR.",
                encoding="utf-8",
            )
            output_dir = root / "case"
            mineru_result = {
                "ok": True,
                "cached": False,
                "fallback_used": False,
                "duration_s": 0.0,
                "figure_count": 0,
                "figure_index": {"figures": [], "unmatched_visuals": []},
            }
            pipeline = ReviewPipeline()
            with (
                patch.object(pipeline, "_render_paper_images", return_value=[]),
                patch(
                    "geng_agent.pipeline.run_mineru_layout_stage",
                    return_value=mineru_result,
                ),
                patch.object(
                    pipeline,
                    "_load_or_create_analysis_stage_json",
                    side_effect=fake_analysis_stage,
                ),
                patch(
                    "geng_agent.pipeline.run_targeted_backfill_loop",
                    side_effect=stop_after_preliminary,
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "stop after preliminary"):
                    pipeline.run(
                        paper_path,
                        output_dir,
                        resume=False,
                        analysis_only=True,
                    )

            persisted = json.loads(
                (output_dir / "repro_tasks_preliminary.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(persisted["_meta"]["cache"], expected_cache)
            self.assertTrue(persisted["_meta"]["untrusted"])
            self.assertEqual(validate_stage("repro_tasks", persisted), [])
            self.assertEqual(persisted, preliminary)
            self.assertEqual(downstream_tasks, preliminary)
    def test_pipeline_does_not_expose_codex_session_wall_clock_limits(self) -> None:
        removed_parameters = {
            "project_timeout",
            "codex_analysis_timeout",
            "codex_agent_timeout",
            "codex_reporter_timeout",
        }
        for method in (ReviewPipeline.run, ReviewPipeline.run_stage):
            with self.subTest(method=method.__name__):
                self.assertTrue(
                    removed_parameters.isdisjoint(
                        inspect.signature(method).parameters
                    )
                )







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
        facade_source = inspect.getsource(ReviewPipeline.run)
        analysis_source = inspect.getsource(run_analysis_flow)
        self.assertIn("paper_thesis = understanding.get", analysis_source)
        self.assertNotIn("if science_loop", facade_source)
        self.assertLess(
            facade_source.index("run_analysis_flow("),
            facade_source.index("run_execution_flow("),
        )
        self.assertLess(
            facade_source.index("run_execution_flow("),
            facade_source.index("run_report_flow("),
        )

    def test_analysis_is_task_driven_and_has_no_open_ended_gap_loop(self) -> None:
        source = inspect.getsource(run_analysis_flow)
        self.assertNotIn("_augment_facts_with_gap_finder", source)
        self.assertNotIn("_augment_tasks_with_gap_finder", source)
        self.assertLess(source.index("engineering_facts_initial.json"), source.index("repro_tasks_preliminary.json"))
        self.assertLess(source.index("repro_tasks_preliminary.json"), source.index("targeted_fact_backfill.md"))
        self.assertLess(source.index("targeted_fact_backfill.md"), source.index("previous_plan="))
        self.assertNotIn("finalize_repro_tasks.md", source)

    def test_isolated_task_reporters_and_final_editor_follow_task_writers(self) -> None:
        facade_source = inspect.getsource(ReviewPipeline.run)
        execution_source = inspect.getsource(run_execution_flow)
        report_source = inspect.getsource(run_report_flow)
        self.assertLess(
            execution_source.index("run_codex_task_writer_workflow("),
            execution_source.index("task_review_callback=_review_one_task"),
        )
        self.assertLess(
            facade_source.index("run_execution_flow("),
            facade_source.index("run_report_flow("),
        )
        self.assertIn('if not runtime_result.get("passed")', execution_source)
        self.assertNotIn("render_review_markdown(", execution_source + report_source)
        self.assertIn("run_codex_task_reporter_workflow(", execution_source)
        self.assertNotIn("revision_target", execution_source + report_source)
        self.assertIn("apply_verified_result(", report_source)
        self.assertNotIn('not verification_result.get("all_terminal")', report_source)
        self.assertIn("report_runner = run_codex_report_editor_workflow", report_source)
        self.assertIn("writer_session_count", report_source)
        self.assertIn("run_supervised_report_editor(", report_source)
        self.assertIn("run_supervised_report_editor", report_source)
        self.assertNotIn("allow_fallback=True", report_source)
        self.assertIn("report_editor_invocations", report_source)
        self.assertNotIn("Report editor failed.", report_source)
        self.assertNotIn("04b_reproducibility_verdict_fallback.json", report_source)

    def test_report_editor_exception_is_recorded_without_stopping_pipeline(self) -> None:
        with TemporaryDirectory() as temp_dir:
            result, output_dir = _run_minimal_full_pipeline(
                Path(temp_dir),
                report_editor_error=RuntimeError("editor boom"),
            )

            self.assertIsNone(result.reproducibility_verdict)
            risk_report = json.loads(
                (output_dir / "risk_report.json").read_text(encoding="utf-8")
            )
            finding = next(
                item
                for item in risk_report["findings"]
                if item.get("type") == "report_editor_failed"
            )
            self.assertIn("scientific task results were preserved", finding["message"])
            self.assertIn("RuntimeError: editor boom", finding["error"])
            generated = json.loads(
                (output_dir / "generated_files.json").read_text(encoding="utf-8")
            )
            self.assertFalse(generated["report_editor"]["ok"])
            self.assertEqual(
                generated["report_editor"]["codex_status"]["error_kind"],
                "report_editor_exception",
            )

    def test_editor_delivery_does_not_invent_a_python_scientific_verdict(self) -> None:
        with TemporaryDirectory() as temp_dir:
            editor_result = {
                "ok": True,
                "retryable": False,
                "cached": False,
                "mode": "isolated_report_editor",
                "completion_mode": "passed",
                "degraded_report_generation": False,
                "codex_status": {"ok": True, "role": "report_editor"},
                "result_review_result": {"enabled": True, "passed": True},
            }
            result, output_dir = _run_minimal_full_pipeline(
                Path(temp_dir),
                report_editor_result=editor_result,
            )

            fallback_path = (
                output_dir / "audit" / "04b_reproducibility_verdict_fallback.json"
            )
            self.assertFalse(fallback_path.exists())
            self.assertIsNone(result.reproducibility_verdict)
            risk_report = json.loads(
                (output_dir / "risk_report.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                risk_report["reproducibility_verdict"],
                result.reproducibility_verdict,
            )
    def test_optional_foundation_is_skipped_for_all_architecture_versions(self) -> None:
        class WriterReached(BaseException):
            pass

        def exercise(*, schema_version: str, architecture_contract: str, resume: bool) -> None:
            with TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                paper_path = root / "paper.md"
                paper_path.write_text(
                    "# Results\nFig. 4 reports bit error rate versus SNR.",
                    encoding="utf-8",
                )
                output_dir = root / "case"
                if resume:
                    output_dir.mkdir()
                    write_json(
                        output_dir / "workflow.json",
                        {
                            "workflow_version": "2",
                            "architecture_contract": architecture_contract,
                        },
                    )

                initial = fact_doc(
                    fact("figure_claim", "Fig. 4"),
                    fact("metric", "bit_error_rate"),
                )
                preliminary = task_doc(task("reproduce_fig_4", "Fig. 4"))
                preliminary["backfill_handoff"] = {
                    "ready_for_writer": True,
                    "blocking_request_ids": [],
                    "reason": "fixture has no missing facts",
                }

                def fake_analysis_stage(**kwargs):
                    architecture = architecture_doc(output_dir, preliminary)
                    architecture["schema_version"] = schema_version
                    document = (understanding_doc(initial) if kwargs["stage_label"] == "01_understand_paper"
                                else {"tasks": preliminary, "scientific_architecture": architecture})
                    document = json.loads(json.dumps(document))
                    write_json(kwargs["output_path"], document)
                    return document


                def fake_experiment_index(**kwargs):
                    document = {
                        "experiments": [
                            {
                                "task_id": "reproduce_fig_4",
                                "experiment_id": "exp_reproduce_fig_4",
                            }
                        ]
                    }
                    write_json(kwargs["output_dir"] / "experiment_index.json", document)
                    return document


                pipeline = ReviewPipeline()
                mineru_result = {
                    "ok": True,
                    "cached": False,
                    "fallback_used": False,
                    "duration_s": 0.0,
                    "figure_count": 0,
                    "figure_index": {"figures": [], "unmatched_visuals": []},
                }
                with (
                    patch.object(pipeline, "_render_paper_images", return_value=[]),
                    patch(
                        "geng_agent.pipeline.run_mineru_layout_stage",
                        return_value=mineru_result,
                    ),
                    patch.object(
                        pipeline,
                        "_load_or_create_analysis_stage_json",
                        side_effect=fake_analysis_stage,
                    ),
                    patch.object(
                        pipeline,
                        "_load_or_create_experiment_index",
                        side_effect=fake_experiment_index,
                    ),
                    patch(
                        "geng_agent.case_runtime.ensure_case_runtime",
                        return_value=case_runtime_fixture(output_dir, "0" * 64),
                    ),
                    patch(
                        "geng_agent.agentic_foundation.run_codex_foundation_writer_workflow",
                        side_effect=ValueError("foundation boom"),
                    ) as foundation_writer,
                    patch(
                        "geng_agent.agentic_task_writers.run_codex_task_writer_workflow",
                        side_effect=WriterReached("task writer reached"),
                    ) as task_writer,
                ):
                    with self.assertRaises(WriterReached):
                        pipeline.run(
                            paper_path, output_dir, resume=resume, analysis_only=False
                        )
                    task_writer.assert_called_once()
                foundation_writer.assert_not_called()
                self.assertFalse((output_dir / "audit" / "03b_foundation_fallback.json").exists())

        exercise(
            schema_version="1.1",
            architecture_contract="scientific_architecture/1.1",
            resume=False,
        )
        exercise(
            schema_version="1.0",
            architecture_contract="scientific_architecture/1.0",
            resume=True,
        )

    def test_planner_revision_is_preserved_without_host_merging_old_tasks(self) -> None:
        from types import SimpleNamespace
        from geng_agent.consolidated_analysis import load_experiment_plan
        from geng_agent.execution_plan import compile_execution_plan

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            base_tasks = task_doc(
                task("task_a", "Claim A"),
                task("task_b", "Claim B"),
            )
            base_tasks["execution_relationships"] = [
                {
                    "relationship_id": "existing_ab",
                    "kind": "same_run_outputs",
                    "strength": "strong",
                    "task_ids": ["task_a", "task_b"],
                    "producer_task_id": None,
                    "consumer_task_ids": [],
                    "artifact_ids": [],
                }
            ]
            final_candidate = task_doc(
                task("task_a", "Claim A refined"),
                task("task_c", "Claim C"),
            )
            final_candidate["execution_relationships"] = [
                {
                    "relationship_id": "new_ac",
                    "kind": "same_run_outputs",
                    "strength": "strong",
                    "task_ids": ["task_a", "task_c"],
                    "producer_task_id": None,
                    "consumer_task_ids": [],
                    "artifact_ids": [],
                }
            ]

            candidate = {"tasks": final_candidate, "scientific_architecture": architecture_doc(root, final_candidate)}
            pipeline = ReviewPipeline()
            context = SimpleNamespace(output_dir=root, audit_dir=root / "audit", options=SimpleNamespace(
                json_repair_attempts=0, resume=False, tasks_timeout=1, analysis_backend="codex", analysis_fallback=False))
            with patch.object(pipeline, "_load_or_create_analysis_stage_json", return_value=candidate) as planner:
                result = load_experiment_plan(pipeline, context, facts=fact_doc(), paper_thesis=thesis_doc(),
                    paper={}, paper_context="paper", paper_images=[], figure_index={}, host_capabilities={},
                    previous_plan={"tasks": base_tasks, "scientific_architecture": None}, round_index=1)
            final_tasks = result["tasks"]
            plan = compile_execution_plan(final_tasks)
            self.assertEqual(result, candidate)
            self.assertEqual(planner.call_args.kwargs["cache_inputs"]["previous_plan"]["tasks"], base_tasks)
            self.assertEqual([item["task_id"] for item in final_tasks["repro_tasks"]], ["task_a", "task_c"])
            self.assertEqual([item["relationship_id"] for item in final_tasks["execution_relationships"]], ["new_ac"])
            self.assertEqual(final_tasks["repro_tasks"][0]["figure_or_claim"], "Claim A refined")
            self.assertEqual(base_tasks["repro_tasks"][0]["figure_or_claim"], "Claim A")
            self.assertEqual(plan["logical_task_count"], 2)
            self.assertEqual(plan["execution_unit_count"], 1)

    def test_optional_foundation_environment_failure_is_never_entered(self) -> None:
        class WriterReached(BaseException):
            pass

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output_dir = root / "case"
            foundation = Mock(
                side_effect=EnvironmentResolutionError(
                    "optional_foundation_dependency",
                    "optional Foundation dependency unavailable",
                )
            )
            task_writer = Mock(side_effect=WriterReached("writer reached"))

            with self.assertRaises(WriterReached):
                _run_to_task_writer_boundary(
                    root,
                    resume=False,
                    environment_mock=Mock(
                        return_value=case_runtime_fixture(output_dir, "0" * 64)
                    ),
                    foundation_mock=foundation,
                    task_writer_mock=task_writer,
                )

            self.assertFalse((output_dir / "audit" / "03b_foundation_fallback.json").exists())

        foundation.assert_not_called()
        task_writer.assert_called_once()

    def test_material_weak_foundation_environment_failure_still_stops(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output_dir = root / "case"
            tasks = task_doc(
                task("task_a", "Claim A"),
                task("task_b", "Claim B"),
            )
            tasks["execution_relationships"] = [
                {
                    "relationship_id": "shared_definition_ab",
                    "kind": "shared_definition",
                    "strength": "weak",
                    "task_ids": ["task_a", "task_b"],
                    "producer_task_id": None,
                    "consumer_task_ids": [],
                    "artifact_ids": ["shared_channel_definition"],
                }
            ]
            foundation = Mock(
                side_effect=EnvironmentResolutionError(
                    "material_foundation_dependency",
                    "material Foundation dependency unavailable",
                )
            )
            task_writer = Mock()

            with self.assertRaises(StageBlocked):
                _run_to_task_writer_boundary(
                    root,
                    resume=False,
                    environment_mock=Mock(
                        return_value=case_runtime_fixture(output_dir, "0" * 64)
                    ),
                    foundation_mock=foundation,
                    task_writer_mock=task_writer,
                    tasks_document=tasks,
                )

            audit = json.loads(
                (output_dir / "audit" / "execution_tool_failures.json").read_text(
                    encoding="utf-8"
                )
            )

        foundation.assert_called_once()
        task_writer.assert_not_called()
        self.assertIn("material Foundation dependency unavailable", audit["foundation"]["error"])

    def test_initial_case_environment_failure_stops_before_writers(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output_dir = root / "case"
            environment = Mock(
                side_effect=EnvironmentResolutionError(
                    "trusted_source_unavailable",
                    "trusted index unavailable",
                    report={"ready": False, "status": "trusted_source_unavailable"},
                )
            )
            foundation = Mock()
            task_writer = Mock()

            with self.assertRaises(StageBlocked):
                _run_to_task_writer_boundary(
                    root,
                    resume=False,
                    environment_mock=environment,
                    foundation_mock=foundation,
                    task_writer_mock=task_writer,
                )

            audit = json.loads(
                (output_dir / "audit" / "03a_environment_blocked.json").read_text(
                    encoding="utf-8"
                )
            )

        foundation.assert_not_called()
        task_writer.assert_not_called()
        self.assertEqual(audit["decision"], "awaiting_supervisor")
        self.assertFalse(audit["pipeline_can_continue"])
        self.assertEqual(audit["node_id"], "environment")
        self.assertEqual(audit["category"], "trusted_source_unavailable")

    def test_environment_extension_resumes_writers_without_building_optional_foundation(self) -> None:
        class WriterReached(BaseException):
            pass

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output_dir = root / "case"
            runtime_0 = case_runtime_fixture(output_dir, "0" * 64)
            runtime_1 = case_runtime_fixture(output_dir, "1" * 64)
            pending = EnvironmentRequestRequired(
                [
                    RequirementRequest(
                        "scipy>=1.11",
                        requested_by="task_writer:reproduce_fig_4",
                        reason="required numerical capability",
                    )
                ],
                source="task_writers",
            )
            environment = Mock(side_effect=[runtime_0, runtime_1])
            foundation = Mock(return_value={"manifest": {"files": []}})
            task_writer = Mock(side_effect=[pending, WriterReached("second writer round")])

            with self.assertRaises(WriterReached):
                _run_to_task_writer_boundary(
                    root,
                    resume=True,
                    environment_mock=environment,
                    foundation_mock=foundation,
                    task_writer_mock=task_writer,
                )

            extension = json.loads(
                (output_dir / "audit" / "03a_environment_extensions.json").read_text(
                    encoding="utf-8"
                )
            )

        foundation.assert_not_called()
        self.assertEqual([call.kwargs["resume"] for call in task_writer.call_args_list], [True, True])
        self.assertEqual(
            [call.kwargs["case_runtime"] for call in task_writer.call_args_list],
            [runtime_0, runtime_1],
        )
        self.assertEqual(environment.call_count, 2)
        second_resolution = environment.call_args_list[1].kwargs
        self.assertTrue(second_resolution["resume"])
        self.assertEqual(second_resolution["extra_requirements"][0].requirement, "scipy>=1.11")
        self.assertEqual(extension["extension_count"], 1)
        self.assertEqual(extension["latest_source"], "task_writers")
        self.assertEqual(extension["environment_lock_hash"], "1" * 64)

    def test_shared_runtime_change_refreshes_and_resumes_writers_automatically(self) -> None:
        class WriterReached(BaseException):
            pass

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output_dir = root / "case"
            environment = Mock(side_effect=[
                case_runtime_fixture(output_dir, "0" * 64),
                case_runtime_fixture(output_dir, "1" * 64),
            ])
            foundation = Mock()
            writer = Mock(side_effect=[
                EnvironmentRequestRequired([], source="shared_runtime_refresh"),
                WriterReached("writer resumed"),
            ])
            with self.assertRaises(WriterReached):
                _run_to_task_writer_boundary(
                    root, resume=True, environment_mock=environment,
                    foundation_mock=foundation, task_writer_mock=writer,
                )
            extension = json.loads((output_dir / "audit" / "03a_environment_extensions.json").read_text(
                encoding="utf-8"))

        foundation.assert_not_called()
        self.assertEqual(environment.call_count, 2)
        self.assertEqual(writer.call_count, 2)
        self.assertEqual(environment.call_args_list[1].kwargs["extra_requirements"], [])
        self.assertEqual(writer.call_args_list[1].kwargs["case_runtime"].environment_hash, "1" * 64)
        self.assertEqual(extension["latest_source"], "shared_runtime_refresh")

    def test_analysis_agent_width_is_not_a_pipeline_option(self) -> None:
        self.assertNotIn("analysis_agent_width", inspect.signature(ReviewPipeline.run).parameters)

    def test_report_delivery_inspects_editor_authored_word_reports(self) -> None:
        with TemporaryDirectory() as temp_dir:
            from docx import Document
            root = Path(temp_dir)
            for name in ("review.md", "reproduction_report.md", "result_review.md"):
                (root / name).write_text("## task_1\n\n报告正文。\n", encoding="utf-8")
            for name in ("review.docx", "reproduction_report.docx", "result_review.docx"):
                document = Document()
                document.add_paragraph("智能体编排的报告正文")
                document.save(root / name)
            before = (root / "result_review.docx").read_bytes()

            result = ReviewPipeline()._inspect_editor_word_reports(
                output_dir=root,
                result_review_result={"passed": True},
            )

            self.assertTrue(result["review_docx"]["passed"])
            self.assertTrue(result["reproduction_report_docx"]["passed"])
            self.assertTrue(result["result_review_docx"]["passed"])
            for name in ("review.docx", "reproduction_report.docx", "result_review.docx"):
                self.assertTrue((root / name).exists())
            self.assertEqual((root / "result_review.docx").read_bytes(), before)

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
            preliminary["backfill_handoff"] = {
                "ready_for_writer": False,
                "blocking_request_ids": ["fig4_normalization"],
                "reason": "power normalization changes the experiment",
                "inferred": False,
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
            final_acceptance = json.loads(json.dumps(finalized))
            final_acceptance["repro_tasks"][0]["required_facts"].append(
                {
                    "type": "simulation_parameter",
                    "name": "Fig. 4 power normalization",
                }
            )

            def fake_analysis_stage(**kwargs):
                label = kwargs["stage_label"]
                calls.append(label)
                if label == "02f_design_scientific_architecture":
                    document = architecture_doc(kwargs["output_dir"])
                    write_json(kwargs["output_path"], document)
                    return document
                documents = {
                    "01_understand_paper": understanding_doc(initial),
                    "02a_plan_experiments": {"tasks": preliminary, "scientific_architecture": None},
                    "02b_round_01_targeted_fact_backfill": backfill,
                    "02c_round_01_revise_experiment_plan": {"tasks": final_acceptance, "scientific_architecture": architecture_doc(output_dir, final_acceptance)},
                }
                document = documents[label]
                write_json(kwargs["output_path"], document)
                return document


            pipeline = ReviewPipeline()
            with (
                patch.object(pipeline, "_load_or_create_analysis_stage_json", side_effect=fake_analysis_stage),
            ):
                result = pipeline.run(paper_path, output_dir, resume=False, analysis_only=True)

            self.assertEqual(
                calls,
                [
                    "01_understand_paper",
                    "02a_plan_experiments",
                    "02b_round_01_targeted_fact_backfill",
                    "02c_round_01_revise_experiment_plan",
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
            self.assertEqual(analysis_result["analysis_stage_invocations"], 4)
            self.assertFalse((output_dir / "repro_project").exists())
            self.assertFalse((output_dir / "runtime_result.json").exists())
            host_capabilities = json.loads(
                (output_dir / "audit" / "02f_architecture_host_capabilities.json").read_text(encoding="utf-8")
            )
            self.assertEqual(host_capabilities["evidence_class"], "host_capability_only_not_paper_evidence")
            self.assertIn("installed_reproduction_packages", host_capabilities)

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
            preliminary["backfill_handoff"] = {
                "ready_for_writer": False,
                "blocking_request_ids": ["fig4_setup"],
                "reason": "simulation setup changes the implementation",
                "inferred": False,
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
                "01_understand_paper": understanding_doc(initial),
                "02a_plan_experiments": {"tasks": preliminary, "scientific_architecture": None},
                "02b_round_01_targeted_fact_backfill": round_1_backfill,
                "02c_round_01_revise_experiment_plan": {"tasks": round_1_task, "scientific_architecture": None},
                "02b_round_02_targeted_fact_backfill": round_2_backfill,
                "02c_round_02_revise_experiment_plan": {"tasks": round_2_task, "scientific_architecture": architecture_doc(output_dir, round_2_task)},
            }

            def fake_analysis_stage(**kwargs):
                label = kwargs["stage_label"]
                calls.append(label)
                document = documents[label]
                write_json(kwargs["output_path"], document)
                return document


            pipeline = ReviewPipeline()
            with (
                patch.object(pipeline, "_load_or_create_analysis_stage_json", side_effect=fake_analysis_stage),
            ):
                pipeline.run(paper_path, output_dir, resume=False, analysis_only=True)

            self.assertEqual(len(calls), 6)
            summary = json.loads(
                (output_dir / "audit" / "02b_targeted_fact_backfill_summary.json").read_text(encoding="utf-8")
            )
            self.assertEqual(summary["round_count"], 2)
            self.assertEqual(summary["terminal_unresolved_count"], 1)
            self.assertEqual(summary["stop_reason"], "planner_ready_handoff")
            analysis_result = json.loads(
                (output_dir / "analysis_result.json").read_text(encoding="utf-8")
            )
            self.assertEqual(analysis_result["analysis_stage_invocations"], 6)
            ledger = json.loads((output_dir / "audit" / "02b_backfill_search_ledger.json").read_text(encoding="utf-8"))
            latest = {item["field_id"]: item for item in ledger["latest"]}
            self.assertEqual(latest["normalization"]["status"], "resolved_explicit")
            self.assertEqual(latest["normalization"]["round"], 1)
            self.assertEqual(latest["trial_count"]["status"], "not_found_in_paper")
            self.assertEqual(latest["trial_count"]["round"], 2)
            before_planner = json.loads((output_dir / "audit" / "02b_round_02_facts_before_planner.json").read_text(encoding="utf-8"))
            self.assertIn(setup_fact, before_planner["engineering_facts"])
            self.assertTrue(all(item in before_planner["engineering_facts"] for item in initial["engineering_facts"]))
            self.assertFalse((output_dir / "audit" / "02c_terminal_gap_diagnostics.json").exists())
            final_tasks = json.loads((output_dir / "repro_tasks.json").read_text(encoding="utf-8"))
            self.assertTrue(
                final_tasks["_meta"]["scientific_acceptance_finalization"]["structure_is_advisory"]
            )


if __name__ == "__main__":
    unittest.main()
