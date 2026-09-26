"""Offline pull-worker protocol tests; scientific/model workers never run."""
from __future__ import annotations

import importlib
import io
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from tests import web_test_env  # noqa: F401
from geng_agent.web import worker_api
from geng_agent.web.db import Base, SessionLocal, init_database
from geng_agent.web.models import CaseRecord, JobRecord, WorkerAssignment, utc_now

app_module = importlib.import_module("geng_agent.web.app")
TOKEN = "offline-test-worker-token-that-is-never-a-real-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
BASE = "/api/v1/worker"


@pytest.fixture(autouse=True)
def database(monkeypatch):
    init_database()
    with SessionLocal() as session:
        for table in reversed(Base.metadata.sorted_tables):
            session.execute(delete(table))
        session.commit()
    configured = replace(worker_api.settings, execution_mode="pull", worker_token=TOKEN)
    monkeypatch.setattr(worker_api, "settings", configured)
    monkeypatch.setattr(app_module, "settings", configured)
    monkeypatch.setattr(app_module, "_dispatch_review", lambda _job: None)


@pytest.fixture
def client():
    with TestClient(app_module.app) as instance:
        yield instance


def create_paper(client):
    if not client.cookies:
        registration = client.post("/api/v1/auth/register", json={
            "email": "pull@example.com", "password": "a-long-offline-password",
        })
        assert registration.status_code == 201, registration.text
        client.headers["X-CSRF-Token"] = registration.json()["csrf_token"]
    response = client.post("/api/v1/cases", data={"display_name": "本地论文"},
                           files={"pdf_file": ("paper.pdf", b"%PDF-1.4 original", "application/pdf")})
    assert response.status_code == 202, response.text
    with SessionLocal() as session:
        case = session.get(CaseRecord, response.json()["case_id"])
        job = session.scalar(select(JobRecord).where(JobRecord.case_id == case.id))
        return case.id, job.id, Path(case.directory)


def claim(client, worker_id="pc-one"):
    return client.post(f"{BASE}/claim", headers=AUTH, json={"worker_id": worker_id})


def bundle_bytes(content=b"transport-only payload"):
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr("任意目录/输出.txt", content)
    return target.getvalue()


def complete(client, job_id, payload=None, worker_id="pc-one"):
    return client.post(f"{BASE}/jobs/{job_id}/complete", headers=AUTH,
                       data={"worker_id": worker_id}, files={
                           "bundle": ("../../untrusted.zip", bundle_bytes() if payload is None else payload, "application/zip"),
                       })


def finish(client, job_id, **body):
    return client.post(f"{BASE}/jobs/{job_id}/finish", headers=AUTH,
                       json={"worker_id": "pc-one", "status": "failed", **body})


def test_worker_authentication_and_mode(client, monkeypatch):
    for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": f"Basic {TOKEN}"}):
        response = client.post(f"{BASE}/claim", headers=headers, json={"worker_id": "pc-one"})
        assert response.status_code == 401
    assert claim(client).json() == {"job": None}
    assert claim(client, "../../escape").status_code == 422
    monkeypatch.setattr(worker_api, "settings", replace(worker_api.settings, worker_token=""))
    assert claim(client).status_code == 503
    monkeypatch.setattr(worker_api, "settings", replace(worker_api.settings, execution_mode="local"))
    assert claim(client).status_code == 404


def test_serial_claim_recovery_and_no_heartbeat_expiry_reassignment(client):
    case_id, job_id, _root = create_paper(client)
    second_case, second_id, _root = create_paper(client)
    first = claim(client).json()["job"]
    assert first == {"job_id": job_id, "case_id": case_id, "display_name": "本地论文",
                     "paper_url": f"{BASE}/jobs/{job_id}/paper", "pipeline_complete": False}
    with SessionLocal() as session:
        assignment = session.get(WorkerAssignment, job_id)
        assignment.heartbeat_at = utc_now() - timedelta(days=30)
        session.commit()
    assert claim(client).json()["job"] == first
    assert claim(client, "pc-two").json()["job"]["job_id"] == second_id
    assert claim(client, "pc-three").json() == {"job": None}
    with SessionLocal() as session:
        assert session.get(WorkerAssignment, job_id).worker_id == "pc-one"
        assert session.get(JobRecord, job_id).attempt == 1
        assert session.get(JobRecord, second_id).case_id == second_case


