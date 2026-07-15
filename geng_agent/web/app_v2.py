from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import threading
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from geng_agent.progress import PHASES
from geng_agent.status import inspect_case_status

from .artifacts import (
    LocalArtifactStore,
    UnsafeArtifactPath,
    catalog_case_artifacts,
    manifest_hash,
    preview_artifact,
)
from .db import SessionLocal, get_session, init_database
from .events import event_cursor, events_after
from .importer import UnsafePdfUrl, download_pdf
from .models import ArtifactRecord, CaseRecord, ExportRecord, JobEvent, JobRecord
from .settings import settings
from .stages import build_stage_progress
from .tasks import build_export, run_review


_FRONTEND_DIST = Path(__file__).resolve().parent / "frontend" / "dist"
_CASE_NAME_RE = re.compile(r"[^\w\-. ]+", re.UNICODE)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_database()
    yield


app = FastAPI(title="耿同学 Agent · 研究航行日志", version="1.0.0", lifespan=lifespan)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; connect-src 'self'; font-src 'self'; "
        "object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
    )
    return response


def _authorized_case(session: Session, case_id: str) -> CaseRecord:
    # Trusted-network v1 seam. OIDC can populate owner_id without changing route internals.
    case = session.get(CaseRecord, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="案例不存在")
    return case


