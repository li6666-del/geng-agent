from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from geng_agent.config import get_cases_root
from geng_agent.pipeline import ReviewPipeline
from geng_agent.status import inspect_case_status


_MAX_PDF_BYTES = 80 * 1024 * 1024


@dataclass
class RunRecord:
    run_id: str
    case_dir: Path
    paper_path: Path
    status: str = "queued"
    error: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    review_path: str | None = None
    thread: threading.Thread | None = field(default=None, repr=False)


_lock = threading.Lock()
_runs: dict[str, RunRecord] = {}


def get_run(run_id: str) -> RunRecord | None:
    with _lock:
        return _runs.get(run_id)


def active_run() -> RunRecord | None:
    with _lock:
        for record in _runs.values():
            if record.status in {"queued", "running"}:
                return record
    return None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_meta(case_dir: Path, *, paper_rel: str, source: str) -> None:
    geng = case_dir / ".geng"
    geng.mkdir(parents=True, exist_ok=True)
    meta = {
        "paper_path": paper_rel,
        "source": source,
        "created_at": _utc_now(),
    }
    (geng / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def save_uploaded_pdf(filename: str, data: bytes) -> RunRecord:
    if len(data) > _MAX_PDF_BYTES:
        raise ValueError(f"PDF 过大（>{_MAX_PDF_BYTES // (1024 * 1024)} MB）。")
    if not filename.lower().endswith(".pdf"):
        raise ValueError("仅支持 PDF 文件。")

    cases_root = get_cases_root()
    cases_root.mkdir(parents=True, exist_ok=True)
    run_id = uuid.uuid4().hex[:12]
    case_dir = cases_root / f"case_{int(time.time())}_{run_id}"
    paper_dir = case_dir / "paper"
    paper_dir.mkdir(parents=True)
    paper_path = paper_dir / Path(filename).name
    paper_path.write_bytes(data)
    _write_meta(case_dir, paper_rel=f"paper/{paper_path.name}", source="upload")
    return _enqueue(run_id, case_dir, paper_path)


def start_from_pdf_url(url: str) -> RunRecord:
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("PDF 地址须为 http:// 或 https:// 链接。")

    request = Request(url, headers={"User-Agent": "geng-agent-web/0.1"})
    with urlopen(request, timeout=120) as response:
        content_type = (response.headers.get("Content-Type") or "").lower()
        data = response.read(_MAX_PDF_BYTES + 1)
    if len(data) > _MAX_PDF_BYTES:
        raise ValueError(f"PDF 过大（>{_MAX_PDF_BYTES // (1024 * 1024)} MB）。")
    if "pdf" not in content_type and not url.lower().endswith(".pdf"):
        raise ValueError("链接内容类型不是 PDF，请检查地址。")

    name = Path(parsed.path).name or "paper.pdf"
    if not name.lower().endswith(".pdf"):
        name = "paper.pdf"

    cases_root = get_cases_root()
    cases_root.mkdir(parents=True, exist_ok=True)
    run_id = uuid.uuid4().hex[:12]
    case_dir = cases_root / f"case_{int(time.time())}_{run_id}"
    paper_dir = case_dir / "paper"
    paper_dir.mkdir(parents=True)
    paper_path = paper_dir / name
    paper_path.write_bytes(data)
    _write_meta(case_dir, paper_rel=f"paper/{paper_path.name}", source="url")
    return _enqueue(run_id, case_dir, paper_path)


def _enqueue(run_id: str, case_dir: Path, paper_path: Path) -> RunRecord:
    with _lock:
        if any(record.status in {"queued", "running"} for record in _runs.values()):
            raise RuntimeError("已有任务在运行，请等待完成后再提交。")

        record = RunRecord(
            run_id=run_id,
            case_dir=case_dir,
            paper_path=paper_path,
            status="queued",
        )
        _runs[run_id] = record

    thread = threading.Thread(target=_run_pipeline, args=(record,), name=f"geng-run-{run_id}", daemon=True)
    record.thread = thread
    thread.start()
    return record


def _run_pipeline(record: RunRecord) -> None:
    with _lock:
        record.status = "running"
        record.started_at = _utc_now()

    try:
        pipeline = ReviewPipeline()
        result = pipeline.run(
            paper_path=record.paper_path,
            output_dir=record.case_dir,
            run_repro=True,
            resume=False,
            analysis_fallback=True,
            analysis_backend="codex",
        )
        with _lock:
            record.status = "succeeded"
            record.review_path = str(result.review_path)
            record.finished_at = _utc_now()
    except Exception as exc:
        with _lock:
            record.status = "failed"
            record.error = f"{type(exc).__name__}: {exc}"
            record.finished_at = _utc_now()


def poll_progress(run_id: str) -> dict[str, Any]:
    record = get_run(run_id)
    if record is None:
        raise KeyError(run_id)

    inspect = inspect_case_status(record.case_dir)
    payload: dict[str, Any] = {
        "run_id": run_id,
        "status": record.status,
        "error": record.error,
        "case_dir": str(record.case_dir),
        "started_at": record.started_at,
        "finished_at": record.finished_at,
        "review_path": record.review_path,
        "next_stage": inspect.get("next_stage"),
        "complete": inspect.get("next_stage") is None,
        "inspect": inspect,
    }
    return payload


def stream_progress(run_id: str, *, interval: float = 1.0) -> Callable[[], dict[str, Any] | None]:
    last_signature = ""

    def next_event() -> dict[str, Any] | None:
        nonlocal last_signature
        record = get_run(run_id)
        if record is None:
            return {"type": "error", "message": "任务不存在"}

        from geng_agent.web.stages import build_stage_progress

        body = poll_progress(run_id)
        stages = build_stage_progress(body["inspect"])
        signature = json.dumps(
            {
                "status": record.status,
                "next_stage": body.get("next_stage"),
                "stages": [(s["id"], s["state"]) for s in stages],
            },
            sort_keys=True,
        )
        if signature == last_signature and record.status in {"queued", "running"}:
            return None
        last_signature = signature

        event: dict[str, Any] = {
            "type": "progress",
            "status": record.status,
            "stages": stages,
            "next_stage": body.get("next_stage"),
            "complete": body.get("complete"),
        }
        if record.status in {"succeeded", "failed"}:
            event["type"] = "finished"
            event["ok"] = record.status == "succeeded"
            event["error"] = record.error
            event["review_path"] = record.review_path
            event["case_dir"] = str(record.case_dir)
        return event

    return next_event