@pytest.mark.parametrize("workers", [("pc-one", "pc-two"), ("pc-one", "pc-one")])
def test_concurrent_claim_is_atomic(client, workers):
    _case_id, job_id, _root = create_paper(client)
    if workers[0] == workers[1]:
        create_paper(client)
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(lambda worker: claim(client, worker), workers))
    assert [response.status_code for response in responses] == [200, 200]
    assigned = [response.json()["job"] for response in responses if response.json()["job"]]
    assert all(job["job_id"] == job_id for job in assigned)
    assert len(assigned) == (2 if workers[0] == workers[1] else 1)
    with SessionLocal() as session:
        assert len(session.scalars(select(WorkerAssignment)).all()) == 1
        assert session.get(JobRecord, job_id).attempt == 1


def test_original_pdf_ownership_and_path_boundaries(client):
    _case_id, job_id, root = create_paper(client)
    _second_case, second_id, second_root = create_paper(client)
    claim(client)
    claim(client, "pc-two")
    url = f"{BASE}/jobs/{job_id}/paper"
    response = client.get(url, headers={**AUTH, "X-Worker-Id": "pc-one"})
    assert response.status_code == 200 and response.content == b"%PDF-1.4 original"
    assert client.get(url, headers={**AUTH, "X-Worker-Id": "pc-two"}).status_code == 404
    assert client.get(url, headers={"X-Worker-Id": "pc-one"}).status_code == 401
    assert complete(client, second_id).status_code == 404
    assert finish(client, second_id).status_code == 404
    assert not (second_root / "exports").exists()
    with SessionLocal() as session:
        case = session.scalar(select(CaseRecord).where(CaseRecord.directory == str(root)))
        case.paper_path = str(second_root / "paper/paper.pdf")
        session.commit()
    assert client.get(url, headers={**AUTH, "X-Worker-Id": "pc-one"}).status_code == 404


def test_upload_download_and_idempotent_completion(client):
    case_id, job_id, root = create_paper(client)
    claim(client)
    first_bundle = bundle_bytes(b"original result")
    response = complete(client, job_id, first_bundle)
    assert response.status_code == 200 and response.json()["status"] == "succeeded"
    destination = root / "exports" / f"{job_id}.zip"
    assert destination.read_bytes() == first_bundle
    assert complete(client, job_id, bundle_bytes(b"replacement")).status_code == 200
    assert destination.read_bytes() == first_bundle
    downloaded = client.get(f"/api/v1/cases/{case_id}/download")
    assert downloaded.status_code == 200 and downloaded.content == first_bundle
    with SessionLocal() as session:
        job = session.get(JobRecord, job_id)
        assert job.options["pipeline_complete"] and job.finished_at
        assert not session.get(WorkerAssignment, job_id).active
    assert not list(root.rglob("*.uploading"))
    assert finish(client, job_id).status_code == 409
    assert claim(client).json() == {"job": None}


def test_bad_or_oversize_zip_never_publishes_partial_results(client, monkeypatch):
    case_id, job_id, root = create_paper(client)
    claim(client)
    assert complete(client, job_id, b"not a ZIP").status_code == 400
    monkeypatch.setattr(worker_api, "settings", replace(worker_api.settings, max_bundle_bytes=5))
    assert complete(client, job_id).status_code == 413
    assert client.get(f"/api/v1/cases/{case_id}/download").status_code == 409
    assert not list(root.rglob("*.uploading"))
    assert not list(root.rglob("*.zip"))
    assert claim(client).json()["job"]["job_id"] == job_id


def test_failed_atomic_replace_keeps_job_recoverable(client, monkeypatch):
    _case_id, job_id, root = create_paper(client)
    claim(client)

    def disk_failure(source, _destination):
        assert Path(source).is_file()
        raise OSError("offline simulated disk failure")

    monkeypatch.setattr(worker_api.os, "replace", disk_failure)
    with pytest.raises(OSError, match="simulated disk failure"):
        complete(client, job_id)
    assert not list(root.rglob("*.uploading"))
    assert not list(root.rglob("*.zip"))
    assert claim(client).json()["job"]["job_id"] == job_id
    with SessionLocal() as session:
        assert session.get(JobRecord, job_id).status == "running"
        assert session.get(WorkerAssignment, job_id).active


def test_cancel_during_transfer_prevents_publication(client, monkeypatch):
    case_id, job_id, root = create_paper(client)
    claim(client)
    original = worker_api.zipfile.is_zipfile

    def cancel_after_transfer(path):
        result = original(path)
        with SessionLocal() as session:
            job = session.get(JobRecord, job_id)
            job.status, job.cancel_requested = "cancel_requested", True
            session.commit()
        return result

    monkeypatch.setattr(worker_api.zipfile, "is_zipfile", cancel_after_transfer)
    assert complete(client, job_id).status_code == 409
    assert not list(root.rglob("*.uploading"))
    assert not list(root.rglob("*.zip"))
    assert client.get(f"/api/v1/cases/{case_id}/download").status_code == 409
    assert finish(client, job_id, status="cancelled").status_code == 200


