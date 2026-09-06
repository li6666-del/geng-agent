import inspect
import json
import os
import sys
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from geng_agent.foundation_snapshot import foundation_snapshot_hash
from geng_agent.outputs import write_json
from geng_agent.agentic_task_writers import (
    _build_task_writer_brief,
    _build_task_writer_continuation_brief,
    _collect_task_writer_delivery,
    _dispatch_task_writers,
    _freeze_repro_project_package,
    _load_task_writer_resume_records,
    _record_has_terminal_task_verification,
    _record_is_valid_current_delivery,
    _run_one_task_writer,
    _run_task_writer_codex_session,
    _task_writer_runtime_task_passed,
    _task_writer_concurrency,
    run_codex_task_writer_workflow,
    apply_verified_result,
)

from geng_agent.task_writer_support import (
    ANALYSIS_ARTIFACT_DIR,
    PAPER_EVIDENCE_DIR,
    WRITER_ANALYSIS_SCHEMA_VERSION,
    WRITER_HANDOFF_POLICY_VERSION,
    WRITER_REQUIRED_ANALYSIS_ARTIFACTS,
    _analysis_snapshot_hash,
    _collect_writer_analysis_artifacts,
    _load_cached_task_writer_workflow,
)
from geng_agent.project_portability import (
    ProjectPortabilityError,
    build_source_inventory,
    validate_repro_project_portability,
)


def _pair(task_id: str) -> tuple[dict, dict]:
    return ({"task_id": task_id, "figure_or_claim": "Fig. 1"},
            {"task_id": task_id, "module": task_id, "script": f"tasks/{task_id}.py", "output_subdir": task_id})


def _delivery(task_id: str, sandbox: Path) -> dict:
    sandbox.mkdir(parents=True, exist_ok=True)
    return {
        "task_id": task_id, "sandbox": str(sandbox), "writer_completed": True,
        "task_writer_status": "ready_for_review",
        "result_json": {"task_id": task_id, "status": "ready_for_review", "summary": "done",
                        "local_image_paths": ["outputs/plot.png"],
                        "execution_summary": {"full_run_count": 1, "last_returncode": 0}},
    }


