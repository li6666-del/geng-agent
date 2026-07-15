from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from celery import Celery
from sqlalchemy import select

from geng_agent.pipeline import ReviewPipeline
from geng_agent.progress import CallbackProgressReporter, PipelineCancelled

from .artifacts import LocalArtifactStore, build_zip, catalog_case_artifacts
from .db import SessionLocal, init_database
from .events import append_event
from .models import ArtifactRecord, CaseRecord, ExportRecord, JobRecord
from .settings import settings


celery_app = Celery("geng_agent.web", broker=settings.redis_url, backend=settings.redis_url)
celery_app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_track_started=True,
    task_always_eager=settings.celery_eager,
)


def _cancel_requested(job_id: str) -> bool:
    with SessionLocal() as session:
        job = session.get(JobRecord, job_id)
        return job is None or job.cancel_requested


def _is_transient(exc: Exception) -> bool:
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return True
    message = str(exc).lower()
    return any(token in message for token in ("timeout", "temporar", "connection reset", "http 429", "http 500", "http 502", "http 503", "http 504"))


@celery_app.task(bind=True, max_retries=2, name="geng.run_review")
def run_review(self, job_id: str) -> None:
    init_database()
    cancelled_before_start = False
    with SessionLocal() as session:
        job = session.get(JobRecord, job_id)
        if job is None or job.status in {"succeeded", "cancelled"}:
            return
        if job.cancel_requested or job.status == "cancel_requested":
            job.status = "cancelled"
            job.finished_at = datetime.now(timezone.utc)
            session.commit()
            cancelled_before_start = True
        if cancelled_before_start:
            case = None
        else:
            case = session.get(CaseRecord, job.case_id)
        if case is None:
            if not cancelled_before_start:
                return
        else:
            job.status = "running"
            job.started_at = job.started_at or datetime.now(timezone.utc)
            job.finished_at = None
            job.attempt += 1
            session.commit()
            case_dir = Path(case.directory)
            paper_path = Path(case.paper_path)
            options = dict(job.options or {})

    if cancelled_before_start:
        append_event(job_id, {"type": "job.cancelled", "message": "任务已在启动前取消"})
        return

    append_event(
        job_id,
        {"type": "job.started", "message": "复现任务已由 worker 接管", "data": {"attempt": self.request.retries + 1}},
    )
    reporter = CallbackProgressReporter(
        callback=lambda payload: append_event(job_id, payload),
        cancelled=lambda: _cancel_requested(job_id),
    )
    try:
        pipeline = ReviewPipeline()
        pipeline.run(
            paper_path=paper_path,
            output_dir=case_dir,
            run_repro=bool(options.get("run_repro", True)),
            resume=True,
            analysis_backend="codex",
            progress=reporter,
        )
        reporter.check_cancelled()
        with SessionLocal() as session:
            job = session.get(JobRecord, job_id)
            case = session.get(CaseRecord, job.case_id) if job else None
            if job is None or case is None:
                return
            catalog_case_artifacts(session, case)
            job.status = "succeeded"
            job.error_code = None
            job.error_message = None
            job.finished_at = datetime.now(timezone.utc)
            session.commit()
        append_event(job_id, {"type": "job.finished", "message": "复现航行已完成", "data": {"ok": True}})
    except PipelineCancelled:
        with SessionLocal() as session:
            job = session.get(JobRecord, job_id)
            if job:
                job.status = "cancelled"
                job.finished_at = datetime.now(timezone.utc)
                session.commit()
        append_event(job_id, {"type": "job.cancelled", "message": "任务已在安全边界停止"})
    except Exception as exc:
        if _is_transient(exc) and self.request.retries < self.max_retries:
            next_retry = self.request.retries + 1
            with SessionLocal() as session:
                job = session.get(JobRecord, job_id)
                if job:
                    job.status = "queued"
                    job.error_code = type(exc).__name__
                    job.error_message = str(exc)[:4000]
                    session.commit()
            append_event(
                job_id,
                {"type": "job.retrying", "message": "上游暂时不可用，任务将从缓存恢复", "data": {"retry": next_retry, "max_retries": self.max_retries}},
            )
            raise self.retry(exc=exc, countdown=min(60, 10 * (2 ** self.request.retries)))
        with SessionLocal() as session:
            job = session.get(JobRecord, job_id)
            if job:
                job.status = "failed"
                job.error_code = type(exc).__name__
                job.error_message = str(exc)[:4000]
                job.finished_at = datetime.now(timezone.utc)
                session.commit()
        append_event(
            job_id,
            {"type": "job.failed", "message": "复现任务失败", "data": {"code": type(exc).__name__, "detail": str(exc)[:1000]}},
        )
        raise


@celery_app.task(bind=True, max_retries=1, name="geng.build_export")
def build_export(self, export_id: str) -> None:
    init_database()
    with SessionLocal() as session:
        export = session.get(ExportRecord, export_id)
        if export is None or export.status == "ready":
            return
        case = session.get(CaseRecord, export.case_id)
        if case is None:
            return
        artifacts = session.scalars(
            select(ArtifactRecord).where(
                ArtifactRecord.case_id == case.id,
                *([ArtifactRecord.phase == export.phase] if export.phase else []),
            )
        ).all()
        export.status = "running"
        session.commit()
        case_dir = Path(case.directory)
        relative = f"exports/{export.id}.zip"
    try:
        build_zip(LocalArtifactStore(case_dir), (item.relative_path for item in artifacts), case_dir / relative)
        with SessionLocal() as session:
            export = session.get(ExportRecord, export_id)
            if export:
                export.status = "ready"
                export.relative_path = relative
                export.finished_at = datetime.now(timezone.utc)
                session.commit()
    except Exception as exc:
        if self.request.retries < self.max_retries:
            with SessionLocal() as session:
                export = session.get(ExportRecord, export_id)
                if export:
                    export.status = "queued"
                    export.error_message = str(exc)[:4000]
                    session.commit()
            raise self.retry(exc=exc, countdown=5)
        with SessionLocal() as session:
            export = session.get(ExportRecord, export_id)
            if export:
                export.status = "failed"
                export.error_message = str(exc)[:4000]
                export.finished_at = datetime.now(timezone.utc)
                session.commit()
        raise
