"""Pull cloud papers over HTTPS and run the existing pipeline on this computer.

Run ``python -m geng_agent.web.local_worker --config worker.json``. The JSON
accepts url, token, worker_id, case_root, poll_seconds, heartbeat_seconds,
request_timeout, retry_seconds and env. Environment credentials take precedence;
there is deliberately no command-line token option.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import re
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import urljoin, urlsplit

import httpx

from geng_agent.artifact_paths import path_is_link
from geng_agent.config import get_cases_root
from geng_agent.progress import CallbackProgressReporter, PipelineCancelled

from .delivery import build_delivery

LOG = logging.getLogger("geng_agent.local_worker")
_ENV_OVERRIDES = {"GENG_MODEL_CONFIG", "GENG_SHARED_SCIENCE_PYTHON", "GENG_PYTHON"}
_RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}


class WorkerError(RuntimeError):
    """A sanitized configuration, protocol or local storage error."""


class WorkerHTTPError(WorkerError):
    def __init__(self, status: int) -> None:
        self.status = status
        super().__init__(f"Cloud worker request returned HTTP {status}")


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise WorkerError("Cannot read worker configuration or durable state JSON") from exc
    if not isinstance(value, dict):
        raise WorkerError("Worker configuration and durable state must be JSON objects")
    return value


def _safe_local_path(root: Path, relative: str) -> Path:
    path = root / relative
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise WorkerError("Worker storage must remain inside the configured case root") from exc
    current = path
    while current != root.parent:
        if path_is_link(current):
            raise WorkerError("Worker storage cannot use symbolic links or junctions")
        if current == root:
            break
        current = current.parent
    return path


@contextmanager
def worker_lock(case_root: Path) -> Iterator[None]:
    """OS lock is released on process death; stale PID files cannot block restart."""
    lock_path = _safe_local_path(case_root, ".cloud-worker/worker.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise WorkerError("A local cloud worker is already using this case root") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@dataclass(frozen=True)
class WorkerConfig:
    url: str
    token: str = field(repr=False)
    case_root: Path
    worker_id: str | None = None
    poll_seconds: float = 10.0
    heartbeat_seconds: float = 20.0
    request_timeout: float = 60.0
    retry_seconds: float = 5.0
    max_pdf_bytes: int = 80 * 1024 * 1024

    def __post_init__(self) -> None:
        address = urlsplit(self.url)
        local = address.hostname in {"localhost", "127.0.0.1", "::1"}
        if (address.scheme != "https" and not (address.scheme == "http" and local)) or not address.hostname:
            raise WorkerError("Worker URL requires HTTPS; HTTP is allowed only for loopback testing")
        if address.username or address.password or address.query or address.fragment or address.path not in {"", "/"}:
            raise WorkerError("Worker URL must be an origin without credentials, path, query or fragment")
        if not isinstance(self.token, str) or len(self.token) < 32 or any(c.isspace() for c in self.token):
            raise WorkerError("Set a strong worker token of at least 32 characters in GENG_WORKER_TOKEN or the config file")
        if self.worker_id is not None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", self.worker_id):
            raise WorkerError("worker_id must start with a letter or digit and contain 1 to 128 letters, digits, underscores, dots or hyphens")
        for name in ("poll_seconds", "heartbeat_seconds", "request_timeout", "retry_seconds"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise WorkerError(f"{name} must be a positive finite number")
        if not isinstance(self.max_pdf_bytes, int) or self.max_pdf_bytes <= 0:
            raise WorkerError("max_pdf_bytes must be a positive integer")

    @classmethod
    def load(cls, path: Path | None = None) -> "WorkerConfig":
        raw = _read_json(path) if path else {}
        overrides = raw.get("env", {})
        if not isinstance(overrides, dict) or set(overrides) - _ENV_OVERRIDES:
            raise WorkerError("env accepts only GENG_MODEL_CONFIG, GENG_SHARED_SCIENCE_PYTHON and GENG_PYTHON")
        if any(not isinstance(value, str) for value in overrides.values()):
            raise WorkerError("Worker env values must be strings")
        try:
            config = cls(
                url=str(os.getenv("GENG_WORKER_URL") or raw.get("url", "")).rstrip("/"),
                token=os.getenv("GENG_WORKER_TOKEN") or raw.get("token", ""),
                case_root=Path(os.getenv("GENG_CASES_ROOT") or raw.get("case_root") or get_cases_root()).expanduser().resolve(),
                worker_id=os.getenv("GENG_WORKER_ID") or raw.get("worker_id"),
                **{key: raw[key] for key in ("poll_seconds", "heartbeat_seconds", "request_timeout", "retry_seconds", "max_pdf_bytes") if key in raw},
            )
        except (TypeError, ValueError) as exc:
            raise WorkerError("Invalid local worker configuration") from exc
        os.environ.update(overrides)
        return config


class LocalWorker:
    def __init__(self, config: WorkerConfig, *, client: httpx.Client | None = None,
                 pipeline_factory: Callable[[], Any] | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.config = config
        config.case_root.mkdir(parents=True, exist_ok=True)
        identity_key = hashlib.sha256(config.url.encode("utf-8")).hexdigest()[:16]
        identity_path = _safe_local_path(config.case_root, f".cloud-worker/{identity_key}.json")
        identity = _read_json(identity_path) if identity_path.exists() else {}
        saved_id = identity.get("worker_id")
        if config.worker_id and saved_id and config.worker_id != saved_id:
            raise WorkerError("Configured worker_id differs from the persisted identity for this cloud")
        self.worker_id = config.worker_id or saved_id or str(uuid.uuid4())
        if not isinstance(self.worker_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", self.worker_id):
            raise WorkerError("Invalid persisted worker identity")
        if not saved_id:
            _atomic_json(identity_path, {"worker_id": self.worker_id})
        self.client = client or httpx.Client(timeout=httpx.Timeout(config.request_timeout), follow_redirects=False)
        self._owns_client = client is None
        self._pipeline_factory = pipeline_factory
        self._sleep = sleep

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.config.token}", "X-Worker-Id": self.worker_id}

    def _url(self, path: str) -> str:
        return self.config.url.rstrip("/") + path

    @staticmethod
    def _job_path(job_id: str, operation: str) -> str:
        return f"/api/v1/worker/jobs/{job_id}/{operation}"

    def _request(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = self.client.post(self._url(path), headers=self._headers(), json=payload,
                                    timeout=self.config.request_timeout, follow_redirects=False)
        return self._response(response)

    @staticmethod
    def _response(response: httpx.Response) -> dict[str, Any]:
        if not 200 <= response.status_code < 300:
            raise WorkerHTTPError(response.status_code)
        try:
            payload = response.json()
        except ValueError as exc:
            raise WorkerError("Cloud worker endpoint returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise WorkerError("Cloud worker endpoint must return a JSON object")
        return payload

    def _retry(self, operation: Callable[[], Any], *, cancelled: threading.Event | None = None) -> Any:
        attempt = 0
        while True:
            if cancelled is not None and cancelled.is_set():
                raise PipelineCancelled("任务已取消")
            try:
                return operation()
            except WorkerHTTPError as exc:
                if exc.status not in _RETRY_STATUS:
                    raise
                reason = f"HTTP {exc.status}"
            except httpx.TransportError:
                reason = "network unavailable"
            attempt += 1
            delay = min(60.0, self.config.retry_seconds * 2 ** min(attempt - 1, 6))
            LOG.warning("Cloud transfer retry in %.1fs (%s); local scientific results are retained", delay, reason)
            if cancelled is not None:
                cancelled.wait(delay)
            else:
                self._sleep(delay)

    def run_once(self) -> bool:
        payload = self._retry(lambda: self._request("/api/v1/worker/claim", {"worker_id": self.worker_id}))
        if "job" not in payload:
            raise WorkerError("Cloud claim response has no job field")
        job = payload["job"]
        if job is None:
            return False
        if not isinstance(job, dict):
            raise WorkerError("Cloud claim job must be an object")
        self.run_job(job)
        return True

    def run_forever(self) -> None:
        while True:
            if not self.run_once():
                self._sleep(self.config.poll_seconds)

    def _download(self, job: dict[str, Any], destination: Path) -> None:
        expected = self._url(self._job_path(job["job_id"], "paper"))
        supplied = urljoin(self.config.url + "/", str(job.get("paper_url", "")))
        if supplied != expected:
            raise WorkerError("Cloud paper URL must match the authenticated job endpoint on the configured origin")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.is_file():
            with destination.open("rb") as existing:
                if existing.read(5) == b"%PDF-":
                    return
            raise WorkerError("Existing local paper is not a PDF; it was preserved for inspection")
        temporary = destination.with_name(f".paper-{uuid.uuid4().hex[:8]}.part")
        try:
            with self.client.stream("GET", expected, headers=self._headers(),
                                    timeout=self.config.request_timeout, follow_redirects=False) as response:
                if not 200 <= response.status_code < 300:
                    raise WorkerHTTPError(response.status_code)
                size = 0
                prefix = bytearray()
                with temporary.open("xb") as handle:
                    for chunk in response.iter_bytes(1024 * 1024):
                        size += len(chunk)
                        if size > self.config.max_pdf_bytes:
                            raise WorkerError("Downloaded paper exceeds max_pdf_bytes")
                        if len(prefix) < 5:
                            prefix.extend(chunk[:5 - len(prefix)])
                        handle.write(chunk)
                    if bytes(prefix) != b"%PDF-":
                        raise WorkerError("Cloud paper endpoint did not return a PDF")
                    handle.flush()
                    os.fsync(handle.fileno())
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)

    def _upload(self, job_id: str, bundle: Path) -> dict[str, Any]:
        # httpx streams file objects; never load a potentially large ZIP in RAM.
        with bundle.open("rb") as handle:
            response = self.client.post(self._url(self._job_path(job_id, "complete")),
                                        headers=self._headers(), data={"worker_id": self.worker_id},
                                        files={"bundle": (bundle.name, handle, "application/zip")},
                                        timeout=self.config.request_timeout, follow_redirects=False)
        return self._response(response)

    def _upload_or_cancel(self, job_id: str, bundle: Path, cancelled: threading.Event) -> dict[str, Any]:
        try:
            return self._upload(job_id, bundle)
        except WorkerHTTPError as exc:
            if exc.status == 409:
                # Cancellation can arrive while a large multipart body is in flight,
                # before the regular heartbeat has seen it. Read the explicit state
                # before deciding whether this conflict means cancellation.
                status = self._retry(lambda: self._request(self._job_path(job_id, "heartbeat"),
                                                           {"worker_id": self.worker_id}))
                if status.get("cancel_requested") is True or status.get("status") == "cancelled":
                    cancelled.set()
                if cancelled.is_set():
                    raise PipelineCancelled("任务已取消") from exc
                if status.get("status") in {"succeeded", "failed"}:
                    return status
            raise

    def _heartbeat(self, job_id: str, cancelled: threading.Event) -> None:
        try:
            payload = self._request(self._job_path(job_id, "heartbeat"), {"worker_id": self.worker_id})
            if payload.get("cancel_requested") is True or payload.get("status") == "cancelled":
                cancelled.set()
        except (httpx.TransportError, WorkerError):
            # A lost connection/lease is never evidence of user cancellation.
            LOG.warning("Heartbeat unavailable; scientific processing continues locally")

    def _heartbeat_loop(self, job_id: str, stopped: threading.Event, cancelled: threading.Event) -> None:
        while not stopped.wait(self.config.heartbeat_seconds):
            self._heartbeat(job_id, cancelled)

    def _pipeline(self) -> Any:
        if self._pipeline_factory is not None:
            return self._pipeline_factory()
        from geng_agent.pipeline import ReviewPipeline
        return ReviewPipeline()

    def run_job(self, job: dict[str, Any]) -> None:
        try:
            job_id, case_id = str(uuid.UUID(job["job_id"])), str(uuid.UUID(job["case_id"]))
        except (KeyError, ValueError, TypeError, AttributeError) as exc:
            raise WorkerError("Cloud job and case identifiers must be UUIDs") from exc
        job = {**job, "job_id": job_id, "case_id": case_id}
        case_dir = _safe_local_path(self.config.case_root, f"cloud_{case_id}")
        case_dir.mkdir(parents=True, exist_ok=True)
        state_path = _safe_local_path(case_dir, ".cloud-worker.json")
        state = _read_json(state_path) if state_path.exists() else {"case_id": case_id}
        if state.get("case_id") != case_id:
            raise WorkerError("Local worker state does not belong to this case")
        complete = state.get("pipeline_complete") is True or job.get("pipeline_complete") is True
        state["pipeline_complete"] = complete
        cancelled, stopped = threading.Event(), threading.Event()
        reporter = CallbackProgressReporter(callback=lambda _payload: None, cancelled=cancelled.is_set)
        self._heartbeat(job_id, cancelled)
        heartbeat = threading.Thread(target=self._heartbeat_loop, args=(job_id, stopped, cancelled), daemon=True,
                                     name="cloud-worker-heartbeat")
        heartbeat.start()
        LOG.info("Processing cloud job %s in %s", job_id, case_dir.name)
        try:
            pending = state.get("pending_finish") if state.get("job_id") == job_id else None
            state["job_id"] = job_id
            if pending:
                self._retry(lambda: self._request(self._job_path(job_id, "finish"), pending))
                state.pop("pending_finish", None)
                _atomic_json(state_path, state)
                return
            state.pop("pending_finish", None)
            _atomic_json(state_path, state)
            failure: dict[str, Any] | None = None
            try:
                reporter.check_cancelled()
                if not complete:
                    paper_path = _safe_local_path(case_dir, "paper/paper.pdf")
                    self._retry(lambda: self._download(job, paper_path), cancelled=cancelled)
                    reporter.check_cancelled()
                    result = self._pipeline().run(paper_path=paper_path, output_dir=case_dir, run_repro=True,
                                                  resume=True, analysis_backend="codex", progress=reporter)
                    delivery_status = getattr(result, "delivery_status", "complete")
                    if delivery_status != "complete":
                        raise WorkerError("Scientific pipeline did not finish delivery")
                    # Persist before cancellation checks, packaging or any HTTP.
                    complete = True
                    state["pipeline_complete"] = True
                    _atomic_json(state_path, state)
                reporter.check_cancelled()
                bundle = build_delivery(case_dir, job_id)
                reporter.check_cancelled()
            except PipelineCancelled:
                failure = {"worker_id": self.worker_id, "status": "cancelled", "pipeline_complete": complete}
            except (WorkerHTTPError, httpx.TransportError):
                raise
            except Exception as exc:
                # Avoid exposing local paths, credentials or model traces to cloud users.
                code = "delivery_package" if complete else "execution_failed"
                LOG.error("Local job failed (%s; %s); retained case %s", code, type(exc).__name__, case_dir.name)
                failure = {"worker_id": self.worker_id, "status": "failed", "error_code": code,
                           "error_message": ("科学流程已完成，但本机交付文件不可用；请在原电脑恢复报告和复现工程后重试。"
                                             if complete else "本地科学流程未完成，请检查原电脑案例记录后重试。"),
                           "pipeline_complete": complete}
            if failure:
                state["pending_finish"] = failure
                _atomic_json(state_path, state)
                self._retry(lambda: self._request(self._job_path(job_id, "finish"), failure))
                state.pop("pending_finish", None)
            else:
                try:
                    uploaded = self._retry(lambda: self._upload_or_cancel(job_id, bundle, cancelled), cancelled=cancelled)
                    if uploaded.get("status") == "succeeded":
                        state["uploaded_job_id"] = job_id
                    state["remote_status"] = uploaded.get("status")
                except PipelineCancelled:
                    failure = {"worker_id": self.worker_id, "status": "cancelled", "pipeline_complete": complete}
                except WorkerHTTPError as exc:
                    if exc.status in {401, 403}:
                        raise
                    # A permanent transfer rejection must release the queue while
                    # retaining completed science for a later delivery-only retry.
                    failure = {"worker_id": self.worker_id, "status": "failed", "error_code": "delivery_upload",
                               "error_message": f"交付包上传被服务器拒绝（HTTP {exc.status}）；本机已保留完整科学结果，请修复上传配置后重试。",
                               "pipeline_complete": complete}
                if failure:
                    state["pending_finish"] = failure
                    _atomic_json(state_path, state)
                    self._retry(lambda: self._request(self._job_path(job_id, "finish"), failure))
                    state.pop("pending_finish", None)
            _atomic_json(state_path, state)
        finally:
            stopped.set()
            heartbeat.join(timeout=self.config.request_timeout + 1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="Local JSON configuration (keep credentials outside the repository)")
    parser.add_argument("--once", action="store_true", help="Claim at most one paper, then exit; delivery retries remain enabled")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        config = WorkerConfig.load(args.config)
        # Credentials must not be inherited by model/tool child processes.
        os.environ.pop("GENG_WORKER_TOKEN", None)
        with worker_lock(config.case_root):
            worker = LocalWorker(config)
            try:
                LOG.info("Local cloud worker ready; worker_id=%s", worker.worker_id)
                if args.once:
                    worker.run_once()
                else:
                    worker.run_forever()
            finally:
                worker.close()
        return 0
    except KeyboardInterrupt:
        LOG.info("Local worker stopped; saved case state will be resumed on the next start")
        return 130
    except WorkerError as exc:
        LOG.error("%s", exc)
        return 1
    except Exception as exc:
        LOG.error("Local worker stopped (%s); inspect local configuration and retained case files", type(exc).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
