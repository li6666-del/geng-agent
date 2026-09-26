"""Account-owned paper uploads and final bundle delivery."""
from __future__ import annotations

import os
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import case as sql_case, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .auth import current_user, router as auth_router
from .db import SessionLocal, get_session, init_database
from .delivery import bundle_path, local_file
from .models import CaseRecord, JobRecord, UserRecord
from .observability import logger
from .settings import settings
from .worker_api import router as worker_router

ACTIVE = {"queued", "running", "cancel_requested"}
_FRONTEND_DIST = Path(__file__).resolve().parent / "frontend" / "dist"


def _dispatch_review(job_id: str) -> None:
    if settings.execution_mode == "pull":
        return
    from .tasks import run_review

    if settings.celery_eager:
        threading.Thread(target=run_review.delay, args=(job_id,),
                         name=f"geng-web-{job_id[:8]}", daemon=False).start()
        return
    try:
        run_review.delay(job_id)
    except Exception:
        logger.exception("Task queue unavailable", extra={"job_id": job_id})
        with SessionLocal() as session:
            job = session.get(JobRecord, job_id)
            if job:
                job.status, job.error_code = "failed", "queue_unavailable"
                session.commit()


def _recover_interrupted_eager_jobs() -> list[str]:
    if settings.execution_mode == "pull" or not settings.celery_eager:
        return []
    with SessionLocal() as session:
        ids = list(session.scalars(select(JobRecord.id).join(CaseRecord, CaseRecord.id == JobRecord.case_id)
                   .where(JobRecord.status.in_(ACTIVE), CaseRecord.owner_id.is_not(None))).all())
    for job_id in ids:
        _dispatch_review(job_id)
    return ids


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if settings.execution_mode not in {"local", "pull"}:
        raise RuntimeError("GENG_EXECUTION_MODE must be local or pull")
    if settings.execution_mode == "pull" and not settings.worker_token:
        raise RuntimeError("Pull execution requires GENG_WORKER_TOKEN")
    init_database()
    _recover_interrupted_eager_jobs()
    yield


app = FastAPI(title="耿同学 · 论文复现", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(auth_router)
app.include_router(worker_router)


@app.middleware("http")
async def response_headers(request: Request, call_next):
    started = time.perf_counter()
    response = await call_next(request)
    response.headers.update({
        "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
        "Referrer-Policy": "same-origin",
        "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'",
    })
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    logger.info("request completed", extra={"event": "http.request", "method": request.method,
                "path": request.url.path, "status_code": response.status_code,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2)})
    return response


def _authorized_case(session: Session, case_id: str, user: UserRecord) -> CaseRecord:
    case = session.scalar(select(CaseRecord).where(CaseRecord.id == case_id, CaseRecord.owner_id == user.id))
    if case is None:
        raise HTTPException(404, "论文任务不存在")
    return case


def _latest_job(session: Session, case_id: str) -> JobRecord | None:
    return session.scalar(select(JobRecord).where(JobRecord.case_id == case_id).order_by(JobRecord.created_at.desc()))


def _case_body(case: CaseRecord, job: JobRecord | None) -> dict:
    status = job.status if job else "idle"
    download_url = None
    if job and status == "succeeded":
        try:
            local_file(Path(case.directory), f"exports/{job.id}.zip")
            download_url = f"/api/v1/cases/{case.id}/download"
        except (OSError, ValueError):
            pass
    messages = {
        "queued": "论文已提交，等待处理。", "running": "正在复现论文，完成后可下载交付包。",
        "cancel_requested": "正在停止，已有运行记录会保留。", "cancelled": "已停止，可继续处理。",
        "succeeded": "两份报告和分任务复现项目已整理完成。",
        "failed": "本次处理未完成，已有运行记录已保留，可继续处理。",
    }
    message = messages.get(status, "尚未开始处理。")
    if job and job.error_code == "delivery_package":
        message = "复现流程已结束，交付包尚未生成。重试只重新打包。"
    if status == "succeeded" and not download_url:
        message = "交付包暂不可用，可以重新打包。"
    return {"id": case.id, "display_name": case.display_name,
            "created_at": case.created_at.isoformat(), "status": status, "message": message,
            "download_url": download_url, "can_retry": status in {"failed", "cancelled", "idle"} or (status == "succeeded" and not download_url)}


@app.get("/api/v1/health")
def health(session: Session = Depends(get_session)) -> dict:
    try:
        return {"ok": session.scalar(select(1)) == 1}
    except Exception as exc:
        raise HTTPException(503, "服务暂不可用") from exc


@app.get("/api/v1/site")
def site_config() -> dict:
    return {"max_pdf_bytes": settings.max_pdf_bytes, "registration_enabled": settings.registration_enabled}


@app.get("/api/v1/cases")
def list_cases(user: UserRecord = Depends(current_user), session: Session = Depends(get_session)) -> dict:
    cases = session.scalars(select(CaseRecord).where(CaseRecord.owner_id == user.id).order_by(CaseRecord.created_at.desc())).all()
    return {"items": [_case_body(case, _latest_job(session, case.id)) for case in cases]}


