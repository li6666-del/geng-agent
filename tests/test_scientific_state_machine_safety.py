from __future__ import annotations

from contextlib import ExitStack
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import geng_agent.agentic_task_writers as writers
import geng_agent.task_writer_runner as writer_runner
from geng_agent.verification_result import (
    normalize_task_verification,
    rerun_evidence_path_issues,
    task_verification_issues,
)


def _task() -> dict:
    return {
        "task_id": "task_a",
        "scientific_acceptance": {
            "core_conclusions": [{"claim_id": "claim.real", "statement": "trend"}],
            "key_numeric_targets": [],
            "information_gaps": [],
        },
    }


def _rerun_note(*, claim_id: str = "claim.real", reason: str = "core_conclusion_failed") -> dict:
    status = "unsupported" if reason == "core_conclusion_failed" else "unassessable_missing_information"
    return {
        "schema_version": "3.0", "task_id": "task_a", "host_action": "rerun_writer",
        "outcome": "not_reproduced" if reason != "invalid_run" else "execution_failed",
        "decision_reason": "The recorded observation requires the proposed correction.",
        "run_valid": reason != "invalid_run",
        "core_conclusions": [{"claim_id": claim_id, "status": status}],
        "rerun_evidence": {
            "rerun_reason": reason,
            "contract_item_ids": [claim_id],
            "paper_evidence_files": ["paper_evidence/source/paper.pdf"],
            "causal_change": "repair the data path",
            "change_targets": ["tasks/task_a.py"],
            "predicted_effect": "produce a valid supported curve",
        },
    }


def _writer_record() -> dict:
    return {
        "task_id": "task_a",
        "task_writer_status": "ready_for_review",
        "writer_completed": True,
        "execution_summary": {"full_run_count": 1, "last_returncode": 0},
        "delivery_warnings": [],
    }


class ScientificStateMachineSafetyTests(unittest.TestCase):
    def test_unknown_contract_id_is_observed_without_changing_requested_rerun(self) -> None:
        result = normalize_task_verification(
            _rerun_note(claim_id="claim.made_up"),
            "task_a",
            task=_task(),
            run_valid_hint=True,
        )
        self.assertEqual(result["core_conclusions"][0]["claim_id"], "claim.made_up")
        self.assertEqual(result["engineering_status"], "verified")
        self.assertTrue(result["host_observations"])
        self.assertEqual(result["host_action"], "rerun_writer")
        self.assertTrue((not task_verification_issues(result, "task_a") and result.get("host_action") == "rerun_writer"))

    def test_reporter_can_mark_rc_zero_output_invalid(self) -> None:
        result = normalize_task_verification(
            _rerun_note(reason="invalid_run"),
            "task_a",
            task=_task(),
            run_valid_hint=True,
        )
        self.assertFalse(result["run_valid"])
        self.assertEqual(result["host_action"], "rerun_writer")
        self.assertTrue((not task_verification_issues(result, "task_a") and result.get("host_action") == "rerun_writer"))

    def test_rerun_paper_evidence_must_exist_under_trusted_root(self) -> None:
        with TemporaryDirectory() as temp:
            workspace = Path(temp)
            trusted = workspace / "paper_evidence" / "source"
            trusted.mkdir(parents=True)
            paper = trusted / "paper.pdf"
            paper.write_bytes(b"paper")
            result = {"rerun_evidence": {"paper_evidence_files": ["paper_evidence/source/paper.pdf"]}}
            self.assertEqual(rerun_evidence_path_issues(result, workspace), [])
            result["rerun_evidence"]["paper_evidence_files"] = ["paper_evidence/source/missing.pdf"]
            self.assertTrue(rerun_evidence_path_issues(result, workspace))
            result["rerun_evidence"]["paper_evidence_files"] = ["outside.pdf"]
            self.assertTrue(rerun_evidence_path_issues(result, workspace))

    def test_reporter_exception_preserves_valid_writer_record(self) -> None:
        record = _writer_record()

        def fail(*_args):
            raise ValueError("reporter crashed")

        action, feedback = writers._attach_task_reporter_review(
            callback=fail,
            index=1,
            task=_task(),
            record=record,
            session_round=1,
        )
        self.assertEqual(action, "failed")
        self.assertIsNone(feedback)
        self.assertEqual(record["task_writer_status"], "ready_for_review")
        self.assertTrue(record["writer_completed"])
        self.assertEqual(record["execution_summary"]["last_returncode"], 0)
        self.assertEqual(record["task_reporter_error_kind"], "task_reporter_callback_failed")

    def test_missing_rerun_evidence_is_observed_without_rewriting_request(self) -> None:
        with TemporaryDirectory() as temp:
            workspace = Path(temp)
            (workspace / "paper_evidence").mkdir()
            note = _rerun_note()
            note["core_conclusions"][0]["local_observation"] = (
                "The submitted CSV reverses the paper's method ordering."
            )
            verification = normalize_task_verification(
                note, "task_a", task=_task(), run_valid_hint=True
            )
            record = _writer_record()
            record["sandbox"] = str(workspace)
            with patch("geng_agent.task_recovery.request_moderation", return_value={
                "action": "stop", "instructions": "The source evidence is unavailable.",
            }) as moderate:
                action, _ = writers._attach_task_reporter_review(
                    callback=lambda *_args: {
                        "ok": True,
                        "workspace": str(workspace),
                        "task_verification": verification,
                    },
                    index=1,
                    task=_task(),
                    record=record,
                    session_round=1,
                )
            self.assertEqual(action, "terminal")
            self.assertEqual(record["task_verification"]["host_action"], "rerun_writer")
            self.assertEqual(record["coordination_status"], "stopped")
            self.assertEqual(record["scientific_stop_reason"], "moderator_stopped_revision")
            self.assertEqual(moderate.call_args.kwargs["context"]["reporter_verification"], verification)

    def test_missing_sandbox_does_not_read_caller_working_directory(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            caller = root / "caller"
            (caller / "tasks").mkdir(parents=True)
            (caller / "tasks" / "leak.py").write_text("LEAK = True\n", encoding="utf-8")
            repro = root / "repro"
            old_cwd = Path.cwd()
            os.chdir(caller)
            try:
                writers._merge_task_writer_deliveries(
                    repro_project_dir=repro,
                    task_manifest={"tasks": []},
                    expected_paths=set(),
                    task_records=[{"task_id": "failed", "module": "failed"}],
                )
            finally:
                os.chdir(old_cwd)
            self.assertFalse((repro / "tasks" / "leak.py").exists())






if __name__ == "__main__":
    unittest.main()
