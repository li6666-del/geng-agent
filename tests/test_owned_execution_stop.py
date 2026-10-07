"""Only a broker's own run identifier can stop its scientific process."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time

import pytest

from geng_agent import execution_client
from geng_agent.execution_receipts import ExecutionBroker


def _wait_for(predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("test-owned execution did not reach the expected state")


def _broker(root: Path) -> ExecutionBroker:
    project = root / "project"
    (project / "tasks").mkdir(parents=True)
    (project / "tasks/__init__.py").write_text("", encoding="utf-8")
    (project / "tasks/task.py").write_text(
        "from pathlib import Path\nimport time\n"
        "def main(config):\n"
        "    Path('outputs/task/started').write_text('started')\n"
        "    deadline = time.monotonic() + 30\n"
        "    while not Path('release').exists():\n"
        "        if time.monotonic() > deadline: raise TimeoutError('test cleanup missing')\n"
        "        time.sleep(0.02)\n"
        "    Path('outputs/task/result.txt').write_text('finished')\n",
        encoding="utf-8",
    )
    (project / "config.json").write_text("{}", encoding="utf-8")
    (project / "tasks_manifest.json").write_text(json.dumps({"tasks": [
        {"task_id": "task", "module": "task", "output_subdir": "task", "config_full": "config.json"}
    ]}), encoding="utf-8")
    return ExecutionBroker(project, root / "audit", Path(sys.executable))


def _submit(broker, request_id):
    (broker.queue / f"{request_id}.request.json").write_text(
        json.dumps({"task_id": "task", "mode": "full", "device": "cpu"}), encoding="utf-8")


def _control(broker, request_id, run_id, task_id="task"):
    (broker.queue / f"{request_id}.stop.json").write_text(
        json.dumps({"task_id": task_id, "run_id": run_id}), encoding="utf-8")
    result = broker.queue / f"{request_id}.result.json"
    _wait_for(result.is_file)
    return json.loads(result.read_text(encoding="utf-8"))


@pytest.fixture
def dummy_science(monkeypatch):
    # These processes run only the tiny test program above. This suite tests
    # broker ownership/cancellation; separate native tests cover OS isolation.
    monkeypatch.setattr("geng_agent.execution_sandbox.scientific_sandbox_launch",
                        lambda command, **kw: {"command": command, "env": {**os.environ, **kw["env"]},
                                               "policy": {"provider": "test_dummy"}})
    monkeypatch.setattr("geng_agent.execution_receipts.probe_execution_environment",
                        lambda *_a, **_k: {"ok": True, "sha256": "fixture", "inventory": {}, "duration_s": 0})


def test_stop_owned_run_preserves_other_broker_and_allows_next_run(tmp_path, dummy_science):
    first, other = _broker(tmp_path / "first"), _broker(tmp_path / "other")
    with first, other:
        try:
            _submit(first, "first")
            _submit(other, "other")
            _wait_for(lambda: all(broker.task_status.get("task", {}).get("state") == "running"
                                 for broker in (first, other)))
            own_run = first.task_status["task"]["run_id"]
            other_run = other.task_status["task"]["run_id"]
            own_process, other_process = first.process, other.process

            assert _control(first, "foreign", other_run)["state"] == "run_not_owned"
            assert _control(first, "wrongtask", own_run, "unrelated")["state"] == "run_not_owned"
            assert _control(first, "pid", str(other_process.pid))["state"] == "run_not_owned"
            assert own_process.poll() is None and other_process.poll() is None

            assert _control(first, "stopfirst", own_run)["state"] == "stop_requested"
            first_result = first.queue / "first.result.json"
            _wait_for(first_result.is_file)
            receipt = json.loads(first_result.read_text(encoding="utf-8"))
            assert receipt["cancelled"] is True and receipt["stop_requested_by"] == "writer"
            assert receipt["returncode"] != 0 and own_process.poll() is not None
            assert other_process.poll() is None
            assert first.thread.is_alive() and not first.cancelled.is_set()

            _submit(first, "next")
            _wait_for(lambda: first.process is not None and first.active_run_id != own_run)
            next_process = first.process
            assert _control(first, "oldrun", own_run)["state"] == "already_finished"
            assert next_process.poll() is None and other_process.poll() is None
            (first.root / "release").write_text("finish", encoding="utf-8")
            next_result = first.queue / "next.result.json"
            _wait_for(next_result.is_file)
            next_receipt = json.loads(next_result.read_text(encoding="utf-8"))
            assert next_receipt["returncode"] == 0 and next_receipt["cancelled"] is False
            assert next_receipt["stop_requested_by"] is None
        finally:
            # Only test-owned processes exist here, and all have a bounded wait.
            (first.root / "release").write_text("finish", encoding="utf-8")
            (other.root / "release").write_text("finish", encoding="utf-8")
    assert not first.control_thread.is_alive() and not other.control_thread.is_alive()


def test_stop_before_launch_does_not_cancel_broker_or_a_future_run(tmp_path):
    broker = _broker(tmp_path)
    with broker:
        response = _control(broker, "early", "submission_request_id")
        assert response["state"] == "run_not_owned" and response["returncode"] == 2
        assert not broker.cancelled.is_set() and broker.process is None
    assert not broker.control_thread.is_alive()


def test_client_stop_is_available_without_manifest_and_never_submits_a_run(tmp_path, monkeypatch, capsys):
    broker = _broker(tmp_path)
    with broker:
        (broker.root / "tasks_manifest.json").unlink()
        (broker.queue / "status.json").write_text("interrupted status update", encoding="utf-8")
        monkeypatch.chdir(broker.root)
        monkeypatch.setattr(execution_client, "__file__", str(broker.root / "run_task.py"))
        monkeypatch.setenv("GENG_EXECUTION_BROKER", broker.session_id)
        monkeypatch.setattr(sys, "argv", ["run_task.py", "--task", "task", "--stop", "unknown_run"])
        assert execution_client.main() == 2
        assert json.loads(capsys.readouterr().out)["state"] == "run_not_owned"
        assert not list(broker.queue.glob("*.request.json"))


def test_client_stop_without_host_is_explicit(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(execution_client, "__file__", str(tmp_path / "run_task.py"))
    monkeypatch.delenv("GENG_EXECUTION_BROKER", raising=False)
    monkeypatch.setattr(sys, "argv", ["run_task.py", "--task", "task", "--stop", "any_run"])
    assert execution_client.main() == 1
    assert json.loads(capsys.readouterr().out)["state"] == "host_unavailable"


def test_owned_job_handle_is_used_instead_of_pid_lookup(tmp_path):
    broker = _broker(tmp_path)

    class OwnedProcess:
        def __init__(self):
            self.stopped = False

        def poll(self):
            return None if not self.stopped else -1

        def kill_tree(self):
            self.stopped = True

        @property
        def pid(self):
            raise AssertionError("stopping the owned job must not inspect or reopen a PID")

    process = OwnedProcess()
    broker.process = process
    broker._stop_process()
    assert process.stopped
