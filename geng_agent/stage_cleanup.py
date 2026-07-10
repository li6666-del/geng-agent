from __future__ import annotations

import shutil
from pathlib import Path

"""Resume/cleanup: remove a stage's downstream outputs and audit files so a re-run from
that stage starts clean, plus atomic clearing of generated project code."""

from .pipeline_helpers import _remove_path_inside


def _clear_stage_outputs(output_dir: Path, stage: str, *, preserve_audit: bool = False) -> None:
    stage_outputs = {
        "paper": [
            "paper_chunks.json",
            "paper_memory.json",
            "memory_manifest.json",
            "engineering_facts.json",
            "fact_conflicts.json",
            "task_conflicts.json",
            "paper_thesis.json",
            "repro_tasks.json",
            "experiment_index.json",
            "repro_project_manifest.json",
            "repro_project",
            "runtime_result.json",
            "risk_report.json",
            "generated_files.json",
            "review.md",
            "review.docx",
            "result_review.json",
            "result_review.md",
            "result_review.docx",
            "result_review_error.json",
            "docx_generation_error.json",
            "failure_memory.jsonl",
            "revision_requests.json",
            "automation_provenance.json",
        ],
        "facts": [
            "memory_manifest.json",
            "engineering_facts.json",
            "fact_conflicts.json",
            "task_conflicts.json",
            "paper_thesis.json",
            "repro_tasks.json",
            "experiment_index.json",
            "repro_project_manifest.json",
            "repro_project",
            "runtime_result.json",
            "risk_report.json",
            "generated_files.json",
            "review.md",
            "review.docx",
            "result_review.json",
            "result_review.md",
            "result_review.docx",
            "result_review_error.json",
            "docx_generation_error.json",
            "failure_memory.jsonl",
            "revision_requests.json",
            "automation_provenance.json",
        ],
        "paper_thesis": [
            "memory_manifest.json",
            "paper_thesis.json",
            "repro_project_manifest.json",
            "repro_project",
            "runtime_result.json",
            "risk_report.json",
            "generated_files.json",
            "review.md",
            "review.docx",
            "result_review.json",
            "result_review.md",
            "result_review.docx",
            "result_review_error.json",
            "docx_generation_error.json",
            "automation_provenance.json",
        ],
        "tasks": [
            "memory_manifest.json",
            "task_conflicts.json",
            "repro_tasks.json",
            "experiment_index.json",
            "repro_project_manifest.json",
            "repro_project",
            "runtime_result.json",
            "risk_report.json",
            "generated_files.json",
            "review.md",
            "review.docx",
            "result_review.json",
            "result_review.md",
            "result_review.docx",
            "result_review_error.json",
            "docx_generation_error.json",
            "failure_memory.jsonl",
            "revision_requests.json",
            "automation_provenance.json",
        ],
        "experiment_index": [
            "experiment_index.json",
            "repro_project_manifest.json",
            "repro_project",
            "runtime_result.json",
            "risk_report.json",
            "generated_files.json",
            "review.md",
            "review.docx",
            "result_review.json",
            "result_review.md",
            "result_review.docx",
            "result_review_error.json",
            "docx_generation_error.json",
            "revision_requests.json",
            "automation_provenance.json",
        ],
        "manifest": [
            "repro_project_manifest.json",
            "repro_project",
            "runtime_result.json",
            "risk_report.json",
            "generated_files.json",
            "review.md",
            "review.docx",
            "result_review.json",
            "result_review.md",
            "result_review.docx",
            "result_review_error.json",
            "docx_generation_error.json",
            "revision_requests.json",
            "automation_provenance.json",
        ],
        "project": [
            "repro_project",
            "runtime_result.json",
            "risk_report.json",
            "generated_files.json",
            "review.md",
            "review.docx",
            "result_review.json",
            "result_review.md",
            "result_review.docx",
            "result_review_error.json",
            "docx_generation_error.json",
            "automation_provenance.json",
        ],
        "runtime": [
            "runtime_result.json",
            "risk_report.json",
            "generated_files.json",
            "review.md",
            "review.docx",
            "result_review.json",
            "result_review.md",
            "result_review.docx",
            "result_review_error.json",
            "docx_generation_error.json",
            "repro_project/outputs",
            "repro_project/repair_logs",
            "automation_provenance.json",
        ],
        "result_review": [
            "result_review.json",
            "result_review.md",
            "result_review.docx",
            "result_review_error.json",
        ],
        "reports": [
            "risk_report.json",
            "generated_files.json",
            "review.md",
            "review.docx",
            "result_review.docx",
            "docx_generation_error.json",
            "automation_provenance.json",
        ],
    }
    for rel_path in stage_outputs.get(stage, []):
        _remove_path_inside(output_dir, output_dir / rel_path)
    if not preserve_audit:
        _clear_stage_audit(output_dir, stage)


def _clear_stage_audit(output_dir: Path, stage: str) -> None:
    audit_dir = output_dir / "audit"
    if not audit_dir.exists():
        return
    stage_numbers = {
        "paper": ["01", "02", "03", "04"],
        "facts": ["01", "02", "03", "04"],
        "paper_thesis": ["01c", "03"],
        "tasks": ["02", "03", "04"],
        "experiment_index": ["02", "03", "04"],
        "manifest": ["03", "04"],
        "project": ["04"],
        "result_review": ["04"],
    }.get(stage, [])
    for number in stage_numbers:
        patterns = [
            f"{number}*",
            f"raw_{number}*",
            f"validation_{number}*",
            f"llm_error_{number}*",
            f"local_{number}*",
            f"local_fallback_{number}*",
            f"partial_{number}*",
            f"resume_*_{number}*",
        ]
        for pattern in patterns:
            for path in audit_dir.glob(pattern):
                _remove_path_inside(output_dir, path)


def _clear_project_code_files(repro_project_dir: Path) -> None:
    """Remove stale generated code/config from the repro project before a fresh manifest is
    written, preserving only the scratch dirs (outputs/, repair_logs/). This prevents orphan
    files from an earlier task-writer run from silently becoming the code that executes."""
    if not repro_project_dir.exists():
        return
    preserve = {"outputs", "repair_logs"}
    for child in repro_project_dir.iterdir():
        if child.name in preserve:
            continue
        try:
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink()
        except OSError:
            pass
