from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .foundation_snapshot import path_is_foundation_link, validate_foundation_snapshot
from .security import _runtime_lock_is_trusted


STAGES = [
    ("paper", "paper_chunks.json", None),
    ("engineering_facts", "engineering_facts.json", "engineering_facts"),
    ("paper_thesis", "paper_thesis.json", "paper_thesis"),
    ("repro_tasks", "repro_tasks.json", "repro_tasks"),
    ("execution_plan", "execution_plan.json", None),
    ("experiment_index", "experiment_index.json", "experiment_index"),
    ("scientific_architecture", "scientific_architecture.json", "scientific_architecture"),
    ("environment_lock", "03a_environment.lock.json", None),
    ("foundation_manifest", "foundation_manifest.json", None),
    ("repro_project_manifest", "repro_project_manifest.json", "repro_project_manifest"),
    ("repro_project", "repro_project", None),
    ("runtime", "runtime_result.json", None),
    ("verification_result", "verification_result.json", "verification_result"),
    ("reproduction_report", "reproduction_report.md", None),
    ("result_review", "result_review.md", None),
    ("review", "review.md", None),
    ("review_docx", "review.docx", None),
    ("reproduction_report_docx", "reproduction_report.docx", None),
    ("result_review_docx", "result_review.docx", None),
]

# These stages enrich a case or package it for humans, but their absence must
# not make a scientifically usable case look incomplete. The pipeline already
# has explicit fallbacks for the architecture/Foundation pair and report DOCX
# rendering.
OPTIONAL_STAGES = {
    "paper_thesis", "scientific_architecture", "foundation_manifest",
    "review_docx", "reproduction_report_docx", "result_review_docx",
}

CURRENT_WORKFLOW_VERSION = "2"


class UnsupportedCaseWorkflowError(RuntimeError):
    """The case cannot be interpreted by the sole supported workflow."""


RESUME_LABELS = {
    "paper": "01_extract_engineering_facts",
    "engineering_facts": "01_extract_engineering_facts",
    "paper_thesis": "02d_extract_paper_thesis",
    "repro_tasks": "02c_finalize_repro_tasks",
    "execution_plan": "02e_compile_execution_plan",
    "experiment_index": "02e_build_experiment_index",
    "scientific_architecture": "02f_design_scientific_architecture",
    "environment_lock": "03a_environment_resolver",
    "foundation_manifest": "03b_foundation_writer",
    "repro_project_manifest": "03c_task_writer_workflow",
    "repro_project": "03c_task_writer_workflow",
    "runtime": "03c_task_writer_workflow",
    "verification_result": "04a_task_reporters",
    "reproduction_report": "04b_report_editor",
    "result_review": "04b_report_editor",
    "review": "04b_report_editor",
    "review_docx": "render_reports",
    "reproduction_report_docx": "render_reports",
    "result_review_docx": "render_reports",
}


def inspect_case_status(output_dir: Path) -> dict[str, Any]:
    output_dir = output_dir.expanduser().resolve()
    try:
        _validate_v2_workflow(output_dir)
    except UnsupportedCaseWorkflowError as exc:
        try:
            latest_audit = latest_audit_items(output_dir / "audit")
        except OSError:
            latest_audit = []
        try:
            output_exists = output_dir.exists()
        except OSError:
            output_exists = False
        return {
            "output_dir": str(output_dir),
            "exists": output_exists,
            "workflow_version": None,
            "supported": False,
            "error_kind": "unsupported_workflow_version",
            "error": str(exc),
            "next_stage": None,
            "resume_from": "rebuild_case",
            "suggested_command": None,
            "stages": [],
            "latest_audit": latest_audit,
        }

    stage_status = []
    next_stage = None
    for name, rel_path, schema_stage in STAGES:
        status = inspect_stage(output_dir, name, rel_path, schema_stage)
        required = name not in OPTIONAL_STAGES
        status["required"] = required
        if not required and not status["ok"]:
            status["advisory"] = True
        stage_status.append(status)
        if (
            next_stage is None
            and not status["ok"]
            and (required or status.get("reason") != "missing")
        ):
            next_stage = name

    try:
        latest_audit = latest_audit_items(output_dir / "audit")
    except OSError:
        latest_audit = []
    try:
        output_exists = output_dir.exists()
    except OSError:
        output_exists = False
    return {
        "output_dir": str(output_dir),
        "exists": output_exists,
        "workflow_version": CURRENT_WORKFLOW_VERSION,
        "supported": True,
        "delivery_status": next((item.get("delivery_status") for item in stage_status if item["stage"] == "runtime"), None),
        "supervision_path": str(output_dir / "audit" / "supervisor") if (output_dir / "audit" / "supervisor").is_dir() else None,
        "next_stage": next_stage,
        "resume_from": RESUME_LABELS.get(next_stage, "complete" if next_stage is None else next_stage),
        "suggested_command": suggested_review_command(output_dir, next_stage),
        "stages": stage_status,
        "latest_audit": latest_audit,
    }


