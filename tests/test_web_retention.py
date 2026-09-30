"""Retention exercises only isolated upload fixtures, never historical cases."""
from __future__ import annotations

import importlib
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests import web_test_env  # noqa: F401
from tests.test_web_portal import client, database, register, upload  # noqa: F401
from geng_agent.web import retention, worker_api
from geng_agent.web.app import app
from geng_agent.web.db import SessionLocal
from geng_agent.web.delivery import bundle_path
from geng_agent.web.models import CaseRecord, JobRecord, UserRecord, WorkerAssignment, utc_now

TOKEN = "offline-retention-worker-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    configured = replace(worker_api.settings, execution_mode="pull", worker_token=TOKEN,
                         artifact_retention_days=7)
    monkeypatch.setattr(worker_api, "settings", configured)
    monkeypatch.setattr(retention, "settings", configured)
    monkeypatch.setattr(importlib.import_module("geng_agent.web.app"), "settings", configured)


def completed_case(client, *, status="succeeded", days=8, archive=True):
    register(client)
    case_id, job_id, root = upload(client)
    now = utc_now()
    with SessionLocal() as session:
        job = session.get(JobRecord, job_id)
        job.status = status
        job.finished_at = now - timedelta(days=days)
        job.options = {"pipeline_complete": True}
        session.add(WorkerAssignment(job_id=job_id, worker_id="pc-one", active=False))
        session.commit()
    path = bundle_path(root, job_id)
    if archive:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"offline-archive-transport-fixture")
    return case_id, job_id, root, path, now


def test_default_dry_run_and_apply_only_touch_registered_final_zip(client):
    case_id, job_id, root, path, now = completed_case(client)
    unknown = path.with_name(f"{uuid.uuid4()}.zip")
    unknown.write_bytes(b"not registered")
    intermediate = root / "audit" / "run.log"
    intermediate.parent.mkdir()
    intermediate.write_text("retained", encoding="utf-8")
    dry = retention.cleanup_archives(now=now)
    assert dry["apply"] is False and [item["job_id"] for item in dry["items"]] == [job_id]
    assert path.exists()
    with SessionLocal() as session:
        assert retention.expired_at(session.get(JobRecord, job_id)) is None
    result = retention.cleanup_archives(apply=True, now=now)
    assert not result["errors"] and not path.exists()
    assert unknown.exists() and intermediate.exists() and (root / "paper/paper.pdf").exists()
    with SessionLocal() as session:
        job = session.get(JobRecord, job_id)
        assert job.status == "succeeded" and job.options["pipeline_complete"] is False
        assert retention.expired_at(job) == now.isoformat()
        assert session.get(CaseRecord, case_id) is not None
        assert session.scalar(select(UserRecord)) is not None
    assert retention.cleanup_archives(apply=True, now=now)["items"] == []


@pytest.mark.parametrize("status", ["succeeded", "failed", "cancelled"])
def test_all_old_terminal_jobs_expire_even_when_no_zip(client, status):
    _case, job_id, _root, _path, now = completed_case(client, status=status, archive=False)
    assert retention.cleanup_archives(apply=True, now=now)["items"][0]["archive_exists"] is False
    response = client.post("/api/v1/worker/cleanup-candidates", headers=AUTH, json={"worker_id": "pc-one"})
    assert response.status_code == 200
    assert response.json()["items"][0]["job_id"] == job_id


@pytest.mark.parametrize("reason", ["recent", "queued", "running", "cancel_requested", "assignment", "no_finished_at", "ownerless"])
def test_recent_unfinished_active_or_unowned_cases_are_protected(client, reason):
    case_id, job_id, _root, path, now = completed_case(client)
    with SessionLocal() as session:
        job = session.get(JobRecord, job_id)
        if reason == "recent":
            job.finished_at = now - timedelta(days=6, hours=23)
        elif reason in {"queued", "running", "cancel_requested"}:
            job.status = reason
        elif reason == "assignment":
            session.get(WorkerAssignment, job_id).active = True
        elif reason == "no_finished_at":
            job.finished_at = None
        else:
            session.get(CaseRecord, case_id).owner_id = None
        session.commit()
    assert retention.cleanup_archives(apply=True, now=now)["items"] == []
    assert path.exists()


def test_new_queued_retry_protects_all_older_delivery_files(client):
    case_id, _job_id, _root, path, now = completed_case(client)
    with SessionLocal() as session:
        session.add(JobRecord(id=str(uuid.uuid4()), case_id=case_id, status="queued", created_at=now))
        session.commit()
    assert retention.cleanup_archives(apply=True, now=now)["items"] == []
    assert path.exists()


@pytest.mark.parametrize("unsafe", ["outside", "export_link"])
def test_storage_escape_and_links_are_rejected(client, monkeypatch, tmp_path, unsafe):
    case_id, _job_id, root, path, now = completed_case(client)
    if unsafe == "outside":
        outside = tmp_path / root.name
        outside.mkdir()
        with SessionLocal() as session:
            session.get(CaseRecord, case_id).directory = str(outside)
            session.commit()
    else:
        actual = retention.path_is_link
        monkeypatch.setattr(retention, "path_is_link", lambda candidate: candidate == path.parent or actual(candidate))
    result = retention.cleanup_archives(apply=True, now=now)
    assert result["errors"] and path.exists()


