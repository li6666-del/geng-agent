"""Deterministic Report Editor fallbacks and terminal packaging status helpers."""

from __future__ import annotations

from pathlib import Path
import shutil
from typing import Any

from .outputs import write_json, write_text
from .report_editor_workspace import (
    REPORT_MARKDOWN_FILES,
    REPORT_MARKDOWN_MAX_BYTES,
    _inspect_report_editor_outputs,
    _recover_unsafe_report_outputs,
    _report_outputs_fingerprint,
)
from .security import redact_text


def _write_fallback_reports(
    *,
    workspace: Path,
    missing: list[str],
    paper: dict[str, Any],
    task_packets: list[dict[str, Any]],
    risk_report: dict[str, Any],
) -> list[str]:
    reports = {
        "review.md": _render_fallback_review(paper=paper, task_packets=task_packets, risk_report=risk_report),
        "reproduction_report.md": _render_fallback_reproduction(task_packets),
        "result_review.md": _render_fallback_result_review(task_packets),
    }
    written: list[str] = []
    for name in missing:
        if name not in reports:
            continue
        write_text(workspace / name, reports[name])
        written.append(name)
    from .report_facts import publish_terminal_facts
    publish_terminal_facts(workspace, task_packets)
    return written


def _render_fallback_review(
    *,
    paper: dict[str, Any],
    task_packets: list[dict[str, Any]],
    risk_report: dict[str, Any],
) -> str:
    title = _compact_value(paper.get("title")) or "未命名论文"
    lines = [
        "# 论文工程复现审查报告",
        "",
        f"论文：{title}",
        "",
        "本报告由确定性降级模板根据已进入终态的任务包生成，不增加或改变科学结论。",
        "",
        "| 任务 | 复现目标 | 终态 | 结论 |",
        "|---|---|---|---|",
    ]
    for index, packet in enumerate(task_packets, start=1):
        verification = packet.get("verification") if isinstance(packet.get("verification"), dict) else {}
        lines.append(
            "| "
            + " | ".join(
                (
                    _markdown_cell(f"{index}. {packet.get('task_id') or 'task'}"),
                    _markdown_cell(_task_target(packet)),
                    _markdown_cell(_packet_outcome_label(packet)),
                    _markdown_cell(_compact_value(verification.get("comparison_summary")) or "见任务级终态记录"),
                )
            )
            + " |"
        )
    verdict = _compact_value(risk_report.get("reproducibility_verdict"))
    lines.extend(
        [
            "",
            "## 总体结论",
            "",
            verdict or f"共 {len(task_packets)} 个任务已形成可报告终态，详细参数、结构化证据和可用图像见另外两份报告。",
            "",
            "- [本地复现报告](reproduction_report.md)",
            "- [论文复现结果对比报告](result_review.md)",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_fallback_reproduction(task_packets: list[dict[str, Any]]) -> str:
    lines = ["# 本地复现报告", "", "以下内容直接整理自终态任务包，不推导新的参数或结论。"]
    for index, packet in enumerate(task_packets, start=1):
        lines.extend(
            [
                "",
                f"## {index}. {_task_target(packet)}",
                "",
                f"- 任务标识：`{packet.get('task_id') or 'task'}`",
                f"- 终态：{_packet_outcome_label(packet)}",
                f"- Writer 摘要：{_compact_value(packet.get('writer_summary')) or '未提供'}",
                f"- 执行信息：{_compact_value(packet.get('execution_summary')) or '未提供'}",
                "- 参数来源与处理：",
            ]
        )
        values = packet.get("parameter_resolution") if isinstance(packet.get("parameter_resolution"), list) else []
        if values:
            lines.extend(f"  - {_compact_value(item)}" for item in values[:24])
        else:
            lines.append("  - 未提供额外参数解析记录。")
        uncertainties = packet.get("remaining_uncertainties")
        lines.extend(["- 剩余不确定性：", *_markdown_bullets(uncertainties)])
    return "\n".join(lines) + "\n"


def _render_fallback_result_review(task_packets: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for index, packet in enumerate(task_packets, start=1):
        verification = packet.get("verification") if isinstance(packet.get("verification"), dict) else {}
        local_assets = packet.get("local_assets") if isinstance(packet.get("local_assets"), list) else []
        paper_assets = packet.get("paper_assets") if isinstance(packet.get("paper_assets"), list) else []
        lines.extend([f"## {index}. {_task_target(packet)}", ""])
        local = str(local_assets[0]) if local_assets else ""
        paper_asset = str(paper_assets[0]) if paper_assets else ""
        if local and paper_asset:
            lines.extend(
                [
                    "| 本地复现图 | 论文原图 |",
                    "|---|---|",
                    f"| ![本地复现图]({local}) | ![论文原图]({paper_asset}) |",
                    "",
                ]
            )
        elif local or paper_asset:
            label = "本地复现图" if local else "论文原图"
            lines.extend([f"![{label}]({local or paper_asset})", ""])
        else:
            structured = packet.get("structured_evidence") if isinstance(packet.get("structured_evidence"), dict) else {}
            evidence_files = structured.get("evidence_files") if isinstance(structured.get("evidence_files"), list) else []
            lines.extend(
                [
                    "**证据形式：** 本任务未提供可用的成对图像，以下结论来自结构化结果、表格、CSV、summary 或文本证据。",
                    "",
                    "**结构化证据文件：**",
                    *_markdown_bullets(evidence_files),
                    "",
                ]
            )
        lines.extend(
            [
                f"**终态：** {_packet_outcome_label(packet)}",
                "",
                f"**结论：** {_compact_value(verification.get('comparison_summary')) or '见任务级终态记录。'}",
                "",
                "**已记录差异：**",
                *_markdown_bullets(verification.get("non_material_differences") or verification.get("differences")),
                "",
                "**剩余不确定性：**",
                *_markdown_bullets(packet.get("remaining_uncertainties")),
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _packet_outcome_label(packet: dict[str, Any]) -> str:
    outcome = str(packet.get("terminal_outcome") or "").strip()
    labels = {
        "reproduced": "已复现",
        "reproduced_with_assumptions": "带公开假设复现",
        "inconclusive_missing_information": "论文信息不足，结论不确定",
        "not_reproduced": "未复现",
        "execution_failed": "执行失败",
    }
    return labels.get(outcome, outcome or "已形成终态")


def _task_target(packet: dict[str, Any]) -> str:
    task = packet.get("task") if isinstance(packet.get("task"), dict) else {}
    return _compact_value(task.get("figure_or_claim") or task.get("title") or packet.get("task_id")) or "复现任务"


def _compact_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (str, int, float)):
        return str(value).strip()
    if isinstance(value, list):
        return "；".join(filter(None, (_compact_value(item) for item in value[:16])))
    if isinstance(value, dict):
        return "；".join(
            f"{key}={text}"
            for key, item in list(value.items())[:16]
            if (text := _compact_value(item))
        )
    return str(value).strip()


def _markdown_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def _markdown_bullets(values: Any) -> list[str]:
    items = values if isinstance(values, list) else ([values] if values else [])
    rendered = [f"- {_compact_value(item)}" for item in items if _compact_value(item)]
    return rendered or ["- 无。"]


def _codex_process_warning(codex_status: dict[str, Any]) -> str:
    return str(
        codex_status.get("blocked_reason")
        or codex_status.get("error")
        or codex_status.get("error_kind")
        or "Codex process did not report success, but complete report files were recovered."
    )


def _completion_mode(
    *,
    ok: bool,
    attempt_no: int,
    fallback_files: list[str],
    normalization_actions: list[str],
    process_warning: str | None,
) -> str:
    if not ok:
        return "hard_failure"
    if fallback_files:
        return "degraded_fallback"
    if process_warning:
        return "passed_with_process_warning"
    if attempt_no > 1:
        return "passed_after_targeted_repair"
    if normalization_actions:
        return "passed_after_normalization"
    return "passed"

def _editor_failure_with_fallback(
    *,
    status_path: Path,
    workspace: Path,
    output_dir: Path,
    input_hash: str,
    paper: dict[str, Any],
    task_packets: list[dict[str, Any]],
    risk_report: dict[str, Any],
    attempt_no: int,
    error: Exception,
    error_kind: str,
    report_markdown_max_bytes: int = REPORT_MARKDOWN_MAX_BYTES,
) -> dict[str, Any]:
    """Keep report packaging failures from changing the scientific terminal state."""

    message = redact_text(f"{type(error).__name__}: {error}")[:1500]
    try:
        workspace.mkdir(parents=True, exist_ok=True)
        recovered, actions, recovery_failures = _recover_unsafe_report_outputs(
            workspace,
            max_bytes=report_markdown_max_bytes,
        )
        if recovery_failures:
            raise OSError("; ".join(recovery_failures))
        fallback_files = _write_fallback_reports(
            workspace=workspace,
            missing=list(REPORT_MARKDOWN_FILES),
            paper=paper,
            task_packets=task_packets,
            risk_report=risk_report,
        )
        inspection = _inspect_report_editor_outputs(
            workspace,
            max_bytes=report_markdown_max_bytes,
        )
        if inspection["missing"] or inspection["hard_issues"]:
            raise OSError("deterministic report fallback remained incomplete")
        output_dir.mkdir(parents=True, exist_ok=True)
        copied: list[str] = []
        for name in REPORT_MARKDOWN_FILES:
            target = output_dir / name
            shutil.copy2(workspace / name, target)
            copied.append(str(target))
        fingerprint = _report_outputs_fingerprint(
            output_dir,
            max_bytes=report_markdown_max_bytes,
        )
        if fingerprint is None:
            raise OSError("deterministic report fallback could not be fingerprinted")
    except Exception as fallback_error:
        return _editor_failure(
            status_path=status_path,
            workspace=workspace,
            input_hash=input_hash,
            error=fallback_error,
            error_kind=error_kind,
        )

    status = _editor_failure(
        status_path=status_path,
        workspace=workspace,
        input_hash=input_hash,
        error=error,
        error_kind=error_kind,
    )
    status.update(
        {
            "ok": True,
            "attempt_no": attempt_no,
            "invocation_count": attempt_no,
            "missing_outputs": [],
            "hard_issues": [],
            "recovered_packaging_issues": [message, *recovered],
            "normalization_actions": actions,
            "fallback_files": fallback_files,
            "degraded_report_generation": True,
            "completion_mode": "degraded_fallback",
            "process_warning": message,
            "retryable": False,
            "output_fingerprint": fingerprint,
            "files": copied,
            "result_review_result": {
                "enabled": True,
                "passed": True,
                "mode": "deterministic_report_fallback",
                "reason": "report-editor preparation failed; terminal science was preserved",
            },
        }
    )
    write_json(status_path, status)
    return status


def _editor_failure(*, status_path: Path, workspace: Path, input_hash: str, error: Exception, error_kind: str) -> dict[str, Any]:
    message = redact_text(f"{type(error).__name__}: {error}")[:1500]
    status = {
        "ok": False,
        "backend": "codex",
        "mode": "final_report_editor",
        "input_hash": input_hash,
        "cached": False,
        "workspace": str(workspace),
        "codex_status": {"ok": False, "error_kind": error_kind, "error": message},
        "missing_outputs": list(REPORT_MARKDOWN_FILES),
        "coverage_issues": [],
        "hard_issues": [],
        "validation_level": "structural_only",
        "normalization_actions": [],
        "repair_targets": [],
        "preserved_files": [],
        "restored_files": [],
        "fallback_files": [],
        "degraded_report_generation": False,
        "completion_mode": "hard_failure",
        "process_warning": None,
        "retryable": error_kind != "preparation_failed",
        "copy_error": None,
        "output_fingerprint": None,
        "files": [],
        "result_review_result": {"enabled": True, "passed": False, "mode": "codex_report_editor", "reason": message},
    }
    write_json(status_path, status)
    return status


def _editor_reason(codex_status: dict[str, Any], missing: list[str], hard_issues: list[str], copy_error: str | None) -> str:
    if hard_issues:
        return "report editor output failed structural safety checks: " + "; ".join(hard_issues[:8])
    if missing:
        return "report editor did not create required reports: " + ", ".join(missing)
    if copy_error:
        return copy_error
    if not codex_status.get("ok"):
        return str(codex_status.get("blocked_reason") or codex_status.get("error") or "report editor failed")
    return "report editor delivery was incomplete"