def _job_body(job: JobRecord | None) -> dict[str, Any] | None:
    if job is None:
        return None
    return {
        "id": job.id,
        "case_id": job.case_id,
        "status": job.status,
        "current_phase": job.current_phase,
        "current_step": job.current_step,
        "cancel_requested": job.cancel_requested,
        "attempt": job.attempt,
        "error": ({"code": job.error_code, "message": job.error_message} if job.error_code else None),
        "created_at": job.created_at.isoformat(),
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


def _artifact_body(item: ArtifactRecord) -> dict[str, Any]:
    return {
        "id": item.id,
        "phase": item.phase,
        "path": item.relative_path,
        "kind": item.kind,
        "mime_type": item.mime_type,
        "size_bytes": item.size_bytes,
        "sha256": item.sha256,
        "created_at": item.created_at.isoformat(),
        "content_url": f"/api/v1/artifacts/{item.id}/content",
        "download_url": f"/api/v1/artifacts/{item.id}/content?download=true",
    }


def _phase_states(session: Session, case_id: str, job: JobRecord | None) -> list[dict[str, Any]]:
    artifacts = session.scalars(select(ArtifactRecord).where(ArtifactRecord.case_id == case_id)).all()
    artifact_counts: dict[str, int] = {}
    for item in artifacts:
        artifact_counts[item.phase] = artifact_counts.get(item.phase, 0) + 1
    events = [] if job is None else session.scalars(
        select(JobEvent).where(JobEvent.job_id == job.id).order_by(JobEvent.id)
    ).all()
    if not events:
        case = session.get(CaseRecord, case_id)
        if case is not None:
            filesystem_rows = build_stage_progress(inspect_case_status(Path(case.directory)))
            for row in filesystem_rows:
                if row["state"] == "running" and (job is None or job.status not in {"running", "cancel_requested"}):
                    row["state"] = "partial" if row.get("completed_steps") else "waiting"
                    row["ok"] = False
                row["artifact_count"] = artifact_counts.get(row["id"], 0)
            return filesystem_rows
    started = {event.phase for event in events if event.event_type == "phase.started"}
    completed = {event.phase for event in events if event.event_type == "phase.completed"}
    rows = []
    for index, (phase_id, label, steps) in enumerate(PHASES, start=1):
        state = "waiting"
        if phase_id in completed:
            state = "success"
        elif job and job.current_phase == phase_id and job.status in {"running", "cancel_requested"}:
            state = "running"
        elif phase_id in started:
            state = "partial"
        if job and job.current_phase == phase_id and job.status == "failed":
            state = "failed"
        if job and job.current_phase == phase_id and job.status == "cancelled":
            state = "cancelled"
        rows.append(
            {
                "id": phase_id,
                "index": index,
                "label": label,
                "steps": list(steps),
                "state": state,
                "artifact_count": artifact_counts.get(phase_id, 0),
            }
        )
    return rows


def _latest_job(session: Session, case_id: str) -> JobRecord | None:
    return session.scalar(select(JobRecord).where(JobRecord.case_id == case_id).order_by(JobRecord.created_at.desc()))


def _dispatch_review(job_id: str) -> None:
    if settings.celery_eager:
        threading.Thread(target=run_review.delay, args=(job_id,), name=f"geng-eager-{job_id[:8]}", daemon=True).start()
        return
    try:
        run_review.delay(job_id)
    except Exception as exc:
        with SessionLocal() as session:
            job = session.get(JobRecord, job_id)
            if job:
                job.status = "failed"
                job.error_code = "queue_unavailable"
                job.error_message = str(exc)
                session.commit()
        raise HTTPException(status_code=503, detail="任务队列暂不可用") from exc


async def _save_upload(upload: UploadFile, destination: Path) -> str:
    temp = destination.with_suffix(".uploading")
    digest = hashlib.sha256()
    total = 0
    header = b""
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with temp.open("wb") as handle:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                if not header:
                    header = chunk[:5]
                total += len(chunk)
                if total > settings.max_pdf_bytes:
                    raise HTTPException(status_code=413, detail="PDF 文件超过 80 MB 限制")
                digest.update(chunk)
                handle.write(chunk)
        if total == 0 or header != b"%PDF-":
            raise HTTPException(status_code=400, detail="上传内容不是有效 PDF")
        os.replace(temp, destination)
        return digest.hexdigest()
    finally:
        if temp.exists():
            temp.unlink()


@app.get("/api/v1/health")
def health(session: Session = Depends(get_session)) -> dict[str, Any]:
    database_ok = session.scalar(select(func.count()).select_from(CaseRecord)) is not None
    redis_ok = False
    queue_depth = None
    try:
        import redis

        client = redis.Redis.from_url(settings.redis_url, socket_connect_timeout=0.3, socket_timeout=0.3)
        redis_ok = bool(client.ping())
        queue_depth = int(client.llen("celery"))
    except Exception:
        pass
    return {
        "ok": database_ok and (redis_ok or settings.celery_eager),
        "database": database_ok,
        "redis": redis_ok,
        "queue_depth": queue_depth,
        "cases_root": str(settings.cases_root),
        "url_import_enabled": settings.enable_url_import,
    }


@app.get("/api/v1/cases")
def list_cases(session: Session = Depends(get_session)) -> dict[str, Any]:
    cases = session.scalars(select(CaseRecord).order_by(CaseRecord.created_at.desc())).all()
    return {
        "items": [
            {
                "id": case.id,
                "display_name": case.display_name,
                "source": case.source,
                "created_at": case.created_at.isoformat(),
                "job": _job_body(_latest_job(session, case.id)),
            }
            for case in cases
        ]
    }


@app.post("/api/v1/cases", status_code=202)
async def create_case(
    pdf_file: UploadFile | None = File(default=None),
    pdf_url: str | None = Form(default=None),
    display_name: str | None = Form(default=None),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    url = (pdf_url or "").strip()
    if (pdf_file is None) == (not url):
        raise HTTPException(status_code=400, detail="请上传 PDF，或填写一个 PDF 地址")
    if url and not settings.enable_url_import:
        raise HTTPException(status_code=403, detail="管理员尚未启用 URL 导入")
    case_id = str(uuid.uuid4())
    case_dir = (settings.cases_root / f"case_{case_id}").resolve()
    paper_dir = case_dir / "paper"
    raw_name = display_name or (pdf_file.filename if pdf_file else Path(url).name) or "未命名论文"
    safe_display = (_CASE_NAME_RE.sub("", raw_name).strip() or "未命名论文")[:255]
    filename = Path(pdf_file.filename or "paper.pdf").name if pdf_file else "paper.pdf"
    if not filename.lower().endswith(".pdf"):
        filename += ".pdf"
    paper_path = paper_dir / filename
    if pdf_file is not None:
        await _save_upload(pdf_file, paper_path)
        source = "upload"
    else:
        temp = paper_path.with_suffix(".download")
        try:
            await asyncio.to_thread(download_pdf, url, temp, settings.max_pdf_bytes)
            paper_dir.mkdir(parents=True, exist_ok=True)
            os.replace(temp, paper_path)
        except UnsafePdfUrl as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            if temp.exists():
                temp.unlink()
        source = "url"
    job_id = str(uuid.uuid4())
    case = CaseRecord(
        id=case_id,
        display_name=safe_display,
        directory=str(case_dir),
        paper_path=str(paper_path),
        source=source,
    )
    job = JobRecord(
        id=job_id,
        case_id=case_id,
        status="queued",
        options={"run_repro": True, "analysis_backend": "codex", "resume": True},
    )
    session.add_all([case, job])
    session.commit()
    _dispatch_review(job_id)
    return {"case_id": case_id, "job_id": job_id}


@app.post("/api/v1/cases/import")
def import_existing_cases(session: Session = Depends(get_session)) -> dict[str, Any]:
    imported: list[str] = []
    for directory in settings.cases_root.glob("case_*"):
        if not directory.is_dir() or directory.is_symlink():
            continue
        if session.scalar(select(CaseRecord).where(CaseRecord.directory == str(directory.resolve()))):
            continue
        paper_candidates = list((directory / "paper").glob("*.pdf")) if (directory / "paper").exists() else []
        if not paper_candidates:
            chunks = directory / "paper_chunks.json"
            if chunks.exists():
                try:
                    source_path = json.loads(chunks.read_text(encoding="utf-8")).get("source_path")
                    if source_path and Path(source_path).exists():
                        paper_candidates = [Path(source_path)]
                except Exception:
                    pass
        if not paper_candidates:
            continue
        case_id = str(uuid.uuid4())
        case = CaseRecord(
            id=case_id,
            display_name=directory.name,
            directory=str(directory.resolve()),
            paper_path=str(paper_candidates[0].resolve()),
            source="import",
        )
        session.add(case)
        session.flush()
        catalog_case_artifacts(session, case)
        imported.append(case_id)
    session.commit()
    return {"imported": imported, "count": len(imported)}


@app.get("/api/v1/cases/{case_id}")
def get_case(case_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    case = _authorized_case(session, case_id)
    job = _latest_job(session, case.id)
    artifacts = session.scalars(select(ArtifactRecord).where(ArtifactRecord.case_id == case.id)).all()
    return {
        "id": case.id,
        "display_name": case.display_name,
        "source": case.source,
        "created_at": case.created_at.isoformat(),
        "job": _job_body(job),
        "phases": _phase_states(session, case.id, job),
        "artifacts": [_artifact_body(item) for item in artifacts],
    }


@app.post("/api/v1/cases/{case_id}/jobs", status_code=202)
def create_job(case_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    case = _authorized_case(session, case_id)
    job = JobRecord(
        id=str(uuid.uuid4()),
        case_id=case.id,
        status="queued",
        options={"run_repro": True, "analysis_backend": "codex", "resume": True},
    )
    session.add(job)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail="该案例已有正在运行的任务") from exc
    _dispatch_review(job.id)
    return {"job_id": job.id}


@app.get("/api/v1/jobs/{job_id}")
def get_job(job_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    job = session.get(JobRecord, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    _authorized_case(session, job.case_id)
    return _job_body(job) or {}


@app.post("/api/v1/jobs/{job_id}/cancel", status_code=202)
def cancel_job(job_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    job = session.get(JobRecord, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    _authorized_case(session, job.case_id)
    if job.status not in {"queued", "running", "cancel_requested"}:
        raise HTTPException(status_code=409, detail="任务已经结束")
    job.cancel_requested = True
    job.status = "cancel_requested"
    session.commit()
    return {"status": "cancel_requested"}


@app.get("/api/v1/jobs/{job_id}/events")
async def stream_events(
    job_id: str,
    request: Request,
    after: int = Query(default=0, ge=0),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    # Do not hold a database connection for the lifetime of an SSE stream.
    with SessionLocal() as session:
        job = session.get(JobRecord, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        _authorized_case(session, job.case_id)
    cursor = event_cursor(after, last_event_id)

    async def generate():
        nonlocal cursor
        idle = 0
        while not await request.is_disconnected():
            batch = await asyncio.to_thread(events_after, job_id, cursor)
            if batch:
                idle = 0
                for event in batch:
                    cursor = int(event["id"])
                    yield f"id: {cursor}\nevent: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
            else:
                idle += 1
                if idle >= 15:
                    idle = 0
                    yield ": keep-alive\n\n"
                await asyncio.sleep(1)
        return

    return StreamingResponse(generate(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@app.get("/api/v1/cases/{case_id}/artifacts")
def list_artifacts(case_id: str, phase: str | None = None, session: Session = Depends(get_session)) -> dict[str, Any]:
    _authorized_case(session, case_id)
    query = select(ArtifactRecord).where(ArtifactRecord.case_id == case_id)
    if phase:
        query = query.where(ArtifactRecord.phase == phase)
    return {"items": [_artifact_body(item) for item in session.scalars(query.order_by(ArtifactRecord.relative_path)).all()]}


@app.get("/api/v1/artifacts/{artifact_id}")
def get_artifact(artifact_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    item = session.get(ArtifactRecord, artifact_id)
    if item is None:
        raise HTTPException(status_code=404, detail="产物不存在")
    case = _authorized_case(session, item.case_id)
    try:
        path = LocalArtifactStore(Path(case.directory)).resolve(item.relative_path)
    except (UnsafeArtifactPath, FileNotFoundError) as exc:
        raise HTTPException(status_code=404, detail="产物文件不存在") from exc
    return {**_artifact_body(item), "preview": preview_artifact(path, item.kind)}


@app.get("/api/v1/artifacts/{artifact_id}/content")
def artifact_content(artifact_id: str, download: bool = False, session: Session = Depends(get_session)) -> FileResponse:
    item = session.get(ArtifactRecord, artifact_id)
    if item is None:
        raise HTTPException(status_code=404, detail="产物不存在")
    case = _authorized_case(session, item.case_id)
    try:
        path = LocalArtifactStore(Path(case.directory)).resolve(item.relative_path)
    except (UnsafeArtifactPath, FileNotFoundError) as exc:
        raise HTTPException(status_code=404, detail="产物文件不存在") from exc
    return FileResponse(path, media_type=item.mime_type, filename=path.name if download else None, content_disposition_type="attachment" if download else "inline")


@app.post("/api/v1/cases/{case_id}/exports", status_code=202)
def create_export(case_id: str, phase: str | None = None, session: Session = Depends(get_session)) -> dict[str, Any]:
    _authorized_case(session, case_id)
    query = select(ArtifactRecord).where(ArtifactRecord.case_id == case_id)
    if phase:
        query = query.where(ArtifactRecord.phase == phase)
    artifacts = session.scalars(query).all()
    if not artifacts:
        raise HTTPException(status_code=409, detail="当前没有可导出的产物")
    digest = manifest_hash(artifacts)
    existing = session.scalar(
        select(ExportRecord).where(
            ExportRecord.case_id == case_id,
            ExportRecord.phase == phase,
            ExportRecord.manifest_hash == digest,
            ExportRecord.status == "ready",
        )
    )
    if existing:
        return {"export_id": existing.id, "status": existing.status}
    export = ExportRecord(id=str(uuid.uuid4()), case_id=case_id, phase=phase, status="queued", manifest_hash=digest)
    session.add(export)
    session.commit()
    if settings.celery_eager:
        threading.Thread(target=build_export.delay, args=(export.id,), daemon=True).start()
    else:
        build_export.delay(export.id)
    return {"export_id": export.id, "status": export.status}


@app.get("/api/v1/exports/{export_id}")
def get_export(export_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    export = session.get(ExportRecord, export_id)
    if export is None:
        raise HTTPException(status_code=404, detail="导出任务不存在")
    _authorized_case(session, export.case_id)
    return {
        "id": export.id,
        "status": export.status,
        "phase": export.phase,
        "error": export.error_message,
        "download_url": f"/api/v1/exports/{export.id}/download" if export.status == "ready" else None,
    }


@app.get("/api/v1/exports/{export_id}/download")
def download_export(export_id: str, session: Session = Depends(get_session)) -> FileResponse:
    export = session.get(ExportRecord, export_id)
    if export is None or export.status != "ready" or not export.relative_path:
        raise HTTPException(status_code=404, detail="导出文件尚未就绪")
    case = _authorized_case(session, export.case_id)
    try:
        path = LocalArtifactStore(Path(case.directory)).resolve(export.relative_path)
    except (UnsafeArtifactPath, FileNotFoundError) as exc:
        raise HTTPException(status_code=404, detail="导出文件不存在") from exc
    name = f"{case.display_name}-{export.phase or 'all'}.zip"
    return FileResponse(path, media_type="application/zip", filename=name)


if _FRONTEND_DIST.exists():
    assets = _FRONTEND_DIST / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=assets), name="frontend-assets")

    @app.get("/{path:path}")
    def frontend(path: str) -> FileResponse:
        candidate = (_FRONTEND_DIST / path).resolve()
        if path and candidate.is_file() and _FRONTEND_DIST.resolve() in candidate.parents:
            return FileResponse(candidate)
        return FileResponse(_FRONTEND_DIST / "index.html")
else:
    @app.get("/")
    def frontend_missing() -> JSONResponse:
        return JSONResponse({"message": "前端尚未构建，请在 geng_agent/web/frontend 运行 npm run build"})
