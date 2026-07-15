from __future__ import annotations

import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import delete, select

from tests import web_test_env  # noqa: F401

from geng_agent.web.db import SessionLocal, init_database
from geng_agent.web.models import ArtifactRecord, CaseRecord, ExportRecord, JobEvent, JobRecord
from geng_agent.web.tasks import run_review


class _FakePipeline:
    last_kwargs: dict | None = None

    def run(self, **kwargs):
        type(self).last_kwargs = kwargs
        progress = kwargs["progress"]
        progress.emit("phase.started", phase="paper_analysis")
        progress.emit("step.completed", phase="paper_analysis", step="paper")
        Path(kwargs["output_dir"], "review.md").write_text("# ok\n", encoding="utf-8")


class WebWorkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        init_database()

    def setUp(self) -> None:
        with SessionLocal() as session:
            for model in (JobEvent, ArtifactRecord, ExportRecord, JobRecord, CaseRecord):
                session.execute(delete(model))
            session.commit()

    def test_worker_calls_current_public_pipeline_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            case_dir = Path(tmp)
            paper_path = case_dir / "paper.pdf"
            paper_path.write_bytes(b"%PDF-1.4\nminimal")
            case_id = str(uuid.uuid4())
            job_id = str(uuid.uuid4())
            with SessionLocal() as session:
                session.add(
                    CaseRecord(
                        id=case_id,
                        display_name="worker contract",
                        directory=str(case_dir),
                        paper_path=str(paper_path),
                        source="upload",
                    )
                )
                session.add(
                    JobRecord(
                        id=job_id,
                        case_id=case_id,
                        status="queued",
                        options={"run_repro": True, "analysis_backend": "codex", "resume": True},
                    )
                )
                session.commit()

            with patch("geng_agent.web.tasks.ReviewPipeline", _FakePipeline):
                run_review.run(job_id)

            self.assertIsNotNone(_FakePipeline.last_kwargs)
            kwargs = _FakePipeline.last_kwargs or {}
            self.assertEqual(kwargs["analysis_backend"], "codex")
            self.assertTrue(kwargs["run_repro"])
            self.assertNotIn("template_fallback", kwargs)
            self.assertNotIn("per_task_layout", kwargs)

            with SessionLocal() as session:
                job = session.get(JobRecord, job_id)
                self.assertIsNotNone(job)
                self.assertEqual(job.status, "succeeded")
                event_types = session.scalars(
                    select(JobEvent.event_type).where(JobEvent.job_id == job_id)
                ).all()
                self.assertIn("job.started", event_types)
                self.assertIn("phase.started", event_types)
                self.assertGreater(
                    len(session.scalars(select(ArtifactRecord).where(ArtifactRecord.case_id == case_id)).all()),
                    0,
                )

    def test_worker_honors_cancel_before_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            case_dir = Path(tmp)
            paper_path = case_dir / "paper.pdf"
            paper_path.write_bytes(b"%PDF-1.4\nminimal")
            case_id = str(uuid.uuid4())
            job_id = str(uuid.uuid4())
            with SessionLocal() as session:
                session.add(
                    CaseRecord(
                        id=case_id,
                        display_name="cancelled worker",
                        directory=str(case_dir),
                        paper_path=str(paper_path),
                        source="upload",
                    )
                )
                session.add(
                    JobRecord(
                        id=job_id,
                        case_id=case_id,
                        status="cancel_requested",
                        cancel_requested=True,
                    )
                )
                session.commit()

            with patch("geng_agent.web.tasks.ReviewPipeline", _FakePipeline):
                _FakePipeline.last_kwargs = None
                run_review.run(job_id)

            self.assertIsNone(_FakePipeline.last_kwargs)
            with SessionLocal() as session:
                job = session.get(JobRecord, job_id)
                self.assertIsNotNone(job)
                self.assertEqual(job.status, "cancelled")


if __name__ == "__main__":
    unittest.main()
