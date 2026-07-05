from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol

PHASES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("paper_analysis", "论文解构", ("paper", "engineering_facts")),
    ("repro_design", "复现设计", ("repro_tasks", "experiment_index")),
    ("project_build", "工程构建", ("repro_project_manifest", "repro_project")),
    ("execution", "执行与自修正", ("runtime",)),
    ("evidence_review", "证据审查", ("result_review", "review")),
)


class PipelineCancelled(RuntimeError):
    """Raised cooperatively between expensive pipeline boundaries."""


class ProgressReporter(Protocol):
    def emit(self, event_type: str, *, phase: str, step: str | None = None,
             message: str | None = None, data: dict[str, Any] | None = None) -> None: ...
    def check_cancelled(self) -> None: ...


class NullProgressReporter:
    def emit(self, event_type: str, *, phase: str, step: str | None = None,
             message: str | None = None, data: dict[str, Any] | None = None) -> None:
        return None

    def check_cancelled(self) -> None:
        return None


@dataclass(slots=True)
class CallbackProgressReporter:
    callback: Callable[[dict[str, Any]], None]
    cancelled: Callable[[], bool] | None = None

    def emit(self, event_type: str, *, phase: str, step: str | None = None,
             message: str | None = None, data: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"type": event_type, "phase": phase}
        if step is not None:
            payload["step"] = step
        if message is not None:
            payload["message"] = message
        if data:
            payload["data"] = data
        self.callback(payload)

    def check_cancelled(self) -> None:
        if self.cancelled is not None and self.cancelled():
            raise PipelineCancelled("任务已取消")


def phase_for_step(step: str) -> str:
    for phase_id, _label, steps in PHASES:
        if step in steps:
            return phase_id
    raise KeyError(step)
