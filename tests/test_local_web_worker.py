"""Offline pull-worker transport and recovery; never starts a model process."""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import uuid
import zipfile
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

from geng_agent.web.local_worker import LocalWorker, WorkerConfig, WorkerError, worker_lock

TOKEN = "offline-test-worker-token-" + "x" * 32


def make_job(**overrides):
    job_id = str(uuid.uuid4())
    return {"job_id": job_id, "case_id": str(uuid.uuid4()), "display_name": "offline paper",
            "paper_url": f"/api/v1/worker/jobs/{job_id}/paper", "pipeline_complete": False, **overrides}


def write_delivery(directory):
    (directory / "result_review.docx").write_bytes(b"mock comparison")
    (directory / "reproduction_report.docx").write_bytes(b"mock reproduction")
    (directory / "repro_project").mkdir(exist_ok=True)
    (directory / "repro_project" / "run.py").write_text("print('mock')", encoding="utf-8")


class Cloud:
    def __init__(self, job):
        self.job = job
        self.requests = []
        self.finishes = []
        self.uploads = []
        self.heartbeat_calls = 0
        self.paper_calls = 0
        self.upload_failures = 0
        self.finish_failures = 0
        self.cancel = False
        self.heartbeat_offline = False

    def __call__(self, request):
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        self.requests.append(request)
        path = request.url.path
        if path.endswith("/claim"):
            return httpx.Response(200, json={"job": self.job})
        if path.endswith("/heartbeat"):
            self.heartbeat_calls += 1
            if self.heartbeat_offline:
                raise httpx.ConnectError("offline", request=request)
            return httpx.Response(200, json={"status": "running", "cancel_requested": self.cancel})
        if path.endswith("/paper"):
            self.paper_calls += 1
            assert request.headers["x-worker-id"]
            return httpx.Response(200, content=b"%PDF-1.4 offline mock")
        if path.endswith("/complete"):
            data = request.read()
            self.uploads.append(data)
            if self.upload_failures:
                self.upload_failures -= 1
                raise httpx.ReadError("response lost", request=request)
            return httpx.Response(200, json={"status": "succeeded"})
        if path.endswith("/finish"):
            self.finishes.append(json.loads(request.content))
            if self.finish_failures:
                self.finish_failures -= 1
                raise httpx.ReadError("response lost", request=request)
            return httpx.Response(200, json={"status": self.finishes[-1]["status"]})
        raise AssertionError(path)


def worker_for(tmp_path, cloud, factory, **config):
    settings = WorkerConfig(url="https://cloud.example", token=TOKEN, case_root=tmp_path,
                            retry_seconds=0.001, **config)
    client = httpx.Client(transport=httpx.MockTransport(cloud))
    return LocalWorker(settings, client=client, pipeline_factory=factory, sleep=lambda _seconds: None)


def test_full_job_uses_original_pipeline_and_packager(tmp_path):
    job = make_job()
    cloud = Cloud(job)
    calls = []

    class Pipeline:
        def run(self, **kwargs):
            calls.append(kwargs)
            assert kwargs["paper_path"].read_bytes().startswith(b"%PDF-")
            write_delivery(kwargs["output_dir"])
            return SimpleNamespace(delivery_status="complete")

    worker = worker_for(tmp_path, cloud, Pipeline)
    assert worker.run_once() is True
    assert len(calls) == 1 and cloud.paper_calls == 1
    assert calls[0]["run_repro"] is True and calls[0]["resume"] is True
    assert calls[0]["analysis_backend"] == "codex"
    case_dir = tmp_path / f"cloud_{job['case_id']}"
    state = json.loads((case_dir / ".cloud-worker.json").read_text(encoding="utf-8"))
    assert state["pipeline_complete"] is True
    assert state["uploaded_job_id"] == job["job_id"]
    with zipfile.ZipFile(case_dir / "exports" / f"{job['job_id']}.zip") as archive:
        prefix = "复现交付包/复现任务/t01_task/"
        assert {info.filename for info in archive.infolist() if not info.is_dir()} == {
            "复现交付包/论文复现结果对比报告.docx", "复现交付包/本地复现报告.docx",
            prefix + "代码/run.py", prefix + "readme.md",
        }
        assert all(name.startswith("复现交付包/") for name in archive.namelist())
        assert archive.read(prefix + "代码/run.py") == b"print('mock')"
        assert "代码/run.py" in archive.read(prefix + "readme.md").decode("utf-8")
    assert b'name="worker_id"' in cloud.uploads[0] and b'name="bundle"' in cloud.uploads[0]


