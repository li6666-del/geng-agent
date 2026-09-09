"""Structural delivery status for the existing report editor; no report prose."""
from __future__ import annotations
from pathlib import Path
from typing import Any
from .outputs import write_json
from .report_editor_workspace import REPORT_MARKDOWN_FILES
from .security import redact_text

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
    normalization_actions: list[str],
    process_warning: str | None,
) -> str:
    if not ok:
        return "hard_failure"
    if process_warning:
        return "passed_with_process_warning"
    if attempt_no > 1:
        return "passed_after_targeted_repair"
    if normalization_actions:
        return "passed_after_normalization"
    return "passed"


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
