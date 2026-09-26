"""Offline account/upload/delivery integration; never starts a model worker."""
from __future__ import annotations

import hashlib
import io
import uuid
import zipfile
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, inspect, select, text

from tests import web_test_env  # noqa: F401
from geng_agent.web.app import app, _recover_interrupted_eager_jobs, _dispatch_review as _REAL_DISPATCH
from geng_agent.web.auth import COOKIE
from geng_agent.web.db import Base, SessionLocal, init_database
from geng_agent.web.delivery import build_delivery, bundle_path, local_file
from geng_agent.web.models import CaseRecord, JobRecord, SessionRecord, UserRecord
from geng_agent.web.tasks import run_review

PASSWORD = "a-long-test-password"


@pytest.fixture(autouse=True)
def database(monkeypatch):
    init_database()
    with SessionLocal() as session:
        for table in reversed(Base.metadata.sorted_tables):
            session.execute(delete(table))
        session.commit()
    monkeypatch.setattr("geng_agent.web.app._dispatch_review", lambda _job: None)


@pytest.fixture
def client():
    with TestClient(app) as instance:
        yield instance


def register(client, email="reader@example.com"):
    response = client.post("/api/v1/auth/register", json={"email": email, "password": PASSWORD})
    assert response.status_code == 201, response.text
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    return response


def upload(client, name="测试论文"):
    response = client.post("/api/v1/cases", data={"display_name": name}, files={"pdf_file": ("../paper.pdf", b"%PDF-1.4 test", "application/pdf")})
    assert response.status_code == 202, response.text
    case_id = response.json()["case_id"]
    with SessionLocal() as session:
        case = session.get(CaseRecord, case_id)
        job = session.scalar(select(JobRecord).where(JobRecord.case_id == case_id))
        return case_id, job.id, Path(case.directory)


def write_delivery(root):
    # Minimal mock payloads exercise transport, not scientific reproduction.
    for name, content in {
        "result_review.docx": b"mock comparison report", "reproduction_report.docx": b"mock reproduction report",
        "repro_project/README.md": b"Run each task independently",
        "repro_project/task_packages/t01_T1/src/main.py": b"print('experiment')",
        "repro_project/task_packages/t01_T1/requirements.txt": b"numpy",
        "repro_project/task_packages/t01_T1/outputs/result.csv": b"x,y\n1,2",
        "repro_project/task_packages/t01_T1/configs/full.json": b'{"size": 2}',
        "repro_project/task_packages/t01_T1/execution_evidence.json": b"{}",
        "repro_project/task_packages/t01_T1/outputs/task_agent_result.json": b"{}",
        "repro_project/task_packages/t01_T1/outputs/execution_receipt.json": b"{}",
        "repro_project/task_packages/t01_T1/task_notes/plan.md": b"intermediate planning",
        "repro_project/task_packages/t01_T1/audit/transcript.json": b"private task audit",
        "audit/transcript.json": b"private intermediate log", "engineering_facts.json": b"intermediate",
        "repro_project/.env": b"private config", "repro_project/.venv/file": b"environment",
        "repro_project/task_packages/t01_T1/__pycache__/main.pyc": b"cache",
    }.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def test_anonymous_access_and_removed_interfaces(client):
    assert client.get("/api/v1/cases").status_code == 401
    assert client.post("/api/v1/cases", files={"pdf_file": ("a.pdf", b"%PDF-a")}).status_code == 401
    for path in ("/api/v1/jobs/x/events/live", "/api/v1/jobs/x/events", "/api/v1/artifacts/x", "/api/v1/cases/x/artifacts", "/api/v1/metrics"):
        assert client.get(path).status_code == 404
    assert client.post("/api/v1/cases/import").status_code in {404, 405}
    assert set(client.get("/api/v1/health").json()) == {"ok"}


def test_registration_hash_session_csrf_and_logout(client):
    response = register(client, "Reader@EXAMPLE.com")
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie
    token = client.cookies.get(COOKIE)
    with SessionLocal() as session:
        user = session.scalar(select(UserRecord))
        stored = session.scalar(select(SessionRecord))
        assert user.email == "reader@example.com" and PASSWORD not in user.password_hash
        assert stored.token_hash == hashlib.sha256(token.encode()).hexdigest()
    assert client.get("/api/v1/auth/session").json()["user"]["email"] == "reader@example.com"
    assert client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert client.post("/api/v1/auth/logout").status_code == 204
    client.cookies.set(COOKIE, token)
    assert client.get("/api/v1/cases").status_code == 401


