from __future__ import annotations

from contextlib import ExitStack
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import geng_agent.agentic_task_writers as writers
from geng_agent.verification_result import (
    normalize_task_verification,
    rerun_evidence_path_issues,
    writer_revision_allowed,
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
    def test_made_up_contract_id_cannot_authorize_rerun(self) -> None:
        result = normalize_task_verification(
            _rerun_note(claim_id="claim.made_up"),
            "task_a",
            task=_task(),
            run_valid_hint=True,
        )
        self.assertEqual(result["core_conclusions"][0]["claim_id"], "claim.real")
        self.assertEqual(result["host_action"], "complete")
        self.assertFalse(writer_revision_allowed(result, "task_a"))

    def test_reporter_can_mark_rc_zero_output_invalid(self) -> None:
        result = normalize_task_verification(
            _rerun_note(reason="invalid_run"),
            "task_a",
            task=_task(),
            run_valid_hint=True,
        )
        self.assertFalse(result["run_valid"])
        self.assertEqual(result["host_action"], "rerun_writer")
        self.assertTrue(writer_revision_allowed(result, "task_a"))

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

    def test_untrusted_rerun_path_becomes_terminal(self) -> None:
        with TemporaryDirectory() as temp:
            workspace = Path(temp)
            (workspace / "paper_evidence").mkdir()
            verification = normalize_task_verification(
                _rerun_note(), "task_a", task=_task(), run_valid_hint=True
            )
            record = _writer_record()
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
            self.assertEqual(record["task_verification"]["host_action"], "complete")
            self.assertEqual(record["scientific_stop_reason"], "untrusted_rerun_paper_evidence")

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

    def test_rerun_fingerprint_is_order_and_case_stable(self) -> None:
        first = {
            "rerun_reason": "core_conclusion_failed",
            "contract_item_ids": ["B", "a"],
            "change_targets": ["Y.py", "x.py"],
            "causal_change": "  Fix   Data Path ",
            "predicted_effect": "Finite Output",
        }
        second = {
            "rerun_reason": "core_conclusion_failed",
            "contract_item_ids": ["A", "b"],
            "change_targets": ["x.PY", "y.PY"],
            "causal_change": "use completely different wording",
            "predicted_effect": "describe the same target with different prose",
        }
        self.assertEqual(
            writers._rerun_evidence_fingerprint(first),
            writers._rerun_evidence_fingerprint(second),
        )

    def test_writer_emergency_cap_stops_unique_rerun_requests(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            base_record = _writer_record()
            attach_calls = 0

            def attach(**_kwargs):
                nonlocal attach_calls
                attach_calls += 1
                evidence = {
                    "rerun_reason": "core_conclusion_failed",
                    "contract_item_ids": [f"claim.{attach_calls}"],
                    "paper_evidence_files": ["paper_evidence/source/paper.pdf"],
                    "causal_change": f"change {attach_calls}",
                    "change_targets": [f"tasks/change_{attach_calls}.py"],
                    "predicted_effect": "improve",
                }
                return "writer_revision", {
                    "task_id": "task_a",
                    "run_valid": True,
                    "host_action": "rerun_writer",
                    "rerun_reason": "core_conclusion_failed",
                    "rerun_evidence": evidence,
                    "remaining_uncertainties": [],
                }

            run_mock = unittest.mock.Mock(return_value={"ok": True})
            with ExitStack() as stack:
                stack.enter_context(patch.object(writers, "_prepare_task_writer_sandbox"))
                stack.enter_context(patch.object(writers, "_load_task_execution_binding", return_value=None))
                stack.enter_context(patch.object(writers, "_build_task_writer_brief", return_value="prompt"))
                stack.enter_context(patch.object(writers, "_run_task_writer_codex_session", run_mock))
                stack.enter_context(patch.object(writers, "_restore_trusted_files"))
                stack.enter_context(patch.object(writers, "_collect_task_writer_delivery", side_effect=lambda **_kwargs: dict(base_record)))
                stack.enter_context(patch.object(writers, "_attach_task_reporter_review", side_effect=attach))
                stack.enter_context(patch.object(writers, "_archive_nonterminal_writer_delivery"))
                state_no = iter(range(100))
                stack.enter_context(patch.object(
                    writers,
                    "_record_source_config_fingerprint",
                    side_effect=lambda *_args: f"state-{next(state_no)}",
                ))
                result = writers._run_one_task_writer(
                    index=1,
                    reuse_existing=False,
                    task=_task(),
                    manifest_entry={"task_id": "task_a", "module": "task_a", "output_subdir": "task_a"},
                    facts={},
                    experiment_index={},
                    paper={},
                    paper_path=root / "paper.pdf",
                    paper_context_json="{}",
                    paper_images=[],
                    paper_thesis={},
                    analysis_snapshot_hash="hash",
                    analysis_artifacts={},
                    task_root=root / "sandboxes",
                    audit_dir=root / "audit",
                    run_repro=True,
                    task_review_callback=lambda *_args: {},
                )
            self.assertEqual(run_mock.call_count, writers.DEFAULT_MAX_EVIDENCE_RERUNS + 1)
            self.assertEqual(
                result["scientific_stop_reason"],
                "external_rerun_budget_exhausted",
            )

    def test_writer_rerun_budget_has_finite_default_and_configuration_override(self) -> None:
        with patch.dict(
            os.environ,
            {"GENG_TASK_WRITER_MAX_EVIDENCE_RERUNS": ""},
        ):
            self.assertEqual(writers._external_writer_rerun_budget(), writers.DEFAULT_MAX_EVIDENCE_RERUNS)
        with patch.dict(
            os.environ,
            {"GENG_TASK_WRITER_MAX_EVIDENCE_RERUNS": "2"},
        ):
            self.assertEqual(writers._external_writer_rerun_budget(), 2)

    def test_unchanged_writer_continuation_stops_before_second_reporter_call(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            base_record = _writer_record()
            verification = {
                "task_id": "task_a",
                "run_valid": True,
                "host_action": "rerun_writer",
                "rerun_reason": "core_conclusion_failed",
                "rerun_evidence": _rerun_note()["rerun_evidence"],
                "remaining_uncertainties": [],
            }
            run_mock = unittest.mock.Mock(return_value={"ok": True})
            attach_mock = unittest.mock.Mock(
                return_value=("writer_revision", verification)
            )
            with ExitStack() as stack:
                stack.enter_context(patch.object(writers, "_prepare_task_writer_sandbox"))
                stack.enter_context(patch.object(writers, "_load_task_execution_binding", return_value=None))
                stack.enter_context(patch.object(writers, "_build_task_writer_brief", return_value="prompt"))
                stack.enter_context(patch.object(writers, "_run_task_writer_codex_session", run_mock))
                stack.enter_context(patch.object(writers, "_restore_trusted_files"))
                stack.enter_context(patch.object(
                    writers,
                    "_collect_task_writer_delivery",
                    side_effect=lambda **_kwargs: dict(base_record),
                ))
                stack.enter_context(patch.object(writers, "_attach_task_reporter_review", attach_mock))
                stack.enter_context(patch.object(writers, "_archive_nonterminal_writer_delivery"))
                stack.enter_context(patch.object(
                    writers,
                    "_record_source_config_fingerprint",
                    return_value="unchanged",
                ))
                result = writers._run_one_task_writer(
                    index=1,
                    reuse_existing=False,
                    task=_task(),
                    manifest_entry={"task_id": "task_a", "module": "task_a", "output_subdir": "task_a"},
                    facts={},
                    experiment_index={},
                    paper={},
                    paper_path=root / "paper.pdf",
                    paper_context_json="{}",
                    paper_images=[],
                    paper_thesis={},
                    analysis_snapshot_hash="hash",
                    analysis_artifacts={},
                    task_root=root / "sandboxes",
                    audit_dir=root / "audit",
                    run_repro=True,
                    task_review_callback=lambda *_args: {},
                )

            self.assertEqual(run_mock.call_count, 2)
            self.assertEqual(attach_mock.call_count, 1)
            self.assertEqual(
                result["scientific_stop_reason"],
                "writer_continuation_without_source_change",
            )


if __name__ == "__main__":
    unittest.main()
