from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class PipelineResult:
    output_dir: Path
    review_path: Path
    repro_project_dir: Path
    risk_report_path: Path
    runtime_passed: bool | None = None
    experiment_index_path: Path | None = None
    scientific_architecture_path: Path | None = None
    result_review_path: Path | None = None
    result_review_passed: bool | None = None
    reproducibility_verdict: dict[str, Any] | None = None
    review_docx_path: Path | None = None
    result_review_docx_path: Path | None = None
    reproduction_report_path: Path | None = None
    reproduction_report_docx_path: Path | None = None


@dataclass(frozen=True)
class PipelineRunOptions:
    max_pages: int | None
    run_repro: bool
    run_timeout: float
    mineru_timeout: float
    json_repair_attempts: int
    tasks_timeout: float
    resume: bool
    analysis_fallback: bool
    analysis_backend: str
    analysis_only: bool


@dataclass(slots=True)
class AnalysisFlowResult:
    paper_path: Path
    paper: dict[str, Any]
    paper_images: list[Any]
    mineru_result: dict[str, Any]
    figure_index: dict[str, Any]
    paper_context: str
    facts: dict[str, Any]
    tasks: dict[str, Any]
    paper_thesis: dict[str, Any] | None
    execution_plan: dict[str, Any]
    experiment_index: dict[str, Any]
    scientific_architecture: dict[str, Any] | None
    analysis_warnings: dict[str, Any]
    analysis_stage_invocations: int
    repro_project_dir: Path


@dataclass(slots=True)
class ExecutionFlowResult:
    validation: dict[str, Any]
    scientific_check: dict[str, Any]
    agentic_result: dict[str, Any]
    manifest: dict[str, Any]
    written_files: list[Path]
    runtime_result: dict[str, Any]
    task_records: list[dict[str, Any]]
    writer_review_document: dict[str, Any]
    writer_summary_result: dict[str, Any]
    risk_report: dict[str, Any]