def test_expired_delivery_cannot_be_downloaded_or_rebuilt_from_stale_zip(client):
    case_id, job_id, _root, path, now = completed_case(client)
    retention.cleanup_archives(apply=True, now=now)
    # Even if another old process writes a stale ZIP, the expiration record wins.
    path.write_bytes(b"stale archive")
    body = client.get(f"/api/v1/cases/{case_id}").json()
    assert body["artifacts_expired_at"] and body["can_retry"] and body["download_url"] is None
    assert client.get(f"/api/v1/cases/{case_id}/download").status_code == 410
    assert client.get(f"/api/v1/cases/{case_id}/reports/comparison").status_code == 410
    result = client.get(f"/api/v1/cases/{case_id}/result").json()
    assert result["artifacts_expired_at"] and result["available"] is False
    assert result["tasks"] == [] and result["reports"] == []
    assert client.post(f"/api/v1/cases/{case_id}/retry").status_code == 202
    with SessionLocal() as session:
        jobs = list(session.scalars(select(JobRecord).where(JobRecord.case_id == case_id)
                                   .order_by(JobRecord.created_at.desc())).all())
        assert jobs[0].id != job_id
        assert jobs[0].options == {"pipeline_complete": False, "reset_local_case": True,
                                   "local_generation": jobs[0].id}
        new_id = jobs[0].id
    claim = client.post("/api/v1/worker/claim", headers=AUTH, json={"worker_id": "pc-one"}).json()["job"]
    assert claim["job_id"] == new_id and claim["local_generation"] == new_id


def test_resurrected_old_archive_is_removed_again_without_refreshing_expiration(client):
    _case, job_id, _root, path, now = completed_case(client)
    retention.cleanup_archives(apply=True, now=now)
    path.write_bytes(b"late old process output")
    result = retention.cleanup_archives(apply=True, now=now + timedelta(days=7))
    assert len(result["items"]) == 1 and not path.exists()
    with SessionLocal() as session:
        assert retention.expired_at(session.get(JobRecord, job_id)) == now.isoformat()


def test_nonexpired_retry_keeps_current_generation_and_packaging_resume(client):
    case_id, job_id, _root, _path, _now = completed_case(client, status="failed", days=1, archive=False)
    generation = str(uuid.uuid4())
    with SessionLocal() as session:
        job = session.get(JobRecord, job_id)
        job.options = {"pipeline_complete": True, "local_generation": generation}
        session.commit()
    assert client.post(f"/api/v1/cases/{case_id}/retry").status_code == 202
    with SessionLocal() as session:
        latest = session.scalar(select(JobRecord).where(JobRecord.case_id == case_id)
                                .order_by(JobRecord.created_at.desc()))
        assert latest.options == {"pipeline_complete": True, "local_generation": generation}


def test_cleanup_candidates_require_worker_auth_and_exact_assignment(client):
    case_id, job_id, _root, _path, now = completed_case(client)
    retention.cleanup_archives(apply=True, now=now)
    route = "/api/v1/worker/cleanup-candidates"
    assert client.post(route, json={"worker_id": "pc-one"}).status_code == 401
    assert client.post(route, headers=AUTH, json={"worker_id": "pc-two"}).json()["items"] == []
    own = client.post(route, headers=AUTH, json={"worker_id": "pc-one"}).json()
    assert own == {"retention_days": 7, "items": [{"case_id": case_id, "job_id": job_id,
                      "expired_at": now.isoformat(), "local_generation": None}]}
    assert client.post(f"/api/v1/cases/{case_id}/retry").status_code == 202
    assert client.post(route, headers=AUTH, json={"worker_id": "pc-one"}).json()["items"] == []


def test_multiple_old_attempts_all_become_expired_candidates(client):
    case_id, job_id, root, _path, now = completed_case(client)
    old_id = str(uuid.uuid4())
    with SessionLocal() as session:
        session.add(JobRecord(id=old_id, case_id=case_id, status="failed", created_at=now - timedelta(days=10),
                              finished_at=now - timedelta(days=9), options={"pipeline_complete": True}))
        session.add(WorkerAssignment(job_id=old_id, worker_id="pc-one", active=False))
        session.commit()
    old_zip = bundle_path(root, old_id)
    old_zip.write_bytes(b"previous attempt")
    result = retention.cleanup_archives(apply=True, now=now)
    assert {item["job_id"] for item in result["items"]} == {job_id, old_id}
    assert not old_zip.exists()
    candidates = client.post("/api/v1/worker/cleanup-candidates", headers=AUTH, json={"worker_id": "pc-one"}).json()
    assert {item["job_id"] for item in candidates["items"]} == {job_id, old_id}


def test_cleanup_and_user_retry_are_serialized(client, monkeypatch):
    case_id, job_id, _root, path, now = completed_case(client, status="failed")
    entered = threading.Event()
    release = threading.Event()
    real_unlink = Path.unlink
    def delayed_unlink(target, *args, **kwargs):
        if target == path:
            entered.set()
            assert release.wait(5)
        return real_unlink(target, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", delayed_unlink)
    with ThreadPoolExecutor(max_workers=2) as executor:
        cleaning = executor.submit(retention.cleanup_archives, apply=True, now=now)
        assert entered.wait(5)
        retrying = executor.submit(client.post, f"/api/v1/cases/{case_id}/retry")
        release.set()
        assert not cleaning.result(timeout=10)["errors"]
        assert retrying.result(timeout=10).status_code == 202
    with SessionLocal() as session:
        previous = session.get(JobRecord, job_id)
        assert retention.expired_at(previous)
        latest = session.scalar(select(JobRecord).where(JobRecord.case_id == case_id)
                                .order_by(JobRecord.created_at.desc()))
        assert latest.options["pipeline_complete"] is False
        assert latest.options["local_generation"] == latest.id
