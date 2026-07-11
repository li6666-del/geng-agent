from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def build_automation_provenance(
    *,
    output_dir: Path,
    paper_path: Path,
    memory_manifest: dict[str, Any],
    facts: dict[str, Any],
    tasks: dict[str, Any],
    experiment_index: dict[str, Any],
    runtime_result: dict[str, Any],
    agentic_status: dict[str, Any],
    settings: dict[str, Any],
) -> dict[str, Any]:
    artifacts: dict[str, dict[str, Any]] = {}
    for name in (
        "paper_chunks.json",
        "paper_memory.json",
        "engineering_facts.json",
        "paper_thesis.json",
        "repro_tasks.json",
        "experiment_index.json",
        "repro_project_manifest.json",
        "runtime_result.json",
        "result_review.md",
        "risk_report.json",
    ):
        path = output_dir / name
        if path.exists() and path.is_file():
            artifacts[name] = {"sha256": _sha256(path), "bytes": path.stat().st_size}

    facts_meta = facts.get("_meta") if isinstance(facts.get("_meta"), dict) else {}
    tasks_meta = tasks.get("_meta") if isinstance(tasks.get("_meta"), dict) else {}
    fact_semantic = facts_meta.get("semantic_merge") if isinstance(facts_meta.get("semantic_merge"), dict) else {}
    task_semantic = tasks_meta.get("semantic_merge") if isinstance(tasks_meta.get("semantic_merge"), dict) else {}
    return {
        "schema_version": "1.0",
        "source": {"paper_path": str(paper_path), "paper_sha256": _sha256(paper_path) if paper_path.exists() else None},
        "memory_snapshot_hash": memory_manifest.get("snapshot_hash"),
        "settings": settings,
        "analysis": {
            "facts_count": len(facts.get("engineering_facts", [])),
            "tasks_count": len(tasks.get("repro_tasks", [])),
            "experiments_count": len(experiment_index.get("experiments", [])),
            "facts_gap": facts_meta.get("gap_finder", {}),
            "tasks_gap": tasks_meta.get("gap_finder", {}),
            "fact_conflicts": len(fact_semantic.get("fact_conflicts", [])),
            "task_conflicts": len(task_semantic.get("task_conflicts", [])),
        },
        "task_writers": {
            "mode": agentic_status.get("mode"),
            "agent_concurrency": agentic_status.get("agent_concurrency"),
            "agent_concurrency_max": agentic_status.get("agent_concurrency_max"),
            "resource_plan": agentic_status.get("resource_plan"),
            "stop_rule": agentic_status.get("stop_rule"),
            "analysis_revision_history": agentic_status.get("analysis_revision_history", []),
            "tasks": [
                {
                    "task_id": item.get("task_id"),
                    "status": item.get("task_writer_status"),
                    "passed": item.get("passed"),
                    "reproducibility_mode": item.get("reproducibility_mode"),
                    "task_contract_hash": item.get("task_contract_hash"),
                    "full_run": item.get("full_run"),
                    "revision_request": item.get("revision_request"),
                }
                for item in runtime_result.get("per_task", [])
                if isinstance(item, dict)
            ],
        },
        "artifacts": artifacts,
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
