"""Small verifiable delivery tools; no generated code or scientific verdict edits."""
from __future__ import annotations

import hashlib
from pathlib import Path

from .outputs import write_json
from .supervisor import REPLAY_REQUIRED, NodeFailure


def write_delivery_index(output_dir: Path, outcome: dict) -> dict:
    assets = []
    for name in ("result_review.md", "reproduction_report.md", "result_review.docx",
                 "reproduction_report.docx", "runtime_result.json", "verification_result.json"):
        if name.endswith((".md", ".docx")) and not outcome.get("reports_accepted"):
            continue
        if name.endswith(".docx") and name not in outcome.get("accepted_docx_paths", []):
            continue
        if name == "runtime_result.json" and not outcome.get("runtime_recorded"):
            continue
        if name == "verification_result.json" and "reports" not in outcome.get("completed_phases", []):
            continue
        path = output_dir / name
        if path.is_file() and not path.is_symlink():
            assets.append({"path": name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                           "bytes": path.stat().st_size})
    index = {**outcome, "files": assets,
             "project_directory": "repro_project" if outcome.get("project_accepted") and (output_dir / "repro_project").is_dir() else None,
             "note": "文件清单仅说明当前可用文件；科研结论以独立核验为准。"}
    write_json(output_dir / "delivery_index.json", index)
    return index


def register_delivery_repairs(supervisor, context, task_records: list[dict]) -> None:
    def restore_assets(arguments: dict) -> dict:
        if arguments:
            raise ValueError("restore_report_assets takes no arguments and uses only this run's records")
        from .report_editor_assets import restore_report_assets
        warnings = restore_report_assets(task_records=task_records, output_dir=context.output_dir,
                                         audit_dir=context.audit_dir)
        if warnings:
            raise NodeFailure("部分报告图片缺少可核验来源，不能补造。", {"warnings": warnings})
        return {"verified": True, "task_ids": [r.get("task_id") for r in task_records]}

    supervisor.register_repair_handler("restore_report_assets", restore_assets,
        reconcile=lambda arguments, record: REPLAY_REQUIRED,
        description="从本次 Reporter 接受的图片清单恢复缺失报告图片；逐一核对源文件和目标文件哈希。无参数，不能画新图。",
        parameters={"type": "object", "properties": {}, "additionalProperties": False})
