"""Offline retention and fresh-case recovery; only pytest scratch is deleted."""
from __future__ import annotations

import json
import os
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from geng_agent.web import local_retention as retention
from geng_agent.web.local_worker import LocalWorker, WorkerConfig, worker_lock
from tests.test_local_web_worker import Cloud, TOKEN, make_job, worker_for, write_delivery


def expired(job, **extra):
    return {"case_id": job["case_id"], "job_id": job["job_id"],
            "local_generation": job.get("local_generation"),
            "expired_at": "2026-09-30T00:00:00+00:00", **extra}


def create_case(root, job, **state_extra):
    directory = root / retention.local_case_name(job["case_id"], job.get("local_generation"))
    directory.mkdir(parents=True, exist_ok=True)
    (directory / ".cloud-worker.json").write_text(json.dumps({
        "case_id": job["case_id"], "job_id": job["job_id"],
        "local_generation": job.get("local_generation"), **state_extra,
    }), encoding="utf-8")
    (directory / "experiment.txt").write_text("preserved experiment", encoding="utf-8")
    return directory


def cleanup_worker(root, candidates):
    def handler(request):
        assert request.url.path == "/api/v1/worker/cleanup-candidates"
        assert request.headers["authorization"] == "Bearer " + TOKEN
        return httpx.Response(200, json={"items": candidates, "retention_days": 7})

    return LocalWorker(WorkerConfig(url="https://cloud.example", token=TOKEN, case_root=root),
                       client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_default_dry_run_and_server_authorized_apply_preserve_manual_cases(tmp_path):
    job = make_job()
    directory = create_case(tmp_path, job)
    manual = tmp_path / "manual_paper"
    manual.mkdir()
    (manual / "data.txt").write_text("keep", encoding="utf-8")
    worker = cleanup_worker(tmp_path, [expired(job)])
    result = retention.cleanup_cases(worker)
    assert result["mode"] == "dry_run" and result["cases"] == [directory.name]
    assert directory.is_dir() and not (tmp_path / ".cloud-trash").exists()
    retention.cleanup_cases(worker, apply=True)
    assert not directory.exists() and (manual / "data.txt").read_text() == "keep"
    assert (tmp_path / ".cloud-worker").is_dir()


def test_pending_finish_or_old_local_files_are_not_deletion_authority(tmp_path):
    old = make_job()
    directory = create_case(tmp_path, old, pending_finish={"status": "failed"})
    worker = cleanup_worker(tmp_path, [])
    retention.cleanup_cases(worker, apply=True)
    assert directory.exists()
    # An earlier job from the same case cannot delete a later retry's state.
    previous = make_job(case_id=old["case_id"])
    worker = cleanup_worker(tmp_path, [expired(previous)])
    result = retention.cleanup_cases(worker, apply=True)
    assert result["skipped"] == 1 and directory.exists()


def test_generation_candidate_cannot_delete_other_generations(tmp_path):
    old = make_job()
    generation = str(uuid.uuid4())
    new = make_job(case_id=old["case_id"], local_generation=generation)
    old_dir, new_dir = create_case(tmp_path, old), create_case(tmp_path, new)
    worker = cleanup_worker(tmp_path, [expired(old)])
    retention.cleanup_cases(worker, apply=True)
    assert not old_dir.exists() and new_dir.exists()
    retention.cleanup_cases(cleanup_worker(tmp_path, [expired(new)]), apply=True)
    assert not new_dir.exists()


def test_linked_shared_environment_survives_case_deletion(tmp_path):
    job = make_job()
    directory = create_case(tmp_path, job)
    outside = tmp_path / "shared_environment"
    outside.mkdir()
    (outside / "library.py").write_text("shared science library", encoding="utf-8")
    link = directory / "shared"
    if os.name == "nt":
        command = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True)
        assert command.returncode == 0, command.stderr
    else:
        link.symlink_to(outside, target_is_directory=True)
    retention.cleanup_cases(cleanup_worker(tmp_path, [expired(job)]), apply=True)
    assert not directory.exists()
    assert (outside / "library.py").read_text() == "shared science library"


def test_interrupted_deletion_resumes_without_state_json(tmp_path, monkeypatch):
    job = make_job()
    directory = create_case(tmp_path, job)
    worker = cleanup_worker(tmp_path, [expired(job)])
    real_remove = retention._remove_tree

    def interrupted(path, root):
        (path / ".cloud-worker.json").unlink()
        raise PermissionError("synthetic locked result")

    monkeypatch.setattr(retention, "_remove_tree", interrupted)
    retention.cleanup_cases(worker, apply=True)
    assert not directory.exists()
    trash = tmp_path / ".cloud-trash"
    assert len(list(trash.glob("*.json"))) == 1
    monkeypatch.setattr(retention, "_remove_tree", real_remove)
    retention.cleanup_cases(cleanup_worker(tmp_path, []), apply=True)
    assert not list(trash.iterdir())


def test_unmarked_staging_is_never_deleted(tmp_path):
    unknown = tmp_path / ".cloud-trash" / "unknown"
    unknown.mkdir(parents=True)
    (unknown / "data.txt").write_text("keep", encoding="utf-8")
    retention.cleanup_cases(cleanup_worker(tmp_path, []), apply=True)
    assert unknown.is_dir()