def test_login_rotation_expiry_and_wrong_password(client):
    register(client)
    old_token = client.cookies.get(COOKIE)
    assert client.post("/api/v1/auth/login", json={"email": "reader@example.com", "password": "incorrect-password"}).status_code == 401
    response = client.post("/api/v1/auth/login", json={"email": "reader@example.com", "password": PASSWORD})
    assert response.status_code == 200 and client.cookies.get(COOKIE) != old_token
    with SessionLocal() as session:
        assert session.get(SessionRecord, hashlib.sha256(old_token.encode()).hexdigest()) is None
        record = session.scalar(select(SessionRecord))
        record.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        session.commit()
    assert client.get("/api/v1/cases").status_code == 401


def test_cross_origin_and_duplicate_account(client):
    assert client.post("/api/v1/auth/register", headers={"Origin": "https://other.example"}, json={"email": "reader@example.com", "password": PASSWORD}).status_code == 403
    register(client)
    assert client.post("/api/v1/auth/register", json={"email": "READER@example.com", "password": PASSWORD}).status_code == 409
    assert client.post("/api/v1/auth/logout", headers={"Origin": "https://other.example"}).status_code == 403
    assert client.post("/api/v1/auth/logout", headers={"Origin": "https://testserver"}).status_code == 403


def test_login_throttle(client):
    for _ in range(15):
        assert client.post("/api/v1/auth/login", json={"email": "unknown@example.com", "password": PASSWORD}).status_code == 401
    assert client.post("/api/v1/auth/login", json={"email": "unknown@example.com", "password": PASSWORD}).status_code == 429


def test_secure_cookie_and_registration_switch(client, monkeypatch):
    from geng_agent.web import auth
    monkeypatch.setattr(auth, "settings", replace(auth.settings, cookie_secure=True))
    response = register(client)
    assert "Secure" in response.headers["set-cookie"]
    monkeypatch.setattr(auth, "settings", replace(auth.settings, registration_enabled=False))
    assert client.post("/api/v1/auth/register", json={"email": "new@example.com", "password": PASSWORD}).status_code == 403


def test_upload_and_cross_account_isolation(client):
    register(client)
    case_id, job_id, root = upload(client)
    assert (root / "paper/paper.pdf").is_file()
    detail = client.get(f"/api/v1/cases/{case_id}").json()
    assert detail["status"] == "queued" and detail["download_url"] is None
    assert not set(detail) & {"artifacts", "phases", "recent_events", "research", "directory", "paper_path"}
    assert client.post(f"/api/v1/cases/{case_id}/retry").status_code == 409
    assert client.get(f"/api/v1/cases/{case_id}/download").status_code == 409
    with TestClient(app) as other:
        register(other, "other@example.com")
        assert other.get("/api/v1/cases").json()["items"] == []
        for method, path in [("get", ""), ("get", "/download"), ("post", "/retry"), ("post", "/cancel")]:
            assert getattr(other, method)(f"/api/v1/cases/{case_id}{path}").status_code == 404


def test_legacy_ownerless_case_is_not_public(client):
    register(client)
    case_id = str(uuid.uuid4())
    with SessionLocal() as session:
        session.add(CaseRecord(id=case_id, display_name="old", directory="old-case", paper_path="old.pdf"))
        session.flush()
        session.add(JobRecord(id=str(uuid.uuid4()), case_id=case_id, status="running"))
        session.commit()
    assert client.get("/api/v1/cases").json()["items"] == []
    assert client.get(f"/api/v1/cases/{case_id}").status_code == 404
    assert _recover_interrupted_eager_jobs() == []


def test_pdf_limits_and_csrf(client, monkeypatch):
    import importlib
    module = importlib.import_module("geng_agent.web.app")
    register(client)
    assert client.post("/api/v1/cases", files={"pdf_file": ("fake.pdf", b"not pdf")}).status_code == 400
    assert client.post("/api/v1/cases", headers={"X-CSRF-Token": ""}, files={"pdf_file": ("a.pdf", b"%PDF-a")}).status_code == 403
    monkeypatch.setattr(module, "settings", replace(module.settings, max_pdf_bytes=8))
    assert client.post("/api/v1/cases", files={"pdf_file": ("a.pdf", b"%PDF-12345")}).status_code == 413


