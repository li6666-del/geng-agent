"""Adapt the scientific pipeline to account-owned, final-delivery Web jobs."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from celery import Celery

from geng_agent.pipeline import ReviewPipeline
from geng_agent.progress import CallbackProgressReporter, PipelineCancelled

from .db import SessionLocal, init_database
from .delivery import build_delivery
from .models import CaseRecord, JobRecord
from .observability import logger
from .settings import settings

celery_app = Celery("geng_agent.web", broker=settings.redis_url, backend=settings.redis_url)
celery_app.conf.update(
    task_acks_late=True, task_reject_on_worker_lost=True, worker_prefetch_multiplier=1,
    task_track_started=True, task_always_eager=settings.celery_eager,
    task_time_limit=None, task_soft_time_limit=None,
)


def _cancel_requested(job_id: str) -> bool:
    with SessionLocal() as session:
        job = session.get(JobRecord, job_id)
        return job is None or job.cancel_requested


def _finish(job_id: str, status: str, code: str | None = None, error: str | None = None) -> None:
    with SessionLocal() as session:
        job = session.get(JobRecord, job_id)
        if job:
            job.status, job.error_code, job.error_message = status, code, error
            job.finished_at = datetime.now(timezone.utc)
            session.commit()


def _is_transient(exc: Exception) -> bool:
    return isinstance(exc, (ConnectionError, TimeoutError)) or any(
        token in str(exc).lower() for token in
        ("timeout", "temporar", "connection reset", "http 429", "http 500", "http 502", "http 503", "http 504")
    )


@celery_app.task(bind=True, max_retries=2, name="geng.run_review")
def run_review(self, job_id: str) -> None:
    init_database()
    with SessionLocal() as session:
        job = session.get(JobRecord, job_id)
        if job is None or job.status in {"succeeded", "cancelled"}:
            return
        if job.cancel_requested:
            job.status = "cancelled"
            job.finished_at = datetime.now(timezone.utc)
            session.commit()
            return
        case = session.get(CaseRecord, job.case_id)
        if case is None:
            return
        job.status = "running"
        job.started_at = job.started_at or datetime.now(timezone.utc)
        job.finished_at = None
        job.error_code = job.error_message = None
        job.attempt += 1
        session.commit()
        case_dir, paper_path = Path(case.directory), Path(case.paper_path)
        pipeline_complete = bool((job.options or {}).get("pipeline_complete"))

    # Cancellation still reaches the core. Progress and intermediate artifacts
    # stay in the core's case records; the Web layer no longer copies them.
    reporter = CallbackProgressReporter(callback=lambda _payload: None,
                                        cancelled=lambda: _cancel_requested(job_id))
    try:
        if not pipeline_complete:
            result = ReviewPipeline().run(paper_path=paper_path, output_dir=case_dir,
                                          run_repro=True, resume=True,
                                          analysis_backend="codex", progress=reporter)
            reporter.check_cancelled()
            delivery_status = getattr(result, "delivery_status", "complete")
            if delivery_status != "complete":
                _finish(job_id, "failed", f"delivery_{delivery_status}", "复现流程尚未完成交付")
                return
            with SessionLocal() as session:
                job = session.get(JobRecord, job_id)
                job.options = {**(job.options or {}), "pipeline_complete": True}
                session.commit()
            pipeline_complete = True
        reporter.check_cancelled()
        build_delivery(case_dir, job_id)
        reporter.check_cancelled()
        _finish(job_id, "succeeded")
    except PipelineCancelled:
        _finish(job_id, "cancelled")
    except Exception as exc:
        logger.exception("Web job failed", extra={"job_id": job_id})
        if not pipeline_complete and _is_transient(exc) and self.request.retries < self.max_retries:
            with SessionLocal() as session:
                job = session.get(JobRecord, job_id)
                job.status = "queued"
                session.commit()
            raise self.retry(exc=exc, countdown=min(60, 10 * 2 ** self.request.retries))
        code = "delivery_package" if pipeline_complete else "execution_failed"
        _finish(job_id, "failed", code, str(exc)[:4000])
        # Details remain in server logs and storage, never returned to browsers.
