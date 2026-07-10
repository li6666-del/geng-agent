from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from geng_agent.codex_runner import split_command
from geng_agent.config import get_cases_root, get_config_value
from geng_agent.web import jobs
from geng_agent.web.stages import build_stage_progress

_STATIC = Path(__file__).resolve().parent / "static"

app = FastAPI(title="耿同学agent", version="0.1.0")


@app.get("/", response_class=HTMLResponse)
def index() -> FileResponse:
    return FileResponse(_STATIC / "index.html")


@app.get("/api/health")
def health() -> dict:
    raw_cmd = get_config_value("GENG_CODEX_CMD") or "codex"
    argv = split_command(raw_cmd)
    codex_path = shutil.which(argv[0]) if argv else None
    codex_ok = codex_path is not None
    active = jobs.active_run()
    return {
        "ok": codex_ok,
        "codex_command": raw_cmd,
        "codex_path": codex_path,
        "codex_error": None if codex_ok else f"Codex CLI not found: {raw_cmd}",
        "cases_root": str(get_cases_root()),
        "active_run_id": active.run_id if active else None,
    }


@app.post("/api/runs")
async def create_run(
    pdf_file: UploadFile | None = File(default=None),
    pdf_url: str | None = Form(default=None),
) -> dict:
    if pdf_file is None and not (pdf_url and pdf_url.strip()):
        raise HTTPException(status_code=400, detail="请上传 PDF 或填写 PDF 链接。")
    if pdf_file is not None and pdf_url and pdf_url.strip():
        raise HTTPException(status_code=400, detail="请只选择一种方式：上传文件或填写链接。")

    try:
        if pdf_file is not None:
            data = await pdf_file.read()
            record = jobs.save_uploaded_pdf(pdf_file.filename or "paper.pdf", data)
        else:
            record = jobs.start_from_pdf_url(pdf_url or "")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return {"run_id": record.run_id, "case_dir": str(record.case_dir)}


@app.get("/api/runs/{run_id}")
def get_run_status(run_id: str) -> dict:
    try:
        body = jobs.poll_progress(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc
    body["stages"] = build_stage_progress(body["inspect"])
    return body


@app.get("/api/runs/{run_id}/stream")
async def stream_run(run_id: str) -> StreamingResponse:
    if jobs.get_run(run_id) is None:
        raise HTTPException(status_code=404, detail="任务不存在")

    async def event_generator():
        getter = jobs.stream_progress(run_id, interval=1.0)
        while True:
            event = await asyncio.to_thread(getter)
            if event is not None:
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            record = jobs.get_run(run_id)
            if record is None:
                break
            if record.status in {"succeeded", "failed"} and event and event.get("type") == "finished":
                break
            await asyncio.sleep(1.0)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


def create_app() -> FastAPI:
    return app


if _STATIC.exists():
    app.mount("/static", StaticFiles(directory=_STATIC), name="static")