def test_manually_running_case_is_kept_until_child_exits(tmp_path):
    import sys

    job = make_job()
    directory = create_case(tmp_path, job)
    worker = cleanup_worker(tmp_path, [expired(job)])
    # This scientific stand-in deliberately does not acquire worker_lock.
    child = subprocess.Popen([sys.executable, "-u", "-c", "import time; print('ready', flush=True); time.sleep(2)"],
                             cwd=directory, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        assert child.stdout.readline().strip() == b"ready"
        result = retention.cleanup_cases(worker, apply=True)
        assert result["skipped"] == 1 and directory.is_dir()
        assert not (tmp_path / ".cloud-trash").exists()
    finally:
        child.wait(timeout=10)
        child.stdout.close()
        child.stderr.close()
    retention.cleanup_cases(worker, apply=True)
    assert not directory.exists()


def test_cli_cannot_clean_while_worker_holds_lock(tmp_path):
    job = make_job()
    directory = create_case(tmp_path, job)
    config = tmp_path / "worker-config.json"
    config.write_text(json.dumps({"url": "https://cloud.example", "token": TOKEN,
                                  "case_root": str(tmp_path)}), encoding="utf-8")
    environment = dict(os.environ)
    for key in ("GENG_CASES_ROOT", "GENG_WORKER_URL", "GENG_WORKER_ID", "GENG_WORKER_TOKEN"):
        environment.pop(key, None)
    import sys
    with worker_lock(tmp_path):
        result = subprocess.run([sys.executable, "-m", "geng_agent.web.local_retention", "--config", str(config), "--apply"],
                                env=environment, capture_output=True)
    assert result.returncode == 0 and b"worker is active" in result.stderr
    assert directory.is_dir()


@pytest.mark.parametrize("failure", ["404", "network"])
def test_cleanup_errors_do_not_prevent_claiming(tmp_path, failure, monkeypatch):
    requests = []

    def handler(request):
        requests.append(request.url.path)
        if request.url.path.endswith("cleanup-candidates"):
            if failure == "network":
                raise httpx.ConnectError("offline", request=request)
            return httpx.Response(404)
        assert request.url.path.endswith("claim")
        return httpx.Response(200, json={"job": None})

    worker = LocalWorker(WorkerConfig(url="https://cloud.example", token=TOKEN, case_root=tmp_path),
                         client=httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(worker, "_sleep", lambda _seconds: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        worker.run_forever()
    assert requests == ["/api/v1/worker/cleanup-candidates", "/api/v1/worker/claim"]


def test_wednesday_empty_pass_does_not_skip_next_monday_window(tmp_path):
    worker = cleanup_worker(tmp_path, [])
    now = datetime(2026, 9, 30, tzinfo=timezone.utc)
    assert retention.cleanup_if_due(worker, now=now) is True
    monday_before = datetime(2026, 10, 4, 20, 9, tzinfo=timezone.utc)
    monday_after = monday_before + timedelta(minutes=2)
    assert retention.cleanup_if_due(worker, now=monday_before) is False
    assert retention.cleanup_if_due(worker, now=monday_after) is True
    assert retention.cleanup_if_due(worker, now=monday_after + timedelta(days=1)) is False


def test_explicit_cli_apply_runs_despite_same_week_success(tmp_path, monkeypatch):
    worker = cleanup_worker(tmp_path, [])
    assert retention.cleanup_if_due(worker) is True
    job = make_job()
    directory = create_case(tmp_path, job)
    candidate_worker = cleanup_worker(tmp_path, [expired(job)])
    monkeypatch.setattr(retention.WorkerConfig, "load", lambda _path: candidate_worker.config)
    monkeypatch.setattr(retention, "LocalWorker", lambda _config: candidate_worker)
    assert retention.main(["--apply"]) == 0
    assert not directory.exists()
    marker = json.loads(retention._maintenance_path(candidate_worker).read_text(encoding="utf-8"))
    assert marker["last_result"]["cases"] == [directory.name]


def test_fresh_generation_does_not_reuse_old_complete_marker(tmp_path):
    old = make_job()
    old_dir = create_case(tmp_path, old, pipeline_complete=True)
    write_delivery(old_dir)
    generation = str(uuid.uuid4())
    new = make_job(case_id=old["case_id"], local_generation=generation, reset_local_case=True)
    cloud = Cloud(new)
    calls = []

    class Pipeline:
        def run(self, **kwargs):
            calls.append(kwargs["output_dir"])
            write_delivery(kwargs["output_dir"])
            return SimpleNamespace(delivery_status="complete")

    worker_for(tmp_path, cloud, Pipeline).run_once()
    assert calls == [tmp_path / f"cloud_{generation}"] and cloud.paper_calls == 1
    state = json.loads((calls[0] / ".cloud-worker.json").read_text())
    assert state["local_generation"] == generation and state["case_id"] == old["case_id"]
    assert old_dir.is_dir()
    # Process restart or later delivery retry keeps this generation's completion.
    worker_for(tmp_path, cloud, lambda: pytest.fail("new generation science repeated")).run_once()
    cloud.job = make_job(case_id=old["case_id"], local_generation=generation)
    worker_for(tmp_path, cloud, lambda: pytest.fail("delivery retry restarted science")).run_once()
    assert len(calls) == 1


def test_same_generation_interruption_resumes_existing_files(tmp_path):
    generation = str(uuid.uuid4())
    job = make_job(local_generation=generation, reset_local_case=True)
    directory = create_case(tmp_path, job, pipeline_complete=False)
    (directory / "checkpoint.bin").write_bytes(b"checkpoint")
    cloud = Cloud(job)

    class Pipeline:
        def run(self, **kwargs):
            assert kwargs["resume"] is True
            assert (kwargs["output_dir"] / "checkpoint.bin").read_bytes() == b"checkpoint"
            write_delivery(kwargs["output_dir"])
            return SimpleNamespace(delivery_status="complete")

    worker_for(tmp_path, cloud, Pipeline).run_once()
    assert cloud.uploads and (directory / "checkpoint.bin").exists()
