from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


PHASES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "paper_analysis",
        "论文解构",
        ("start", "mineru_layout", "facts_initial"),
    ),
    (
        "repro_design",
        "复现设计",
        ("tasks_preliminary", "facts", "tasks", "thesis", "experiment_index", "scientific_architecture"),
    ),
    (
        "task_reproduction",
        "任务级复现",
        ("foundation", "generation", "runtime", "task_reporters"),
    ),
    (
        "report_composition",
        "报告编排",
        ("report_editor",),
    ),
    (
        "report_delivery",
        "交付物生成",
        ("reports",),
    ),
)

STEP_PHASE_ALIASES: dict[str, str] = {
    "paper": "paper_analysis",
    "engineering_facts": "paper_analysis",
    "repro_tasks": "repro_design",
    "paper_thesis": "repro_design",
    "scientific_architecture": "repro_design",
    "repro_project_manifest": "task_reproduction",
    "foundation_manifest": "task_reproduction",
    "repro_project": "task_reproduction",
    "verification_result": "task_reproduction",
    "reproduction_report": "report_composition",
    "result_review": "report_composition",
    "review": "report_composition",
    "review_docx": "report_delivery",
    "reproduction_report_docx": "report_delivery",
    "result_review_docx": "report_delivery",
}


class PipelineCancelled(RuntimeError):
    """Raised when a Web job requests cancellation at a safe boundary."""


class ProgressReporter(Protocol):
    def emit(
        self,
        event_type: str,
        *,
        phase: str,
        step: str | None = None,
        message: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None: ...

    def check_cancelled(self) -> None: ...


class NullProgressReporter:
    def emit(
        self,
        event_type: str,
        *,
        phase: str,
        step: str | None = None,
        message: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        return None

    def check_cancelled(self) -> None:
        return None


@dataclass(slots=True)
class CallbackProgressReporter:
    callback: Callable[[dict[str, Any]], None]
    cancelled: Callable[[], bool] | None = None

    def emit(
        self,
        event_type: str,
        *,
        phase: str,
        step: str | None = None,
        message: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
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
    if step in STEP_PHASE_ALIASES:
        return STEP_PHASE_ALIASES[step]
    raise KeyError(step)


@dataclass(slots=True)
class PhaseProgressTracker:
    reporter: ProgressReporter
    current_phase: str | None = None
    started_steps: set[str] = field(default_factory=set)

    def begin(self, step: str, message: str | None = None) -> None:
        self.reporter.check_cancelled()
        phase = phase_for_step(step)
        if phase != self.current_phase:
            if self.current_phase is not None:
                self.reporter.emit("phase.completed", phase=self.current_phase)
            self.current_phase = phase
            self.reporter.emit("phase.started", phase=phase)
        if step not in self.started_steps:
            self.started_steps.add(step)
            self.reporter.emit("step.started", phase=phase, step=step, message=message)

    def complete(self, step: str, message: str | None = None) -> None:
        self.begin(step)
        self.reporter.emit(
            "step.completed",
            phase=phase_for_step(step),
            step=step,
            message=message,
        )

    def finish(self) -> None:
        if self.current_phase is not None:
            self.reporter.emit("phase.completed", phase=self.current_phase)
            self.current_phase = None
