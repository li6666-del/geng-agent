from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .outputs import write_json
from .progress import PipelineCancelled
from .report_editor_word import inspect_word_file


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


def inspect_editor_word_reports(
    *,
    output_dir: Path,
    result_review_result: dict[str, Any],
) -> dict[str, Any]:
    """Inspect Editor-authored Word files; never generate or rewrite them."""

    specs = ("review", "reproduction_report", "result_review")
    result: dict[str, Any] = {}
    if not result_review_result.get("passed"):
        reason = str(result_review_result.get("reason") or "Report Editor did not complete")
        return {f"{stem}_docx": {"passed": None, "path": None, "reason": reason}
                for stem in specs}

    errors: list[dict[str, str]] = []
    for stem in specs:
        name = f"{stem}.docx"
        path = output_dir / name
        key = f"{stem}_docx"
        if stem == "review" and not path.exists():
            result[key] = {"passed": None, "path": None, "reason": "optional navigation not generated"}
            continue
        issue = inspect_word_file(path)
        if issue:
            error = {"stage": name, "error": issue}
            errors.append(error)
            result[key] = {"passed": False, "path": None, "error": issue}
        else:
            result[key] = {"passed": True, "path": str(path), "authored_by": "report_editor"}

    if errors:
        _write_docx_error(output_dir, errors)
    else:
        (output_dir / "docx_generation_error.json").unlink(missing_ok=True)
    return result
