"""Authenticated outbound-PC protocol; the website never runs scientific work."""
from __future__ import annotations

import os
import secrets
import uuid
import zipfile
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy import exists, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased

from geng_agent.artifact_paths import path_is_link

from .db import get_session
from .delivery import bundle_path, local_file
from .models import CaseRecord, JobRecord, WorkerAssignment, utc_now
from .settings import settings

ACTIVE = {"running", "cancel_requested"}
TERMINAL = {"succeeded", "failed", "cancelled"}
WorkerId = Annotated[str, StringConstraints(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")]


def authenticate_worker(authorization: str | None = Header(default=None)) -> None:
    if settings.execution_mode != "pull":
        raise HTTPException(404, "接口不存在")
    token = settings.worker_token
    if not token:
        raise HTTPException(503, "工作机连接尚未配置")
    scheme, _, supplied = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(supplied.encode(), token.encode()):
        raise HTTPException(401, "工作机认证失败", headers={"WWW-Authenticate": "Bearer"})


router = APIRouter(prefix="/api/v1/worker", dependencies=[Depends(authenticate_worker)])


class WorkerRequest(BaseModel):
    worker_id: WorkerId


class FinishRequest(WorkerRequest):
    status: Literal["failed", "cancelled"]
    error_code: str | None = Field(default=None, max_length=64)
    error_message: str | None = Field(default=None, max_length=4000)
    pipeline_complete: bool = False


def _begin_write(session: Session) -> None:
    # SQLite has no row-level FOR UPDATE. Take its write lock before reading
    # ownership, so simultaneous clients cannot both choose the same work.
    if session.get_bind().dialect.name == "sqlite":
        session.execute(text("BEGIN IMMEDIATE"))


def _owned_job(session: Session, job_id: str, worker_id: str, *, lock: bool = False):
    statement = select(WorkerAssignment, JobRecord, CaseRecord).join(
        JobRecord, JobRecord.id == WorkerAssignment.job_id,
    ).join(CaseRecord, CaseRecord.id == JobRecord.case_id).where(
        WorkerAssignment.job_id == job_id, WorkerAssignment.worker_id == worker_id,
        CaseRecord.owner_id.is_not(None),
    )
    if lock:
        statement = statement.with_for_update(of=(WorkerAssignment, JobRecord))
    row = session.execute(statement).first()
    if row is None:
        raise HTTPException(404, "工作机任务不存在")
    return row


def _job_body(job: JobRecord, case: CaseRecord) -> dict:
    return {"job_id": job.id, "case_id": case.id, "display_name": case.display_name,
            "paper_url": f"/api/v1/worker/jobs/{job.id}/paper",
            "pipeline_complete": bool((job.options or {}).get("pipeline_complete"))}


def _status(job: JobRecord) -> dict:
    return {"status": job.status, "cancel_requested": bool(job.cancel_requested or job.status == "cancel_requested")}


@router.post("/claim")
def claim(body: WorkerRequest, session: Session = Depends(get_session)) -> dict:
    # The unique active-worker index also serializes two claims from the same
    # PC on PostgreSQL, including the first claim when there is no row to lock.
    for _ in range(3):
        try:
            _begin_write(session)
            current = session.execute(select(WorkerAssignment, JobRecord, CaseRecord).join(
                JobRecord, JobRecord.id == WorkerAssignment.job_id,
            ).join(CaseRecord, CaseRecord.id == JobRecord.case_id).where(
                WorkerAssignment.worker_id == body.worker_id, WorkerAssignment.active.is_(True),
            ).with_for_update(of=(WorkerAssignment, JobRecord))).first()
            if current:
                assignment, job, case = current
                if job.status in ACTIVE:
                    assignment.heartbeat_at = utc_now()
                    session.commit()
                    return {"job": _job_body(job, case)}
                assignment.active = False
                session.flush()

            previous_job = aliased(JobRecord)
            foreign_history = exists(select(WorkerAssignment.job_id).join(
                previous_job, previous_job.id == WorkerAssignment.job_id,
            ).where(previous_job.case_id == JobRecord.case_id, WorkerAssignment.worker_id != body.worker_id))
            candidate = session.execute(select(JobRecord, CaseRecord).join(
                CaseRecord, CaseRecord.id == JobRecord.case_id,
            ).where(JobRecord.status == "queued", JobRecord.cancel_requested.is_(False),
                    CaseRecord.owner_id.is_not(None), ~foreign_history,
                    ~exists(select(WorkerAssignment.job_id).where(WorkerAssignment.job_id == JobRecord.id)))
                .order_by(JobRecord.created_at, JobRecord.id).limit(1)
                .with_for_update(of=JobRecord, skip_locked=True)).first()
            if candidate is None:
                session.commit()
                return {"job": None}
            job, case = candidate
            now = utc_now()
            job.status, job.started_at = "running", now
            job.attempt = (job.attempt or 0) + 1
            job.error_code = job.error_message = None
            session.add(WorkerAssignment(job_id=job.id, worker_id=body.worker_id,
                                         active=True, claimed_at=now, heartbeat_at=now))
            session.commit()
            return {"job": _job_body(job, case)}
        except IntegrityError:
            session.rollback()
    raise HTTPException(409, "工作机正在领取任务，请重试")


@router.post("/jobs/{job_id}/heartbeat")
def heartbeat(job_id: str, body: WorkerRequest, session: Session = Depends(get_session)) -> dict:
    _begin_write(session)
    assignment, job, _case = _owned_job(session, job_id, body.worker_id, lock=True)
    assignment.heartbeat_at = utc_now()
    session.commit()
    return _status(job)


def _case_directory(case: CaseRecord) -> Path:
    root = settings.cases_root.absolute()
    directory = Path(case.directory).absolute()
    try:
        directory.relative_to(root)
        directory.resolve().relative_to(root.resolve())
        for part in (directory, *directory.parents):
            if path_is_link(part):
                raise ValueError("linked case directory")
            if part == root:
                break
    except ValueError as exc:
        raise HTTPException(409, "案例存储路径不可用") from exc
    return directory


@router.get("/jobs/{job_id}/paper")
def paper(job_id: str, worker_id: WorkerId = Header(alias="X-Worker-Id"),
          session: Session = Depends(get_session)) -> FileResponse:
    _assignment, _job, case = _owned_job(session, job_id, worker_id)
    directory = _case_directory(case)
    try:
        path = local_file(directory, Path(case.paper_path).absolute().relative_to(directory).as_posix())
    except (OSError, ValueError) as exc:
        raise HTTPException(404, "论文文件不可用") from exc
    return FileResponse(path, media_type="application/pdf", filename="paper.pdf")


def _bundle_destination(case: CaseRecord, job_id: str) -> Path:
    try:
        destination = bundle_path(_case_directory(case), job_id)
        if path_is_link(destination.parent) or path_is_link(destination):
            raise ValueError("linked export path")
        destination.parent.mkdir(parents=True, exist_ok=True)
        return destination
    except (OSError, ValueError) as exc:
        raise HTTPException(409, "交付包存储路径不可用") from exc


def _can_complete(job: JobRecord) -> None:
    if job.cancel_requested or job.status == "cancel_requested":
        raise HTTPException(409, "任务已请求停止")
    if job.status not in ACTIVE:
        raise HTTPException(409, "任务已经结束")


@router.post("/jobs/{job_id}/complete")
def complete(job_id: str, worker_id: WorkerId = Form(), bundle: UploadFile = File(),
             session: Session = Depends(get_session)) -> dict:
    temporary = None
    try:
        _assignment, job, case = _owned_job(session, job_id, worker_id)
        if job.status == "succeeded":
            return _status(job)
        _can_complete(job)
        destination = _bundle_destination(case, job_id)
        session.rollback()  # Do not hold a DB transaction while transferring a large ZIP.
        temporary = destination.with_name(f".{destination.stem}-{uuid.uuid4().hex}.uploading")
        total = 0
        with temporary.open("xb") as handle:
            while chunk := bundle.file.read(1024 * 1024):
                total += len(chunk)
                if total > settings.max_bundle_bytes:
                    raise HTTPException(413, "交付包超过允许的文件大小")
                handle.write(chunk)
        # Only recognize the container format; report contents and scientific
        # output remain owned by the existing local pipeline.
        if not zipfile.is_zipfile(temporary):
            raise HTTPException(400, "请上传 ZIP 格式的交付包")
        _begin_write(session)
        assignment, job, case = _owned_job(session, job_id, worker_id, lock=True)
        if job.status == "succeeded":
            return _status(job)
        _can_complete(job)
        destination = _bundle_destination(case, job_id)
        os.replace(temporary, destination)
        job.status, job.finished_at = "succeeded", utc_now()
        job.options = {**(job.options or {}), "pipeline_complete": True}
        job.error_code = job.error_message = None
        assignment.active, assignment.heartbeat_at = False, utc_now()
        session.commit()
        return _status(job)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        bundle.file.close()


@router.post("/jobs/{job_id}/finish")
def finish(job_id: str, body: FinishRequest, session: Session = Depends(get_session)) -> dict:
    _begin_write(session)
    assignment, job, _case = _owned_job(session, job_id, body.worker_id, lock=True)
    if job.status in TERMINAL:
        if job.status == body.status or (job.status == "cancelled" and job.cancel_requested):
            return _status(job)
        raise HTTPException(409, "任务已经结束")
    job.status = "cancelled" if job.cancel_requested or job.status == "cancel_requested" else body.status
    job.finished_at = utc_now()
    job.error_code = body.error_code if job.status == "failed" else None
    job.error_message = body.error_message if job.status == "failed" else None
    job.options = {**(job.options or {}), "pipeline_complete": bool(
        (job.options or {}).get("pipeline_complete") or body.pipeline_complete)}
    assignment.active, assignment.heartbeat_at = False, utc_now()
    session.commit()
    return _status(job)
