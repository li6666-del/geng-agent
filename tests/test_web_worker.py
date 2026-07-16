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
from geng_agent.web.artifacts import catalog_case_artifacts
from geng_agent.web.tasks import celery_app, run_review


class _FakePipeline:
    last_kwargs: dict | None = None
    artifacts_visible_during_run = False

    def run(self, **kwargs):
        type(self).last_kwargs = kwargs
        progress = kwargs["progress"]
        progress.emit("phase.started", phase="paper_analysis")
        Path(kwargs["output_dir"], "engineering_facts_initial.json").write_text(
            '{"engineering_facts": []}\n', encoding="utf-8"
        )
        progress.emit("step.completed", phase="paper_analysis", step="paper")
        with SessionLocal() as session:
            type(self).artifacts_visible_during_run = bool(
                session.scalar(
                    select(ArtifactRecord).where(
                        ArtifactRecord.relative_path == "engineering_facts_initial.json"
                    )
                )
            )
        Path(kwargs["output_dir"], "review.md").write_text("# ok\n", encoding="utf-8")


class _MinimalPipeline:
    def run(self, **kwargs):
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
                _FakePipeline.artifacts_visible_during_run = False
                run_review.run(job_id)

            self.assertIsNotNone(_FakePipeline.last_kwargs)
            kwargs = _FakePipeline.last_kwargs or {}
            self.assertEqual(kwargs["analysis_backend"], "codex")
            self.assertTrue(kwargs["run_repro"])
            self.assertNotIn("template_fallback", kwargs)
            self.assertNotIn("per_task_layout", kwargs)
            self.assertTrue(_FakePipeline.artifacts_visible_during_run)
            self.assertIsNone(celery_app.conf.task_time_limit)
            self.assertIsNone(celery_app.conf.task_soft_time_limit)

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

                first = session.scalar(
                    select(ArtifactRecord).where(
                        ArtifactRecord.case_id == case_id,
                        ArtifactRecord.relative_path == "review.md",
                    )
                )
                self.assertIsNotNone(first)
                first_id = first.id
                first_hash = first.sha256

            (case_dir / "review.md").write_text("# changed\n", encoding="utf-8")
            with SessionLocal() as session:
                case = session.get(CaseRecord, case_id)
                catalog_case_artifacts(session, case)
                updated = session.scalar(
                    select(ArtifactRecord).where(
                        ArtifactRecord.case_id == case_id,
                        ArtifactRecord.relative_path == "review.md",
                    )
                )
                self.assertEqual(updated.id, first_id)
                self.assertNotEqual(updated.sha256, first_hash)

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

    def test_artifact_sync_failure_never_fails_scientific_job(self) -> None:
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
                        display_name="sync failure",
                        directory=str(case_dir),
                        paper_path=str(paper_path),
                        source="upload",
                    )
                )
                session.add(JobRecord(id=job_id, case_id=case_id, status="queued"))
                session.commit()

            with (
                patch("geng_agent.web.tasks.ReviewPipeline", _MinimalPipeline),
                patch("geng_agent.web.tasks.catalog_case_artifacts", side_effect=OSError("busy output")),
            ):
                run_review.run(job_id)

            with SessionLocal() as session:
                job = session.get(JobRecord, job_id)
                self.assertEqual(job.status, "succeeded")
                event_types = session.scalars(
                    select(JobEvent.event_type).where(JobEvent.job_id == job_id)
                ).all()
                self.assertIn("artifact.sync_failed", event_types)
                self.assertIn("job.finished", event_types)


if __name__ == "__main__":
    unittest.main()
