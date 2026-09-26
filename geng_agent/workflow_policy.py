from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .outputs import write_json
from .scientific_materiality import SCIENTIFIC_POLICY_ID


CURRENT_WORKFLOW_VERSION = "2"

_WORKFLOW_STAGE_SENTINELS = (
    "paper_chunks.json",
    "engineering_facts.json",
    "repro_tasks_preliminary.json",
    "repro_tasks.json",
    "paper_thesis.json",
    "experiment_index.json",
    "execution_plan.json",
    "scientific_architecture.json",
    "03a_environment_request.json",
    "03a_environment.lock.json",
    "repro_project_manifest.json",
    "repro_project",
    "runtime_result.json",
    "verification_result.json",
    "reproduction_report.md",
    "review.md",
)


class UnsupportedWorkflowVersionError(RuntimeError):
    """The case belongs to a workflow generation this code no longer runs."""






def _ensure_v2_workflow(output_dir: Path) -> None:
    """Create or validate the sole supported workflow marker."""

    marker_path = output_dir / "workflow.json"
    if marker_path.is_file():
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise UnsupportedWorkflowVersionError(
                "workflow.json is unreadable; rebuild in a new clean case directory"
            ) from exc
        if not isinstance(marker, dict):
            raise UnsupportedWorkflowVersionError(
                "workflow.json is not an object; rebuild in a new clean case directory"
            )
        existing = str(marker.get("workflow_version") or "").strip()
        if existing != CURRENT_WORKFLOW_VERSION:
            raise UnsupportedWorkflowVersionError(
                f"unsupported case workflow_version {existing or '<missing>'!r}; "
                "rebuild in a new clean case directory"
            )
    else:
        stale_paths = [
            name for name in _WORKFLOW_STAGE_SENTINELS if (output_dir / name).exists()
        ]
        if stale_paths:
            raise UnsupportedWorkflowVersionError(
                "case has pipeline artifacts but no V2 workflow marker; "
                "rebuild in a new clean case directory"
            )
    write_json(
        marker_path,
        {
            "workflow_version": CURRENT_WORKFLOW_VERSION,
            "architecture_contract": "scientific_architecture/advisory-1.1",
            "task_execution_contract": "planner-owned-tasks/3.0",
            "execution_plan_contract": "execution-plan/2.0",
            "environment_contract": "case-environment/1",
            "scientific_policy_id": SCIENTIFIC_POLICY_ID,
        },
    )
