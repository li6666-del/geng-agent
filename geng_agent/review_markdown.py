from __future__ import annotations

from pathlib import Path
from typing import Any

"""Human-readable Markdown review report rendering + docx-status/error formatting."""

from .outputs import write_json


def _docx_error(stage: str, exc: Exception) -> dict[str, str]:
    return {"stage": stage, "error": f"{type(exc).__name__}: {exc}"}


def _write_docx_error(output_dir: Path, errors: list[dict[str, str]]) -> None:
    write_json(
        output_dir / "docx_generation_error.json",
        {
            "passed": False,
            "errors": errors,
        },
    )


def render_review_markdown(
    paper: dict[str, Any],
    facts: dict[str, Any],
    tasks: dict[str, Any],
    risk_report: dict[str, Any],
    validation: dict[str, Any],
    runtime_result: dict[str, Any],
    result_review_result: dict[str, Any],
    repro_project_dir: Path,
    docx_generation: dict[str, Any] | None = None,
) -> str:
    engineering_facts = facts.get("engineering_facts", [])
    missing = facts.get("missing_information", [])
    repro_tasks = tasks.get("repro_tasks", [])
    experiment_index = risk_report.get("experiment_index", {})
    experiments = experiment_index.get("experiments") if isinstance(experiment_index, dict) else []
    verdict = risk_report.get("reproducibility_verdict", {})

    lines = [
        "# 耿同学agent 论文工程复现审查报告",
        "",
        "## 基本信息",
        "",
        f"- 论文文件：`{paper.get('source_path')}`",
        f"- 文本块数量：{paper.get('chunk_count')}",
        f"- 复现项目：`{repro_project_dir}`",
        "",
        "## 工程事实概览",
        "",
        f"- 抽取事实数量：{len(engineering_facts) if isinstance(engineering_facts, list) else 0}",
        f"- 缺失信息数量：{len(missing) if isinstance(missing, list) else 0}",
        f"- 论文复现类型：`{facts.get('paper_repro_type', 'unknown')}`",
        "",
    ]

    if isinstance(engineering_facts, list) and engineering_facts:
        for fact in engineering_facts[:12]:
            if not isinstance(fact, dict):
                continue
            source = fact.get("source", {}) if isinstance(fact.get("source"), dict) else {}
            lines.append(f"- `{fact.get('type', 'unknown')}`：{fact.get('name', 'unnamed')}，置信度 `{fact.get('confidence', 'unknown')}`，来源 `{source.get('chunk_id', 'unknown')}`")
    else:
        lines.append("- 未抽取到工程事实。")

    lines.extend(["", "## 复现任务", ""])
    if isinstance(repro_tasks, list) and repro_tasks:
        for task in repro_tasks:
            if isinstance(task, dict):
                lines.append(f"- `{task.get('task_id', 'task')}`：{task.get('target', 'unknown target')}，指标 `{task.get('metric', 'unknown')}`，图表/结论 `{task.get('figure_or_claim', 'unknown')}`")
    else:
        lines.append("- 未生成复现任务。")

    lines.extend(
        [
            "",
            "## 复现风险",
            "",
            f"- 总风险等级：`{risk_report.get('risk_level')}`",
            f"- 假设数量：{risk_report.get('assumptions_count')}",
            f"- 缺失信息数量：{risk_report.get('missing_information_count')}",
            f"- 未解决任务证据缺口：{risk_report.get('task_evidence_gap_count', 0)}",
            "- 说明：风险等级只表示工程复现风险，不等同于造假结论；smoke 通过也不等于论文完全复现成功。",
            "",
            "### 多维风险",
            "",
        ]
    )
    lines.extend(["", "## Experiment Index", ""])
    if isinstance(experiments, list) and experiments:
        for experiment in experiments:
            if isinstance(experiment, dict):
                lines.append(
                    f"- `{experiment.get('experiment_id', 'experiment')}`: task `{experiment.get('task_id')}`, "
                    f"metric `{experiment.get('metric')}`, figure/table `{experiment.get('figure_or_table')}`, "
                    f"pages {experiment.get('source_pages', [])}, limitations {experiment.get('limitations', [])}"
                )
    else:
        lines.append("- experiment_index.json was not generated.")

    if isinstance(verdict, dict) and verdict:
        lines.extend(
            [
                "",
                "## Final Reproducibility Verdict",
                "",
                f"- verdict: `{verdict.get('verdict')}`",
                f"- confidence: `{verdict.get('confidence')}`",
                f"- recommended_action: {verdict.get('recommended_action')}",
                "",
                "### Verdict Reasons",
                "",
            ]
        )
        reasons = verdict.get("reasons")
        if isinstance(reasons, list) and reasons:
            lines.extend(f"- {reason}" for reason in reasons)
        else:
            lines.append("- No reasons were recorded.")

    dimensions = risk_report.get("risk_dimensions", {})
    if isinstance(dimensions, dict):
        for name, dimension in dimensions.items():
            if isinstance(dimension, dict):
                evidence = "; ".join(str(item) for item in dimension.get("evidence", []))
                lines.append(f"- `{name}`：`{dimension.get('level')}`；{evidence}")

    lines.extend(
        [
            "",
            "## 生成项目校验",
            "",
            f"- 必要文件齐全：{validation.get('required_files_present')}",
            f"- Python 语法可编译：{validation.get('python_compiles')}",
            f"- 自动运行复现代码：{_format_runtime_status(runtime_result)}",
            "",
            "## 结果级二次审查",
            "",
            f"- 状态：{_format_result_review_status(result_review_result)}",
        ]
    )
    if result_review_result.get("result_review_path"):
        lines.append(f"- 结构化报告：`{result_review_result.get('result_review_path')}`")
    if result_review_result.get("result_review_markdown_path"):
        lines.append(f"- 可读报告：`{result_review_result.get('result_review_markdown_path')}`")

    docx_generation = docx_generation or {}
    lines.extend(
        [
            "",
            "## Word 报告",
            "",
            f"- 主审查 Word：{_format_docx_status(docx_generation.get('review_docx'))}",
            f"- 结果审查 Word：{_format_docx_status(docx_generation.get('result_review_docx'))}",
        ]
    )
    if docx_generation.get("review_docx", {}).get("path"):
        lines.append(f"- 主审查文档：`{docx_generation['review_docx']['path']}`")
    if docx_generation.get("result_review_docx", {}).get("path"):
        lines.append(f"- 结果审查文档：`{docx_generation['result_review_docx']['path']}`")

    lines.extend(
        [
            "",
            "## 人工运行建议",
            "",
            "```bash",
            "cd repro_project",
            "python -m pip install -r requirements.txt",
            "python run_experiment.py config_smoke.json",
            "```",
            "",
            "运行后重点检查 `outputs/*.csv`、`outputs/*.png` 和 `outputs/summary*.json`，再与论文图表和结论比对。",
            "",
        ]
    )
    return "\n".join(lines)


