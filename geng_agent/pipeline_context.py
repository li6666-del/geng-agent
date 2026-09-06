from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .pipeline_models import PipelineRunOptions
from .progress import PhaseProgressTracker


@dataclass(slots=True)
class PipelineRunContext:
    paper_path: Path
    output_dir: Path
    audit_dir: Path
    options: PipelineRunOptions
    progress_tracker: PhaseProgressTracker
    cumulative_usage: Callable[[], dict[str, int]]
    usage_by_model: Callable[[], dict[str, dict[str, int]]]
    run_start: float = field(default_factory=time.perf_counter)
    wall_start: float = field(default_factory=time.time)
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    cost_marks: list[dict[str, Any]] = field(default_factory=list)

    def begin(self, stage: str) -> None:
        self.progress_tracker.begin(stage)

    def mark(self, stage: str) -> None:
        self.progress_tracker.complete(stage)
        self.cost_marks.append(
            {
                "stage": stage,
                "elapsed_s": round(time.perf_counter() - self.run_start, 3),
                **self.cumulative_usage(),
            }
        )

    def elapsed_s(self) -> float:
        return round(time.perf_counter() - self.run_start, 3)

    def finish(self) -> None:
        self.progress_tracker.finish()

    def persist_cost_snapshot(self) -> None:
        """Save interrupted-run costs unless a richer terminal report saved them."""
        if (self.audit_dir / "pipeline_cost_events" / f"{self.run_id}.json").is_file():
            return
        from .risk_report import _build_run_cost
        from .codex_cost import persist_pipeline_cost
        marks = [*self.cost_marks, {"stage": "interrupted", "elapsed_s": self.elapsed_s(), **self.cumulative_usage()}]
        cost = _build_run_cost(marks, total_wall_s=self.elapsed_s(), by_model=self.usage_by_model(),
                               audit_dir=self.audit_dir, codex_since=self.wall_start)
        cost["interrupted_before_terminal_report"] = True
        persist_pipeline_cost(self.output_dir, cost, run_id=self.run_id, started_at=self.wall_start)