def _validate_v2_workflow(output_dir: Path) -> None:
    marker = output_dir / "workflow.json"
    if marker.is_file():
        try:
            payload = read_json(marker)
        except Exception as exc:
            raise UnsupportedCaseWorkflowError(
                "workflow.json is unreadable; rebuild in a new clean case directory"
            ) from exc
        value = str(payload.get("workflow_version") or "") if isinstance(payload, dict) else ""
        if value != CURRENT_WORKFLOW_VERSION:
            raise UnsupportedCaseWorkflowError(
                f"unsupported case workflow_version {value or '<missing>'!r}; "
                "rebuild in a new clean case directory"
            )
        return
    if any((output_dir / rel_path).exists() for _, rel_path, _ in STAGES):
        raise UnsupportedCaseWorkflowError(
            "case has pipeline artifacts but no V2 workflow marker; "
            "rebuild in a new clean case directory"
        )


def inspect_stage(output_dir: Path, name: str, rel_path: str, schema_stage: str | None) -> dict[str, Any]:
    path = output_dir / rel_path
    if not path.exists():
        if name in {"review", "reproduction_report", "result_review"}:
            error_path = output_dir / "report_editor_error.json"
            if error_path.exists():
                try:
                    error_data = read_json(error_path)
                    reason = error_data.get("reason") or error_data.get("error") or "result review failed"
                except Exception:
                    reason = "result review failed"
                return {
                    "stage": name,
                    "ok": False,
                    "path": str(path),
                    "reason": str(reason),
                    "error_path": str(error_path),
                }
        return {"stage": name, "ok": False, "path": str(path), "reason": "missing"}

    if name == "paper":
        try:
            data = read_json(path)
            ok = isinstance(data.get("chunks"), list) and bool(data["chunks"])
            return {"stage": name, "ok": ok, "path": str(path), "reason": "valid" if ok else "paper chunks missing"}
        except Exception as exc:
            return {"stage": name, "ok": False, "path": str(path), "reason": f"invalid json: {exc}"}

    if name == "environment_lock":
        try:
            lock = read_json(path)
        except Exception as exc:
            return {"stage": name, "ok": False, "path": str(path), "reason": f"invalid json: {exc}"}
        ok = bool(
            isinstance(lock, dict)
            and _runtime_lock_is_trusted(lock)
            and lock.get("capabilities_ok") is True
            and str(lock.get("environment_hash") or "")
        )
        return {
            "stage": name,
            "ok": ok,
            "path": str(path),
            "reason": "valid" if ok else "environment lock is not ready or trusted",
        }

    if name == "foundation_manifest":
        try:
            if path_is_foundation_link(path):
                raise ValueError("Foundation manifest must not be a link or reparse point")
            manifest = read_json(path)
        except Exception as exc:
            return {"stage": name, "ok": False, "path": str(path), "reason": f"invalid json: {exc}"}
        snapshot_dir = output_dir / "audit" / "03b_foundation_snapshot"
        try:
            issues = validate_foundation_snapshot(manifest, snapshot_dir)
        except OSError as exc:
            issues = [{"path": str(snapshot_dir), "message": f"cannot inspect Foundation snapshot: {exc}"}]
        ok = not issues
        return {
            "stage": name,
            "ok": ok,
            "path": str(path),
            "snapshot_dir": str(snapshot_dir),
            "reason": "valid" if ok else "invalid foundation snapshot",
            "issues": issues[:5],
        }

    if name == "repro_project":
        # Status is observation only. Compiling or importing the project here
        # would mutate its evidence and create a second execution gate.
        ok = path.is_dir()
        return {"stage": name, "ok": ok, "path": str(path),
                "reason": "present" if ok else "not a directory", "validation_repeated": False}

    if name == "runtime":
        try:
            data = read_json(path)
            ok = isinstance(data, dict)
            return {"stage": name, "ok": ok, "path": str(path),
                    "passed": data.get("passed") if ok else None,
                    "delivery_status": data.get("delivery_status") if ok else None,
                    "reason": "recorded" if ok else "runtime record is not an object"}
        except Exception as exc:
            return {"stage": name, "ok": False, "path": str(path), "reason": f"invalid json: {exc}"}

    if name in {"review", "reproduction_report", "result_review"}:
        error_path = output_dir / "report_editor_error.json"
        if error_path.exists() and error_path.stat().st_mtime >= path.stat().st_mtime:
            try:
                error_data = read_json(error_path)
                reason = error_data.get("reason") or error_data.get("error") or "result review failed"
            except Exception:
                reason = "result review failed"
            return {
                "stage": name,
                "ok": False,
                "path": str(path),
                "reason": str(reason),
                "error_path": str(error_path),
            }

    if schema_stage:
        try:
            data = read_json(path)
            ok = isinstance(data, dict)
            return {"stage": name, "ok": ok, "path": str(path),
                    "reason": "recorded" if ok else "stage record is not an object",
                    "validation_repeated": False}
        except Exception as exc:
            return {"stage": name, "ok": False, "path": str(path), "reason": f"invalid json: {exc}"}

    return {"stage": name, "ok": path.is_file(), "path": str(path), "reason": "present" if path.is_file() else "not a file"}


