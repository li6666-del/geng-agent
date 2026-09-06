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


def architecture_doc(output_dir: Path) -> dict:
    index = json.loads((output_dir / "experiment_index.json").read_text(encoding="utf-8"))
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
        if kwargs["stage_label"] == "01_extract_engineering_facts":
            document = initial
        elif (
            kwargs["stage_label"] == "02d_finalize_scientific_acceptance"
            and isinstance(final_tasks_candidate, dict)
        ):
            document = final_tasks_candidate
        else:
            document = preliminary
        document = json.loads(json.dumps(document))
        write_json(kwargs["output_path"], document)
        return document

    def fake_thesis(**kwargs):
        document = {
            "central_claim": "BER decreases with SNR",
            "proposed_method": "test method",
            "mechanism": "higher SNR improves decoding",
            "comparisons": [],
            "headline_shape": "decreasing",
            "caveats": [],
        }
        write_json(kwargs["output_dir"] / "paper_thesis.json", document)
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

    def fake_architecture(**kwargs):
        document = architecture_doc(kwargs["output_dir"])
        write_json(kwargs["output_dir"] / "scientific_architecture.json", document)
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
        patch.object(pipeline, "_load_or_create_paper_thesis", side_effect=fake_thesis),
        patch.object(
            pipeline,
            "_load_or_create_experiment_index",
            side_effect=fake_experiment_index,
        ),
        patch.object(
            pipeline,
            "_load_or_create_scientific_architecture",
            side_effect=fake_architecture,
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
    verdict_candidate: dict,
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
        document = initial if kwargs["stage_label"] == "01_extract_engineering_facts" else preliminary
        document = json.loads(json.dumps(document))
        write_json(kwargs["output_path"], document)
        return document

    def fake_thesis(**kwargs):
        document = {
            "central_claim": "BER decreases with SNR",
            "proposed_method": "test method",
            "mechanism": "higher SNR improves decoding",
            "comparisons": [],
            "headline_shape": "decreasing",
            "caveats": [],
        }
        write_json(kwargs["output_dir"] / "paper_thesis.json", document)
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
        patch.object(pipeline, "_load_or_create_paper_thesis", side_effect=fake_thesis),
        patch.object(
            pipeline,
            "_load_or_create_experiment_index",
            side_effect=fake_experiment_index,
        ),
        patch.object(pipeline, "_load_or_create_scientific_architecture", return_value=None),
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
        patch("geng_agent.pipeline.derive_reproducibility_verdict", return_value=verdict_candidate),
        patch.object(
            pipeline,
            "_generate_docx_reports",
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
    def test_preliminary_task_cache_survives_host_deduplication(self) -> None:
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
                initial
                if kwargs["stage_label"] == "01_extract_engineering_facts"
                else preliminary
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
            self.assertNotIn("untrusted", persisted["_meta"])
            self.assertEqual(
                persisted["_meta"]["semantic_merge"]["merge_version"],
                4,
            )
            self.assertEqual(validate_stage("repro_tasks", persisted), [])
            self.assertNotIn(
                "cache",
                downstream_tasks.get("_meta", {}),
            )
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

    def test_optional_architecture_candidate_does_not_repair_execution_gaps(self) -> None:
        with TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "case"
            audit_dir = output_dir / "audit"
            audit_dir.mkdir(parents=True)
            experiment_index = {
                "experiments": [
                    {
                        "task_id": "reproduce_fig_4",
                        "experiment_id": "exp_reproduce_fig_4",
                    }
                ]
            }
            write_json(output_dir / "experiment_index.json", experiment_index)
            advisory_candidate = architecture_doc(output_dir)
            advisory_candidate["components"][0]["basis"] = {
                "status": "paper_explicit",
                "evidence_facts": [
                    {"type": "channel_model", "name": "missing paper evidence"}
                ],
                "assumption_refs": [],
                "note": "This evidence reference is intentionally unresolved.",
            }
            blocked_candidate = json.loads(json.dumps(advisory_candidate))
            blocked_candidate["components"][0]["module"] = "../system.py"
            captured: dict[str, list] = {}

            def fake_stage(**kwargs):
                validate_candidate = kwargs["candidate_extra_validation"]
                captured["advisory"] = validate_candidate(advisory_candidate)
                captured["blocked"] = validate_candidate(blocked_candidate)
                write_json(kwargs["output_path"], advisory_candidate)
                return advisory_candidate

            pipeline = ReviewPipeline()
            with (
                patch(
                    "geng_agent.preflight.architecture_capability_inventory",
                    return_value={
                        "evidence_class": "host_capability_only_not_paper_evidence",
                        "installed_reproduction_packages": [],
                    },
                ),
                patch.object(
                    pipeline,
                    "_load_or_create_analysis_stage_json",
                    side_effect=fake_stage,
                ),
            ):
                pipeline._load_or_create_scientific_architecture(
                    output_dir=output_dir,
                    audit_dir=audit_dir,
                    facts=fact_doc(fact("figure_claim", "Fig. 4")),
                    tasks=task_doc(task("reproduce_fig_4", "Fig. 4")),
                    experiment_index=experiment_index,
                    paper_thesis=None,
                    paper_context="paper context",
                    paper_images=[],
                    resume=False,
                    max_attempts=1,
                    analysis_backend=CODEX_ANALYSIS_BACKEND,
                )

            self.assertEqual(captured["advisory"], [])
            self.assertEqual(captured["blocked"], [])
            audit = json.loads(
                (audit_dir / "02f_scientific_architecture_normalization.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertTrue(audit["ok"])
            self.assertEqual(audit["execution_blocker_count"], 0)
            self.assertTrue(
                any(
                    issue["path"] == "$.components[0].basis.evidence_facts[0]"
                    for issue in audit["groups"]["cross_document_diagnostics"]
                )
            )

    def test_strong_only_malformed_binding_is_warned_then_downgraded(self) -> None:
        with TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "case"
            audit_dir = output_dir / "audit"
            audit_dir.mkdir(parents=True)
            tasks = task_doc(
                task("task_a", "Claim A"),
                task("task_b", "Claim B"),
            )
            tasks["execution_relationships"] = [
                {
                    "relationship_id": "strong_ab",
                    "kind": "same_run_outputs",
                    "strength": "strong",
                    "task_ids": ["task_a", "task_b"],
                    "producer_task_id": None,
                    "consumer_task_ids": [],
                    "artifact_ids": [],
                }
            ]
            experiment_index = {
                "experiments": [
                    {"task_id": "task_a", "experiment_id": "exp_task_a"},
                    {"task_id": "task_b", "experiment_id": "exp_task_b"},
                ]
            }
            write_json(output_dir / "experiment_index.json", experiment_index)
            malformed = architecture_doc(output_dir)
            malformed["bindings"] = [malformed["bindings"][0]]
            captured: dict[str, list] = {}

            def fake_stage(**kwargs):
                captured["candidate_issues"] = kwargs[
                    "candidate_extra_validation"
                ](malformed)
                write_json(kwargs["output_path"], malformed)
                return malformed

            pipeline = ReviewPipeline()
            with (
                patch(
                    "geng_agent.preflight.architecture_capability_inventory",
                    return_value={
                        "evidence_class": "host_capability_only_not_paper_evidence",
                        "installed_reproduction_packages": [],
                    },
                ),
                patch.object(
                    pipeline,
                    "_load_or_create_analysis_stage_json",
                    side_effect=fake_stage,
                ),
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "continue with task-local Writers without Foundation",
                ):
                    pipeline._load_or_create_scientific_architecture(
                        output_dir=output_dir,
                        audit_dir=audit_dir,
                        facts=fact_doc(fact("figure_claim", "Fig. 4")),
                        tasks=tasks,
                        experiment_index=experiment_index,
                        paper_thesis=None,
                        paper_context="paper context",
                        paper_images=[],
                        resume=False,
                        max_attempts=1,
                        analysis_backend=CODEX_ANALYSIS_BACKEND,
                    )

            audit = json.loads(
                (audit_dir / "02f_scientific_architecture_normalization.json").read_text(
                    encoding="utf-8"
                )
            )

        self.assertEqual(captured["candidate_issues"], [])
        self.assertTrue(audit["ok"])
        self.assertEqual(audit["execution_blocker_count"], 0)
        self.assertTrue(
            any(
                issue["path"] == "$.bindings"
                for issue in audit["groups"]["optional_execution_gaps"]
            )
        )

    def test_material_weak_architecture_keeps_execution_gap_repair_blocker(self) -> None:
        with TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "case"
            audit_dir = output_dir / "audit"
            audit_dir.mkdir(parents=True)
            tasks = task_doc(
                task("task_a", "Claim A"),
                task("task_b", "Claim B"),
            )
            tasks["execution_relationships"] = [
                {
                    "relationship_id": "weak_ab",
                    "kind": "shared_definition",
                    "strength": "weak",
                    "task_ids": ["task_a", "task_b"],
                    "producer_task_id": None,
                    "consumer_task_ids": [],
                    "artifact_ids": ["shared_definition"],
                }
            ]
            experiment_index = {
                "experiments": [
                    {"task_id": "task_a", "experiment_id": "exp_task_a"},
                    {"task_id": "task_b", "experiment_id": "exp_task_b"},
                ]
            }
            write_json(output_dir / "experiment_index.json", experiment_index)
            valid = architecture_doc(output_dir)
            invalid = json.loads(json.dumps(valid))
            invalid["components"][0]["module"] = "../shared.py"
            captured: dict[str, list] = {}

            def fake_stage(**kwargs):
                captured["candidate_issues"] = kwargs[
                    "candidate_extra_validation"
                ](invalid)
                write_json(kwargs["output_path"], valid)
                return valid

            pipeline = ReviewPipeline()
            with (
                patch(
                    "geng_agent.preflight.architecture_capability_inventory",
                    return_value={
                        "evidence_class": "host_capability_only_not_paper_evidence",
                        "installed_reproduction_packages": [],
                    },
                ),
                patch.object(
                    pipeline,
                    "_load_or_create_analysis_stage_json",
                    side_effect=fake_stage,
                ),
            ):
                pipeline._load_or_create_scientific_architecture(
                    output_dir=output_dir,
                    audit_dir=audit_dir,
                    facts=fact_doc(fact("figure_claim", "Fig. 4")),
                    tasks=tasks,
                    experiment_index=experiment_index,
                    paper_thesis=None,
                    paper_context="paper context",
                    paper_images=[],
                    resume=False,
                    max_attempts=1,
                    analysis_backend=CODEX_ANALYSIS_BACKEND,
                )

        self.assertTrue(
            any(
                issue.path == "$.components[0].module"
                for issue in captured["candidate_issues"]
            )
        )

    def test_resume_preserves_generation_host_inventory_and_records_current_host_separately(self) -> None:
        with TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "case"
            audit_dir = output_dir / "audit"
            audit_dir.mkdir(parents=True)
            write_json(
                output_dir / "workflow.json",
                {
                    "workflow_version": "2",
                    "architecture_contract": "scientific_architecture/1.1",
                },
            )
            experiment_index = {
                "experiments": [
                    {
                        "task_id": "reproduce_fig_4",
                        "experiment_id": "exp_reproduce_fig_4",
                    }
                ]
            }
            write_json(output_dir / "experiment_index.json", experiment_index)
            architecture = architecture_doc(output_dir)
            write_json(output_dir / "scientific_architecture.json", architecture)
            write_json(
                audit_dir / "02f_architecture_host_capabilities.json",
                {"marker": "generation"},
            )
            current_inventory = {
                "marker": "current",
                "python_runtime_registry": [],
                "external_runtime_registry": [],
                "accelerators": {"devices": []},
            }
            pipeline = ReviewPipeline()

            with (
                patch(
                    "geng_agent.preflight.architecture_capability_inventory",
                    return_value=current_inventory,
                ),
                patch.object(
                    pipeline,
                    "_load_or_create_analysis_stage_json",
                    return_value=architecture,
                ),
            ):
                pipeline._load_or_create_scientific_architecture(
                    output_dir=output_dir,
                    audit_dir=audit_dir,
                    facts=fact_doc(fact("figure_claim", "Fig. 4")),
                    tasks=task_doc(task("reproduce_fig_4", "Fig. 4")),
                    experiment_index=experiment_index,
                    paper_thesis=None,
                    paper_context="paper context",
                    paper_images=[],
                    resume=True,
                    max_attempts=1,
                    analysis_backend=CODEX_ANALYSIS_BACKEND,
                )

            generation = json.loads(
                (audit_dir / "02f_architecture_host_capabilities.json").read_text(
                    encoding="utf-8"
                )
            )
            current = json.loads(
                (audit_dir / "02f_architecture_host_capabilities_current.json").read_text(
                    encoding="utf-8"
                )
            )
            gaps = json.loads(
                (audit_dir / "02f_architecture_execution_capability_gaps.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(generation["marker"], "generation")
            self.assertEqual(current["marker"], "current")
            self.assertTrue(gaps["ok"])
            self.assertEqual(gaps["gap_count"], 0)

    def test_resume_salvages_a_normalizable_failed_candidate_before_cleanup(self) -> None:
        with TemporaryDirectory() as temp:
            output = Path(temp) / "case"
            audit = output / "audit"
            audit.mkdir(parents=True)
            candidate = fact_doc(fact("channel_model", "AWGN"))
            cache_inputs = {"paper_source_sha256": "a" * 64}
            candidate["_meta"] = {
                "cache": build_stage_cache_metadata(
                    stage_label="probe",
                    schema_stage="engineering_facts",
                    prompt="must not run",
                    policy_version=SCIENTIFIC_POLICY_ID,
                    inputs=cache_inputs,
                )
            }
            candidate_path = audit / "normalized_probe_attempt_1.json"
            write_json(candidate_path, candidate)

            result = ReviewPipeline()._load_or_create_stage_json(
                output_path=output / "engineering_facts.json",
                output_dir=output,
                audit_dir=audit,
                prompt="must not run",
                stage_label="probe",
                cleanup_stage="facts",
                schema_stage="engineering_facts",
                max_attempts=1,
                resume=True,
                candidate_normalizer=lambda value: value,
                salvage_failed_candidates=True,
                cache_inputs=cache_inputs,
                backend=CODEX_ANALYSIS_BACKEND,
            )

            self.assertEqual(result["engineering_facts"][0]["name"], "AWGN")
            self.assertEqual(result["_meta"]["analysis_resume_source"], candidate_path.name)
            self.assertTrue((audit / "resume_probe.json").is_file())
            self.assertTrue((output / "engineering_facts.json").is_file())

    def test_resume_rejects_salvage_candidate_from_different_scientific_inputs(self) -> None:
        with TemporaryDirectory() as temp:
            output = Path(temp) / "case"
            audit = output / "audit"
            audit.mkdir(parents=True)
            old_inputs = {"paper_source_sha256": "a" * 64}
            current_inputs = {"paper_source_sha256": "b" * 64}
            stale = fact_doc(fact("channel_model", "stale AWGN"))
            stale["_meta"] = {
                "cache": build_stage_cache_metadata(
                    stage_label="probe",
                    schema_stage="engineering_facts",
                    prompt="same semantic stage",
                    policy_version=SCIENTIFIC_POLICY_ID,
                    inputs=old_inputs,
                )
            }
            write_json(audit / "normalized_probe_attempt_1.json", stale)
            fresh = fact_doc(fact("channel_model", "fresh Rayleigh"))

            with patch(
                "geng_agent.pipeline.run_codex_json_stage",
                return_value=fresh,
            ) as run_stage:
                result = ReviewPipeline()._load_or_create_stage_json(
                    output_path=output / "engineering_facts.json",
                    output_dir=output,
                    audit_dir=audit,
                    prompt="same semantic stage",
                    stage_label="probe",
                    cleanup_stage="facts",
                    schema_stage="engineering_facts",
                    max_attempts=1,
                    resume=True,
                    candidate_normalizer=lambda value: value,
                    salvage_failed_candidates=True,
                    cache_inputs=current_inputs,
                    backend=CODEX_ANALYSIS_BACKEND,
                )

            run_stage.assert_called_once()
            self.assertEqual(result["engineering_facts"][0]["name"], "fresh Rayleigh")
            self.assertEqual(len(list(audit.glob("resume_rejected_probe_*.json"))), 1)

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
        self.assertIn("paper_thesis = pipeline._load_or_create_paper_thesis(", analysis_source)
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
        self.assertLess(source.index("targeted_fact_backfill.md"), source.index("finalize_repro_tasks.md"))

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
        self.assertIn('not verification_result.get("all_terminal")', report_source)
        self.assertIn("run_codex_report_editor_workflow(", report_source)
        self.assertIn("writer_session_count", report_source)
        self.assertIn('report_editor_result.get(\n        "retryable"', report_source)
        self.assertIn("repair_context=report_editor_result", report_source)
        self.assertIn("allow_fallback=True", report_source)
        self.assertIn("report_editor_invocations += int(", report_source)
        self.assertNotIn("Report editor failed.", report_source)
        self.assertIn("04b_reproducibility_verdict_fallback.json", report_source)

    def test_report_editor_exception_is_recorded_without_stopping_pipeline(self) -> None:
        with TemporaryDirectory() as temp_dir:
            valid_verdict = {
                "verdict": "inconclusive",
                "confidence": "low",
                "reasons": ["fixture verdict"],
                "recommended_action": "inspect task-level evidence",
            }
            result, output_dir = _run_minimal_full_pipeline(
                Path(temp_dir),
                report_editor_error=RuntimeError("editor boom"),
                verdict_candidate=valid_verdict,
            )

            self.assertEqual(result.reproducibility_verdict, valid_verdict)
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

    def test_invalid_verdict_is_audited_and_replaced_by_valid_inconclusive(self) -> None:
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
            invalid_candidate = {"verdict": "unsupported_label"}
            result, output_dir = _run_minimal_full_pipeline(
                Path(temp_dir),
                report_editor_result=editor_result,
                verdict_candidate=invalid_candidate,
            )

            fallback_path = (
                output_dir / "audit" / "04b_reproducibility_verdict_fallback.json"
            )
            fallback_audit = json.loads(fallback_path.read_text(encoding="utf-8"))
            self.assertTrue(fallback_audit["advisory"])
            self.assertEqual(fallback_audit["candidate"], invalid_candidate)
            self.assertTrue(fallback_audit["errors"])
            self.assertEqual(result.reproducibility_verdict["verdict"], "inconclusive")
            self.assertEqual(
                validate_stage("reproducibility_verdict", result.reproducibility_verdict),
                [],
            )
            risk_report = json.loads(
                (output_dir / "risk_report.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                risk_report["reproducibility_verdict"],
                result.reproducibility_verdict,
            )
    def test_foundation_failure_falls_back_to_task_writers_for_all_architecture_versions(self) -> None:
        class WriterReached(RuntimeError):
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
                    documents = {
                        "01_extract_engineering_facts": initial,
                        "02a_build_preliminary_repro_tasks": preliminary,
                    }
                    document = json.loads(json.dumps(documents.get(kwargs["stage_label"], preliminary)))
                    write_json(kwargs["output_path"], document)
                    return document

                def fake_thesis(**kwargs):
                    document = {
                        "central_claim": "BER decreases with SNR",
                        "proposed_method": "test method",
                        "mechanism": "higher SNR improves decoding",
                        "comparisons": [],
                        "headline_shape": "decreasing",
                        "caveats": [],
                    }
                    write_json(kwargs["output_dir"] / "paper_thesis.json", document)
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

                def fake_architecture(**kwargs):
                    document = architecture_doc(kwargs["output_dir"])
                    document["schema_version"] = schema_version
                    write_json(kwargs["output_dir"] / "scientific_architecture.json", document)
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
                        "_load_or_create_paper_thesis",
                        side_effect=fake_thesis,
                    ),
                    patch.object(
                        pipeline,
                        "_load_or_create_experiment_index",
                        side_effect=fake_experiment_index,
                    ),
                    patch.object(
                        pipeline,
                        "_load_or_create_scientific_architecture",
                        side_effect=fake_architecture,
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
                foundation_writer.assert_called_once()
                fallback = json.loads(
                    (output_dir / "audit" / "03b_foundation_fallback.json").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual(fallback["decision"], "fallback")
                self.assertTrue(fallback["pipeline_can_continue"])

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

    def test_final_task_designer_snapshot_preserves_coverage_and_replaces_old_graph(self) -> None:
        class WriterReached(RuntimeError):
            pass

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
            task_writer = Mock(side_effect=WriterReached("writer reached"))

            with self.assertRaises(WriterReached):
                _run_to_task_writer_boundary(
                    root,
                    resume=False,
                    environment_mock=Mock(
                        return_value=case_runtime_fixture(root / "case", "0" * 64)
                    ),
                    foundation_mock=Mock(return_value=None),
                    task_writer_mock=task_writer,
                    tasks_document=base_tasks,
                    final_tasks_candidate=final_candidate,
                )

            writer_tasks = task_writer.call_args.kwargs["tasks"]
            writer_plan = task_writer.call_args.kwargs["execution_plan"]
            prior_snapshot = json.loads(
                (root / "case" / "audit" / "02d_tasks_before_final_snapshot.json").read_text(encoding="utf-8")
            )
            snapshot_changes = json.loads(
                (root / "case" / "audit" / "02d_final_task_snapshot_changes.json").read_text(encoding="utf-8")
            )

        self.assertEqual(
            [item["task_id"] for item in writer_tasks["repro_tasks"]],
            ["task_a", "task_b", "task_c"],
        )
        self.assertEqual(
            [
                item["relationship_id"]
                for item in writer_tasks["execution_relationships"]
            ],
            ["new_ac"],
        )
        self.assertEqual(writer_tasks["repro_tasks"][0]["figure_or_claim"], "Claim A refined")
        self.assertEqual(prior_snapshot["repro_tasks"][0]["figure_or_claim"], "Claim A")
        self.assertEqual(prior_snapshot["execution_relationships"][0]["relationship_id"], "existing_ab")
        self.assertEqual(snapshot_changes["preserved_task_ids"], ["task_b"])
        self.assertEqual(snapshot_changes["removed_relationship_ids"], ["existing_ab"])
        self.assertEqual(writer_plan["logical_task_count"], 3)
        self.assertEqual(writer_plan["execution_unit_count"], 2)

    def test_optional_foundation_environment_failure_falls_back(self) -> None:
        class WriterReached(RuntimeError):
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

            fallback = json.loads(
                (output_dir / "audit" / "03b_foundation_fallback.json").read_text(
                    encoding="utf-8"
                )
            )

        foundation.assert_called_once()
        task_writer.assert_called_once()
        self.assertEqual(fallback["decision"], "fallback")
        self.assertTrue(fallback["pipeline_can_continue"])
        self.assertEqual(fallback["category"], "optional_foundation_dependency")

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

            with self.assertRaises(EnvironmentResolutionError) as caught:
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
                (output_dir / "audit" / "03a_environment_blocked.json").read_text(
                    encoding="utf-8"
                )
            )

        self.assertEqual(caught.exception.category, "material_foundation_dependency")
        foundation.assert_called_once()
        task_writer.assert_not_called()
        self.assertEqual(audit["source"], "foundation_writer")
        self.assertFalse(audit["pipeline_can_continue"])

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

            with self.assertRaises(EnvironmentResolutionError) as caught:
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

        self.assertEqual(caught.exception.category, "trusted_source_unavailable")
        foundation.assert_not_called()
        task_writer.assert_not_called()
        self.assertEqual(audit["decision"], "stop")
        self.assertEqual(audit["stop_class"], "blocked_environment")
        self.assertFalse(audit["pipeline_can_continue"])
        self.assertEqual(audit["source"], "initial_resolution")
        self.assertEqual(audit["category"], "trusted_source_unavailable")

    def test_task_writer_environment_extension_restarts_foundation_and_all_writers(self) -> None:
        class WriterReached(RuntimeError):
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

        self.assertEqual([call.kwargs["resume"] for call in foundation.call_args_list], [True, True])
        self.assertEqual([call.kwargs["resume"] for call in task_writer.call_args_list], [True, True])
        self.assertEqual(
            [call.kwargs["case_runtime"] for call in foundation.call_args_list],
            [runtime_0, runtime_1],
        )
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

    def test_repeated_environment_request_without_hash_progress_stops(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output_dir = root / "case"
            runtime_0 = case_runtime_fixture(output_dir, "0" * 64)
            runtime_1 = case_runtime_fixture(output_dir, "1" * 64)
            pending = EnvironmentRequestRequired(
                [RequirementRequest("scipy>=1.11")],
                source="task_writers",
            )
            environment = Mock(side_effect=[runtime_0, runtime_1, runtime_1])
            foundation = Mock(return_value={"manifest": {"files": []}})
            task_writer = Mock(side_effect=[pending, pending])

            with self.assertRaises(EnvironmentResolutionError) as caught:
                _run_to_task_writer_boundary(
                    root,
                    resume=True,
                    environment_mock=environment,
                    foundation_mock=foundation,
                    task_writer_mock=task_writer,
                )

            audit = json.loads(
                (output_dir / "audit" / "03a_environment_blocked.json").read_text(
                    encoding="utf-8"
                )
            )

        self.assertEqual(caught.exception.category, "resolution_stalled")
        self.assertEqual(task_writer.call_count, 2)
        self.assertEqual(foundation.call_count, 2)
        self.assertEqual(environment.call_count, 3)
        self.assertEqual(audit["source"], "task_writers")
        self.assertEqual(audit["category"], "resolution_stalled")

    def test_revision_dependency_failure_keeps_prior_foundation_and_reaches_reportable_writer_boundary(self) -> None:
        from geng_agent.foundation_revision import FoundationRevisionRequired

        class WriterReached(RuntimeError):
            pass

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output_dir = root / "case"
            runtime = case_runtime_fixture(output_dir, "0" * 64)
            extended_runtime = case_runtime_fixture(output_dir, "1" * 64)
            prior = {"snapshot_hash": "old-science", "manifest": {"snapshot_hash": "old-science", "files": []}}
            request = {"request_id": "noise-repair", "component_ids": ["system"],
                       "affected_task_ids": ["reproduce_fig_4"], "evidence_root": str(root / "evidence")}
            environment = Mock(side_effect=[runtime, EnvironmentResolutionError("offline", "dependency unavailable"), extended_runtime])
            foundation = Mock(side_effect=[prior,
                EnvironmentRequestRequired([RequirementRequest("scipy>=1.11")], source="foundation_writer"), prior, prior])
            task_writer = Mock(side_effect=[FoundationRevisionRequired(request),
                EnvironmentRequestRequired([RequirementRequest("matplotlib>=3.8")], source="task_writers"),
                WriterReached("reportable terminal")])
            with self.assertRaises(WriterReached):
                _run_to_task_writer_boundary(root, resume=True, environment_mock=environment,
                                            foundation_mock=foundation, task_writer_mock=task_writer)
            failure = json.loads((output_dir / "audit/03b_foundation_revision_failures/noise-repair.json").read_text())
            self.assertEqual(failure["decision"], "retain_previous_version_and_report_unresolved_science")
            blocked = json.loads((output_dir / "audit/03a_environment_blocked.json").read_text())
            self.assertTrue(blocked["pipeline_can_continue"])
            self.assertEqual(blocked["scope"], "foundation_revision")
            resumed = task_writer.call_args_list[-1].kwargs
            self.assertEqual(resumed["declined_foundation_revision_ids"], {"noise-repair"})
            self.assertEqual(resumed["force_task_ids"], set())
            self.assertIs(resumed["foundation"], prior)
            self.assertEqual(foundation.call_count, 4)
            self.assertEqual(environment.call_count, 3)
            self.assertEqual([item.requirement for item in environment.call_args_list[-1].kwargs["extra_requirements"]], ["matplotlib>=3.8"])

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
                    "01_extract_engineering_facts": initial,
                    "02a_build_preliminary_repro_tasks": preliminary,
                    "02b_round_01_targeted_fact_backfill": backfill,
                    "02c_round_01_refresh_repro_tasks": finalized,
                    "02d_finalize_scientific_acceptance": final_acceptance,
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
                    "02d_finalize_scientific_acceptance",
                    "02f_design_scientific_architecture",
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
            self.assertEqual(analysis_result["analysis_stage_invocations"], 7)
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
                "01_extract_engineering_facts": initial,
                "02a_build_preliminary_repro_tasks": preliminary,
                "02b_round_01_targeted_fact_backfill": round_1_backfill,
                "02c_round_01_refresh_repro_tasks": round_1_task,
                "02b_round_02_targeted_fact_backfill": round_2_backfill,
                "02c_round_02_refresh_repro_tasks": round_2_task,
                "02d_finalize_scientific_acceptance": round_2_task,
            }

            def fake_analysis_stage(**kwargs):
                label = kwargs["stage_label"]
                calls.append(label)
                if label == "02f_design_scientific_architecture":
                    document = architecture_doc(kwargs["output_dir"])
                    write_json(kwargs["output_path"], document)
                    return document
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

            self.assertEqual(len(calls), 8)
            summary = json.loads(
                (output_dir / "audit" / "02b_targeted_fact_backfill_summary.json").read_text(encoding="utf-8")
            )
            self.assertEqual(summary["round_count"], 2)
            self.assertEqual(summary["terminal_unresolved_count"], 1)
            self.assertEqual(summary["stop_reason"], "task_expert_handoff_ready")
            analysis_result = json.loads(
                (output_dir / "analysis_result.json").read_text(encoding="utf-8")
            )
            self.assertEqual(analysis_result["analysis_stage_invocations"], 9)
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
            self.assertTrue(
                final_tasks["_meta"]["scientific_acceptance_finalization"]["structure_is_advisory"]
            )


if __name__ == "__main__":
    unittest.main()