def _write_frozen_test_project(root: Path) -> tuple[Path, Path, Path]:
    output = root / "case"
    audit = output / "audit"
    repro = output / "repro_project"
    repro.mkdir(parents=True)
    (repro / "run_experiment.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    (repro / "config.json").write_text("{}\n", encoding="utf-8")
    (repro / "config_smoke.json").write_text('{"smoke": true}\n', encoding="utf-8")
    write_json(
        output / "runtime_result.json",
        {
            "validation": {
                "required_files_present": True,
                "python_compiles": True,
                "local_imports_resolve": True,
            },
            "requirements_warnings": [],
            "requirements_issues": [],
            "security_issues": [],
        },
    )
    _freeze_repro_project_package(
        repro_project_dir=repro,
        output_dir=output,
        audit_path=audit / "03c_project_portability.json",
        task_manifest={"version": 1, "tasks": []},
        expected_paths={
            "run_experiment.py",
            "config.json",
            "config_smoke.json",
            "source_inventory.json",
        },
        analysis_snapshot_hash="analysis-hash",
        foundation_snapshot_hash="foundation-hash",
        environment_hash="environment-hash",
        run_smoke=False,
    )
    return output, audit, repro


class AutonomousTaskWriterTests(unittest.TestCase):
    def test_environment_request_wins_even_when_codex_session_reports_failure(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            sandbox = root / "sandbox"
            audit_dir = root / "audit"
            sandbox.mkdir()
            audit_dir.mkdir()

            def failed_after_request(**kwargs):
                write_json(
                    Path(kwargs["work_dir"]) / "environment_request.json",
                    {"requirements": [{"requirement": "scipy>=1.11"}]},
                )
                return {"ok": False, "error": "codex session ended after requesting a package"}

            with patch(
                "geng_agent.task_writer_runner.run_codex_subprocess",
                side_effect=failed_after_request,
            ):
                status = _run_task_writer_codex_session(
                    label="task_writer_request_priority",
                    prompt="implement the paper task",
                    sandbox=sandbox,
                    audit_dir=audit_dir,
                )

        self.assertEqual(status["error_kind"], "environment_request")
        self.assertEqual(status["environment_requests"][0]["requirement"], "scipy>=1.11")
        self.assertNotIn("codex session ended", status["blocked_reason"])

    @unittest.skipIf(os.name == "nt", "symlink creation is not reliably available on Windows")
    def test_writer_requirements_symlink_is_rejected_without_leaking_target(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            sandbox = root / "sandbox"
            audit_dir = root / "audit"
            sandbox.mkdir()
            audit_dir.mkdir()
            secret = root / "secret.txt"
            secret.write_text("TOP-SECRET-CONTENT", encoding="utf-8")

            def linked_requirements(**kwargs):
                (Path(kwargs["work_dir"]) / "requirements.txt").symlink_to(secret)
                return {"ok": True}

            runtime = type(
                "Runtime",
                (),
                {
                    "python_executable": Path(sys.executable),
                    "venv_dir": Path(sys.executable).parent.parent,
                    "lock": {},
                },
            )()
            with patch(
                "geng_agent.task_writer_runner.run_codex_subprocess",
                side_effect=linked_requirements,
            ):
                status = _run_task_writer_codex_session(
                    label="task_writer_link_safety",
                    prompt="implement the paper task",
                    sandbox=sandbox,
                    audit_dir=audit_dir,
                    case_runtime=runtime,
                )

        self.assertEqual(status["error_kind"], "environment_request_invalid")
        self.assertNotIn("TOP-SECRET-CONTENT", str(status))

    def test_cached_terminal_requires_v2_host_outcome(self) -> None:
        record = {
            "task_id": "task_1",
            "task_verification": {
                "schema_version": "1.0",
                "task_id": "task_1",
                "verdict": "accepted",
            },
        }
        self.assertFalse(_record_has_terminal_task_verification(record))

        record["task_verification"] = {
            "schema_version": "2.0",
            "task_id": "task_1",
            "outcome": "not_reproduced",
            "host_action": "complete",
            "rerun_reason": "none",
            "run_valid": True,
        }
        self.assertTrue(_record_has_terminal_task_verification(record))

        record["task_verification"]["host_action"] = "rerun_writer"
        self.assertFalse(_record_has_terminal_task_verification(record))

    def test_runtime_pass_requires_valid_verification_but_preserves_build_only(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            failed = _delivery("failed", root / "failed")
            failed["task_verification"] = {
                "task_id": "failed",
                "outcome": "execution_failed",
                "host_action": "complete",
                "rerun_reason": "none",
                "run_valid": False,
            }
            build_only = _delivery("build_only", root / "build_only")

            self.assertFalse(_task_writer_runtime_task_passed(failed))
            self.assertTrue(_task_writer_runtime_task_passed(build_only))

    def test_stopping_assessment_is_advisory(self) -> None:
        with TemporaryDirectory() as temp:
            sandbox = Path(temp)
            output = sandbox / "outputs" / "task_1"
            output.mkdir(parents=True)
            result = {
                "task_id": "task_1",
                "status": "ready_for_review",
                "summary": "done",
                "local_image_paths": ["outputs/task_1/plot.png"],
                "execution_summary": {"full_run_count": 1, "last_returncode": 0},
            }
            write_json(output / "task_agent_result.json", result)
            (output / "plot.png").write_bytes(b"png")
            kwargs = {
                "index": 1,
                "task": {"task_id": "task_1"},
                "manifest_entry": {
                    "task_id": "task_1",
                    "module": "task_1",
                    "output_subdir": "task_1",
                },
                "sandbox": sandbox,
                "writer_status": {"ok": True},
            }

            normal = _collect_task_writer_delivery(**kwargs)
            explicitly_checked = _collect_task_writer_delivery(
                **kwargs,
                require_stopping_assessment=True,
            )

            self.assertEqual(normal["task_writer_status"], "ready_for_review")
            self.assertEqual(explicitly_checked["task_writer_status"], "ready_for_review")
            self.assertTrue(any(
                "stopping_assessment" in warning
                for warning in explicitly_checked["delivery_warnings"]
            ))
    def test_initial_writer_prompt_uses_scientific_materiality_without_format_gate(self) -> None:
        prompt = _build_task_writer_brief(
            index=1,
            task={"task_id": "task_1", "figure_or_claim": "Fig. 1"},
            manifest_entry={"task_id": "task_1", "module": "task_1", "output_subdir": "task_1"},
            facts={"engineering_facts": []},
            experiment_index={"experiments": []},
            paper={"chunks": []},
            paper_context_json="",
            paper_thesis=None,
            run_repro=True,
        )

        self.assertIn("Highest law: fidelity to the paper's established facts", prompt)
        self.assertIn("may never overwrite higher-priority evidence", prompt)
        self.assertIn("An assumed algorithm is acceptable only", prompt)
        self.assertIn("Core-result stopping policy", prompt)
        self.assertIn("Missing or imperfect structure is not itself a scientific failure", prompt)
        self.assertIn("A ratio below 10 is non-material", prompt)
        self.assertIn("Another Writer execution is allowed only", prompt)
        self.assertIn("core_conclusion_failed", prompt)
        self.assertIn("key_numeric_ratio_ge_10", prompt)
        self.assertIn("invalid_run", prompt)
        self.assertIn("ready_for_review", prompt)
        self.assertNotIn("stopping_assessment", prompt)
        self.assertNotIn("pixel-level", prompt)
    def test_repair_prompt_requires_causal_scientific_change(self) -> None:
        prompt = _build_task_writer_continuation_brief(
            base_prompt="base",
            task_id="task_1",
            module="task_1",
            session_round=2,
            review_feedback={"differences": ["change an explicit paper parameter"]},
        )

        self.assertIn("Highest law: fidelity to the paper's established facts", prompt)
        self.assertIn("Reporter feedback is evidence to investigate, not authority", prompt)
        self.assertIn("Classify every reporter item before editing", prompt)
        self.assertIn("Never rerun unchanged code solely to answer non-blocking feedback", prompt)
        self.assertIn("Investigate the causal rerun note", prompt)
        self.assertIn("failure of any assigned core conclusion", prompt)
        self.assertIn("numerical mismatch below a factor of 10", prompt)
        self.assertIn("permitted by the mandatory stopping policy", prompt)
        self.assertIn("do not change any assumption, seed, dataset filter, configuration, or epoch count", prompt)
    def test_analysis_warnings_are_collected_as_optional_writer_evidence(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "analysis_warnings.json").write_text(
                "{}", encoding="utf-8"
            )

            artifacts = _collect_writer_analysis_artifacts(output_dir=root)

            self.assertEqual(
                artifacts["analysis_warnings.json"],
                (root / "analysis_warnings.json").resolve(),
            )

    def test_analysis_snapshot_hash_changes_with_final_artifact(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            paper = root / "paper.pdf"
            facts = root / "engineering_facts.json"
            paper.write_bytes(b"paper")
            facts.write_text("{}", encoding="utf-8")
            first = _analysis_snapshot_hash(
                paper_path=paper,
                artifacts={"engineering_facts.json": facts},
            )
            facts.write_text('{"changed": true}', encoding="utf-8")
            second = _analysis_snapshot_hash(
                paper_path=paper,
                artifacts={"engineering_facts.json": facts},
            )
            self.assertNotEqual(first, second)

    def test_delivery_metadata_warnings_do_not_invalidate_a_successful_full_run(self) -> None:
        with TemporaryDirectory() as temp:
            record = _delivery("task_1", Path(temp) / "sandbox")
            record["result_json"].update({
                "status": "complete",
                "summary": "",
                "local_image_paths": [],
            })
            self.assertTrue(_record_is_valid_current_delivery(record))

            record["result_json"]["execution_summary"]["last_returncode"] = 1
            self.assertTrue(_record_is_valid_current_delivery(record))

    def test_binding_advisory_does_not_invalidate_cached_delivery(self) -> None:
        with TemporaryDirectory() as temp:
            record = _delivery('task_1', Path(temp) / 'sandbox')
            record['result_json']['delivery_warnings'] = [
                'shared_component_advisory: shared_model is not proven by static scanning'
            ]
            with patch(
                'geng_agent.task_writer_delivery._task_execution_binding_issues',
                side_effect=AssertionError('cached delivery must not run the advisory scanner'),
            ):
                self.assertTrue(_record_is_valid_current_delivery(record))

    def test_concurrency_equals_task_count(self) -> None:
        self.assertEqual(_task_writer_concurrency(7, 1, run_repro=True), 7)

    def test_writer_call_chain_has_no_codex_wall_clock_timeout_parameter(self) -> None:
        for target in (
            run_codex_task_writer_workflow,
            _dispatch_task_writers,
            _run_one_task_writer,
            _run_task_writer_codex_session,
        ):
            with self.subTest(target=target.__name__):
                self.assertNotIn("timeout", inspect.signature(target).parameters)

    def test_dispatch_launches_all_and_relaunches_only_revised_task(self) -> None:
        pairs = [_pair("accepted"), _pair("revise")]
        with TemporaryDirectory() as temp:
            root = Path(temp)
            existing = {1: _delivery("accepted", root / "a"), 2: _delivery("revise", root / "b")}
            calls = []
            def fake_writer(**kwargs):
                calls.append(kwargs)
                return _delivery(kwargs["task"]["task_id"], root / (kwargs["task"]["task_id"] + "2"))
            with patch("geng_agent.task_writer_dispatch._run_one_task_writer", side_effect=fake_writer):
                records, audit = _dispatch_task_writers(
                    task_pairs=pairs, facts={}, experiment_index={}, paper={}, paper_path=root / "paper.pdf",
                    paper_context_json="", paper_images=[], paper_thesis=None,
                    analysis_snapshot_hash="snapshot", analysis_artifacts={}, task_root=root / "sandboxes", audit_dir=root,
                    run_repro=True, initial_records_by_index=existing,
                    review_feedback={"revise": {"differences": ["curve differs"]}}, force_task_ids={"revise"})
            self.assertEqual([item["task_id"] for item in records], ["accepted", "revise"])
            self.assertEqual([call["task"]["task_id"] for call in calls], ["revise"])
            self.assertEqual(calls[0]["review_feedback"]["differences"], ["curve differs"])
            self.assertNotIn("timeout", calls[0])
            self.assertTrue(audit["codex_sessions_unbounded"])
            self.assertNotIn("session_timeout_s", audit)
            self.assertNotIn("overall_runtime_limit_s", audit)
            self.assertEqual(audit["reused_task_ids"], ["accepted"])

    def test_resume_records_recover_matching_sandbox_when_checkpoint_is_missing(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            audit_dir = root / "audit"
            sandbox = audit_dir / "03c_task_writer_sandboxes" / "02_task_beta"
            write_json(
                sandbox / "paper_evidence" / "index.json",
                {
                    "analysis_snapshot_hash": "snapshot",
                    "tasks": [{"task_id": "task_beta"}],
                },
            )

            records = _load_task_writer_resume_records(
                audit_dir=audit_dir,
                task_pairs=[_pair("task_alpha"), _pair("task_beta")],
                expected_analysis_snapshot_hash="snapshot",
            )

            checkpoint = json.loads(
                (audit_dir / "03c_task_writers_records.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                checkpoint["checkpoint"],
                "sandbox_snapshot_recovery",
            )
            self.assertEqual(
                checkpoint["recovery"]["recovered_indexes"],
                [2],
            )
            self.assertEqual(set(records), {2})
            self.assertEqual(records[2]["task_id"], "task_beta")
            self.assertEqual(Path(records[2]["sandbox"]).resolve(), sandbox.resolve())
            self.assertEqual(records[2]["analysis_snapshot_hash"], "snapshot")

    def test_resume_records_do_not_recover_untrusted_sandbox_evidence(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            audit_dir = root / "audit"
            sandbox = audit_dir / "03c_task_writer_sandboxes" / "01_task_1"
            sandbox.mkdir(parents=True)

            with self.subTest(reason="paper evidence is missing"):
                records = _load_task_writer_resume_records(
                    audit_dir=audit_dir,
                    task_pairs=[_pair("task_1")],
                    expected_analysis_snapshot_hash="snapshot",
                )
                self.assertEqual(records, {})

            write_json(
                sandbox / "paper_evidence" / "index.json",
                {"analysis_snapshot_hash": "different-snapshot"},
            )
            with self.subTest(reason="analysis snapshot hash does not match"):
                records = _load_task_writer_resume_records(
                    audit_dir=audit_dir,
                    task_pairs=[_pair("task_1")],
                    expected_analysis_snapshot_hash="snapshot",
                )
                self.assertEqual(records, {})

    def test_resume_records_recover_when_only_runtime_environment_changed(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            audit_dir = root / "audit"
            sandbox = audit_dir / "03c_task_writer_sandboxes" / "01_task_1"
            paper_path = (
                sandbox
                / PAPER_EVIDENCE_DIR
                / "source"
                / "paper.pdf"
            )
            paper_path.parent.mkdir(parents=True)
            paper_path.write_bytes(b"same paper")
            artifacts: dict[str, Path] = {}
            for name in WRITER_REQUIRED_ANALYSIS_ARTIFACTS:
                path = sandbox / PAPER_EVIDENCE_DIR / ANALYSIS_ARTIFACT_DIR / name
                write_json(path, {"name": name, "scientific_input": "same"})
                artifacts[name] = path
            handoff_hash = _analysis_snapshot_hash(
                paper_path=paper_path,
                artifacts=artifacts,
            )
            write_json(
                sandbox / PAPER_EVIDENCE_DIR / "index.json",
                {
                    "analysis_snapshot_hash": "old-runtime-snapshot",
                    "policy_version": WRITER_HANDOFF_POLICY_VERSION,
                    "analysis_schema_version": WRITER_ANALYSIS_SCHEMA_VERSION,
                    "paper_source": {
                        "copied": True,
                        "relative_path": f"{PAPER_EVIDENCE_DIR}/source/paper.pdf",
                    },
                    "analysis_artifacts": {"complete": True},
                    "tasks": [{"task_id": "task_1"}],
                },
            )

            records = _load_task_writer_resume_records(
                audit_dir=audit_dir,
                task_pairs=[_pair("task_1")],
                expected_analysis_snapshot_hash="new-runtime-snapshot",
                expected_analysis_handoff_hash=handoff_hash,
            )

            self.assertEqual(set(records), {1})
            self.assertTrue(records[1]["environment_refresh_required"])
            self.assertTrue(records[1]["runtime_refresh_required"])
            self.assertEqual(
                records[1]["stored_analysis_snapshot_hash"],
                "old-runtime-snapshot",
            )
            recovery = json.loads(
                (
                    audit_dir
                    / "03c_task_writers_sandbox_recovery.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(recovery["environment_refresh_indexes"], [1])

    def test_resume_records_reject_runtime_refresh_when_scientific_input_changed(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            audit_dir = root / "audit"
            sandbox = audit_dir / "03c_task_writer_sandboxes" / "01_task_1"
            paper_path = sandbox / PAPER_EVIDENCE_DIR / "source" / "paper.pdf"
            paper_path.parent.mkdir(parents=True)
            paper_path.write_bytes(b"same paper")
            artifacts: dict[str, Path] = {}
            for name in WRITER_REQUIRED_ANALYSIS_ARTIFACTS:
                path = sandbox / PAPER_EVIDENCE_DIR / ANALYSIS_ARTIFACT_DIR / name
                write_json(path, {"name": name, "scientific_input": "same"})
                artifacts[name] = path
            original_handoff_hash = _analysis_snapshot_hash(
                paper_path=paper_path,
                artifacts=artifacts,
            )
            write_json(
                artifacts["engineering_facts.json"],
                {"scientific_input": "changed"},
            )
            write_json(
                sandbox / PAPER_EVIDENCE_DIR / "index.json",
                {
                    "analysis_snapshot_hash": "old-runtime-snapshot",
                    "policy_version": WRITER_HANDOFF_POLICY_VERSION,
                    "analysis_schema_version": WRITER_ANALYSIS_SCHEMA_VERSION,
                    "paper_source": {
                        "copied": True,
                        "relative_path": f"{PAPER_EVIDENCE_DIR}/source/paper.pdf",
                    },
                    "analysis_artifacts": {"complete": True},
                    "tasks": [{"task_id": "task_1"}],
                },
            )

            records = _load_task_writer_resume_records(
                audit_dir=audit_dir,
                task_pairs=[_pair("task_1")],
                expected_analysis_snapshot_hash="new-runtime-snapshot",
                expected_analysis_handoff_hash=original_handoff_hash,
            )

            self.assertEqual(records, {})

    def test_dispatch_checkpoints_each_completed_future(self) -> None:
        pairs = [_pair("task_1"), _pair("task_2")]
        checkpoint_documents: list[dict] = []
        with TemporaryDirectory() as temp:
            root = Path(temp)

            def fake_writer(**kwargs):
                task_id = kwargs["task"]["task_id"]
                return _delivery(task_id, root / "deliveries" / task_id)

            def recording_write(path, document):
                if Path(path).name == "03c_task_writers_records.json":
                    checkpoint_documents.append(document)
                return write_json(path, document)

            with patch(
                "geng_agent.task_writer_dispatch._run_one_task_writer",
                side_effect=fake_writer,
            ), patch(
                "geng_agent.task_writer_state.write_json",
                side_effect=recording_write,
            ):
                records, _ = _dispatch_task_writers(
                    task_pairs=pairs,
                    facts={},
                    experiment_index={},
                    paper={},
                    paper_path=root / "paper.pdf",
                    paper_context_json="",
                    paper_images=[],
                    paper_thesis=None,
                    analysis_snapshot_hash="snapshot",
                    analysis_artifacts={},
                    task_root=root / "sandboxes",
                    audit_dir=root / "audit",
                    run_repro=True,
                )

        self.assertEqual({item["task_id"] for item in records}, {"task_1", "task_2"})
        self.assertGreaterEqual(len(checkpoint_documents), 2)
        self.assertEqual(len(checkpoint_documents[0]["tasks"]), 1)
        self.assertEqual(len(checkpoint_documents[-1]["tasks"]), 2)
        self.assertEqual(
            {item["task_id"] for item in checkpoint_documents[-1]["tasks"]},
            {"task_1", "task_2"},
        )

    def test_dispatch_passes_runtime_refresh_to_pending_writer(self) -> None:
        pairs = [_pair("task_1")]
        calls: list[dict] = []
        with TemporaryDirectory() as temp:
            root = Path(temp)
            sandbox = root / "sandboxes" / "01_task_1"
            sandbox.mkdir(parents=True)

            def fake_writer(**kwargs):
                calls.append(kwargs)
                return _delivery("task_1", sandbox)

            with patch(
                "geng_agent.task_writer_dispatch._run_one_task_writer",
                side_effect=fake_writer,
            ):
                _dispatch_task_writers(
                    task_pairs=pairs,
                    facts={},
                    experiment_index={},
                    paper={},
                    paper_path=root / "paper.pdf",
                    paper_context_json="",
                    paper_images=[],
                    paper_thesis=None,
                    analysis_snapshot_hash="snapshot",
                    analysis_artifacts={},
                    task_root=root / "sandboxes",
                    audit_dir=root / "audit",
                    run_repro=True,
                    initial_records_by_index={
                        1: {
                            "task_id": "task_1",
                            "sandbox": str(sandbox),
                            "runtime_refresh_required": True,
                        }
                    },
                )

        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0]["reuse_existing"])
        self.assertTrue(calls[0]["runtime_refresh_required"])

    def test_pending_runtime_refresh_survives_rewritten_current_evidence(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            audit_dir = root / "audit"
            sandbox = audit_dir / "03c_task_writer_sandboxes" / "01_task_1"
            write_json(
                sandbox / PAPER_EVIDENCE_DIR / "index.json",
                {
                    "analysis_snapshot_hash": "current-snapshot",
                    "tasks": [{"task_id": "task_1"}],
                },
            )
            write_json(
                sandbox / ".geng_runtime_refresh_pending.json",
                {"target_analysis_snapshot_hash": "current-snapshot"},
            )

            records = _load_task_writer_resume_records(
                audit_dir=audit_dir,
                task_pairs=[_pair("task_1")],
                expected_analysis_snapshot_hash="current-snapshot",
            )

        self.assertEqual(set(records), {1})
        self.assertTrue(records[1]["runtime_refresh_required"])

    def test_terminal_success_grants_matched(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            record = _delivery("task_1", root / "sandbox")
            self.assertTrue(_record_is_valid_current_delivery(record))
            output = root / "case"
            audit = output / "audit"
            repro = output / "repro_project"
            audit.mkdir(parents=True)
            repro.mkdir()
            (repro / "config.json").write_text("{}", encoding="utf-8")
            write_json(
                output / "runtime_result.json",
                {
                    "validation": {
                        "required_files_present": True,
                        "python_compiles": True,
                        "local_imports_resolve": True,
                    }
                },
            )
            verification = {
                "schema_version": "2.0",
                "all_terminal": True,
                "all_successful": True,
                "outcome_counts": {"reproduced": 1},
                "tasks": [{
                    "task_id": "task_1",
                    "outcome": "reproduced",
                    "host_action": "complete",
                    "rerun_reason": "none",
                    "run_valid": True,
                    "core_conclusions": [],
                    "key_numeric_comparisons": [],
                    "comparison_summary": "direct match",
                    "differences": [],
                    "evidence_files": ["local.csv"],
                    "feedback": [],
                    "confidence": "high",
                }],
            }

            runtime = apply_verified_result(
                task_records=[record],
                verification_result=verification,
                output_dir=output,
                audit_dir=audit,
                repro_project_dir=repro,
            )

            self.assertTrue(runtime["passed"])
            self.assertEqual(record["task_writer_status"], "matched")
            self.assertEqual(record["scientific_outcome"], "reproduced")
            self.assertTrue(record["verification_verified"])

    def test_verified_result_refreezes_the_final_package_after_config_mutation(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            output, audit, repro = _write_frozen_test_project(root)
            initial_inventory = json.loads(
                (repro / "source_inventory.json").read_text(encoding="utf-8")
            )
            record = _delivery("task_1", root / "sandbox")
            verification = {
                "schema_version": "2.0",
                "all_terminal": True,
                "all_successful": True,
                "outcome_counts": {"reproduced": 1},
                "tasks": [
                    {
                        "task_id": "task_1",
                        "outcome": "reproduced",
                        "host_action": "complete",
                        "rerun_reason": "none",
                        "run_valid": True,
                        "core_conclusions": [],
                        "key_numeric_comparisons": [],
                        "comparison_summary": "direct match",
                        "differences": [],
                        "evidence_files": ["local.csv"],
                        "feedback": [],
                        "confidence": "high",
                    }
                ],
            }

            runtime = apply_verified_result(
                task_records=[record],
                verification_result=verification,
                output_dir=output,
                audit_dir=audit,
                repro_project_dir=repro,
            )

            final_inventory = json.loads(
                (repro / "source_inventory.json").read_text(encoding="utf-8")
            )
            self.assertNotEqual(
                initial_inventory["inventory_sha256"],
                final_inventory["inventory_sha256"],
            )
            self.assertTrue(validate_repro_project_portability(repro)["portable"])
            self.assertTrue(
                json.loads((repro / "config.json").read_text(encoding="utf-8"))[
                    "verification_verified"
                ]
            )
            self.assertEqual(
                runtime["validation"]["final_package_inventory_sha256"],
                final_inventory["inventory_sha256"],
            )
            self.assertTrue((audit / "03c_project_portability_final.json").is_file())

    def test_failed_package_smoke_persists_diagnostics_before_raising(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "case"
            audit = output / "audit"
            repro = output / "repro_project"
            audit.mkdir(parents=True)
            repro.mkdir()
            (repro / "run_experiment.py").write_text(
                "raise SystemExit(1)\n",
                encoding="utf-8",
            )
            (repro / "config.json").write_text("{}\n", encoding="utf-8")
            (repro / "config_smoke.json").write_text('{"smoke": true}\n', encoding="utf-8")
            audit_path = audit / "03c_project_portability.json"

            with self.assertRaises(ProjectPortabilityError):
                _freeze_repro_project_package(
                    repro_project_dir=repro,
                    output_dir=output,
                    audit_path=audit_path,
                    task_manifest={"version": 1, "tasks": []},
                    expected_paths={
                        "run_experiment.py",
                        "config.json",
                        "config_smoke.json",
                        "source_inventory.json",
                    },
                    analysis_snapshot_hash="analysis-hash",
                    foundation_snapshot_hash="foundation-hash",
                    environment_hash="environment-hash",
                    run_smoke=True,
                )

            report = json.loads(audit_path.read_text(encoding="utf-8"))
            self.assertFalse(report["portable"])
            self.assertEqual(report["smoke"]["returncode"], 1)
            self.assertTrue(
                any(item["code"] == "relocated_smoke_failed" for item in report["issues"])
            )

    def test_cached_writer_workflow_rejects_tree_changed_after_final_inventory(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            output, _audit, repro = _write_frozen_test_project(root)

            cached = _load_cached_task_writer_workflow(
                output_dir=output,
                repro_project_dir=repro,
                run_repro=False,
                analysis_snapshot_hash="analysis-hash",
            )
            self.assertIsNotNone(cached)

            (repro / "config.json").write_text('{"changed": true}\n', encoding="utf-8")
            write_json(repro / "source_inventory.json", build_source_inventory(repro))
            self.assertTrue(validate_repro_project_portability(repro)["portable"])
            stale = _load_cached_task_writer_workflow(
                output_dir=output,
                repro_project_dir=repro,
                run_repro=False,
                analysis_snapshot_hash="analysis-hash",
            )
            self.assertIsNone(stale)

    def test_cached_writer_revalidates_reporter_cache_miss_without_dispatching_writer(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "case"
            audit = output / "audit"
            paper = root / "paper.pdf"
            paper.write_bytes(b"paper")
            task, _entry = _pair("task_1")
            tasks = {"repro_tasks": [task]}
            write_json(output / "engineering_facts.json", {"engineering_facts": []})
            write_json(output / "repro_tasks.json", tasks)
            write_json(output / "experiment_index.json", {"experiments": []})
            record = _delivery("task_1", audit / "03c_task_writer_sandboxes" / "01_task_1")
            record.update(
                {
                    "index": 1,
                    "writer_session_count": 1,
                    "task_verification": {
                        "schema_version": "2.0",
                        "task_id": "task_1",
                        "outcome": "reproduced",
                        "host_action": "complete",
                        "rerun_reason": "none",
                        "run_valid": True,
                    },
                }
            )
            callback_calls: list[str] = []

            def callback(index, assigned_task, task_record, round_no):
                callback_calls.append(str(task_record["task_id"]))
                return {
                    "ok": True,
                    "cached": False,
                    "task_id": "task_1",
                    "task_verification": {
                        "schema_version": "2.0",
                        "task_id": "task_1",
                        "outcome": "reproduced",
                        "host_action": "complete",
                        "rerun_reason": "none",
                        "run_valid": True,
                    },
                }

            cached = {
                "manifest": {"_meta": {"backend": "codex", "mode": "task_writers"}},
                "runtime_result": {"passed": True},
                "task_records": [record],
                "written_files": [],
                "status": {"cached": True},
            }
            with patch(
                "geng_agent.agentic_task_writers._load_cached_task_writer_workflow",
                return_value=cached,
            ), patch(
                "geng_agent.agentic_task_writers._load_task_writer_resume_records",
                return_value={1: record},
            ), patch(
                "geng_agent.agentic_task_writers._dispatch_task_writers"
            ) as dispatch:
                result = run_codex_task_writer_workflow(
                    facts={"engineering_facts": []},
                    tasks=tasks,
                    experiment_index={"experiments": []},
                    paper={"chunks": []},
                    paper_path=paper,
                    paper_context_json="",
                    paper_images=[],
                    paper_thesis=None,
                    output_dir=output,
                    audit_dir=audit,
                    repro_project_dir=output / "repro_project",
                    run_repro=True,
                    resume=True,
                    task_review_callback=callback,
                )

            self.assertEqual(callback_calls, ["task_1"])
            dispatch.assert_not_called()
            self.assertFalse(result["task_records"][0]["task_reporter"]["cached"])
            refresh = json.loads(
                (audit / "03c_cached_task_reporters.json").read_text(encoding="utf-8")
            )
            self.assertEqual(refresh["writer_sessions_launched"], 0)
            self.assertEqual(refresh["actions"][0]["reporter_cached"], False)
            authoritative_records = json.loads(
                (audit / "03c_task_writers_records.json").read_text(encoding="utf-8")
            )
            authoritative_status = json.loads(
                (audit / "03c_task_writers_status.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                authoritative_records["checkpoint"],
                "cached_writer_reporters_revalidated",
            )
            self.assertFalse(
                authoritative_records["tasks"][0]["task_reporter"]["cached"]
            )
            self.assertTrue(authoritative_status["reporter_refresh_complete"])
            self.assertEqual(authoritative_status["stop_class"], "verified_matched")
            self.assertEqual(
                authoritative_status["tasks"][0]["task_reporter_action"],
                "terminal",
            )

    def test_cached_writer_calls_valid_reporter_cache_then_reuses_everything(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "case"
            audit = output / "audit"
            paper = root / "paper.pdf"
            paper.write_bytes(b"paper")
            task, _entry = _pair("task_1")
            tasks = {"repro_tasks": [task]}
            write_json(output / "engineering_facts.json", {"engineering_facts": []})
            write_json(output / "repro_tasks.json", tasks)
            write_json(output / "experiment_index.json", {"experiments": []})
            record = _delivery("task_1", audit / "03c_task_writer_sandboxes" / "01_task_1")
            record.update({"index": 1, "writer_session_count": 1})
            callback_calls: list[str] = []

            def callback(index, assigned_task, task_record, round_no):
                callback_calls.append(str(task_record["task_id"]))
                return {
                    "ok": True,
                    "cached": True,
                    "task_id": "task_1",
                    "task_verification": {
                        "schema_version": "2.0",
                        "task_id": "task_1",
                        "outcome": "reproduced",
                        "host_action": "complete",
                        "rerun_reason": "none",
                        "run_valid": True,
                    },
                }

            cached = {
                "manifest": {"_meta": {"backend": "codex", "mode": "task_writers"}},
                "runtime_result": {"passed": True},
                "task_records": [record],
                "written_files": [],
                "status": {"cached": True},
            }
            with patch(
                "geng_agent.agentic_task_writers._load_cached_task_writer_workflow",
                return_value=cached,
            ), patch(
                "geng_agent.agentic_task_writers._load_task_writer_resume_records",
                return_value={1: record},
            ), patch(
                "geng_agent.agentic_task_writers._dispatch_task_writers"
            ) as dispatch:
                result = run_codex_task_writer_workflow(
                    facts={"engineering_facts": []},
                    tasks=tasks,
                    experiment_index={"experiments": []},
                    paper={"chunks": []},
                    paper_path=paper,
                    paper_context_json="",
                    paper_images=[],
                    paper_thesis=None,
                    output_dir=output,
                    audit_dir=audit,
                    repro_project_dir=output / "repro_project",
                    run_repro=True,
                    resume=True,
                    task_review_callback=callback,
                )

            self.assertEqual(callback_calls, ["task_1"])
            dispatch.assert_not_called()
            self.assertTrue(result["task_records"][0]["task_reporter"]["cached"])
            self.assertTrue(result["status"]["cached_writer_reused"])
            self.assertTrue(result["status"]["task_reporters_revalidated"])

    def test_cached_reporter_revision_replays_once_into_existing_writer_continuation(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "case"
            audit = output / "audit"
            paper = root / "paper.pdf"
            paper.write_bytes(b"paper")
            task, _entry = _pair("task_1")
            tasks = {"repro_tasks": [task]}
            write_json(output / "engineering_facts.json", {"engineering_facts": []})
            write_json(output / "repro_tasks.json", tasks)
            write_json(output / "experiment_index.json", {"experiments": []})
            record = _delivery("task_1", audit / "03c_task_writer_sandboxes" / "01_task_1")
            record.update({"index": 1, "writer_session_count": 1})
            reporter_workspace = root / "reporter_workspace"
            write_json(reporter_workspace / "paper_evidence" / "index.json", {})
            callback_calls: list[str] = []
            reporter_result = {
                "ok": True,
                "cached": False,
                "task_id": "task_1",
                "workspace": str(reporter_workspace),
                "task_verification": {
                    "schema_version": "2.0",
                    "task_id": "task_1",
                    "outcome": "not_reproduced",
                    "host_action": "rerun_writer",
                    "rerun_reason": "core_conclusion_failed",
                    "run_valid": True,
                    "core_conclusions": [
                        {
                            "claim_id": "claim_order",
                            "status": "unsupported",
                            "local_observation": "the cached full result reverses the paper ordering",
                            "evidence_files": ["outputs/task_1/results.csv"],
                        }
                    ],
                    "rerun_evidence": {
                        "rerun_reason": "core_conclusion_failed",
                        "contract_item_ids": ["claim_order"],
                        "paper_evidence_files": ["paper_evidence/index.json"],
                        "causal_change": "correct the paper-defined normalization",
                        "change_targets": ["tasks/task_1.py:normalize"],
                        "predicted_effect": "restore the paper ordering",
                    },
                },
            }

            def callback(index, assigned_task, task_record, round_no):
                callback_calls.append(str(task_record["task_id"]))
                return reporter_result

            cached = {
                "manifest": {"_meta": {"backend": "codex", "mode": "task_writers"}},
                "runtime_result": {"passed": True},
                "task_records": [record],
                "written_files": [],
                "status": {"cached": True},
            }

            class DispatchReached(RuntimeError):
                pass

            def capture_dispatch(**kwargs):
                self.assertEqual(kwargs["force_task_ids"], {"task_1"})
                self.assertIs(kwargs["initial_records_by_index"][1], record)
                replayed = kwargs["task_review_callback"](1, task, record, 1)
                self.assertIs(replayed, reporter_result)
                self.assertEqual(callback_calls, ["task_1"])
                raise DispatchReached

            with patch(
                "geng_agent.agentic_task_writers._load_cached_task_writer_workflow",
                return_value=cached,
            ), patch(
                "geng_agent.agentic_task_writers._load_task_writer_resume_records",
                return_value={1: record},
            ), patch(
                "geng_agent.agentic_task_writers._clear_stage_outputs"
            ) as clear_outputs, patch(
                "geng_agent.agentic_task_writers._dispatch_task_writers",
                side_effect=capture_dispatch,
            ):
                with self.assertRaises(DispatchReached):
                    run_codex_task_writer_workflow(
                        facts={"engineering_facts": []},
                        tasks=tasks,
                        experiment_index={"experiments": []},
                        paper={"chunks": []},
                        paper_path=paper,
                        paper_context_json="",
                        paper_images=[],
                        paper_thesis=None,
                        output_dir=output,
                        audit_dir=audit,
                        repro_project_dir=output / "repro_project",
                        run_repro=True,
                        resume=True,
                        task_review_callback=callback,
                    )

            self.assertEqual(callback_calls, ["task_1"])
            clear_outputs.assert_called_once_with(
                output,
                "manifest",
                preserve_audit=True,
                preserve_paths={"report_assets"},
            )

    def test_one_writer_reruns_once_for_complete_causal_request(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            records = [
                _delivery("task_1", root / "sandbox"),
                _delivery("task_1", root / "sandbox"),
            ]
            callback_calls = []
            reporter_workspace = root / "reporter_workspace"
            paper_evidence = reporter_workspace / "paper_evidence"
            paper_evidence.mkdir(parents=True)
            (paper_evidence / "index.json").write_text("{}", encoding="utf-8")

            def callback(index, task, record, round_no):
                callback_calls.append(round_no)
                if len(callback_calls) == 1:
                    verification = {
                        "core_conclusions": [{
                            "claim_id": "claim_order",
                            "status": "unsupported",
                            "local_observation": "local ordering contradicts the paper",
                            "evidence_files": ["outputs/task_1/results.csv"],
                        }],
                        "schema_version": "2.0",
                        "task_id": "task_1",
                        "outcome": "not_reproduced",
                        "host_action": "rerun_writer",
                        "rerun_reason": "core_conclusion_failed",
                        "run_valid": True,
                        "rerun_evidence": {
                            "rerun_reason": "core_conclusion_failed",
                            "contract_item_ids": ["claim_order"],
                            "paper_evidence_files": ["paper_evidence/index.json"],
                            "causal_change": "correct the paper-defined normalization",
                            "change_targets": ["tasks/task_1.py:normalize"],
                            "predicted_effect": "restore the paper ordering",
                        },
                    }
                else:
                    verification = {
                        "schema_version": "2.0",
                        "task_id": "task_1",
                        "outcome": "reproduced",
                        "host_action": "complete",
                        "rerun_reason": "none",
                        "run_valid": True,
                    }
                return {
                    "ok": True,
                    "task_id": "task_1",
                    "workspace": str(reporter_workspace),
                    "task_verification": verification,
                }

            with patch("geng_agent.task_writer_runner._prepare_task_writer_sandbox"), patch(
                "geng_agent.task_writer_runner._build_task_writer_brief", return_value="base"
            ), patch(
                "geng_agent.task_writer_runner._run_task_writer_codex_session", return_value={"ok": True}
            ) as run_session, patch(
                "geng_agent.task_writer_runner._restore_trusted_files"
            ), patch(
                "geng_agent.task_writer_runner._collect_task_writer_delivery", side_effect=records
            ), patch(
                "geng_agent.task_writer_runner._archive_nonterminal_writer_delivery"
            ), patch(
                "geng_agent.task_writer_runner._record_source_config_fingerprint",
                side_effect=["initial-source", "changed-source"],
            ):
                result = _run_one_task_writer(
                    index=1,
                    reuse_existing=False,
                    task={"task_id": "task_1"},
                    manifest_entry={"task_id": "task_1", "module": "task_1", "output_subdir": "task_1"},
                    facts={},
                    experiment_index={},
                    paper={},
                    paper_path=root / "paper.pdf",
                    paper_context_json="",
                    paper_images=[],
                    paper_thesis=None,
                    analysis_snapshot_hash="snapshot",
                    analysis_artifacts={},
                    task_root=root / "task_root",
                    audit_dir=root,
                    run_repro=True,
                    task_review_callback=callback,
                )

            self.assertTrue(
                all(
                    "timeout" not in call.kwargs
                    for call in run_session.call_args_list
                )
            )
            self.assertEqual(callback_calls, [1, 2])
            self.assertEqual(result["task_verification"]["outcome"], "reproduced")
            self.assertEqual(result["writer_session_count"], 2)
    def test_non_material_difference_does_not_reopen_writer(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            record = _delivery("task_1", root / "sandbox")

            def callback(index, task, record, round_no):
                return {
                    "ok": True,
                    "task_id": "task_1",
                    "task_verification": {
                        "schema_version": "2.0",
                        "task_id": "task_1",
                        "outcome": "reproduced",
                        "host_action": "complete",
                        "rerun_reason": "none",
                        "run_valid": True,
                        "key_numeric_comparisons": [{
                            "target_id": "anchor",
                            "paper_magnitude": 1.0,
                            "local_magnitude": 5.0,
                            "symmetric_ratio": 5.0,
                        }],
                        "comparison_summary": "curve differs by factor 5",
                        "non_material_differences": ["curve position differs by factor 5"],
                    },
                }

            with patch("geng_agent.task_writer_runner._prepare_task_writer_sandbox"), patch(
                "geng_agent.task_writer_runner._build_task_writer_brief", return_value="base"
            ), patch(
                "geng_agent.task_writer_runner._run_task_writer_codex_session", return_value={"ok": True}
            ) as run_session, patch(
                "geng_agent.task_writer_runner._restore_trusted_files"
            ), patch(
                "geng_agent.task_writer_runner._collect_task_writer_delivery", return_value=record
            ), patch(
                "geng_agent.task_writer_runner._archive_nonterminal_writer_delivery"
            ) as archive_delivery:
                result = _run_one_task_writer(
                    index=1,
                    reuse_existing=False,
                    task={"task_id": "task_1"},
                    manifest_entry={"task_id": "task_1", "module": "task_1", "output_subdir": "task_1"},
                    facts={},
                    experiment_index={},
                    paper={},
                    paper_path=root / "paper.pdf",
                    paper_context_json="",
                    paper_images=[],
                    paper_thesis=None,
                    analysis_snapshot_hash="snapshot",
                    analysis_artifacts={},
                    task_root=root / "task_root",
                    audit_dir=root,
                    run_repro=True,
                    task_review_callback=callback,
                )

            self.assertEqual(run_session.call_count, 1)
            archive_delivery.assert_not_called()
            self.assertEqual(result["task_verification"]["host_action"], "complete")
            self.assertEqual(result["task_verification"]["outcome"], "reproduced")
            self.assertNotIn("writer_error_kind", result)
    def test_resume_reviews_existing_terminal_delivery_without_rerunning_writer(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            existing = _delivery("task_1", root / "task_root" / "01_task_1")
            callback_calls = []

            def callback(index, task, record, round_no):
                callback_calls.append((index, task["task_id"], record["task_id"], round_no))
                return {
                    "ok": True,
                    "task_id": "task_1",
                    "task_verification": {
                        "schema_version": "2.0",
                        "task_id": "task_1",
                        "outcome": "reproduced",
                        "host_action": "complete",
                        "rerun_reason": "none",
                        "run_valid": True,
                        "comparison_summary": "existing full result supports the paper conclusion",
                    },
                }

            with patch("geng_agent.task_writer_runner._prepare_task_writer_sandbox"), patch(
                "geng_agent.task_writer_runner._build_task_writer_brief", return_value="base"
            ), patch(
                "geng_agent.task_writer_runner._collect_task_writer_delivery", return_value=existing
            ), patch(
                "geng_agent.task_writer_runner._run_task_writer_codex_session"
            ) as run_writer, patch(
                "geng_agent.task_writer_runner._archive_nonterminal_writer_delivery"
            ) as archive_delivery:
                result = _run_one_task_writer(
                    index=1,
                    reuse_existing=True,
                    task={"task_id": "task_1"},
                    manifest_entry={"task_id": "task_1", "module": "task_1", "output_subdir": "task_1"},
                    facts={},
                    experiment_index={},
                    paper={},
                    paper_path=root / "paper.pdf",
                    paper_context_json="",
                    paper_images=[],
                    paper_thesis=None,
                    analysis_snapshot_hash="snapshot",
                    analysis_artifacts={},
                    task_root=root / "task_root",
                    audit_dir=root,
                    run_repro=True,
                    task_review_callback=callback,
                )

            self.assertEqual(result["task_verification"]["outcome"], "reproduced")
            self.assertEqual(callback_calls, [(1, "task_1", "task_1", 1)])
            run_writer.assert_not_called()
            archive_delivery.assert_not_called()

    def test_runtime_refresh_reruns_writer_before_reporter(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            sandbox = root / "task_root" / "01_task_1"
            existing = _delivery("task_1", sandbox)

            def callback(index, task, record, round_no):
                return {
                    "ok": True,
                    "task_id": "task_1",
                    "task_verification": {
                        "schema_version": "2.0",
                        "task_id": "task_1",
                        "outcome": "reproduced",
                        "host_action": "complete",
                        "rerun_reason": "none",
                        "run_valid": True,
                    },
                }

            with patch(
                "geng_agent.task_writer_runner._prepare_task_writer_sandbox"
            ) as prepare, patch(
                "geng_agent.task_writer_runner._build_task_writer_brief",
                return_value="base",
            ), patch(
                "geng_agent.task_writer_runner._run_task_writer_codex_session",
                return_value={"ok": True},
            ) as run_writer, patch(
                "geng_agent.task_writer_runner._restore_trusted_files"
            ), patch(
                "geng_agent.task_writer_runner._collect_task_writer_delivery",
                return_value=existing,
            ), patch(
                "geng_agent.task_writer_runner._archive_nonterminal_writer_delivery"
            ) as archive_delivery:
                result = _run_one_task_writer(
                    index=1,
                    reuse_existing=True,
                    runtime_refresh_required=True,
                    task={"task_id": "task_1"},
                    manifest_entry={
                        "task_id": "task_1",
                        "module": "task_1",
                        "output_subdir": "task_1",
                    },
                    facts={},
                    experiment_index={},
                    paper={},
                    paper_path=root / "paper.pdf",
                    paper_context_json="",
                    paper_images=[],
                    paper_thesis=None,
                    analysis_snapshot_hash="current-snapshot",
                    analysis_artifacts={},
                    task_root=root / "task_root",
                    audit_dir=root,
                    run_repro=True,
                    task_review_callback=callback,
                )

            prepare.assert_called_once()
            archive_delivery.assert_called_once()
            run_writer.assert_called_once()
            self.assertTrue(result["runtime_refresh_completed"])
            self.assertFalse(
                (sandbox / ".geng_runtime_refresh_pending.json").exists()
            )

    def test_resume_accepts_existing_writer_only_delivery_without_rerunning_writer(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            existing = _delivery('task_1', root / 'task_root' / '01_task_1')
            with patch('geng_agent.task_writer_runner._prepare_task_writer_sandbox'), patch(
                'geng_agent.task_writer_runner._build_task_writer_brief', return_value='base'
            ), patch(
                'geng_agent.task_writer_runner._collect_task_writer_delivery', return_value=existing
            ), patch(
                'geng_agent.task_writer_runner._run_task_writer_codex_session'
            ) as run_writer, patch(
                'geng_agent.task_writer_runner._archive_nonterminal_writer_delivery'
            ) as archive_delivery:
                result = _run_one_task_writer(
                    index=1, reuse_existing=True, task={'task_id': 'task_1'},
                    manifest_entry={'task_id': 'task_1', 'module': 'task_1', 'output_subdir': 'task_1'},
                    facts={}, experiment_index={}, paper={}, paper_path=root / 'paper.pdf', paper_context_json='',
                    paper_images=[], paper_thesis=None, analysis_snapshot_hash='snapshot',
                    analysis_artifacts={}, task_root=root / 'task_root', audit_dir=root, run_repro=True,
                    task_review_callback=None,
                )

            self.assertEqual(result['task_writer_status'], 'ready_for_review')
            run_writer.assert_not_called()
            archive_delivery.assert_not_called()

    def test_fresh_workflow_preserves_foundation_snapshot_before_dispatch(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "case"
            audit = output / "audit"
            snapshot = audit / "03b_foundation_snapshot"
            source = snapshot / "src" / "channel.py"
            source.parent.mkdir(parents=True)
            source.write_text("VALUE = 1\n", encoding="utf-8")
            paper = output / "paper.pdf"
            paper.parent.mkdir(parents=True, exist_ok=True)
            paper.write_bytes(b"paper")
            tasks = {"repro_tasks": [{"task_id": "task_1", "figure_or_claim": "Fig. 1"}]}
            write_json(output / "engineering_facts.json", {"engineering_facts": []})
            write_json(output / "repro_tasks.json", tasks)
            write_json(output / "experiment_index.json", {"experiments": []})
            digest = __import__("hashlib").sha256(source.read_bytes()).hexdigest()
            files = [{"path": "src/channel.py", "sha256": digest, "bytes": source.stat().st_size}]
            snapshot_hash = foundation_snapshot_hash(files)
            manifest = {
                "schema_version": "1.0",
                "workflow_version": "2",
                "contract_version": "1",
                "input_hash": "a" * 64,
                "analysis_snapshot_hash": "b" * 64,
                "snapshot_hash": snapshot_hash,
                "files": files,
                "frozen_files": files,
                "required_modules": ["src/channel.py"],
                "validation": {"tests_passed": True, "local_imports_resolve": True},
            }
            foundation = {
                "snapshot_dir": str(snapshot),
                "snapshot_hash": snapshot_hash,
                "manifest": manifest,
            }

            class StopAfterDispatchProbe(RuntimeError):
                pass

            def probe_dispatch(**kwargs):
                self.assertTrue(source.is_file(), "manifest cleanup deleted the 03b Foundation snapshot")
                from geng_agent.agentic_foundation import install_foundation_snapshot

                probe = root / "probe_sandbox"
                installed = install_foundation_snapshot(probe, kwargs["foundation"])
                self.assertEqual(installed, {"src/channel.py"})
                self.assertEqual((probe / "src" / "channel.py").read_bytes(), source.read_bytes())
                raise StopAfterDispatchProbe

            with patch(
                "geng_agent.agentic_task_writers._dispatch_task_writers",
                side_effect=probe_dispatch,
            ):
                with self.assertRaises(StopAfterDispatchProbe):
                    run_codex_task_writer_workflow(
                        facts={"engineering_facts": []}, tasks=tasks,
                        experiment_index={"experiments": []}, paper={"chunks": []},
                        paper_path=paper, paper_context_json="", paper_images=[], paper_thesis=None,
                        output_dir=output, audit_dir=audit, repro_project_dir=output / "repro_project",
                        run_repro=True, resume=False, foundation=foundation,
                    )


if __name__ == "__main__":
    unittest.main()
