"""Race regressions through the real queue and host-observed toy processes."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import threading
import time

import pytest

from geng_agent.execution_receipts import ExecutionBroker, file_hash
from geng_agent import task_writer_runner
from tests.test_execution_sandbox import native_sandbox_temporary_directory


def _project(root: Path):
    project = root / "project"
    (project / "tasks").mkdir(parents=True)
    (project / "tasks/__init__.py").write_text("", encoding="utf-8")
    (project / "tasks/sample.py").write_text(
        "import json\nfrom pathlib import Path\ndef main(config):\n"
        "    value = int(Path('undeclared.txt').read_text())\n"
        "    if Path('optional.py').is_file():\n"
        "        from optional import OFFSET\n"
        "        value += OFFSET\n"
        "    factor = json.loads(Path(config).read_text())['factor']\n"
        "    Path('outputs/sample/result.csv').write_text(str(value * factor))\n", encoding="utf-8")
    (project / "undeclared.txt").write_text("7", encoding="utf-8")
    (project / "config.json").write_text('{"factor":1}', encoding="utf-8")
    (project / "other.json").write_text('{"factor":3}', encoding="utf-8")
    (project / "config_smoke.json").write_text('{"factor":3}', encoding="utf-8")
    (project / "tasks_manifest.json").write_text(json.dumps({"tasks": [{"task_id": "sample",
        "module": "sample", "output_subdir": "sample", "config_full": "config.json",
        "config_smoke": "config_smoke.json"}]}), encoding="utf-8")
    return project, root / "audit"


def _wait_result(path: Path):
    deadline = time.monotonic() + 45
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(.02)
    assert path.exists(), f"host did not publish {path.name}"
    return json.loads(path.read_text(encoding="utf-8"))


def _mutate_after_pending_cached(change):
    with native_sandbox_temporary_directory() as temporary:
        project, audit = _project(Path(temporary))
        broker = ExecutionBroker(project, audit, Path(sys.executable))
        running, queued, cached, changed = (threading.Event() for _ in range(4))
        original_status, original_publish = broker._set_status, broker._publish_response
        first_receipt = {}

        def status(task_id, update):
            original_status(task_id, update)
            if update.get("state") == "running" and not running.is_set():
                running.set()
                assert queued.wait(45)

        def publish(path, value):
            if path.name == "first.result.json":
                first_receipt.update(value)
                assert "second.request.json" in broker.completed_pending, value
                cached.set()
                assert changed.wait(45)
            original_publish(path, value)

        broker._set_status, broker._publish_response = status, publish
        with broker:
            try:
                request = {"task_id": "sample", "mode": "full", "config": "config.json", "inputs": []}
                (broker.queue / "first.request.json").write_text(json.dumps(request), encoding="utf-8")
                assert running.wait(45)
                (broker.queue / "second.request.json").write_text(json.dumps(request), encoding="utf-8")
                queued.set()
                assert cached.wait(45), "first observed execution did not reach pending cache"
                assert first_receipt["returncode"] == 0 and first_receipt["inputs_stable"] is True, first_receipt
                old_data_hash = first_receipt["input_hashes"]["undeclared.txt"]
                if change == "config":
                    (project / "config.json").write_text('{"factor":2}', encoding="utf-8")
                elif change == "request":
                    request["config"] = "other.json"
                    (broker.queue / "second.request.json").write_text(json.dumps(request), encoding="utf-8")
                elif change == "undeclared_input":
                    (project / "undeclared.txt").write_text("11", encoding="utf-8")
                    assert file_hash(project / "undeclared.txt") != old_data_hash
                elif change == "unstable_receipt":
                    broker.completed_pending["second.request.json"]["receipt"]["inputs_stable"] = False
                elif change == "new_source":
                    (project / "optional.py").write_text("OFFSET = 4\n", encoding="utf-8")
                changed.set()
                second = _wait_result(broker.queue / "second.result.json")
                assert second["returncode"] == 0, second
                assert second["run_id"] != first_receipt["run_id"]
                assert len(broker.receipts) == 2
                return (project / "outputs/sample/result.csv").read_text(encoding="utf-8")
            finally:
                queued.set(); changed.set()


@pytest.mark.parametrize("change, expected", [("config", "14"), ("request", "21")])
def test_cached_pending_request_rechecks_config_and_request_at_consumption(change, expected):
    assert _mutate_after_pending_cached(change) == expected


def test_cached_pending_request_rechecks_hardcoded_observed_input():
    # undeclared.txt is absent from CLI/config inputs; only actual read tracing records it.
    assert _mutate_after_pending_cached("undeclared_input") == "11"


def test_pending_request_never_reuses_unstable_observation():
    assert _mutate_after_pending_cached("unstable_receipt") == "7"


def test_pending_request_rechecks_new_optional_source_files():
    assert _mutate_after_pending_cached("new_source") == "11"


def test_preparation_writer_session_host_rejects_full_but_runs_smoke(monkeypatch):
    with native_sandbox_temporary_directory() as temporary:
        project, audit = _project(Path(temporary))
        audit.mkdir()
        responses = {}
        def writer_session(*, extra_env, **kwargs):
            queue = project / ".geng_execution" / extra_env["GENG_EXECUTION_BROKER"]
            for mode in ("full", "smoke"):
                (queue / f"{mode}.request.json").write_text(json.dumps({"task_id": "sample", "mode": mode}), encoding="utf-8")
                responses[mode] = _wait_result(queue / f"{mode}.result.json")
            return {"ok": True}
        monkeypatch.setattr(task_writer_runner, "run_codex_subprocess", writer_session)
        result = task_writer_runner._run_task_writer_codex_session(label="prepared", prompt="prepare",
            sandbox=project, audit_dir=audit, require_execution_receipt=False)
        assert result["ok"] is True
        assert responses["full"]["returncode"] == 1 and "full execution is disabled" in responses["full"]["error"]
        assert responses["smoke"]["returncode"] == 0 and responses["smoke"]["mode"] == "smoke"
        receipts = list((audit / "execution_runs").glob("*/execution_receipt.json"))
        assert len(receipts) == 1
        assert json.loads(receipts[0].read_text(encoding="utf-8"))["mode"] == "smoke"
