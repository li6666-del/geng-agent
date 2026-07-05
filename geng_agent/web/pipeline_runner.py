from __future__ import annotations

from typing import Any

from geng_agent.pipeline import ReviewPipeline
from geng_agent.progress import ProgressReporter


class InstrumentedReviewPipeline(ReviewPipeline):
    """Adds durable phase callbacks without changing the CLI pipeline contract."""

    def __init__(self, *args: Any, progress: ProgressReporter, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.progress = progress
        self._phase_started: set[str] = set()

    def _emit(self, event_type: str, phase: str, step: str | None = None, message: str | None = None,
              data: dict[str, Any] | None = None) -> None:
        self.progress.check_cancelled()
        if event_type == "phase.started":
            if phase in self._phase_started:
                return
            self._phase_started.add(phase)
        self.progress.emit(event_type, phase=phase, step=step, message=message, data=data)

    def _complete_step(self, phase: str, step: str, message: str, value: Any) -> Any:
        self._emit("step.completed", phase, step, message)
        return value

    def _load_or_create_paper(self, **kwargs: Any) -> dict[str, Any]:
        self._emit("phase.started", "paper_analysis", message="开始解析论文")
        return self._complete_step("paper_analysis", "paper", "论文解析完成", super()._load_or_create_paper(**kwargs))

    def _augment_facts_with_gap_finder(self, **kwargs: Any) -> dict[str, Any]:
        value = super()._augment_facts_with_gap_finder(**kwargs)
        self._complete_step("paper_analysis", "engineering_facts", "工程事实提取完成", value)
        self._emit("phase.completed", "paper_analysis", message="论文解构完成")
        return value

    def _augment_tasks_with_gap_finder(self, **kwargs: Any) -> dict[str, Any]:
        self._emit("phase.started", "repro_design", message="开始设计复现任务")
        return self._complete_step("repro_design", "repro_tasks", "复现任务生成完成", super()._augment_tasks_with_gap_finder(**kwargs))

    def _load_or_create_experiment_index(self, **kwargs: Any) -> dict[str, Any]:
        value = super()._load_or_create_experiment_index(**kwargs)
        self._complete_step("repro_design", "experiment_index", "实验索引建立完成", value)
        self._emit("phase.completed", "repro_design", message="复现设计完成")
        return value

    def _load_or_create_repro_manifest(self, **kwargs: Any) -> dict[str, Any]:
        self._emit("phase.started", "project_build", message="开始生成复现工程")
        return self._complete_step("project_build", "repro_project_manifest", "复现清单生成完成", super()._load_or_create_repro_manifest(**kwargs))

    def _ensure_repro_project_from_manifest(self, **kwargs: Any) -> list:
        value = super()._ensure_repro_project_from_manifest(**kwargs)
        return self._complete_step("project_build", "repro_project", "复现工程构建完成", value)

    def _load_or_run_repro(self, **kwargs: Any) -> dict[str, Any]:
        self._emit("phase.completed", "project_build", message="工程构建完成")
        self._emit("phase.started", "execution", message="开始受限运行与自修正")
        value = super()._load_or_run_repro(**kwargs)
        self._complete_step("execution", "runtime", "运行阶段结束", value)
        self._emit("phase.completed", "execution", message="执行与自修正阶段结束")
        return value

    def _run_result_review_if_ready(self, **kwargs: Any) -> dict[str, Any]:
        self._emit("phase.started", "evidence_review", message="开始证据对比")
        return self._complete_step("evidence_review", "result_review", "结果证据审查完成", super()._run_result_review_if_ready(**kwargs))

    def _generate_docx_reports(self, **kwargs: Any) -> dict[str, Any]:
        value = super()._generate_docx_reports(**kwargs)
        self._complete_step("evidence_review", "review", "报告产物生成完成", value)
        self._emit("phase.completed", "evidence_review", message="复现航行完成")
        return value
