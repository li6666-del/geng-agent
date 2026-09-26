"""Independent scientific processes share GPUs without host admission queues."""

from __future__ import annotations

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from geng_agent import execution_client, gpu_resources
from geng_agent.execution_receipts import ExecutionBroker
from geng_agent.progress import PipelineCancelled
from tests.test_execution_sandbox import native_sandbox_temporary_directory


def _wait_for(predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("timed out waiting for concurrent scientific processes")


def _project(root: Path, *, wait_for_release=False) -> ExecutionBroker:
    project = root / "project"
    (project / "tasks").mkdir(parents=True)
    (project / "tasks/__init__.py").write_text("", encoding="utf-8")
    (project / "tasks/sample.py").write_text(
        "import json,os,time\nfrom pathlib import Path\n"
        "def main(config):\n"
        "    config = json.loads(Path(config).read_text(encoding='utf-8'))\n"
        "    Path('outputs/sample/started').write_text('ready')\n"
        "    if config.get('wait_for_release'):\n"
        "        deadline = time.monotonic() + 60\n"
        "        while not Path('release').is_file():\n"
        "            if time.monotonic() > deadline:\n"
        "                raise TimeoutError('test did not release the process')\n"
        "            time.sleep(0.02)\n"
        "    Path('outputs/sample/device.txt').write_text(os.environ.get('CUDA_VISIBLE_DEVICES', 'missing'))\n",
        encoding="utf-8")
    (project / "config.json").write_text(json.dumps({"wait_for_release": wait_for_release}), encoding="utf-8")
    (project / "tasks_manifest.json").write_text(json.dumps({"tasks": [{"task_id": "sample",
        "module": "sample", "output_subdir": "sample", "config_full": "config.json"}]}), encoding="utf-8")
    return ExecutionBroker(project, root / "audit", Path(sys.executable))


@pytest.mark.parametrize("device_request", ["gpu", "auto"])
def test_two_writer_brokers_execute_on_the_same_gpu_concurrently(monkeypatch, device_request):
    # GPU discovery is simulated; two real scientific subprocesses must both
    # enter their task before either is allowed to finish. This tests admission,
    # not physical CUDA kernel throughput or memory capacity.
    monkeypatch.setattr(gpu_resources, "_visible_gpu_devices", lambda: ["GPU-fixture"])
    with native_sandbox_temporary_directory() as native_root:
        root = Path(native_root)
        first = _project(root / "a", wait_for_release=True)
        second = _project(root / "b", wait_for_release=True)
        request = {"task_id": "sample", "mode": "full", "device": device_request}
        with ThreadPoolExecutor(max_workers=2) as pool:
            first_run = pool.submit(first.execute, request)
            second_run = pool.submit(second.execute, request)
            try:
                _wait_for(lambda: all((broker.root / "outputs/sample/started").is_file()
                                     for broker in (first, second)))
                assert not first_run.done() and not second_run.done()
                assert first.task_status["sample"]["state"] == "running"
                assert second.task_status["sample"]["state"] == "running"
            finally:
                for broker in (first, second):
                    (broker.root / "release").write_text("finish", encoding="utf-8")
            first_result = first_run.result(timeout=30)
            second_result = second_run.result(timeout=30)
        assert first_result["returncode"] == 0, first_result["stderr_tail"]
        assert second_result["returncode"] == 0, second_result["stderr_tail"]
        assert max(first_result["started_at"], second_result["started_at"]) < min(
            first_result["finished_at"], second_result["finished_at"])
        for broker, receipt in ((first, first_result), (second, second_result)):
            assert receipt["compute_resource"]["gpu_uuid"] == "GPU-fixture"
            assert (broker.root / "outputs/sample/device.txt").read_text() == "GPU-fixture"


def test_cpu_execution_keeps_cuda_hidden_without_querying_gpu(monkeypatch):
    monkeypatch.setattr(gpu_resources, "_visible_gpu_devices",
                        lambda: (_ for _ in ()).throw(AssertionError("CPU run queried GPU")))
    with native_sandbox_temporary_directory() as native_root:
        broker = _project(Path(native_root))
        receipt = broker.execute({"task_id": "sample", "mode": "full", "device": "cpu"})
        assert receipt["returncode"] == 0, receipt["stderr_tail"]
        assert receipt["compute_resource"]["gpu_uuid"] is None
        assert (broker.root / "outputs/sample/device.txt").read_text() == ""


def test_user_cancellation_before_launch_still_prevents_execution(monkeypatch, tmp_path):
    broker = _project(tmp_path)
    broker.cancelled.set()
    monkeypatch.setattr(gpu_resources, "select_compute",
                        lambda request: pytest.fail("cancelled request reached device selection"))
    with pytest.raises(PipelineCancelled, match="cancelled before launch"):
        broker.execute({"task_id": "sample", "mode": "full", "device": "gpu"})
    assert not (broker.root / "outputs/sample/started").exists()


def test_gpu_request_does_not_silently_fall_back(monkeypatch):
    monkeypatch.setattr(gpu_resources, "_visible_gpu_devices", lambda: [])
    with pytest.raises(RuntimeError, match="GPU requested"):
        gpu_resources.select_compute("gpu")
    device = gpu_resources.select_compute("auto")
    assert device.gpu_uuid is None and device.cuda_visible_devices == ""


@pytest.mark.parametrize("allowed,expected", [
    (None, ["GPU-first", "GPU-second"]),
    ("", []),
    ("1", ["GPU-second"]),
    ("GPU-sec", ["GPU-second"]),
    ("0,GPU-sec", ["GPU-first", "GPU-second"]),
    ("-1", []),
])
def test_device_selection_respects_host_cuda_visibility(monkeypatch, allowed, expected):
    if allowed is None:
        monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)
    else:
        monkeypatch.setenv("CUDA_VISIBLE_DEVICES", allowed)
    monkeypatch.setattr(gpu_resources.subprocess, "run", lambda *args, **kwargs:
                        SimpleNamespace(returncode=0, stdout="0, GPU-first\n1, GPU-second\n"))
    assert gpu_resources._visible_gpu_devices() == expected


@pytest.mark.parametrize("state", ["starting", "running"])
def test_client_does_not_resubmit_the_same_active_task(monkeypatch, tmp_path, capsys, state):
    project = tmp_path / "project"
    project.mkdir()
    (project / "run_task.py").write_text("", encoding="utf-8")
    (project / "tasks_manifest.json").write_text(json.dumps({"tasks": [
        {"task_id": "sample", "config_full": "config.json"}]}), encoding="utf-8")
    queue = project / ".geng_execution" / "session123"
    queue.mkdir(parents=True)
    (queue / "status.json").write_text(json.dumps({"tasks": {"sample": {
        "task_id": "sample", "state": state, "request_id": "earlier"}}}), encoding="utf-8")
    monkeypatch.setattr(execution_client, "__file__", str(project / "run_task.py"))
    monkeypatch.setattr(sys, "argv", ["run_task.py", "--task", "sample", "--device", "gpu"])
    monkeypatch.setenv("GENG_EXECUTION_BROKER", "session123")
    assert execution_client.main() == 75
    assert json.loads(capsys.readouterr().out)["state"] == "in_flight_conflict"
    assert not list(queue.glob("*.request.json"))
