"""Run-local observations of actual Writer and Reporter model sessions."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
import time
from typing import Any, Iterator
import uuid

from .outputs import write_json
from .progress import ProgressReporter


_ROLES = ("task_writer", "task_reporter")
_CURRENT: ContextVar[AgentActivity | None] = ContextVar("geng_agent_activity", default=None)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AgentActivity:
    """Serialize observations, never the model calls that they describe."""

    def __init__(self, audit_dir: Path, reporter: ProgressReporter | None) -> None:
        self.audit_dir = audit_dir
        self.reporter = reporter
        self.lock = Lock()
        self.started: dict[str, float] = {}
        self.document: dict[str, Any] = {
            "schema_version": 1,
            "run_id": uuid.uuid4().hex,
            "scope_status": "running",
            "started_at": _now(),
            "updated_at": _now(),
            "sequence": 0,
            "active": {**dict.fromkeys(_ROLES, 0), "total": 0},
            "peak": {**dict.fromkeys(_ROLES, 0), "total": 0},
            "cached": dict.fromkeys(_ROLES, 0),
            "sessions": {},
            "cache_hits": [],
            "warnings": [],
        }
        with self.lock:
            self._persist()

    def _persist(self) -> None:
        try:
            # Keep each resume's history while publishing one live snapshot.
            write_json(self.audit_dir / "agent_activity_runs" / f"{self.document['run_id']}.json", self.document)
            write_json(self.audit_dir / "agent_activity.json", self.document)
        except OSError as exc:
            self._warn(f"activity snapshot unavailable: {type(exc).__name__}")

    def _warn(self, message: str) -> None:
        if message not in self.document["warnings"]:
            self.document["warnings"].append(message)

    def _publish(self, event_type: str, item: dict[str, Any]) -> None:
        self.document["sequence"] += 1
        self.document["updated_at"] = _now()
        self._persist()
        if self.reporter is None:
            return
        role = str(item["role"])
        name = "Writer" if role == "task_writer" else "Reporter"
        action = {"agent.started": "开始", "agent.completed": "完成", "agent.failed": "失败", "agent.cached": "复用缓存"}[event_type]
        active = dict(self.document["active"])
        try:
            self.reporter.emit(
                event_type,
                phase="task_reproduction",
                message=(f"{name} {item['label']} {action}；"
                         f"当前运行 Writer {active['task_writer']}，Reporter {active['task_reporter']}"),
                data={**item, "run_id": self.document["run_id"],
                      "sequence": self.document["sequence"], "active": active,
                      "peak": dict(self.document["peak"])},
            )
        except Exception as exc:
            # A UI outage must not turn a completed scientific session into failure.
            self._warn(f"activity event unavailable: {type(exc).__name__}")
            self._persist()

    def start(self, *, invocation_id: str, role: str, label: str, work_dir: Path) -> None:
        with self.lock:
            if invocation_id in self.document["sessions"]:
                raise ValueError("duplicate agent activity invocation")
            item = {"invocation_id": invocation_id, "role": role, "label": label,
                    "work_dir": str(work_dir), "status": "running", "started_at": _now()}
            self.document["sessions"][invocation_id] = item
            self.started[invocation_id] = time.monotonic()
            for key in (role, "total"):
                self.document["active"][key] += 1
                self.document["peak"][key] = max(self.document["peak"][key], self.document["active"][key])
            self._publish("agent.started", item)

    def finish(self, invocation_id: str, *, ok: bool, error_kind: str | None) -> None:
        with self.lock:
            item = self.document["sessions"].get(invocation_id)
            if item is None or item["status"] != "running":
                return
            item.update(status="completed" if ok else "failed", finished_at=_now(),
                        duration_s=round(time.monotonic() - self.started.pop(invocation_id), 3))
            if error_kind:
                item["error_kind"] = error_kind
            for key in (item["role"], "total"):
                self.document["active"][key] -= 1
            self._publish("agent.completed" if ok else "agent.failed", item)

    def cache_hit(self, *, role: str, label: str, work_dir: Path, task_id: str | None) -> None:
        with self.lock:
            item = {"role": role, "label": label, "work_dir": str(work_dir),
                    "status": "cached", "observed_at": _now()}
            if task_id is not None:
                item["task_id"] = task_id
            self.document["cache_hits"].append(item)
            self.document["cached"][role] += 1
            self._publish("agent.cached", item)

    def close(self, *, failed: bool) -> None:
        with self.lock:
            self.document["scope_status"] = "failed" if failed else "completed"
            self.document["finished_at"] = _now()
            self.document["updated_at"] = _now()
            self._persist()


@dataclass(frozen=True, slots=True)
class _SessionHandle:
    tracker: AgentActivity
    invocation_id: str


@contextmanager
def agent_activity_scope(audit_dir: Path, reporter: ProgressReporter | None = None) -> Iterator[AgentActivity]:
    tracker = AgentActivity(audit_dir, reporter)
    token = _CURRENT.set(tracker)
    failed = True
    try:
        yield tracker
        failed = False
    finally:
        _CURRENT.reset(token)
        tracker.close(failed=failed)


def session_started(*, invocation_id: str, role: str, label: str, work_dir: Path) -> _SessionHandle | None:
    tracker = _CURRENT.get()
    if tracker is None or role not in _ROLES:
        return None
    tracker.start(invocation_id=invocation_id, role=role, label=label, work_dir=work_dir)
    return _SessionHandle(tracker, invocation_id)


def session_finished(handle: _SessionHandle | None, *, ok: bool, error_kind: str | None = None) -> None:
    if handle is not None:
        handle.tracker.finish(handle.invocation_id, ok=ok, error_kind=error_kind)


def record_agent_cached(*, role: str, label: str, work_dir: Path, task_id: str | None = None) -> None:
    tracker = _CURRENT.get()
    if tracker is not None and role in _ROLES:
        tracker.cache_hit(role=role, label=label, work_dir=work_dir, task_id=task_id)