def test_upload_response_loss_and_process_restart_do_not_repeat_science(tmp_path):
    job = make_job()
    cloud = Cloud(job)
    cloud.upload_failures = 1
    calls = []

    class Pipeline:
        def run(self, **kwargs):
            calls.append("run")
            write_delivery(kwargs["output_dir"])
            return SimpleNamespace(delivery_status="complete")

    worker = worker_for(tmp_path, cloud, Pipeline)
    worker.run_once()
    assert len(cloud.uploads) == 2 and calls == ["run"]
    # A repeat claim after a process restart sees the durable completion marker.
    restarted = worker_for(tmp_path, cloud, lambda: pytest.fail("completed science ran twice"))
    assert restarted.worker_id == worker.worker_id
    restarted.run_once()
    assert len(cloud.uploads) == 3 and cloud.paper_calls == 1
    # A server-side retry makes a new job for the same case.
    cloud.job = make_job(case_id=job["case_id"])
    restarted.run_once()
    assert len(cloud.uploads) == 4 and cloud.paper_calls == 1


def test_marker_precedes_packaging_and_missing_files_never_restart_science(tmp_path, monkeypatch):
    job = make_job()
    cloud = Cloud(job)
    real_case = tmp_path / f"cloud_{job['case_id']}"

    class Pipeline:
        def run(self, **kwargs):
            write_delivery(kwargs["output_dir"])
            return SimpleNamespace(delivery_status="complete")

    def broken_delivery(*_args):
        state = json.loads((real_case / ".cloud-worker.json").read_text(encoding="utf-8"))
        assert state["pipeline_complete"] is True
        raise OSError("mock disk problem")

    monkeypatch.setattr("geng_agent.web.local_worker.build_delivery", broken_delivery)
    worker_for(tmp_path, cloud, Pipeline).run_once()
    assert cloud.finishes[-1]["pipeline_complete"] is True
    assert cloud.finishes[-1]["error_code"] == "delivery_package"
    worker_for(tmp_path, cloud, lambda: pytest.fail("completed science restarted")).run_once()
    assert len(cloud.finishes) == 2 and not cloud.uploads


def test_server_completion_without_local_delivery_reports_failure(tmp_path):
    cloud = Cloud(make_job(pipeline_complete=True))
    worker_for(tmp_path, cloud, lambda: pytest.fail("server completed science restarted")).run_once()
    assert cloud.paper_calls == 0 and not cloud.uploads
    assert cloud.finishes[0]["status"] == "failed"
    assert cloud.finishes[0]["pipeline_complete"] is True
    assert "原电脑" in cloud.finishes[0]["error_message"]


def test_heartbeat_network_loss_does_not_cancel_pipeline(tmp_path):
    cloud = Cloud(make_job())
    cloud.heartbeat_offline = True

    class Pipeline:
        def run(self, **kwargs):
            kwargs["progress"].check_cancelled()
            write_delivery(kwargs["output_dir"])
            return SimpleNamespace(delivery_status="complete")

    worker_for(tmp_path, cloud, Pipeline).run_once()
    assert cloud.uploads and not cloud.finishes


def test_heartbeat_explicit_cancel_reaches_running_pipeline(tmp_path):
    cloud = Cloud(make_job())
    observed_cancel = threading.Event()

    class Pipeline:
        def run(self, **kwargs):
            cloud.cancel = True
            # Wait only for heartbeat delivery; the production core owns safe boundaries.
            progress = kwargs["progress"]
            for _ in range(100):
                if progress.cancelled():
                    observed_cancel.set()
                    progress.check_cancelled()
                observed_cancel.wait(0.01)
            pytest.fail("heartbeat did not deliver explicit cancellation")

    worker_for(tmp_path, cloud, Pipeline, heartbeat_seconds=0.01).run_once()
    assert observed_cancel.is_set()
    assert cloud.finishes[0]["status"] == "cancelled" and not cloud.uploads


