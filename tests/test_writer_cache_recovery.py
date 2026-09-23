"""Cache ACL failures must not discard a verified full or relax link checks."""
from contextlib import contextmanager
import errno
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from geng_agent import foundation_snapshot as snapshot
from geng_agent import foundation_snapshot_delivery as foundation
from geng_agent import task_writer_runner as runner
from geng_agent.supervisor import RunSupervisor, supervisor_scope


@pytest.mark.parametrize("name", [".pytest_cache", "__pycache__"])
def test_unreadable_regular_cache_is_not_traversed(tmp_path, monkeypatch, name):
    cache = tmp_path / name
    cache.mkdir()
    (tmp_path / "science.py").write_text("VALUE = 1\n", encoding="utf-8")
    original = snapshot.os.scandir
    visited = []
    def scandir(path):
        visited.append(Path(path))
        if Path(path) == cache:
            raise PermissionError(errno.EACCES, "cache ACL denies enumeration", str(cache))
        return original(path)
    monkeypatch.setattr(snapshot.os, "scandir", scandir)
    foundation._assert_foundation_sandbox_layout_safe(tmp_path)
    assert cache not in visited
    assert tmp_path in visited


@pytest.mark.parametrize("name", [".pytest_cache", "__pycache__"])
def test_cache_named_reparse_point_still_fails_without_traversal(tmp_path, monkeypatch, name):
    cache = tmp_path / name
    cache.mkdir()
    original = snapshot.path_is_foundation_link
    monkeypatch.setattr(snapshot, "path_is_foundation_link", lambda path: Path(path) == cache or original(path))
    with pytest.raises(RuntimeError, match="link or reparse point"):
        foundation._assert_foundation_sandbox_layout_safe(tmp_path)


@pytest.mark.parametrize("name", [".pytest_cache", "__pycache__"])
def test_cache_named_nonregular_entry_still_fails(tmp_path, monkeypatch, name):
    cache = tmp_path / name
    cache.write_text("fixture placeholder for a FIFO", encoding="utf-8")
    entry = SimpleNamespace(path=str(cache), name=name,
        is_dir=lambda **_kwargs: False, is_file=lambda **_kwargs: False)
    @contextmanager
    def scan(_path):
        yield iter([entry])
    monkeypatch.setattr(snapshot.os, "scandir", scan)
    with pytest.raises(RuntimeError, match="non-regular entry"):
        foundation._assert_foundation_sandbox_layout_safe(tmp_path)


def test_unreadable_scientific_directory_still_fails(tmp_path, monkeypatch):
    source = tmp_path / "src"
    source.mkdir()
    original = snapshot.os.scandir
    def scan(path):
        if Path(path) == source:
            raise PermissionError(errno.EACCES, "source inaccessible", str(source))
        return original(path)
    monkeypatch.setattr(snapshot.os, "scandir", scan)
    with pytest.raises(PermissionError):
        foundation._assert_foundation_sandbox_layout_safe(tmp_path)


