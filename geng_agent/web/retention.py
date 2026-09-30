"""Expire delivered archives while retaining accounts, papers and job history."""
from __future__ import annotations

import argparse
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from geng_agent.artifact_paths import path_is_link

from .db import SessionLocal, init_database
from .delivery import bundle_path
from .models import CaseRecord, JobRecord, WorkerAssignment, utc_now
from .settings import settings

ACTIVE = {"queued", "running", "cancel_requested"}
TERMINAL = {"succeeded", "failed", "cancelled"}


def begin_write(session: Session) -> None:
    """Serialize retention with retry before either reads the case state."""
    if session.get_bind().dialect.name == "sqlite":
        session.execute(text("BEGIN IMMEDIATE"))


def expired_at(job: JobRecord | None) -> str | None:
    value = (job.options or {}).get("artifacts_expired_at") if job else None
    return value if isinstance(value, str) and value else None


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _safe_export(case: CaseRecord, job_id: str) -> Path:
    root = settings.cases_root.absolute()
    case_id = str(uuid.UUID(case.id))
    directory = Path(case.directory).absolute()
    if directory != root / f"case_{case_id}":
        raise ValueError("case storage does not match registered upload directory")
    path = bundle_path(directory, job_id)
    path.resolve().relative_to(root.resolve())
    # Check every existing path component, including junctions on Windows.
    for part in (path, *path.parents):
        if path_is_link(part):
            raise ValueError("linked archive storage")
        if part == root:
            break
    if path.exists() and not path.is_file():
        raise ValueError("archive storage is not a file")
    return path


def _jobs(session: Session, case_id: str) -> list[JobRecord]:
    return list(session.scalars(select(JobRecord).where(JobRecord.case_id == case_id)
                               .order_by(JobRecord.created_at.desc(), JobRecord.id.desc())
                               .with_for_update()).all())


def case_has_active_work(session: Session, case_id: str) -> bool:
    if session.scalar(select(JobRecord.id).where(JobRecord.case_id == case_id,
                                               JobRecord.status.in_(ACTIVE)).limit(1)):
        return True
    return session.scalar(select(WorkerAssignment.job_id).join(JobRecord).where(
        JobRecord.case_id == case_id, WorkerAssignment.active.is_(True)).limit(1)) is not None


def _eligible(session: Session, case: CaseRecord, jobs: list[JobRecord], cutoff: datetime) -> bool:
    if not case.owner_id or not jobs or case_has_active_work(session, case.id):
        return False
    latest = jobs[0]
    return (latest.status in TERMINAL and latest.finished_at is not None
            and _utc(latest.finished_at) <= cutoff
            and all(job.status in TERMINAL and job.finished_at is not None
                    and _utc(job.finished_at) <= cutoff for job in jobs))


def cleanup_archives(*, apply: bool = False, now: datetime | None = None) -> dict:
    """Only registered final ZIPs are removed; dry runs never change DB or files."""
    moment = _utc(now or utc_now())
    cutoff = moment - timedelta(days=settings.artifact_retention_days)
    result = {"apply": apply, "retention_days": settings.artifact_retention_days,
              "cutoff": cutoff.isoformat(), "items": [], "errors": []}
    with SessionLocal() as session:
        case_ids = list(session.scalars(select(CaseRecord.id).where(CaseRecord.owner_id.is_not(None))).all())
    for case_id in case_ids:
        try:
            with SessionLocal() as session:
                begin_write(session)
                case = session.scalar(select(CaseRecord).where(CaseRecord.id == case_id).with_for_update())
                if case is None:
                    continue
                jobs = _jobs(session, case_id)
                if not _eligible(session, case, jobs, cutoff):
                    continue
                pending = []
                for job in jobs:
                    path = _safe_export(case, job.id)
                    if not expired_at(job) or path.exists():
                        pending.append((job, path))
                for job, path in pending:
                    item = {"case_id": case_id, "job_id": job.id, "archive_exists": path.is_file()}
                    if apply:
                        path.unlink(missing_ok=True)
                        job.options = {**(job.options or {}), "artifacts_expired_at": expired_at(job) or moment.isoformat(),
                                       "pipeline_complete": False}
                    result["items"].append(item)
                if apply:
                    session.commit()
        except (OSError, ValueError) as exc:
            result["errors"].append({"case_id": case_id, "error": type(exc).__name__})
    return result


def cleanup_candidates(session: Session, worker_id: str) -> list[dict]:
    """Authorize only expired work owned by this PC; never authorize active cases."""
    rows = session.execute(select(JobRecord, CaseRecord).join(CaseRecord).join(
        WorkerAssignment, WorkerAssignment.job_id == JobRecord.id).where(
        CaseRecord.owner_id.is_not(None), WorkerAssignment.worker_id == worker_id,
        WorkerAssignment.active.is_(False), JobRecord.status.in_(TERMINAL)))
    items = []
    for job, case in rows:
        stamp = expired_at(job)
        if stamp and not case_has_active_work(session, case.id):
            items.append({"case_id": case.id, "job_id": job.id, "expired_at": stamp,
                          "local_generation": (job.options or {}).get("local_generation")})
    return items


def main() -> int:
    parser = argparse.ArgumentParser(description="Clear expired website delivery ZIPs (dry run by default)")
    parser.add_argument("--apply", action="store_true", help="Remove eligible ZIPs and record expiration")
    arguments = parser.parse_args()
    init_database()
    result = cleanup_archives(apply=arguments.apply)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
