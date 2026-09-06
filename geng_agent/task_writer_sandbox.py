"""Prepare isolated task-writer and compound execution-unit sandboxes."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from .agentic_foundation import foundation_violations, install_foundation_snapshot, restore_foundation_snapshot
from .io_runtime import inject_io_runtime
from .outputs import write_json, write_text
from .paper_evidence import safe_label
from .task_scripts import write_task_scaffolding
from .task_writer_files import _read_optional_json_object
from .task_writer_support import _write_paper_evidence_bundle
from .task_writer_units import _public_execution_unit


def _prepare_task_writer_sandbox(
    *,
    sandbox: Path,
    task: dict[str, Any],
    manifest_entry: dict[str, Any],
    paper: dict[str, Any],
    paper_path: Path,
    facts: dict[str, Any],
    paper_thesis: dict[str, Any] | None,
    analysis_snapshot_hash: str,
    analysis_artifacts: dict[str, Path] | None = None,
    full_paper_images: list[Any] | None = None,
    reuse_existing: bool = False,
    foundation: dict[str, Any] | None = None,
    execution_unit_id: str | None = None,
) -> None:
    if reuse_existing and sandbox.exists():
        _remove_legacy_writer_scoring_state(sandbox)
        _write_paper_evidence_bundle(
            repro_project_dir=sandbox,
            paper_path=paper_path,
            paper=paper,
            facts=facts,
            tasks={"repro_tasks": [task]},
            paper_thesis=paper_thesis,
            analysis_snapshot_hash=analysis_snapshot_hash,
            analysis_artifacts=analysis_artifacts,
            full_paper_images=full_paper_images,
        )
        if foundation is not None:
            frozen_issues = foundation_violations(sandbox, foundation)
            if frozen_issues:
                restore_foundation_snapshot(sandbox, foundation)
                remaining_issues = foundation_violations(sandbox, foundation)
                if remaining_issues:
                    raise RuntimeError(
                        f"cached task sandbox no longer matches frozen foundation: {remaining_issues}"
                    )
        _ensure_unit_asset_namespace(sandbox, execution_unit_id or str(task.get("task_id") or "task"))
        inject_io_runtime(sandbox)
        write_task_scaffolding(sandbox, {"version": 1, "tasks": [manifest_entry]})
        return
    if sandbox.exists():
        shutil.rmtree(sandbox)
    sandbox.mkdir(parents=True, exist_ok=True)
    single_manifest = {"version": 1, "tasks": [manifest_entry]}
    inject_io_runtime(sandbox)
    write_task_scaffolding(sandbox, single_manifest)
    _write_minimal_shared_project_files(
        sandbox,
        task,
        manifest_entry,
        foundation_enabled=foundation is not None,
    )
    if foundation is not None:
        install_foundation_snapshot(sandbox, foundation)
    _write_paper_evidence_bundle(
        repro_project_dir=sandbox,
        paper_path=paper_path,
        paper=paper,
        facts=facts,
        tasks={"repro_tasks": [task]},
        paper_thesis=paper_thesis,
        analysis_snapshot_hash=analysis_snapshot_hash,
        analysis_artifacts=analysis_artifacts,
        full_paper_images=full_paper_images,
    )
    _ensure_unit_asset_namespace(sandbox, execution_unit_id or str(task.get("task_id") or "task"))

def _ensure_unit_asset_namespace(sandbox: Path, execution_unit_id: str) -> str:
    relative = f"execution_units/{safe_label(execution_unit_id)}"
    (sandbox / Path(relative)).mkdir(parents=True, exist_ok=True)
    for config_path in (sandbox / "config.json", sandbox / "config_smoke.json"):
        config = _read_optional_json_object(config_path)
        if not config:
            continue
        config["unit_asset_root"] = relative
        write_json(config_path, config)
    return relative

def _prepare_execution_unit_writer_sandbox(
    *,
    sandbox: Path,
    unit: dict[str, Any],
    members: list[tuple[int, dict[str, Any], dict[str, Any]]],
    paper: dict[str, Any],
    paper_path: Path,
    facts: dict[str, Any],
    paper_thesis: dict[str, Any] | None,
    analysis_snapshot_hash: str,
    analysis_artifacts: dict[str, Path],
    full_paper_images: list[Any] | None,
    foundation: dict[str, Any] | None,
    reuse_existing: bool = False,
) -> None:
    if reuse_existing and sandbox.exists():
        _remove_legacy_writer_scoring_state(sandbox)
        _write_paper_evidence_bundle(
            repro_project_dir=sandbox,
            paper_path=paper_path,
            paper=paper,
            facts=facts,
            tasks={"repro_tasks": [task for _index, task, _entry in members]},
            paper_thesis=paper_thesis,
            analysis_snapshot_hash=analysis_snapshot_hash,
            analysis_artifacts=analysis_artifacts,
            full_paper_images=full_paper_images,
        )
        write_json(sandbox / "execution_unit.json", _public_execution_unit(unit))
        inject_io_runtime(sandbox)
        write_task_scaffolding(sandbox, {"version": 1, "execution_plan_version": "1.0",
            "execution_units": [_public_execution_unit(unit)], "tasks": [entry for _, _, entry in members]})
        if foundation is not None:
            frozen_issues = foundation_violations(sandbox, foundation)
            if frozen_issues:
                restore_foundation_snapshot(sandbox, foundation)
                remaining_issues = foundation_violations(sandbox, foundation)
                if remaining_issues:
                    raise RuntimeError(
                        "cached execution-unit sandbox no longer matches frozen "
                        f"Foundation: {remaining_issues}"
                    )
        return
    if sandbox.exists():
        shutil.rmtree(sandbox)
    sandbox.mkdir(parents=True, exist_ok=True)
    manifest = {
        "version": 1,
        "execution_plan_version": "1.0",
        "execution_units": [_public_execution_unit(unit)],
        "tasks": [entry for _index, _task, entry in members],
    }
    inject_io_runtime(sandbox)
    write_task_scaffolding(sandbox, manifest)
    for _index, task, entry in members:
        _write_minimal_shared_project_files(
            sandbox,
            task,
            entry,
            foundation_enabled=foundation is not None,
        )
    if foundation is not None:
        install_foundation_snapshot(sandbox, foundation)

    configs_dir = sandbox / "configs"
    configs_dir.mkdir(parents=True, exist_ok=True)
    unit_asset_root = f"execution_units/{safe_label(str(unit.get('unit_id') or 'unit'))}"
    (sandbox / Path(unit_asset_root)).mkdir(parents=True, exist_ok=True)
    for _index, task, entry in members:
        task_id = str(task.get("task_id") or entry.get("task_id") or "task")
        module = str(entry.get("module") or safe_label(task_id))
        write_json(
            configs_dir / f"{module}_config.json",
            {
                "run_profile": "full",
                "task_id": task_id,
                "execution_unit_id": str(unit.get("unit_id") or ""),
                "seed": 1,
                "backend": "auto",
                "unit_asset_root": unit_asset_root,
            },
        )
        write_json(
            configs_dir / f"{module}_config_smoke.json",
            {
                "run_profile": "smoke",
                "task_id": task_id,
                "execution_unit_id": str(unit.get("unit_id") or ""),
                "seed": 1,
                "smoke": True,
                "backend": "auto",
                "unit_asset_root": unit_asset_root,
            },
        )
    write_json(
        sandbox / "config.json",
        {
            "run_profile": "full",
            "execution_unit_id": str(unit.get("unit_id") or ""),
            "task_ids": [str(task.get("task_id") or entry.get("task_id") or "") for _, task, entry in members],
        },
    )
    write_json(
        sandbox / "config_smoke.json",
        {
            "run_profile": "smoke",
            "smoke": True,
            "execution_unit_id": str(unit.get("unit_id") or ""),
        },
    )
    write_text(
        sandbox / "README.md",
        "# Compound execution-unit Writer sandbox\n\n"
        "All listed logical tasks share one scientific execution and remain independently reportable.\n",
    )
    write_json(sandbox / "execution_unit.json", _public_execution_unit(unit))
    unit_tasks = {
        "schema_version": "2.0",
        "backfill_handoff": {
            "ready_for_writer": True,
            "blocking_request_ids": [],
            "reason": "host-compiled execution unit",
            "inferred": True,
        },
        "execution_relationships": unit.get("relationships", []),
        "repro_tasks": [task for _index, task, _entry in members],
    }
    _write_paper_evidence_bundle(
        repro_project_dir=sandbox,
        paper_path=paper_path,
        paper=paper,
        facts=facts,
        tasks=unit_tasks,
        paper_thesis=paper_thesis,
        analysis_snapshot_hash=analysis_snapshot_hash,
        analysis_artifacts=analysis_artifacts,
        full_paper_images=full_paper_images,
    )

def _remove_legacy_writer_scoring_state(sandbox: Path) -> None:
    for path in sandbox.rglob("task_work_state.json"):
        if path.is_file():
            path.unlink()

def _write_minimal_shared_project_files(
    sandbox: Path,
    task: dict[str, Any],
    manifest_entry: dict[str, Any],
    *,
    foundation_enabled: bool = False,
) -> None:
    if foundation_enabled:
        task_id = str(task.get('task_id') or manifest_entry.get('task_id') or 'task')
        module = str(manifest_entry.get('module') or 'task')
        write_text(sandbox / 'README.md', f'# Task writer sandbox\n\nTask: `{task_id}`\n')
        write_json(
            sandbox / 'config.json',
            {'run_profile': 'full', 'task_id': task_id, 'seed': 1, 'backend': 'auto'},
        )
        write_json(
            sandbox / 'config_smoke.json',
            {
                'run_profile': 'smoke',
                'task_id': task_id,
                'seed': 1,
                'smoke': True,
                'backend': 'auto',
            },
        )
        task_script = sandbox / 'tasks' / f'{module}.py'
        if not task_script.exists():
            write_text(
                task_script,
                '\n'.join(
                    [
                        'from __future__ import annotations',
                        '',
                        'def main(config_path=None) -> int:',
                        '''    raise RuntimeError('task writer did not implement this task yet')''',
                        '',
                        '''if __name__ == '__main__':''',
                        '    raise SystemExit(main())',
                        '',
                    ]
                ),
            )
        return
    task_id = str(task.get("task_id") or manifest_entry.get("task_id") or "task")
    module = str(manifest_entry.get("module") or "task")
    write_text(sandbox / "README.md", f"# Task writer sandbox\n\nTask: `{task_id}`\n")
    write_text(sandbox / "requirements.txt", "numpy\nmatplotlib\n")
    write_json(sandbox / "config.json", {"run_profile": "full", "task_id": task_id, "seed": 1, "backend": "auto"})
    write_json(
        sandbox / "config_smoke.json",
        {"run_profile": "smoke", "task_id": task_id, "seed": 1, "smoke": True, "backend": "auto"},
    )
    task_script = sandbox / "tasks" / f"{module}.py"
    if not task_script.exists():
        write_text(
            task_script,
            "\n".join(
                [
                    "from __future__ import annotations",
                    "",
                    "def main(config_path=None) -> int:",
                    "    raise RuntimeError('task writer did not implement this task yet')",
                    "",
                    "if __name__ == '__main__':",
                    "    raise SystemExit(main())",
                    "",
                ]
            ),
        )