def test_cancel_during_bundle_upload_is_finished_without_repeating_science(tmp_path):
    cloud = Cloud(make_job())

    def handler(request):
        if request.url.path.endswith("/complete"):
            cloud.cancel = True
            return httpx.Response(409, json={"detail": "任务已请求停止"})
        return cloud(request)

    class Pipeline:
        def run(self, **kwargs):
            write_delivery(kwargs["output_dir"])
            return SimpleNamespace(delivery_status="complete")

    worker_for(tmp_path, handler, Pipeline).run_once()
    assert cloud.finishes == [{"worker_id": cloud.requests[0].headers["x-worker-id"],
                               "status": "cancelled", "pipeline_complete": True}]


@pytest.mark.parametrize("status", [400, 413, 422])
def test_permanent_upload_rejection_releases_job_and_retains_science(tmp_path, status):
    cloud = Cloud(make_job())

    def handler(request):
        if request.url.path.endswith("/complete"):
            return httpx.Response(status, json={"detail": "permanent upload rejection"})
        return cloud(request)

    class Pipeline:
        def run(self, **kwargs):
            write_delivery(kwargs["output_dir"])
            return SimpleNamespace(delivery_status="complete")

    worker = worker_for(tmp_path, handler, Pipeline)
    worker.run_once()
    assert cloud.finishes[0]["status"] == "failed"
    assert cloud.finishes[0]["error_code"] == "delivery_upload"
    assert cloud.finishes[0]["pipeline_complete"] is True
    state = json.loads((tmp_path / f"cloud_{cloud.job['case_id']}" / ".cloud-worker.json").read_text(encoding="utf-8"))
    assert state["pipeline_complete"] is True and "pending_finish" not in state
    # The same worker can poll its next paper after reporting the terminal error.
    cloud.job = None
    assert worker.run_once() is False


def test_upload_conflict_with_remote_success_does_not_overwrite_success(tmp_path):
    cloud = Cloud(make_job())
    uploaded = False

    def handler(request):
        nonlocal uploaded
        if request.url.path.endswith("/complete"):
            uploaded = True
            return httpx.Response(409, json={"detail": "already ended"})
        if uploaded and request.url.path.endswith("/heartbeat"):
            return httpx.Response(200, json={"status": "succeeded", "cancel_requested": False})
        return cloud(request)

    class Pipeline:
        def run(self, **kwargs):
            write_delivery(kwargs["output_dir"])
            return SimpleNamespace(delivery_status="complete")

    worker_for(tmp_path, handler, Pipeline).run_once()
    assert not cloud.finishes
    state = json.loads((tmp_path / f"cloud_{cloud.job['case_id']}" / ".cloud-worker.json").read_text(encoding="utf-8"))
    assert state["uploaded_job_id"] == cloud.job["job_id"]


def test_pending_failure_is_persisted_before_network_retry(tmp_path):
    cloud = Cloud(make_job())
    cloud.finish_failures = 1

    class Pipeline:
        def run(self, **_kwargs):
            raise RuntimeError("model detail including a secret must never reach cloud")

    worker = worker_for(tmp_path, cloud, Pipeline)
    state_path = tmp_path / f"cloud_{cloud.job['case_id']}" / ".cloud-worker.json"

    def interrupt_retry(_seconds):
        assert json.loads(state_path.read_text(encoding="utf-8"))["pending_finish"]["status"] == "failed"
        raise KeyboardInterrupt()

    worker._sleep = interrupt_retry
    with pytest.raises(KeyboardInterrupt):
        worker.run_once()
    worker_for(tmp_path, cloud, lambda: pytest.fail("pending failure repeated science")).run_once()
    assert len(cloud.finishes) == 2
    assert "secret" not in cloud.finishes[0]["error_message"]
    assert "pending_finish" not in json.loads(state_path.read_text(encoding="utf-8"))


