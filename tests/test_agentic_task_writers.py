import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from geng_agent.agentic_task_writers import (
    _dispatch_task_writers,
    _record_is_valid_current_delivery,
    _run_one_task_writer,
    _task_writer_concurrency,
    apply_verified_result,
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
                    paper_context_json="", paper_images=[], paper_thesis=None, paper_memory=None,
                    memory_snapshot_hash="", analysis_artifacts={}, task_root=root / "sandboxes", audit_dir=root,
                    run_repro=True, initial_records_by_index=existing,
                    review_feedback={"revise": {"differences": ["curve differs"]}}, force_task_ids={"revise"})
            self.assertEqual([item["task_id"] for item in records], ["accepted", "revise"])
            self.assertEqual([call["task"]["task_id"] for call in calls], ["revise"])
            self.assertEqual(calls[0]["review_feedback"]["differences"], ["curve differs"])
            self.assertEqual(audit["reused_task_ids"], ["accepted"])

    def test_delivery_validation_and_direct_verification_grant_matched(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            record = _delivery("task_1", root / "sandbox")
            self.assertTrue(_record_is_valid_current_delivery(record))
            output = root / "case"; audit = output / "audit"; repro = output / "repro_project"
            audit.mkdir(parents=True); repro.mkdir(); (repro / "config.json").write_text("{}", encoding="utf-8")
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
            ), patch(
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
                    paper_images=[], paper_thesis=None, paper_memory=None, memory_snapshot_hash="",
                    analysis_artifacts={}, task_root=root / "task_root", audit_dir=root, run_repro=True,
                    task_review_callback=callback,
                )
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
                    paper_images=[], paper_thesis=None, paper_memory=None, memory_snapshot_hash="",
                    analysis_artifacts={}, task_root=root / "task_root", audit_dir=root, run_repro=True,
                    task_review_callback=callback,
                )

            self.assertEqual(result["task_verification"]["verdict"], "accepted")
            self.assertEqual(callback_calls, [(1, "task_1", "task_1", 1)])
            run_writer.assert_not_called()
            archive_delivery.assert_not_called()


if __name__ == "__main__":
    unittest.main()
