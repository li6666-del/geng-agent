from __future__ import annotations

import shutil
from pathlib import Path

"""Resume/cleanup: remove a stage's downstream outputs and audit files so a re-run from
that stage starts clean, plus atomic clearing of generated project code."""

from .pipeline_helpers import _remove_path_inside


def _clear_stage_outputs(
    output_dir: Path,
    stage: str,
    *,
    preserve_audit: bool = False,
    preserve_paths: set[str] | None = None,
) -> None:
    """Remove invalid downstream products for ``stage``.

    ``preserve_paths`` lets the normal pipeline commit a replacement stage
    before invalidating its downstream products. An old usable case therefore
    remains intact when replacement generation fails.

    Explicit operator restarts may omit ``preserve_paths`` to retain the
    original destructive reset semantics.
    """
    preserved = {
        Path(path).as_posix().lstrip("./")
        for path in (preserve_paths or set())
        if isinstance(path, str) and path.strip()
    }
    stage_outputs = {
        "paper": [
            "paper_chunks.json",
            "paper_figure_index.json",
            "engineering_facts_initial.json",
            "engineering_facts_backfill.json",
            "engineering_facts.json",
            "fact_conflicts.json",
            "task_conflicts.json",
            "paper_thesis.json",
            "repro_tasks_preliminary.json",
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
            "automation_provenance.json",
        ],
        "facts": [
            "engineering_facts_initial.json",
            "engineering_facts_backfill.json",
            "engineering_facts.json",
            "fact_conflicts.json",
            "task_conflicts.json",
            "paper_thesis.json",
            "repro_tasks_preliminary.json",
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
            "automation_provenance.json",
        ],
        "paper_thesis": [
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
            "engineering_facts_backfill.json",
            "engineering_facts.json",
            "fact_conflicts.json",
            "task_conflicts.json",
            "paper_thesis.json",
            "repro_tasks_preliminary.json",
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
    v2_downstream = [
        "scientific_architecture.json",
        "foundation_manifest.json",
        "repro_project_manifest.json",
        "repro_project",
        "runtime_result.json",
        "risk_report.json",
        "generated_files.json",
        "automation_provenance.json",
    ]
    for upstream in ("paper", "facts", "paper_thesis", "tasks", "experiment_index"):
        stage_outputs[upstream].extend(v2_downstream)
    stage_outputs["scientific_architecture"] = [
        "scientific_architecture.json",
        "foundation_manifest.json",
        "repro_project_manifest.json",
        "repro_project",
        "runtime_result.json",
        "risk_report.json",
        "generated_files.json",
        "automation_provenance.json",
    ]
    report_outputs = [
        "review.md",
        "review.docx",
        "reproduction_report.md",
        "reproduction_report.docx",
        "result_review.md",
        "result_review.docx",
        "report_assets",
        "verification_result.json",
        "report_editor_error.json",
        "docx_generation_error.json",
    ]
    outputs = list(stage_outputs.get(stage, []))
    if stage in stage_outputs:
        outputs.extend(report_outputs)
    for rel_path in dict.fromkeys(outputs):
        if Path(rel_path).as_posix().lstrip("./") in preserved:
            continue
        _remove_path_inside(output_dir, output_dir / rel_path)
    if not preserve_audit:
        _clear_stage_audit(output_dir, stage)


def _clear_stage_audit(output_dir: Path, stage: str) -> None:
    audit_dir = output_dir / "audit"
    if not audit_dir.exists():
        return
    stage_numbers = {
        "paper": ["00", "01", "02", "03", "04"],
        "facts": ["01", "02", "03", "04"],
        "paper_thesis": ["02d", "03"],
        "tasks": ["02", "03", "04"],
        "experiment_index": ["02", "03", "04"],
        "scientific_architecture": ["02f", "03", "04"],
        # Rebuilding task-writer outputs must preserve the already validated
        # Foundation snapshot from stage 03b. A broad ``03*`` cleanup deletes
        # the canonical snapshot before stage 03c can install it into task
        # sandboxes, making every fresh v2 run fail at the hand-off boundary.
        "manifest": ["03c", "04"],
        "project": ["04"],
        "result_review": ["04"],
        "reports": ["04"],
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
