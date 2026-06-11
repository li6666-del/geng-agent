from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .outputs import validate_repro_project
from .schemas import validate_stage


STAGES = [
    ("paper", "paper_chunks.json", None),
    ("engineering_facts", "engineering_facts.json", "engineering_facts"),
    ("repro_tasks", "repro_tasks.json", "repro_tasks"),
    ("experiment_index", "experiment_index.json", "experiment_index"),
    ("repro_project_manifest", "repro_project_manifest.json", "repro_project_manifest"),
    ("repro_project", "repro_project", None),
    ("runtime", "runtime_result.json", None),
    ("result_review", "result_review.md", None),
    ("review", "review.md", None),
    ("review_docx", "review.docx", None),
    ("result_review_docx", "result_review.docx", None),
]

RESUME_LABELS = {
    "paper": "01_extract_engineering_facts",
    "engineering_facts": "01_extract_engineering_facts",
    "repro_tasks": "02_build_repro_tasks",
    "experiment_index": "02b_build_experiment_index",
    "repro_project_manifest": "03_generate_repro_project",
    "repro_project": "write_repro_project_files",
    "runtime": "run_repro_project",
    "result_review": "04_review_reproduction_results",
    "review": "render_reports",
    "review_docx": "render_reports",
    "result_review_docx": "render_reports",
}


def inspect_case_status(output_dir: Path) -> dict[str, Any]:
    output_dir = output_dir.expanduser().resolve()
    stage_status = []
    next_stage = None
    for name, rel_path, schema_stage in STAGES:
        status = inspect_stage(output_dir, name, rel_path, schema_stage)
        stage_status.append(status)
        if next_stage is None and not status["ok"] and name not in {"result_review", "result_review_docx"}:
            next_stage = name

    runtime = next((item for item in stage_status if item["stage"] == "runtime"), None)
    result_review = next((item for item in stage_status if item["stage"] == "result_review"), None)
    if runtime and runtime["ok"] and result_review and not result_review["ok"]:
        next_stage = "result_review"

    latest_audit = latest_audit_items(output_dir / "audit")
    return {
        "output_dir": str(output_dir),
        "exists": output_dir.exists(),
        "next_stage": next_stage,
        "resume_from": RESUME_LABELS.get(next_stage, "complete" if next_stage is None else next_stage),
        "suggested_command": suggested_review_command(output_dir, next_stage),
        "stages": stage_status,
        "latest_audit": latest_audit,
    }


def inspect_stage(output_dir: Path, name: str, rel_path: str, schema_stage: str | None) -> dict[str, Any]:
    path = output_dir / rel_path
    if not path.exists():
        if name == "result_review":
            error_path = output_dir / "result_review_error.json"
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

    if name == "repro_project":
        validation = validate_repro_project(path)
        ok = bool(validation.get("required_files_present") and validation.get("python_compiles"))
        return {"stage": name, "ok": ok, "path": str(path), "validation": validation, "reason": "valid" if ok else "invalid project"}

    if name == "runtime":
        try:
            data = read_json(path)
            ok = data.get("passed") is True
            return {"stage": name, "ok": ok, "path": str(path), "passed": data.get("passed"), "reason": "passed" if ok else "not passed"}
        except Exception as exc:
            return {"stage": name, "ok": False, "path": str(path), "reason": f"invalid json: {exc}"}

    if name == "result_review":
        error_path = output_dir / "result_review_error.json"
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
            if name == "result_review":
                error_path = output_dir / "result_review_error.json"
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
            required_files = _required_files_for_stage(name, data)
            issues = validate_stage(schema_stage, data, required_files=required_files)
            ok = not issues
            return {
                "stage": name,
                "ok": ok,
                "path": str(path),
                "reason": "valid" if ok else "schema validation failed",
                "issues": [issue.as_dict() for issue in issues[:5]],
            }
        except Exception as exc:
            return {"stage": name, "ok": False, "path": str(path), "reason": f"invalid json: {exc}"}

    return {"stage": name, "ok": path.is_file(), "path": str(path), "reason": "present" if path.is_file() else "not a file"}


def _required_files_for_stage(name: str, data: dict[str, Any]) -> set[str] | None:
    if name != "repro_project_manifest":
        return None
    meta = data.get("_meta") if isinstance(data, dict) else None
    if not isinstance(meta, dict) or not meta.get("agentic_project_used"):
        return None
    generated = meta.get("generated_paths")
    if isinstance(generated, list) and all(isinstance(item, str) for item in generated):
        return set(generated)
    return None


def latest_audit_items(audit_dir: Path, limit: int = 8) -> list[dict[str, Any]]:
    if not audit_dir.exists():
        return []
    items = sorted((path for path in audit_dir.iterdir() if path.is_file()), key=lambda path: path.stat().st_mtime, reverse=True)
    return [
        {
            "name": path.name,
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
