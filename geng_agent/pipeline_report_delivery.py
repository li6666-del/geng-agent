from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .outputs import write_json
from .progress import PipelineCancelled


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


def report_editor_exception_result(exc: Exception) -> dict[str, Any]:
    """Record an editor failure without authoring substitute reports."""

    reason = f"{type(exc).__name__}: {exc}"
    return {
        "ok": False,
        "retryable": False,
        "mode": "isolated_report_editor",
        "cached": False,
        "completion_mode": "hard_failure",
        "degraded_report_generation": False,
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


def _report_editor_handoff_summary(result: dict[str, Any]) -> dict[str, Any]:
    """Retain delivery/error data while ignoring the host's cache-hit flag."""
    return {key: value for key, value in result.items() if key != "cached"}


class ReportOperationError(RuntimeError):
    """A recoverable delivery result, including the original owner diagnostics."""

    def __init__(self, result: dict[str, Any]):
        self.result = result
        reason = (result.get("result_review_result") or {}).get("reason")
        super().__init__(str(reason or result.get("error") or "report delivery incomplete"))


def run_supervised_report_editor(
    runner: Callable[..., dict[str, Any]], *, arguments: dict[str, Any],
) -> tuple[dict[str, Any], int]:
    """One supervisor-owned repair budget replaces the legacy second attempt."""
    from .supervisor import REPLAY_REQUIRED, StageBlocked, current_supervisor, supervised_call

    latest: dict[str, Any] = {}
    attempt_no = 1
    invocation_count = 0
    repair_context: dict[str, Any] | None = None
    applied_instruction: dict[str, Any] | None = None
    reconcile_cache = False

    def run() -> dict[str, Any]:
        nonlocal latest, invocation_count, reconcile_cache
        supervisor = current_supervisor()
        instruction = supervisor.current_instruction("report_editor") if supervisor is not None else None
        if instruction and instruction.get("action") == "retry" and instruction != applied_instruction:
            repair(instruction)
            reconcile_cache = True
        try:
            resume = reconcile_cache or (arguments["resume"] if attempt_no == 1 else False)
            reconcile_cache = False
            latest = runner(**{**arguments, "resume": resume,
                               "attempt_no": attempt_no, "repair_context": repair_context})
        except PipelineCancelled:
            raise
        except Exception as exc:
            latest = report_editor_exception_result(exc)
            raise
        status = latest.get("codex_status")
        invocation_count += int(not latest.get("cached") and isinstance(status, dict)
                                and status.get("role") == "report_editor")
        if not latest.get("ok"):
            raise ReportOperationError(latest)
        return latest

    def repair(decision: dict[str, Any]) -> None:
        nonlocal attempt_no, repair_context, latest, applied_instruction
        if not latest:
            from .agentic_report_editor import _read_json_object
            latest = _read_json_object(arguments["audit_dir"] / "04b_report_editor_status.json")
        attempt_no = max(attempt_no, int(latest.get("attempt_no") or 1)) + 1
        repair_context = {**latest, "supervisor_guidance": decision}
        applied_instruction = decision

    def reconcile(_state: dict) -> Any:
        nonlocal reconcile_cache
        reconcile_cache = True
        return REPLAY_REQUIRED

    try:
        result = supervised_call(
            "report_editor", run,
            inputs={"owner": "report_editor", "task_verifications": arguments["task_verifications"],
                    "delivery_status": arguments["runtime_result"].get("delivery_status"),
                    "engineering_failures": arguments["runtime_result"].get("engineering_failures", [])},
            evidence_roots={**{name.replace(".", "_"): arguments["output_dir"] / name for name in
                              ("review.md", "result_review.md", "reproduction_report.md", "verification_result.json")},
                            "report_assets": arguments["output_dir"] / "report_assets",
                            "editor_status": arguments["audit_dir"] / "04b_report_editor_status.json"},
            summarize=_report_editor_handoff_summary, repair=repair,
            reconcile=reconcile, passthrough=(PipelineCancelled,),
        )
    except PipelineCancelled:
        raise
    except Exception as exc:
        # Report failure cannot erase completed task evidence. No Python-authored
        # replacement report is created; the owner drafts remain available.
        result = latest if latest and not latest.get("ok") else report_editor_exception_result(exc)
        result = {**result, "supervisor_blocked": True,
                  "supervisor_decision": getattr(exc, "decision", {})}
        write_json(arguments["output_dir"] / "report_editor_error.json", result)
    return result, invocation_count


def generate_docx_reports(
    *,
    output_dir: Path,
    result_review_result: dict[str, Any],
) -> dict[str, Any]:
    """Convert the two reports and optional navigation without writing prose."""

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
    except PipelineCancelled:
        raise
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
                "passed": None if stem == "review" else False,
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
        except PipelineCancelled:
            raise
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
