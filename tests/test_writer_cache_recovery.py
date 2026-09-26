"""Cache ACL failures must not discard a verified full or relax link checks."""
from contextlib import contextmanager
import errno
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from geng_agent import task_writer_runner as runner
from geng_agent.supervisor import RunSupervisor, supervisor_scope












def coordinate(root, callback):
    coordinator = RunSupervisor(root, root / "audit", {"task": "tiny"})
    coordinator._request = callback
    return coordinator


def retry(node_id, **kwargs):
    if kwargs["trigger"] == "node_failed":
        return {"action": "retry", "decision_id": "repair-cache", "diagnosis": "inspect cache",
                "instructions": "Repair the non-scientific cache issue; retain current results",
                "expected_change": "The same full receipt remains valid"}
    return {"action": "approve", "diagnosis": "verified"}


@pytest.mark.parametrize("all_valid", [True, False])
def test_handoff_preserves_outputs_without_host_receipt_approval(tmp_path, monkeypatch, all_valid):
    coordinator = coordinate(tmp_path, retry)
    archive = Mock()
    checked = []
    def check(_root, _audit, task_id):
        checked.append(task_id)
        return {"passed": all_valid or task_id == "a", "run_id": "full-" + task_id}
    monkeypatch.setattr("geng_agent.execution_receipts.find_host_execution", check)
    requests = []
    def produce(instructions, attempt):
        requests.append(instructions)
        return ({"ok": True, "execution_receipts_required": True, "execution_audit_dir": str(tmp_path / "audit")},
                [{"task_id": task_id, "writer_completed": attempt > 1,
                  "host_execution": {"passed": attempt > 1}} for task_id in ("a", "b")])
    with supervisor_scope(coordinator):
        runner._supervise_writer_delivery(node_id="writer:unit:round:1", produce=produce, archive=archive,
            tasks=[{"task_id": "a"}, {"task_id": "b"}], sandbox=tmp_path, analysis_snapshot_hash="a")
    assert checked == []
    archive.assert_not_called()
    assert requests == [None]


def test_changed_input_observation_reaches_reporter_without_host_block(tmp_path, monkeypatch):
    calls = 0
    def decisions(node_id, **kwargs):
        nonlocal calls
        calls += 1
        return retry(node_id, **kwargs) if calls == 1 else {"action": "block", "diagnosis": "new full required"}
    coordinator = coordinate(tmp_path, decisions)
    monkeypatch.setattr("geng_agent.execution_receipts.find_host_execution", Mock(return_value={"passed": True, "run_id": "old-full"}))
    archive = Mock()
    def produce(_instructions, attempt):
        return ({"execution_receipts_required": True, "execution_audit_dir": str(tmp_path / "audit")},
                [{"task_id": "t", "writer_completed": attempt > 1, "host_execution": {
                    "passed": False, "issues": ["source or configuration changed after execution"]}}])
    with supervisor_scope(coordinator):
        _status, records = runner._supervise_writer_delivery(node_id="writer:t:round:1", produce=produce,
            archive=archive, tasks=[{"task_id": "t"}], sandbox=tmp_path, analysis_snapshot_hash="a")
    archive.assert_not_called()
    assert records[0]["host_execution"]["passed"] is False
    assert not records[0].get("supervisor_blocked")
    assert calls == 0  # The anomaly is handed to Reporter without host approval.


@pytest.mark.parametrize("resume", [False, True])
def test_verified_mechanical_recovery_does_not_reopen_writer(tmp_path, monkeypatch, resume):
    coordinator = coordinate(tmp_path, retry)
    monkeypatch.setattr("geng_agent.execution_receipts.find_host_execution", Mock(return_value={"passed": True, "run_id": "same-full"}))
    archive = Mock()
    attempts = []
    def produce(instructions, attempt):
        attempts.append(attempt)
        status = {"execution_receipts_required": True, "execution_audit_dir": str(tmp_path / "audit")}
        if attempt:
            status["error_kind"] = "sandbox_inspection_failed"
        return (status, [{"task_id": "t", "writer_completed": attempt == 0,
            "host_execution": {"passed": True, "run_id": "same-full"},
            "result_json": {"status": "done", "acceptance_checklist": ["preserved"]}}])
    with supervisor_scope(coordinator):
        status, records = runner._supervise_writer_delivery(node_id="writer:t:round:1", produce=produce,
            archive=archive, tasks=[{"task_id": "t"}], sandbox=tmp_path, analysis_snapshot_hash="a",
            reconcile_first=resume)
    assert attempts == ([0] if resume else [1])
    archive.assert_not_called()
    assert records[0]["host_execution"]["run_id"] == "same-full"
    assert records[0]["result_json"]["acceptance_checklist"] == ["preserved"]
    assert status.get("error_kind") == (None if resume else "sandbox_inspection_failed")