def test_completed_job_downloads_only_final_delivery(client, monkeypatch):
    register(client)
    case_id, job_id, root = upload(client)
    def run(_self, **kwargs):
        assert kwargs["run_repro"] is True and kwargs["resume"] is True
        assert kwargs["analysis_backend"] == "codex"
        kwargs["progress"].emit("step.completed", phase="paper_analysis", step="facts_initial")
        write_delivery(kwargs["output_dir"])
        return SimpleNamespace(delivery_status="complete")
    monkeypatch.setattr("geng_agent.web.tasks.ReviewPipeline.run", run)
    run_review.run(job_id)
    detail = client.get(f"/api/v1/cases/{case_id}").json()
    assert detail["status"] == "succeeded"
    response = client.get(detail["download_url"])
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        prefix = "复现交付包/复现任务/t01_T1/"
        assert {info.filename for info in archive.infolist() if not info.is_dir()} == {
            "复现交付包/论文复现结果对比报告.docx", "复现交付包/本地复现报告.docx",
            prefix + "代码/src/main.py", prefix + "代码/requirements.txt",
            prefix + "代码/configs/full.json", prefix + "复现结果/outputs/result.csv",
            prefix + "readme.md",
        }
        assert all(name.startswith("复现交付包/") for name in archive.namelist())
        assert archive.read(prefix + "复现结果/outputs/result.csv") == b"x,y\n1,2"
        readme = archive.read(prefix + "readme.md").decode("utf-8")
        for relative in ("代码/src/main.py", "代码/requirements.txt", "代码/configs/full.json",
                         "复现结果/outputs/result.csv"):
            assert relative in readme
    assert (root / "audit/transcript.json").exists()
    assert client.post(f"/api/v1/cases/{case_id}/retry").status_code == 409


def test_package_retry_does_not_rerun_pipeline(client, monkeypatch):
    register(client)
    case_id, job_id, root = upload(client)
    calls = []
    def run(_self, **kwargs):
        calls.append(1)
        write_delivery(root)
        return SimpleNamespace(delivery_status="complete")
    monkeypatch.setattr("geng_agent.web.tasks.ReviewPipeline.run", run)
    def broken(*_args):
        raise OSError("mock disk unavailable")
    monkeypatch.setattr("geng_agent.web.tasks.build_delivery", broken)
    run_review.run(job_id)
    detail = client.get(f"/api/v1/cases/{case_id}").json()
    assert detail["status"] == "failed" and "只重新打包" in detail["message"]
    assert "mock disk" not in str(detail)
    monkeypatch.setattr("geng_agent.web.tasks.build_delivery", build_delivery)
    assert client.post(f"/api/v1/cases/{case_id}/retry").status_code == 202
    with SessionLocal() as session:
        retry = session.scalar(select(JobRecord).where(JobRecord.case_id == case_id, JobRecord.id != job_id))
    run_review.run(retry.id)
    assert calls == [1]
    assert client.get(f"/api/v1/cases/{case_id}").json()["download_url"]


@pytest.mark.parametrize("delivery_status", ["partial", "blocked"])
def test_incomplete_pipeline_never_offers_final_download(client, monkeypatch, delivery_status):
    register(client)
    case_id, job_id, root = upload(client)
    write_delivery(root)
    monkeypatch.setattr("geng_agent.web.tasks.ReviewPipeline.run", lambda *_args, **_kwargs: SimpleNamespace(delivery_status=delivery_status))
    run_review.run(job_id)
    detail = client.get(f"/api/v1/cases/{case_id}").json()
    assert detail["status"] == "failed" and not detail["download_url"]
    assert (root / "result_review.docx").is_file()


def test_cancel_before_start_keeps_paper_and_does_not_call_pipeline(client, monkeypatch):
    register(client)
    case_id, job_id, root = upload(client)
    monkeypatch.setattr("geng_agent.web.tasks.ReviewPipeline.run", lambda *_a, **_k: pytest.fail("cancelled job started"))
    assert client.post(f"/api/v1/cases/{case_id}/cancel").status_code == 202
    run_review.run(job_id)
    assert client.get(f"/api/v1/cases/{case_id}").json()["status"] == "cancelled"
    assert (root / "paper/paper.pdf").is_file()