def latest_audit_items(audit_dir: Path, limit: int = 8) -> list[dict[str, Any]]:
    if not audit_dir.exists():
        return []
    items = sorted(
        (
            path
            for path in audit_dir.rglob("*")
            if path.is_file() and (path.suffix.lower() == ".json" or path.name.endswith("_brief.md"))
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return [
        {
            "name": path.relative_to(audit_dir).as_posix(),
            "size": path.stat().st_size,
            "mtime": path.stat().st_mtime,
            "summary": audit_summary(path),
        }
        for path in items[:limit]
    ]


def audit_summary(path: Path) -> str:
    if path.suffix.lower() != ".json":
        return ""
    try:
        data = read_json(path)
    except Exception:
        return ""
    if "errors" in data:
        return json.dumps(data["errors"][:2] if isinstance(data["errors"], list) else data["errors"], ensure_ascii=False)
    if "error" in data:
        return str(data["error"])
    if data.get("ok") is True:
        return "ok"
    return ""


def suggested_review_command(output_dir: Path, next_stage: str | None) -> str:
    if next_stage is None:
        return "complete"

    paper_path = "<paper.pdf>"
    chunks_path = output_dir / "paper_chunks.json"
    if chunks_path.exists():
        try:
            data = read_json(chunks_path)
            source_path = data.get("source_path")
            if isinstance(source_path, str) and source_path:
                paper_path = source_path
        except Exception:
            pass

    command = [
        "python",
        "-m",
        "geng_agent",
        "review",
        quote_for_shell(paper_path),
        "--out",
        quote_for_shell(str(output_dir)),
        "--run-repro",
    ]
    return " ".join(command)


def quote_for_shell(value: str) -> str:
    return '"' + value.replace('"', '\\"') + '"'


def read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("expected JSON object")
    return data