def test_atomic_download_preserves_previous_file_and_cleans_partial(tmp_path):
    job = make_job()

    class BrokenStream(httpx.SyncByteStream):
        def __iter__(self):
            yield b"%PDF-partial"
            raise httpx.ReadError("disconnected")

    def handler(request):
        return httpx.Response(200, stream=BrokenStream())

    worker = worker_for(tmp_path, handler, lambda: None)
    destination = tmp_path / "paper" / "paper.pdf"
    with pytest.raises(httpx.ReadError):
        worker._download(job, destination)
    assert not destination.exists() and not list(destination.parent.glob("*.part"))
    destination.write_bytes(b"%PDF-prior-complete")
    worker._download(job, destination)
    assert destination.read_bytes() == b"%PDF-prior-complete"


def test_origin_guards_prevent_token_leak_and_invalid_ids(tmp_path):
    with pytest.raises(WorkerError, match="HTTPS"):
        WorkerConfig(url="http://cloud.example", token=TOKEN, case_root=tmp_path)
    for address in ("https://user:password@cloud.example", "https://cloud.example/?secret=foo", "https://cloud.example/api"):
        with pytest.raises(WorkerError):
            WorkerConfig(url=address, token=TOKEN, case_root=tmp_path)
    assert WorkerConfig(url="http://127.0.0.1:1234", token=TOKEN, case_root=tmp_path)
    worker = worker_for(tmp_path, lambda _request: pytest.fail("unexpected HTTP"), lambda: None)
    with pytest.raises(WorkerError, match="origin"):
        worker._download(make_job(paper_url="https://other.example/paper"), tmp_path / "paper.pdf")
    with pytest.raises(WorkerError, match="UUID"):
        worker.run_job(make_job(case_id="../../outside"))
    assert TOKEN not in repr(worker.config)


def test_configuration_env_overrides_and_identity_validation(tmp_path, monkeypatch):
    for name in ("GENG_WORKER_URL", "GENG_WORKER_TOKEN", "GENG_CASES_ROOT", "GENG_WORKER_ID"):
        monkeypatch.delenv(name, raising=False)
    config_path = tmp_path / "worker.json"
    config_path.write_text(json.dumps({"url": "https://cloud.example", "token": TOKEN, "case_root": str(tmp_path),
                                       "env": {"GENG_MODEL_CONFIG": "configured-model.json",
                                               "GENG_SHARED_SCIENCE_PYTHON": "shared-python.exe"}}), encoding="utf-8")
    monkeypatch.setenv("GENG_MODEL_CONFIG", "old-model.json")
    monkeypatch.setenv("GENG_SHARED_SCIENCE_PYTHON", "old-python.exe")
    config = WorkerConfig.load(config_path)
    assert config.case_root == tmp_path and config.token == TOKEN
    import os
    assert os.environ["GENG_MODEL_CONFIG"] == "configured-model.json"
    assert os.environ["GENG_SHARED_SCIENCE_PYTHON"] == "shared-python.exe"
    worker = LocalWorker(config)
    worker.close()
    with pytest.raises(WorkerError, match="persisted identity"):
        LocalWorker(replace(config, worker_id="different-worker"))
    config_path.write_text(json.dumps({"env": {"OPENAI_API_KEY": "never-read-this"}}), encoding="utf-8")
    with pytest.raises(WorkerError, match="env accepts only"):
        WorkerConfig.load(config_path)


def test_os_lock_prevents_second_process_and_releases_after_exit(tmp_path):
    code = "from pathlib import Path; import sys; from geng_agent.web.local_worker import worker_lock; "
    code += "\nwith worker_lock(Path(sys.argv[1])): print('locked')"
    with worker_lock(tmp_path):
        blocked = subprocess.run([sys.executable, "-c", code, str(tmp_path)], capture_output=True)
        assert blocked.returncode != 0 and b"already using" in blocked.stderr
    available = subprocess.run([sys.executable, "-c", code, str(tmp_path)], capture_output=True)
    assert available.returncode == 0, available.stderr


def test_idle_claim_does_not_create_case_or_pipeline(tmp_path):
    cloud = Cloud(None)
    assert worker_for(tmp_path, cloud, lambda: pytest.fail("idle worker started pipeline")).run_once() is False
    assert not list(tmp_path.glob("cloud_*"))