def test_interrupted_job_recovery_and_missing_bundle(client, monkeypatch):
    register(client)
    case_id, job_id, root = upload(client)
    dispatched = []
    monkeypatch.setattr("geng_agent.web.app._dispatch_review", dispatched.append)
    assert _recover_interrupted_eager_jobs() == [job_id]
    assert dispatched == [job_id]
    with SessionLocal() as session:
        job = session.get(JobRecord, job_id)
        job.status, job.options = "succeeded", {"pipeline_complete": True}
        session.commit()
    detail = client.get(f"/api/v1/cases/{case_id}").json()
    assert detail["can_retry"] and detail["download_url"] is None
    assert client.get(f"/api/v1/cases/{case_id}/download").status_code == 404


def test_package_path_escape_and_missing_report(tmp_path):
    with pytest.raises(ValueError):
        local_file(tmp_path, "../secret")
    with pytest.raises(FileNotFoundError):
        build_delivery(tmp_path, str(uuid.uuid4()))
    assert not list(tmp_path.glob("exports/*.zip"))


def test_package_omits_unselected_link_without_blocking_delivery(tmp_path, monkeypatch):
    write_delivery(tmp_path)
    target = tmp_path / "audit/transcript.json"
    link = tmp_path / "repro_project/private.txt"
    try:
        link.symlink_to(target)
    except OSError:
        # Windows accounts may lack symlink permission. An irrelevant root
        # file must not be inspected or projected into the task delivery.
        link.write_bytes(b"linked file stand-in")
        from geng_agent.web import delivery
        actual = delivery.path_is_link
        monkeypatch.setattr(delivery, "path_is_link", lambda path: path == link or actual(path))
    with zipfile.ZipFile(build_delivery(tmp_path, str(uuid.uuid4()))) as archive:
        assert not any("private.txt" in name or "transcript" in name for name in archive.namelist())
        assert "复现交付包/复现任务/t01_T1/代码/src/main.py" in archive.namelist()


def test_package_rejects_link_in_delivered_report_path(tmp_path, monkeypatch):
    write_delivery(tmp_path)
    link = tmp_path / "result_review.docx"
    link.unlink()
    target = tmp_path / "audit/transcript.json"
    try:
        link.symlink_to(target)
    except OSError:
        link.write_bytes(b"linked report stand-in")
        from geng_agent.web import delivery
        actual = delivery.path_is_link
        monkeypatch.setattr(delivery, "path_is_link", lambda path: path == link or actual(path))
    with pytest.raises(ValueError):
        build_delivery(tmp_path, str(uuid.uuid4()))
    assert not list(tmp_path.glob("exports/*.zip"))


def test_existing_local_database_is_preserved(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'legacy.db').as_posix()}")
    with engine.begin() as connection:
        # Extra legacy phase/event data survives additive account creation.
        CaseRecord.__table__.create(connection)
        JobRecord.__table__.create(connection)
        connection.execute(text("ALTER TABLE jobs ADD COLUMN current_phase VARCHAR(64)"))
        connection.execute(text("CREATE TABLE job_events (id INTEGER PRIMARY KEY, message TEXT)"))
        connection.execute(text("INSERT INTO job_events VALUES (1, 'retained local history')"))
        connection.execute(CaseRecord.__table__.insert().values(id="old", display_name="old", directory="old", paper_path="paper.pdf"))
    Base.metadata.create_all(engine)
    with engine.connect() as connection:
        assert connection.execute(text("SELECT message FROM job_events")).scalar() == "retained local history"
        assert connection.execute(text("SELECT id FROM cases")).scalar() == "old"
    assert {"web_users", "web_sessions", "web_login_attempts"} <= set(inspect(engine).get_table_names())
    engine.dispose()


def test_queue_failure_is_recoverable_without_exposing_internal_errors(client, monkeypatch):
    import importlib
    module = importlib.import_module("geng_agent.web.app")
    register(client)
    case_id, job_id, _root = upload(client)
    monkeypatch.setattr(module, "settings", replace(module.settings, celery_eager=False))
    def broken(*_args):
        raise ConnectionError("redis://private-host:6379 secret")
    monkeypatch.setattr(run_review, "delay", broken)
    # The real function is recovered via its separately imported binding.
    _REAL_DISPATCH(job_id)
    detail = client.get(f"/api/v1/cases/{case_id}").json()
    assert detail["status"] == "failed" and detail["can_retry"]
    assert "private-host" not in str(detail)