def test_export_link_is_not_followed(client, monkeypatch):
    _case_id, job_id, root = create_paper(client)
    claim(client)
    original = worker_api.path_is_link
    monkeypatch.setattr(worker_api, "path_is_link", lambda path: path == root / "exports" or original(path))
    assert complete(client, job_id).status_code == 409
    assert not (root / "exports").exists()


def test_running_cancellation_is_observed_and_retains_assignment(client):
    case_id, job_id, _root = create_paper(client)
    claim(client)
    assert client.post(f"/api/v1/cases/{case_id}/cancel").status_code == 202
    heartbeat = client.post(f"{BASE}/jobs/{job_id}/heartbeat", headers=AUTH, json={"worker_id": "pc-one"})
    assert heartbeat.json() == {"status": "cancel_requested", "cancel_requested": True}
    assert claim(client).json()["job"]["job_id"] == job_id
    assert claim(client, "pc-two").json() == {"job": None}
    assert complete(client, job_id).status_code == 409
    response = finish(client, job_id, status="cancelled")
    assert response.status_code == 200 and response.json()["status"] == "cancelled"
    assert finish(client, job_id, status="cancelled").status_code == 200
    with SessionLocal() as session:
        assert not session.get(WorkerAssignment, job_id).active


def test_queued_cancellation_is_not_claimed(client):
    case_id, job_id, _root = create_paper(client)
    assert client.post(f"/api/v1/cases/{case_id}/cancel").status_code == 202
    assert claim(client).json() == {"job": None}
    with SessionLocal() as session:
        assert session.get(JobRecord, job_id).status == "cancelled"
        assert session.get(WorkerAssignment, job_id) is None


@pytest.mark.parametrize("concurrent_status,expected_status,expected_code", [
    ("running", "cancel_requested", 202),
    ("succeeded", "succeeded", 409),
])
def test_cancel_uses_current_status_after_concurrent_claim_or_completion(
    client, monkeypatch, concurrent_status, expected_status, expected_code,
):
    case_id, job_id, _root = create_paper(client)
    original = app_module._latest_job

    def change_after_read(session, target_case_id):
        stale_job = original(session, target_case_id)
        assert stale_job.status == "queued"
        with SessionLocal() as other:
            current = other.get(JobRecord, job_id)
            current.status = concurrent_status
            other.commit()
        return stale_job

    monkeypatch.setattr(app_module, "_latest_job", change_after_read)
    response = client.post(f"/api/v1/cases/{case_id}/cancel")
    assert response.status_code == expected_code, response.text
    with SessionLocal() as session:
        current = session.get(JobRecord, job_id)
        assert current.status == expected_status
        assert current.cancel_requested == (expected_code == 202)


def test_failed_retry_returns_to_same_pc_and_preserves_pipeline_completion(client):
    case_id, job_id, _root = create_paper(client)
    claim(client)
    response = finish(client, job_id, error_code="delivery_package", error_message="disk unavailable", pipeline_complete=True)
    assert response.status_code == 200 and response.json()["status"] == "failed"
    assert finish(client, job_id).status_code == 200
    assert client.post(f"/api/v1/cases/{case_id}/retry").status_code == 202
    assert claim(client, "pc-two").json() == {"job": None}
    retry = claim(client).json()["job"]
    assert retry["job_id"] != job_id and retry["case_id"] == case_id and retry["pipeline_complete"]
    with SessionLocal() as session:
        old = session.get(JobRecord, job_id)
        assert old.options["pipeline_complete"] and old.error_code == "delivery_package"
        assert not session.get(WorkerAssignment, job_id).active


def test_unknown_and_wrong_worker_operations_are_denied(client):
    _case_id, job_id, _root = create_paper(client)
    claim(client)
    for target in (job_id, str(uuid.uuid4())):
        assert client.post(f"{BASE}/jobs/{target}/heartbeat", headers=AUTH,
                           json={"worker_id": "pc-other"}).status_code == 404
        assert complete(client, target, worker_id="pc-other").status_code == 404
        assert finish(client, target, worker_id="pc-other").status_code == 404
    assert client.post(f"{BASE}/jobs/{job_id}/finish", headers=AUTH,
                       json={"worker_id": "pc-one", "status": "succeeded"}).status_code == 422


def test_legacy_ownerless_jobs_are_not_claimed(client):
    with SessionLocal() as session:
        case = CaseRecord(id=str(uuid.uuid4()), display_name="historical", directory="old-case", paper_path="old.pdf")
        session.add(case)
        session.flush()
        session.add(JobRecord(id=str(uuid.uuid4()), case_id=case.id, status="queued"))
        session.commit()
    assert claim(client).json() == {"job": None}
