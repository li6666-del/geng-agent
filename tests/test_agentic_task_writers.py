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
    _dispatch_task_writers,
    _record_is_valid_current_delivery,
    _run_one_task_writer,
    _task_writer_concurrency,
    run_codex_task_writer_workflow,
    apply_verified_result,
)

from geng_agent.task_writer_support import (
    _analysis_snapshot_hash,
    _collect_writer_analysis_artifacts,
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


class AutonomousTaskWriterTests(unittest.TestCase):
    def test_initial_writer_prompt_makes_paper_fidelity_the_highest_law(self) -> None:
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
        self.assertIn("the core claim is supported", prompt)
        self.assertNotIn("A trend-only, ordering-only, or merely visually similar result is not enough", prompt)

    def test_repair_prompt_rechecks_reporter_feedback_against_paper_evidence(self) -> None:
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
        self.assertIn("If a suggestion conflicts with explicit paper evidence", prompt)

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
            self.assertFalse(_record_is_valid_current_delivery(record))
    def test_concurrency_equals_task_count(self) -> None:
        self.assertEqual(_task_writer_concurrency(7, 1, run_repro=True), 7)

    def test_dispatch_launches_all_and_relaunches_only_revised_task(self) -> None:
        pairs = [_pair("accepted"), _pair("revise")]
        with TemporaryDirectory() as temp:
            root = Path(temp)
            existing = {1: _delivery("accepted", root / "a"), 2: _delivery("revise", root / "b")}
            calls = []
            def fake_writer(**kwargs):
                calls.append(kwargs)
                return _delivery(kwargs["task"]["task_id"], root / (kwargs["task"]["task_id"] + "2"))
            with patch("geng_agent.agentic_task_writers._run_one_task_writer", side_effect=fake_writer):
                records, audit = _dispatch_task_writers(
                    task_pairs=pairs, facts={}, experiment_index={}, paper={}, paper_path=root / "paper.pdf",
                    paper_context_json="", paper_images=[], paper_thesis=None,
                    analysis_snapshot_hash="snapshot", analysis_artifacts={}, task_root=root / "sandboxes", audit_dir=root,
                    run_repro=True, timeout=321, initial_records_by_index=existing,
                    review_feedback={"revise": {"differences": ["curve differs"]}}, force_task_ids={"revise"})
            self.assertEqual([item["task_id"] for item in records], ["accepted", "revise"])
            self.assertEqual([call["task"]["task_id"] for call in calls], ["revise"])
            self.assertEqual(calls[0]["review_feedback"]["differences"], ["curve differs"])
            self.assertEqual(calls[0]["timeout"], 321)
            self.assertEqual(audit["session_timeout_s"], 321.0)
            self.assertIsNone(audit["overall_runtime_limit_s"])
            self.assertEqual(audit["reused_task_ids"], ["accepted"])

    def test_delivery_validation_and_direct_verification_grant_matched(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            record = _delivery("task_1", root / "sandbox")
            self.assertTrue(_record_is_valid_current_delivery(record))
            output = root / "case"; audit = output / "audit"; repro = output / "repro_project"
            audit.mkdir(parents=True); repro.mkdir(); (repro / "config.json").write_text("{}", encoding="utf-8")
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
            runtime = apply_verified_result(
                task_records=[record],
                verification_result={"schema_version": "1.0", "all_accepted": True, "tasks": [{
                    "task_id": "task_1", "verdict": "accepted", "comparison_summary": "direct match",
                    "differences": [], "evidence_files": ["paper.png", "local.png"], "feedback": [],
                    "confidence": "high"}]},
                output_dir=output, audit_dir=audit, repro_project_dir=repro)
            self.assertTrue(runtime["passed"])
            self.assertEqual(record["task_writer_status"], "matched")
            self.assertTrue(record["verification_verified"])

    def test_one_writer_continues_only_its_own_loop_after_task_reporter_revision(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            records = [
                _delivery("task_1", root / "sandbox"),
                _delivery("task_1", root / "sandbox"),
            ]
            callback_calls = []

            def callback(index, task, record, round_no):
                callback_calls.append(round_no)
                verdict = "revise" if len(callback_calls) == 1 else "accepted"
                return {
                    "ok": True,
                    "task_id": "task_1",
                    "task_verification": {
                        "task_id": "task_1", "verdict": verdict,
                        "revision_target": "writer" if verdict == "revise" else "none",
                        "comparison_summary": "comparison",
                        "differences": ["adjust parameter"] if verdict == "revise" else [],
                        "evidence_files": ["evidence.png"],
                        "feedback": ["change parameter"] if verdict == "revise" else [],
                        "confidence": "high",
                    },
                }

            with patch("geng_agent.agentic_task_writers._prepare_task_writer_sandbox"), patch(
                "geng_agent.agentic_task_writers._build_task_writer_brief", return_value="base"
            ), patch(
                "geng_agent.agentic_task_writers._run_task_writer_codex_session", return_value={"ok": True}
            ) as run_session, patch(
                "geng_agent.agentic_task_writers._restore_trusted_files"
            ), patch(
                "geng_agent.agentic_task_writers._collect_task_writer_delivery", side_effect=records
            ), patch(
                "geng_agent.agentic_task_writers._archive_nonterminal_writer_delivery"
            ):
                result = _run_one_task_writer(
                    index=1, reuse_existing=False, task={"task_id": "task_1"},
                    manifest_entry={"task_id": "task_1", "module": "task_1", "output_subdir": "task_1"},
                    facts={}, experiment_index={}, paper={}, paper_path=root / "paper.pdf", paper_context_json="",
                    paper_images=[], paper_thesis=None, analysis_snapshot_hash="snapshot",
                    analysis_artifacts={}, task_root=root / "task_root", audit_dir=root, run_repro=True,
                    timeout=456, task_review_callback=callback,
                )
            self.assertEqual([call.kwargs["timeout"] for call in run_session.call_args_list], [456, 456])
            self.assertEqual(callback_calls, [1, 2])
            self.assertEqual(result["task_verification"]["verdict"], "accepted")
            self.assertEqual(result["writer_session_count"], 2)

    def test_resume_reviews_existing_delivery_without_rerunning_writer(self) -> None:
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
                        "task_id": "task_1",
                        "verdict": "accepted",
                        "revision_target": "none",
                        "comparison_summary": "existing full result matches the paper",
                        "differences": [],
                        "evidence_files": ["paper.png", "local.png"],
                        "feedback": [],
                        "confidence": "high",
                    },
                }

            with patch("geng_agent.agentic_task_writers._prepare_task_writer_sandbox"), patch(
                "geng_agent.agentic_task_writers._build_task_writer_brief", return_value="base"
            ), patch(
                "geng_agent.agentic_task_writers._collect_task_writer_delivery", return_value=existing
            ), patch(
                "geng_agent.agentic_task_writers._run_task_writer_codex_session"
            ) as run_writer, patch(
                "geng_agent.agentic_task_writers._archive_nonterminal_writer_delivery"
            ) as archive_delivery:
                result = _run_one_task_writer(
                    index=1, reuse_existing=True, task={"task_id": "task_1"},
                    manifest_entry={"task_id": "task_1", "module": "task_1", "output_subdir": "task_1"},
                    facts={}, experiment_index={}, paper={}, paper_path=root / "paper.pdf", paper_context_json="",
                    paper_images=[], paper_thesis=None, analysis_snapshot_hash="snapshot",
                    analysis_artifacts={}, task_root=root / "task_root", audit_dir=root, run_repro=True,
                    task_review_callback=callback,
                )

            self.assertEqual(result["task_verification"]["verdict"], "accepted")
            self.assertEqual(callback_calls, [(1, "task_1", "task_1", 1)])
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
