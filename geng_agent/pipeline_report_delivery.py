from __future__ import annotations

from pathlib import Path
from typing import Any

from .review_markdown import _docx_error, _write_docx_error


def report_editor_exception_result(exc: Exception) -> dict[str, Any]:
    """Translate an unexpected editor failure into a reportable fallback result."""

    reason = f"{type(exc).__name__}: {exc}"
    return {
        "ok": False,
        "retryable": False,
        "mode": "isolated_report_editor",
        "cached": False,
        "completion_mode": "exception_fallback",
        "degraded_report_generation": True,
        "codex_status": {
            "ok": False,
            "error_kind": "report_editor_exception",
            "error": reason,
        },
        "result_review_result": {
            "enabled": True,
            "passed": False,
            "reason": reason,
        },
    }


def generate_docx_reports(
    *,
    output_dir: Path,
    result_review_result: dict[str, Any],
) -> dict[str, Any]:
    """Render the three final Markdown reports to DOCX when delivery is usable."""

    errors: list[dict[str, str]] = []
    specs = (
        (
            "review",
            "耿同学agent 论文工程复现审查报告",
            "通信论文工程复现的总体结论、风险与证据摘要",
        ),
        (
            "reproduction_report",
            "本地复现报告",
            "各复现任务实际采用的参数、假设、配置与运行产物",
        ),
        (
            "result_review",
            "论文复现结果对比报告",
            "本地复现结果与论文原图的逐任务证据对比",
        ),
    )
    result: dict[str, Any] = {
        f"{stem}_docx": {
            "passed": None,
            "path": None,
            "reason": "Codex reporter did not complete",
        }
        for stem, _, _ in specs
    }

    try:
        from .docx_writer import write_markdown_report_docx
    except Exception as exc:
        error = _docx_error("import_docx_writer", exc)
        errors.append(error)
        for key in result:
            result[key] = {
                "passed": False,
                "path": None,
                "error": error["error"],
            }
        _write_docx_error(output_dir, errors)
        return result

    if not result_review_result.get("passed"):
        reason = str(
            result_review_result.get("reason") or "Codex reporter did not complete"
        )
        for key in result:
            result[key]["reason"] = reason
        return result

    for stem, title, subtitle in specs:
        key = f"{stem}_docx"
        markdown_path = output_dir / f"{stem}.md"
        docx_path = output_dir / f"{stem}.docx"
        if not markdown_path.exists():
            result[key] = {
                "passed": False,
                "path": None,
                "reason": f"{markdown_path.name} was not generated",
            }
            continue
        try:
            generated = write_markdown_report_docx(
                docx_path,
                markdown_text=markdown_path.read_text(
                    encoding="utf-8", errors="replace"
                ),
                title=title,
                subtitle=subtitle,
                base_dir=output_dir,
            )
            result[key] = {"passed": True, "path": str(generated)}
        except Exception as exc:
            error = _docx_error(docx_path.name, exc)
            errors.append(error)
            result[key] = {
                "passed": False,
                "path": None,
                "error": error["error"],
            }

    if errors:
        _write_docx_error(output_dir, errors)
    else:
        error_path = output_dir / "docx_generation_error.json"
        if error_path.exists():
            error_path.unlink()
    return result