def _format_runtime_status(runtime_result: dict[str, Any]) -> str:
    if not runtime_result.get("enabled"):
        return "未运行（默认关闭；需要显式传 --run-repro 才会启动受限运行器）"
    warning_count = _runtime_requirement_warning_count(runtime_result)
    if runtime_result.get("passed"):
        warning_note = f"，有 {warning_count} 条依赖告警" if warning_count else ""
        return f"通过{warning_note}，任务 full 覆盖 {runtime_result.get('coverage', '未记录')}"
    return f"未全部通过，任务 full 覆盖 {runtime_result.get('coverage', '未记录')}"


def _runtime_requirement_warning_count(runtime_result: dict[str, Any]) -> int:
    warnings = runtime_result.get("requirements_warnings")
    return len(warnings) if isinstance(warnings, list) else 0


def _format_result_review_status(result_review_result: dict[str, Any]) -> str:
    if not result_review_result.get("enabled"):
        return f"未运行（{result_review_result.get('reason', 'unknown reason')}）"
    if result_review_result.get("passed"):
        return "通过，已生成 result_review.md"
    return f"失败（{result_review_result.get('error', 'unknown error')}）"


def _format_docx_status(status: Any) -> str:
    if not isinstance(status, dict):
        return "未记录"
    if status.get("passed") is True:
        return "已生成"
    if status.get("passed") is False:
        return f"失败（{status.get('error', 'unknown error')}）"
    return f"未生成（{status.get('reason', '未触发')}）"