async def _save_upload(upload: UploadFile, destination: Path) -> None:
    temporary = destination.with_suffix(".uploading")
    destination.parent.mkdir(parents=True, exist_ok=True)
    total, header = 0, b""
    try:
        with temporary.open("wb") as handle:
            while chunk := await upload.read(1024 * 1024):
                header = (header + chunk)[:5] if len(header) < 5 else header
                total += len(chunk)
                if total > settings.max_pdf_bytes:
                    raise HTTPException(413, "论文超过允许的文件大小")
                handle.write(chunk)
        if header != b"%PDF-":
            raise HTTPException(400, "请上传 PDF 格式的论文")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
        await upload.close()


@app.post("/api/v1/cases", status_code=202)
async def create_case(pdf_file: UploadFile = File(), display_name: str = Form(default=""),
                      user: UserRecord = Depends(current_user), session: Session = Depends(get_session)) -> dict:
    case_id = str(uuid.uuid4())
    case_dir = settings.cases_root / f"case_{case_id}"
    # Never use the upload's filename as a filesystem path.
    paper_path = case_dir / "paper" / "paper.pdf"
    name = display_name.strip() or Path((pdf_file.filename or "论文").replace("\\", "/")).stem
    name = "".join(c for c in name if c.isprintable())[:255] or "未命名论文"
    await _save_upload(pdf_file, paper_path)
    case = CaseRecord(id=case_id, display_name=name, directory=str(case_dir), paper_path=str(paper_path), owner_id=user.id)
    session.add(case)
    session.flush()
    job = JobRecord(id=str(uuid.uuid4()), case_id=case_id, status="queued", options={})
    session.add(job)
    session.commit()
    _dispatch_review(job.id)
    return {"case_id": case_id}


@app.get("/api/v1/cases/{case_id}")
def get_case(case_id: str, user: UserRecord = Depends(current_user), session: Session = Depends(get_session)) -> dict:
    case = _authorized_case(session, case_id, user)
    return _case_body(case, _latest_job(session, case.id))


@app.post("/api/v1/cases/{case_id}/retry", status_code=202)
def retry_case(case_id: str, user: UserRecord = Depends(current_user), session: Session = Depends(get_session)) -> dict:
    case = _authorized_case(session, case_id, user)
    previous = _latest_job(session, case.id)
    if previous and (previous.status in ACTIVE or (previous.status == "succeeded" and _case_body(case, previous)["download_url"])):
        raise HTTPException(409, "该任务正在处理或已完成交付")
    job = JobRecord(id=str(uuid.uuid4()), case_id=case_id, status="queued",
                    options={"pipeline_complete": bool(previous and (previous.options or {}).get("pipeline_complete"))})
    session.add(job)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(409, "该任务已经开始处理") from exc
    _dispatch_review(job.id)
    return {"case_id": case_id}


@app.post("/api/v1/cases/{case_id}/cancel", status_code=202)
def cancel_case(case_id: str, user: UserRecord = Depends(current_user), session: Session = Depends(get_session)) -> dict:
    case = _authorized_case(session, case_id, user)
    job = _latest_job(session, case.id)
    if not job or job.status not in ACTIVE:
        raise HTTPException(409, "任务已经结束")
    changes = {"cancel_requested": True, "status": "cancel_requested"}
    if settings.execution_mode == "pull":
        # Check the status at write time: claim or completion may have committed
        # since the earlier read. A queued job has no process to wait for.
        changes["status"] = sql_case((JobRecord.status == "queued", "cancelled"), else_="cancel_requested")
        changes["finished_at"] = sql_case(
            (JobRecord.status == "queued", datetime.now(timezone.utc)), else_=JobRecord.finished_at)
    changed = session.execute(update(JobRecord).where(JobRecord.id == job.id, JobRecord.status.in_(ACTIVE))
                              .values(**changes).execution_options(synchronize_session=False))
    if not changed.rowcount:
        session.rollback()
        raise HTTPException(409, "任务已经结束")
    session.commit()
    return {"case_id": case_id}


@app.get("/api/v1/cases/{case_id}/download")
def download_case(case_id: str, user: UserRecord = Depends(current_user), session: Session = Depends(get_session)) -> FileResponse:
    case = _authorized_case(session, case_id, user)
    job = _latest_job(session, case.id)
    if not job or job.status != "succeeded":
        raise HTTPException(409, "最终交付包尚未生成")
    try:
        path = local_file(Path(case.directory), bundle_path(Path(case.directory), job.id).relative_to(case.directory).as_posix())
    except (OSError, ValueError) as exc:
        raise HTTPException(404, "交付包暂不可用，请重新打包") from exc
    return FileResponse(path, media_type="application/zip", filename=f"论文复现交付-{case.id[:8]}.zip")


if (_FRONTEND_DIST / "assets").exists():
    app.mount("/assets", StaticFiles(directory=_FRONTEND_DIST / "assets"), name="frontend-assets")


@app.get("/{path:path}")
def frontend(path: str) -> FileResponse:
    if path == "api" or path.startswith("api/"):
        raise HTTPException(404, "接口不存在")
    index = _FRONTEND_DIST / "index.html"
    if not index.is_file():
        raise HTTPException(503, "网页尚未构建")
    return FileResponse(index, headers={"Cache-Control": "no-cache"})


def create_app() -> FastAPI:
    return app
