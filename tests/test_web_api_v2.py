from __future__ import annotations

import unittest
import json
import shutil
import time
from unittest.mock import patch

from tests import web_test_env  # noqa: F401

from fastapi.testclient import TestClient
from sqlalchemy import delete

from geng_agent.web.app_v2 import app
from geng_agent.web.db import SessionLocal, init_database
from geng_agent.web.models import ArtifactRecord, CaseRecord, ExportRecord, JobEvent, JobRecord
from geng_agent.web.settings import settings


class WebApiV2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if settings.cases_root != web_test_env.WEB_TEST_ROOT:
            raise RuntimeError("Web tests are not using the isolated temporary case root")
        init_database()
        cls.client = TestClient(app)

    def setUp(self) -> None:
        with SessionLocal() as session:
            for model in (JobEvent, ArtifactRecord, ExportRecord, JobRecord, CaseRecord):
                session.execute(delete(model))
            session.commit()
        for directory in settings.cases_root.glob("case_*"):
            if directory.is_dir():
                shutil.rmtree(directory)

    @patch("geng_agent.web.app_v2._dispatch_review")
    def test_create_case_and_enforce_one_active_job(self, dispatch) -> None:
        response = self.client.post(
            "/api/v1/cases",
            files={"pdf_file": ("paper.pdf", b"%PDF-1.4\nminimal", "application/pdf")},
            data={"display_name": "测试航行"},
        )
        self.assertEqual(response.status_code, 202, response.text)
        case_id = response.json()["case_id"]
        dispatch.assert_called_once()

        detail = self.client.get(f"/api/v1/cases/{case_id}")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(len(detail.json()["phases"]), 5)
        self.assertEqual(detail.headers["x-content-type-options"], "nosniff")

        duplicate = self.client.post(f"/api/v1/cases/{case_id}/jobs")
        self.assertEqual(duplicate.status_code, 409)

    def test_rejects_non_pdf_magic_bytes(self) -> None:
        response = self.client.post(
            "/api/v1/cases",
            files={"pdf_file": ("paper.pdf", b"not a pdf", "application/pdf")},
        )
        self.assertEqual(response.status_code, 400)

    @patch("geng_agent.web.app_v2._dispatch_review")
    def test_case_detail_preserves_scientific_failure_and_recent_history(self, dispatch) -> None:
        response = self.client.post("/api/v1/cases", files={"pdf_file": ("paper.pdf", b"%PDF-1.4\nminimal", "application/pdf")})
        case_id, job_id = response.json()["case_id"], response.json()["job_id"]
        with SessionLocal() as session:
            case = session.get(CaseRecord, case_id)
            from pathlib import Path
            (Path(case.directory) / "verification_result.json").write_text(json.dumps({
                "all_successful": False, "tasks": [{"task_id": "T1", "outcome": "not_reproduced", "decision_reason": "Recorded difference"}]
            }), encoding="utf-8")
            job = session.get(JobRecord, job_id)
            job.status = "succeeded"
            for index in range(45):
                session.add(JobEvent(job_id=job_id, event_type="step.completed", phase="task_reproduction", step="generation", message=f"event {index}"))
            session.commit()
        detail = self.client.get(f"/api/v1/cases/{case_id}").json()
        self.assertEqual(detail["job"]["status"], "succeeded")
        self.assertEqual(detail["research"]["tasks"][0]["outcome"], "not_reproduced")
        self.assertFalse(detail["research"]["all_successful"])
        self.assertEqual(len(detail["recent_events"]), 40)
        self.assertEqual(detail["recent_events"][0]["message"], "event 5")
        self.assertEqual(detail["recent_events"][-1]["message"], "event 44")

    @patch("geng_agent.web.app_v2._dispatch_review")
    def test_case_events_expose_live_agent_counts_without_advancing_phase(self, dispatch) -> None:
        from pathlib import Path
        from geng_agent.agent_activity import agent_activity_scope, session_started, session_finished
        from geng_agent.progress import CallbackProgressReporter
        from geng_agent.web.events import append_event

        response = self.client.post("/api/v1/cases", files={"pdf_file": ("paper.pdf", b"%PDF-1.4\nminimal", "application/pdf")})
        case_id, job_id = response.json()["case_id"], response.json()["job_id"]
        with SessionLocal() as session:
            case_dir = Path(session.get(CaseRecord, case_id).directory)
        append_event(job_id, {"type": "phase.started", "phase": "task_reproduction"})
        append_event(job_id, {"type": "step.started", "phase": "task_reproduction", "step": "generation"})
        reporter = CallbackProgressReporter(lambda payload: append_event(job_id, payload))
        with agent_activity_scope(case_dir / "audit", reporter):
            writer = session_started(invocation_id="writer", role="task_writer", label="t1", work_dir=case_dir)
            reviewer = session_started(invocation_id="reviewer", role="task_reporter", label="t2", work_dir=case_dir)
            detail = self.client.get(f"/api/v1/cases/{case_id}").json()
            activity = [event for event in detail["recent_events"] if event["type"] == "agent.started"]
            self.assertEqual(activity[-1]["data"]["active"], {"task_writer": 1, "task_reporter": 1, "total": 2})
            self.assertIn("Writer 1，Reporter 1", activity[-1]["message"])
            with SessionLocal() as session:
                job = session.get(JobRecord, job_id)
                self.assertEqual(job.current_phase, "task_reproduction")
                self.assertEqual(job.current_step, "generation")
            session_finished(writer, ok=True)
            session_finished(reviewer, ok=True)
        detail = self.client.get(f"/api/v1/cases/{case_id}").json()
        completed = [event for event in detail["recent_events"] if event["type"] == "agent.completed"]
        self.assertEqual(completed[-1]["data"]["active"]["total"], 0)

    def test_import_catalogs_existing_case_and_recovers_phase_state(self) -> None:
        case_dir = settings.cases_root / "case_existing"
        paper_dir = case_dir / "paper"
        paper_dir.mkdir(parents=True)
        (paper_dir / "paper.pdf").write_bytes(b"%PDF-1.4\nminimal")
        (case_dir / "workflow.json").write_text(
            json.dumps({"workflow_version": "2"}),
            encoding="utf-8",
        )
        (case_dir / "paper_chunks.json").write_text(
            json.dumps({"chunks": [{"chunk_id": "p1", "text": "paper"}]}),
            encoding="utf-8",
        )

        imported = self.client.post("/api/v1/cases/import")
        self.assertEqual(imported.status_code, 200, imported.text)
        self.assertEqual(imported.json()["count"], 1)

        case_id = imported.json()["imported"][0]
        detail = self.client.get(f"/api/v1/cases/{case_id}")
        self.assertEqual(detail.status_code, 200)
        self.assertGreaterEqual(len(detail.json()["artifacts"]), 2)
        json_artifact = next(
            item for item in detail.json()["artifacts"] if item["path"] == "paper_chunks.json"
        )
        preview = self.client.get(f"/api/v1/artifacts/{json_artifact['id']}")
        self.assertEqual(preview.status_code, 200)
        self.assertIn("json", preview.json()["preview"])
        phases = {item["id"]: item for item in detail.json()["phases"]}
        self.assertEqual(phases["paper_analysis"]["state"], "partial")

        export = self.client.post(f"/api/v1/cases/{case_id}/exports")
        self.assertEqual(export.status_code, 202)
        export_id = export.json()["export_id"]
        status = {}
        for _ in range(100):
            status = self.client.get(f"/api/v1/exports/{export_id}").json()
            if status["status"] in {"ready", "failed"}:
                break
            time.sleep(0.01)
        self.assertEqual(status["status"], "ready", status)
        archive = self.client.get(status["download_url"])
        self.assertEqual(archive.status_code, 200)
        self.assertTrue(archive.content.startswith(b"PK"))


if __name__ == "__main__":
    unittest.main()
