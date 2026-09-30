"""An expired local-mode case must start in a new directory, not old evidence."""
from pathlib import Path
from types import SimpleNamespace

from tests import web_test_env  # noqa: F401
from tests.test_web_portal import client, database, register, upload, write_delivery  # noqa: F401
from geng_agent.web.db import SessionLocal
from geng_agent.web.models import JobRecord
from geng_agent.web.tasks import run_review


def test_expired_local_mode_retry_leaves_old_evidence_and_uses_fresh_directory(client, monkeypatch):
    register(client)
    case_id, job_id, root = upload(client)
    write_delivery(root)
    (root / "keep-old-evidence.txt").write_text("old evidence", encoding="utf-8")
    with SessionLocal() as session:
        job = session.get(JobRecord, job_id)
        job.status = "succeeded"
        job.options = {"pipeline_complete": False, "artifacts_expired_at": "2026-09-30T00:00:00Z"}
        session.commit()
    response = client.post(f"/api/v1/cases/{case_id}/retry")
    assert response.status_code == 202
    with SessionLocal() as session:
        job = session.query(JobRecord).filter(JobRecord.case_id == case_id, JobRecord.id != job_id).one()
        new_job_id = job.id
        assert job.options["local_generation"] == new_job_id
    calls = []
    def run(_pipeline, **kwargs):
        destination = Path(kwargs["output_dir"])
        calls.append(destination)
        destination.mkdir(parents=True, exist_ok=True)
        write_delivery(destination)
        return SimpleNamespace(delivery_status="complete")
    monkeypatch.setattr("geng_agent.web.tasks.ReviewPipeline.run", run)
    run_review.run(new_job_id)
    assert calls == [root / "runs" / new_job_id]
    assert (root / "keep-old-evidence.txt").read_text(encoding="utf-8") == "old evidence"
    detail = client.get(f"/api/v1/cases/{case_id}").json()
    assert detail["status"] == "succeeded" and not detail["artifacts_expired_at"]
    assert client.get(detail["download_url"]).status_code == 200