def test_inspection_failure_preserves_original_path_and_exception(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "run_codex_subprocess", Mock(return_value={"ok": True, "returncode": 0}))
    monkeypatch.setattr(runner, "ExecutionBroker", MagicMock())
    inaccessible = tmp_path / "src"
    error = PermissionError(errno.EACCES, "directory ACL denies access", str(inaccessible))
    monkeypatch.setattr(runner, "_assert_foundation_sandbox_layout_safe", Mock(side_effect=error))
    read_request = Mock(side_effect=AssertionError("must not read files after inspection failure"))
    monkeypatch.setattr(runner, "read_environment_request", read_request)
    result = runner._run_task_writer_codex_session(label="offline", prompt="fixture", sandbox=tmp_path,
                                                   audit_dir=tmp_path / "audit")
    assert result["error_kind"] == "sandbox_inspection_failed"
    assert result["inspection_error"]["type"] == "PermissionError"
    assert result["inspection_error"]["path"] == str(inaccessible)
    assert result["inspection_error"]["errno"] == errno.EACCES
    assert "PermissionError" in result["blocked_reason"]
    read_request.assert_not_called()


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
def test_repair_preserves_outputs_only_if_every_actual_host_receipt_is_valid(tmp_path, monkeypatch, all_valid):
    coordinator = coordinate(tmp_path, retry)
    archive = Mock()
    checked = []
    def check(_root, _audit, task_id):
        checked.append(task_id)
        return {"passed": all_valid or task_id == "a", "run_id": "full-" + task_id}
    monkeypatch.setattr(runner, "find_host_execution", check)
    requests = []
    def produce(instructions, attempt):
        requests.append(instructions)
        return ({"ok": True, "execution_receipts_required": True, "execution_audit_dir": str(tmp_path / "audit")},
                [{"task_id": task_id, "writer_completed": attempt > 1,
                  "host_execution": {"passed": attempt > 1}} for task_id in ("a", "b")])
    with supervisor_scope(coordinator):
        runner._supervise_writer_delivery(node_id="writer:unit:round:1", produce=produce, archive=archive,
            tasks=[{"task_id": "a"}, {"task_id": "b"}], sandbox=tmp_path, analysis_snapshot_hash="a")
    assert checked == ["a", "b"]
    assert archive.call_count == (0 if all_valid else 1)
    assert requests[1]["preserved_full_receipts"] == ({"a": "full-a", "b": "full-b"} if all_valid else {})


def test_changed_scientific_inputs_cannot_reuse_a_previously_valid_receipt(tmp_path, monkeypatch):
    calls = 0
    def decisions(node_id, **kwargs):
        nonlocal calls
        calls += 1
        return retry(node_id, **kwargs) if calls == 1 else {"action": "block", "diagnosis": "new full required"}
    coordinator = coordinate(tmp_path, decisions)
    monkeypatch.setattr(runner, "find_host_execution", Mock(return_value={"passed": True, "run_id": "old-full"}))
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
    assert calls == 1  # The anomaly is handed to Reporter, not retried again here.


@pytest.mark.parametrize("resume", [False, True])
def test_verified_mechanical_recovery_does_not_reopen_writer(tmp_path, monkeypatch, resume):
    coordinator = coordinate(tmp_path, retry)
    monkeypatch.setattr(runner, "find_host_execution", Mock(return_value={"passed": True, "run_id": "same-full"}))
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
    assert attempts == ([0] if resume else [1, 0])
    archive.assert_not_called()
    assert records[0]["host_execution"]["run_id"] == "same-full"
    assert records[0]["result_json"]["acceptance_checklist"] == ["preserved"]
    assert not status.get("error_kind")


def test_mechanical_reconcile_keeps_layout_hard_boundary(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_assert_foundation_sandbox_layout_safe",
                        Mock(side_effect=RuntimeError("link or reparse point at src")))
    read_request = Mock(side_effect=AssertionError("unsafe files must not be read"))
    monkeypatch.setattr(runner, "read_environment_request", read_request)
    result = runner._inspect_task_writer_completion(status={"ok": True}, sandbox=tmp_path,
        audit_dir=tmp_path / "audit", case_runtime=None, request_source="t", require_execution_receipt=True)
    assert result["error_kind"] == "sandbox_inspection_failed"
    assert "link or reparse point" in result["blocked_reason"]
    read_request.assert_not_called()


def test_blocked_task_cannot_abort_or_install_but_valid_sibling_request_survives():
    from copy import deepcopy
    from geng_agent.task_writer_state import _task_environment_requests
    records = [
        {"task_id": "blocked", "supervisor_blocked": {"action": "block"},
         "writer_error_kind": "environment_request_invalid", "environment_requests": ["malformed"]},
        {"task_id": "sibling", "environment_requests": [{"requirement": "numpy", "reason": "needed"}]},
    ]
    before = deepcopy(records)
    requests = _task_environment_requests(records)
    assert len(requests) == 1
    assert requests[0].requirement == "numpy"
    assert requests[0].requested_by == "sibling"
    assert records == before


def test_unhandled_invalid_environment_request_is_local_observation():
    from geng_agent.task_writer_state import _task_environment_requests
    record = {"writer_error_kind": "environment_request_invalid"}
    assert _task_environment_requests([record]) == ()
    assert record["coordination_status"] == "needs_review"
    assert record["coordination_observations"]
