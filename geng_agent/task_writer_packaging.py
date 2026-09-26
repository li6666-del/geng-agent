"""Merge writer sandboxes and freeze the portable reproduction package."""

from __future__ import annotations

import ast
import hashlib
import json
import shutil
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from uuid import uuid4

from .case_runtime import CaseRuntime
from .artifact_paths import path_is_link
from .outputs import write_json, write_text
from .paper_evidence import safe_label
from .project_portability import (
    ProjectPortabilityError,
    build_source_inventory,
    validate_repro_project_portability,
)
from .task_writer_files import _read_optional_json_object, _task_owned_files, _task_result_file_path
from .task_writer_support import (
    PAPER_EVIDENCE_DIR,
    _manifest_from_project,
    _prepare_project_workspace,
    _prune_unexpected_files,
    _restore_trusted_files,
)


def _freeze_repro_project_package(
    *,
    repro_project_dir: Path,
    output_dir: Path,
    audit_path: Path,
    task_manifest: dict[str, Any],
    expected_paths: set[str],
    analysis_snapshot_hash: str,
    environment_hash: str,
    run_smoke: bool,
    python_executable: Path | None = None,
    contextual_findings: list[dict[str, Any]] | None = None,
    package_layout: str = "shared_source",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Freeze the final tree only after its last package mutation.

    ``source_inventory.json`` deliberately excludes its own bytes.  Everything
    else, including outputs, tests and binary checkpoints, is hashed before the
    portability audit and text-compatible project manifest are committed.
    """

    if package_layout == "shared_source":
        from .delivery_environment import export_installation
        expected_paths.update(export_installation(repro_project_dir, python_executable=python_executable))
    elif package_layout != "task_directories":
        raise ValueError(f"unknown package layout: {package_layout}")
    source_inventory = build_source_inventory(repro_project_dir)
    write_json(repro_project_dir / "source_inventory.json", source_inventory)
    portability = validate_repro_project_portability(
        repro_project_dir,
        run_smoke=False,
        python_executable=python_executable,
        smoke_command=["python", "run_experiment.py", "config_smoke.json"],
        smoke_timeout_s=120.0,
        raise_on_error=False,
        contextual_findings=contextual_findings,
    )
    evidence = _read_optional_json_object(repro_project_dir / "execution_evidence.json")
    evidence_tasks = evidence.get("tasks", [])
    incomplete = [str(item.get("task_id")) for item in evidence_tasks if not item.get("all_bytes_available")]
    portability["execution_evidence"] = {"tasks_with_host_receipts": len(evidence_tasks),
                                          "tasks_with_missing_bytes": incomplete}
    if incomplete:
        portability.setdefault("warnings", []).append({
            "code": "execution_evidence_incomplete", "severity": "warning",
            "message": "Original execution bytes are unavailable for tasks: " + ", ".join(incomplete),
        })
    write_json(audit_path, portability)
    if not portability.get("portable"):
        # Only byte integrity / filesystem safety can prevent freezing.
        raise ProjectPortabilityError(portability)
    lineage = _read_optional_json_object(repro_project_dir / "artifact_lineage.json")
    portability.setdefault("observations", []).extend(lineage.get("observations") or [])
    write_json(audit_path, portability)
    manifest_paths = (expected_paths if package_layout == "shared_source" else
                      {path for path in expected_paths if not path.startswith("task_packages/")})
    manifest = _manifest_from_project(
        repro_project_dir=repro_project_dir,
        expected_paths=manifest_paths,
        task_manifest=task_manifest,
        round_no=1,
    )
    if package_layout == "task_directories":
        # The aggregate manifest describes an index. Task files are delivered
        # and inventoried on disk, but their source text does not belong in a
        # second, synthetic project manifest with a single root entrypoint.
        inventoried = {item["path"]: item for item in source_inventory["files"]}
        for relative in sorted(expected_paths - manifest_paths):
            item = inventoried.get(relative)
            if item is None:
                raise FileNotFoundError(f"task package file missing from inventory: {relative}")
            manifest["_meta"]["packaged_only_files"].append({
                "path": relative, "bytes": item["size"], "sha256": item["sha256"],
                "represented_by": "source_inventory.json",
            })
    manifest["_meta"]["mode"] = "task_writers"
    manifest["_meta"]["package_layout"] = package_layout
    manifest["_meta"]["analysis_snapshot_hash"] = analysis_snapshot_hash
    manifest["_meta"]["environment_lock_hash"] = environment_hash or None
    inventory = portability.get("inventory")
    manifest["_meta"]["source_inventory_sha256"] = (
        inventory.get("inventory_sha256") if isinstance(inventory, dict) else None
    )
    write_json(output_dir / "repro_project_manifest.json", manifest)
    return manifest, portability


def _package_task_directories(
    *,
    repro_project_dir: Path,
    output_dir: Path,
    audit_dir: Path,
    task_manifest: dict[str, Any],
    task_records: list[dict[str, Any]],
    execution_plan: dict[str, Any],
    case_runtime: CaseRuntime | None,
    analysis_snapshot_hash: str,
    environment_hash: str,
    require_lineage: bool,
) -> tuple[set[str], dict[str, Any], dict[str, Any]]:
    """Deliver one runnable folder per final task and its supplied inputs.

    Historical multi-task sandboxes retain their original files when packaged;
    new plans always dispatch one complete task per Writer. The aggregate root
    never claims to be a unified scientific program.
    Build it beside the current project so a failed attempt preserves the old
    tree and every Writer sandbox for supervisor recovery.
    """
    case_root = output_dir.resolve()
    for path in (repro_project_dir, audit_dir):
        try:
            path.resolve().relative_to(case_root)
        except ValueError as exc:
            raise ValueError("package destinations must stay inside the case") from exc
    if path_is_link(repro_project_dir) or path_is_link(audit_dir):
        raise ValueError("linked package destination")

    stage = audit_dir / "pkg_stages" / uuid4().hex[:8]
    stage.mkdir(parents=True)
    tasks_root = stage / "task_packages"
    tasks_root.mkdir()
    task_entries: list[dict[str, Any]] = []
    task_to_directory: dict[str, str] = {}
    aggregate_evidence: list[dict[str, Any]] = []
    planned_units = execution_plan.get("execution_units")
    if not isinstance(planned_units, list) or not planned_units:
        raise ValueError("task delivery requires execution units")
    manifest_entries = [item for item in task_manifest.get("tasks", []) if isinstance(item, dict)]
    task_order = {str(item.get("task_id")): index
                  for index, item in enumerate(manifest_entries, start=1)}
    for unit in planned_units:
        if not isinstance(unit, dict):
            raise ValueError("execution unit must be an object")
        unit_id = str(unit.get("unit_id") or "")
        task_ids = [str(value) for value in unit.get("task_ids", []) if str(value)]
        records = [record for record in task_records if record.get("execution_unit_id") == unit_id]
        entries = [item for item in manifest_entries if str(item.get("task_id")) in task_ids]
        recorded_tasks = {str(record.get("task_id")) for record in records}
        if (not unit_id or not task_ids or len(entries) != len(task_ids)
                or not set(task_ids).issubset(recorded_tasks)):
            raise ValueError(f"execution unit has no complete package handoff: {unit_id}")
        if not any(Path(str(record.get("sandbox") or "")).is_dir() for record in records):
            raise ValueError(f"execution unit sandbox unavailable: {unit_id}")
        unit_manifest = {**task_manifest, "tasks": entries, "execution_units": [unit]}
        unit_plan = {**execution_plan, "execution_units": [unit],
                     "task_to_execution_unit": {task_id: unit_id for task_id in task_ids},
                     "task_policy": "planner_final_tasks"}
        for task_id in task_ids:
            index = task_order[task_id]
            relative_root = f"task_packages/t{index:02d}_{safe_label(task_id)}"
            task_root = stage / relative_root
            _prepare_project_workspace(task_root, unit_manifest)
            task_expected = _merge_task_writer_deliveries(
                repro_project_dir=task_root, task_manifest=unit_manifest,
                expected_paths=set(), task_records=records,
                execution_plan=unit_plan, case_runtime=case_runtime,
                require_lineage=require_lineage,
            )
            _restore_trusted_files(task_root, unit_manifest)
            write_json(task_root / "tasks_manifest.json", unit_manifest)
            task_audit = audit_dir / "pkg_task_manifests" / f"t{index:02d}"
            task_audit.mkdir(parents=True, exist_ok=True)
            task_frozen, task_portability = _freeze_repro_project_package(
                repro_project_dir=task_root, output_dir=task_audit,
                audit_path=task_audit / "portability.json", task_manifest=unit_manifest,
                expected_paths=task_expected,
                analysis_snapshot_hash=analysis_snapshot_hash,

                environment_hash=environment_hash, run_smoke=False,
                python_executable=case_runtime.python_executable if case_runtime else None,
            )
            if not task_portability.get("portable"):
                raise RuntimeError(f"task package is incomplete: {task_id}")
            task_entries.append({
                "task_id": task_id, "execution_unit_id": unit_id,
                "unit_task_ids": task_ids, "directory": relative_root,
                "output_directory": f"{relative_root}/outputs",
                "full_command": ["python", "run_experiment.py", "config.json"],
                "smoke_command": ["python", "run_experiment.py", "config_smoke.json"],
                "requirements": f"{relative_root}/requirements.txt",
                "environment_lock": f"{relative_root}/environment.lock.json" if case_runtime else None,
                "installation": f"{relative_root}/installation.json",
                "source_inventory": f"{relative_root}/source_inventory.json",
                "execution_evidence": f"{relative_root}/execution_evidence.json",
                "source_inventory_sha256": task_frozen.get("_meta", {}).get("source_inventory_sha256"),
            })
            task_to_directory[task_id] = relative_root
            task_evidence = _read_optional_json_object(task_root / "execution_evidence.json")
            for evidence in task_evidence.get("tasks", []):
                if not isinstance(evidence, dict) or evidence.get("task_id") != task_id:
                    continue
                files = [{**item, "packaged_path": f"{relative_root}/{item['packaged_path']}"}
                         if isinstance(item, dict) and item.get("packaged_path") else item
                         for item in evidence.get("files", [])]
                aggregate_evidence.append({**evidence, "execution_unit_id": unit_id,
                    "receipt": f"{relative_root}/{evidence['receipt']}", "files": files})

    delivered_tasks = []
    for entry in manifest_entries:
        task_id = str(entry.get("task_id") or "")
        prefix = task_to_directory[task_id]
        delivered_tasks.append({**entry, "task_directory": prefix,
            "script": f"{prefix}/{entry['script']}",
            "config_full": f"{prefix}/{entry['config_full']}",
            "config_smoke": f"{prefix}/{entry['config_smoke']}",
            "output_directory": f"{prefix}/outputs/{entry['output_subdir']}"})
    write_json(stage / "tasks_manifest.json", {**task_manifest, "tasks": delivered_tasks})
    write_json(stage / "execution_plan.json", execution_plan)
    write_json(stage / "package_index.json", {"schema_version": "1.0",
        "layout": "task_directories",
        "tasks": task_entries})
    write_json(stage / "artifact_lineage.json", {"schema_version": "1.0",
        "layout": "task_directories",
        "task_lineages": [{"task_id": item["task_id"],
            "path": f"{item['directory']}/artifact_lineage.json"} for item in task_entries]})
    write_json(stage / "execution_evidence.json", {"schema_version": 1,
        "meaning": "Original host observations; each mapping points into the executed unit's delivered project.",
        "tasks": aggregate_evidence})
    write_json(stage / "reproducibility_manifest.json", {"schema_version": "1.0",
        "layout": "task_directories", "tasks_manifest": "tasks_manifest.json",
        "execution_plan": "execution_plan.json", "artifact_lineage": "artifact_lineage.json",
        "source_inventory": "source_inventory.json", "execution_evidence": "execution_evidence.json",
        "task_packages": task_entries})
    readme = ["# 复现项目：按任务交付", "",
              "每个任务目录保留其 Writer 执行单元使用的代码、配置、结果与运行证据。",
              "每个任务拥有自己的完整实现；合并任务内部包含多个实验及逐项结果。", "",
              "本次已有的图表、数据和其他结果随对应任务交付，保留原始相对路径。", "",
              "| 任务 | 执行单元 | 目录 | 本次复现结果 |", "| --- | --- | --- | --- |"]
    for item in task_entries:
        readme.append(f"| {item['task_id']} | {item['execution_unit_id']} | "
                      f"[{item['directory']}]({item['directory']}/) | "
                      f"[查看结果]({item['output_directory']}/) |")
    readme += ["", "进入相应任务目录，先按其 `README.md` 安装依赖，然后运行：", "",
               "```sh", "python run_experiment.py config_smoke.json",
               "python run_experiment.py config.json", "```", "",
               "各任务的环境清单、执行收据和源码哈希分别位于其目录中；科研结论见案例报告。", ""]
    write_text(stage / "README.md", "\n".join(readme))
    expected = {path.relative_to(stage).as_posix() for path in stage.rglob("*") if path.is_file()}
    expected.add("source_inventory.json")
    root_audit = audit_dir / "pkg_aggregate"
    root_audit.mkdir(parents=True, exist_ok=True)
    frozen, portability = _freeze_repro_project_package(
        repro_project_dir=stage, output_dir=root_audit,
        audit_path=audit_dir / "03c_project_portability.json",
        task_manifest={**task_manifest, "tasks": delivered_tasks},
        expected_paths=expected, analysis_snapshot_hash=analysis_snapshot_hash,
        environment_hash=environment_hash,
        run_smoke=False, package_layout="task_directories",
    )
    frozen["_meta"]["task_packages"] = task_entries
    previous = None
    if repro_project_dir.exists():
        previous = audit_dir / "pkg_previous" / uuid4().hex[:8]
        previous.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(repro_project_dir), str(previous))
    try:
        shutil.move(str(stage), str(repro_project_dir))
    except Exception:
        if previous is not None and not repro_project_dir.exists():
            shutil.move(str(previous), str(repro_project_dir))
        raise
    write_json(output_dir / "repro_project_manifest.json", frozen)
    write_json(audit_dir / "03c_task_package_delivery.json", {
        "layout": "task_directories", "tasks": task_entries,
        "source_inventory_sha256": frozen.get("_meta", {}).get("source_inventory_sha256"),
        "previous_project": str(previous) if previous is not None else None,
    })
    return expected, frozen, portability

def _expected_paths_from_project_manifest(manifest: dict[str, Any]) -> set[str]:
    paths: set[str] = set()
    for item in manifest.get("files", []):
        if isinstance(item, dict) and isinstance(item.get("path"), str):
            paths.add(str(item["path"]))
    meta = manifest.get("_meta")
    packaged = meta.get("packaged_only_files") if isinstance(meta, dict) else None
    for item in packaged if isinstance(packaged, list) else []:
        if isinstance(item, dict) and isinstance(item.get("path"), str):
            paths.add(str(item["path"]))
    return paths

def _remove_packaged_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)

def _clear_previous_packaged_runtime_files(repro_project_dir: Path) -> None:
    """Do not certify stale outputs or repair scratch from an earlier assembly."""

    for name in ("outputs", "repair_logs", "task_requirements"):
        path = repro_project_dir / name
        if path.exists() or path.is_symlink():
            _remove_packaged_path(path)

def _streaming_file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

def _merge_task_writer_deliveries(
    *,
    repro_project_dir: Path,
    task_manifest: dict[str, Any],
    expected_paths: set[str],
    task_records: list[dict[str, Any]],
    execution_plan: dict[str, Any] | None = None,
    case_runtime: CaseRuntime | None = None,
    require_lineage: bool = False,
) -> set[str]:
    _clear_previous_packaged_runtime_files(repro_project_dir)
    _write_final_shared_project_files(repro_project_dir, task_records)
    expected_paths.update(
        {
            "README.md",
            "requirements.txt",
            "config.json",
            "config_smoke.json",
            "execution_plan.json",
            "artifact_lineage.json",
            "reproducibility_manifest.json",
            "source_inventory.json",
        }
    )
    if case_runtime is not None:
        expected_paths.add("environment.lock.json")
    write_json(repro_project_dir / "execution_plan.json", execution_plan or {})
    configs_dir = repro_project_dir / "configs"
    configs_dir.mkdir(parents=True, exist_ok=True)
    combined_requirements: list[str] = ["numpy", "matplotlib"]
    copied_task_files: dict[str, tuple[str, str]] = {}
    processed_sandboxes: set[str] = set()
    unit_requirements: dict[str, str] = {}
    unit_environment_locks: dict[str, str] = {}
    for record in task_records:
        raw_sandbox = str(record.get("sandbox") or "").strip()
        if not raw_sandbox:
            continue
        sandbox = Path(raw_sandbox)
        module = str(record.get("module") or "")
        output_subdir = str(record.get("output_subdir") or record.get("task_id") or "")
        if not sandbox.is_dir():
            continue
        sandbox_key = str(sandbox.resolve())
        if sandbox_key not in processed_sandboxes:
            writer_readme = sandbox / "README.md"
            if writer_readme.is_file() and not writer_readme.is_symlink():
                note_path = f"task_notes/{safe_label(str(record.get('execution_unit_id') or record.get('task_id') or module))}.md"
                (repro_project_dir / note_path).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(writer_readme, repro_project_dir / note_path)
                expected_paths.add(note_path)
            for source in [*_task_owned_files(sandbox), *_writer_package_files(sandbox)]:
                _copy_merged_writer_file(
                    source=source,
                    sandbox=sandbox,
                    repro_project_dir=repro_project_dir,
                    copied_files=copied_task_files,
                    owner=str(record.get("execution_unit_id") or record.get("task_id") or module),
                    expected_paths=expected_paths,
                )
            req_path = sandbox / "requirements.txt"
            if req_path.exists():
                combined_requirements.extend(_read_requirement_names(req_path))
                unit_id = safe_label(str(record.get("execution_unit_id") or record.get("task_id") or sandbox.name))
                relative_requirements = f"task_requirements/{unit_id}.txt"
                target_requirements = repro_project_dir / relative_requirements
                target_requirements.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(req_path, target_requirements)
                unit_requirements[unit_id] = relative_requirements
                expected_paths.add(relative_requirements)
            env_lock_path = sandbox / "writer_environment.lock.json"
            if env_lock_path.is_file():
                unit_id = safe_label(str(record.get("execution_unit_id") or record.get("task_id") or sandbox.name))
                relative_env_lock = f"task_requirements/{unit_id}.lock.json"
                target_env_lock = repro_project_dir / relative_env_lock
                target_env_lock.parent.mkdir(parents=True, exist_ok=True)
                local_lock = _read_optional_json_object(env_lock_path)
                write_json(target_env_lock, {
                    "schema_version": 1,
                    "shared_environment_hash": local_lock.get("shared_environment_hash"),
                    "local_distributions": local_lock.get("local_distributions", []),
                })
                unit_environment_locks[unit_id] = relative_env_lock
                expected_paths.add(relative_env_lock)
            unit_result = sandbox / "execution_unit_result.json"
            if unit_result.is_file():
                unit_id = str(record.get("execution_unit_id") or sandbox.name)
                relative = f"execution_units/{safe_label(unit_id)}/execution_unit_result.json"
                target = repro_project_dir / Path(relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(unit_result, target)
                expected_paths.add(relative)
            processed_sandboxes.add(sandbox_key)
        for config_name, target_name in (
            ("config.json", f"{module}_config.json"),
            ("config_smoke.json", f"{module}_config_smoke.json"),
        ):
            source = sandbox / "configs" / target_name
            if not source.exists():
                source = sandbox / config_name
            if source.exists():
                target = configs_dir / target_name
                shutil.copy2(source, target)
                expected_paths.add(f"configs/{target_name}")
        # The complete outputs/ tree was copied with this sandbox's other files.
        # Writers may use outputs/ directly or additional subdirectories; the
        # configured output_subdir is not an exhaustive list of their results.
        result_dir = repro_project_dir / "outputs" / output_subdir
        result_dir.mkdir(parents=True, exist_ok=True)
        for name in ("task_agent_result.json", "task_agent_result.md"):
            source, _ = _task_result_file_path(
                sandbox,
                output_subdir,
                name,
                allow_root_fallback=(
                    int(record.get("execution_unit_member_count") or 1) <= 1
                ),
            )
            if source.exists():
                shutil.copy2(source, result_dir / name)
    lineage = _build_artifact_lineage(
        repro_project_dir=repro_project_dir,
        execution_plan=execution_plan or {},
        task_records=task_records,
        require_lineage=require_lineage,
    )
    write_json(repro_project_dir / "artifact_lineage.json", lineage)
    if case_runtime is not None:
        write_json(
            repro_project_dir / "environment.lock.json",
            _portable_environment_lock(case_runtime),
        )
    write_json(
        repro_project_dir / "reproducibility_manifest.json",
        {
            "schema_version": "1.0",
            "tasks_manifest": "tasks_manifest.json",
            "execution_plan": "execution_plan.json",
            "artifact_lineage": "artifact_lineage.json",
            "environment_lock": (
                "environment.lock.json" if case_runtime is not None else None
            ),
            "requirements_by_execution_unit": unit_requirements,
            "environment_lock_by_execution_unit": unit_environment_locks,
            "source_inventory": "source_inventory.json",
            "execution_evidence": "execution_evidence.json",
            "smoke_command": ["python", "run_experiment.py", "config_smoke.json"],
            "full_command": ["python", "run_experiment.py", "config.json"],
        },
    )
    write_text(repro_project_dir / "requirements.txt", _format_requirements(combined_requirements))
    from .delivery_evidence import package_execution_evidence
    expected_paths.update(package_execution_evidence(repro_project_dir, task_records))
    _prune_unexpected_files(repro_project_dir, expected_paths)
    return expected_paths

def _documentation_only_package(path: Path) -> bool:
    if path.name != "__init__.py":
        return False
    try:
        body = ast.parse(path.read_text(encoding="utf-8-sig")).body
    except (OSError, SyntaxError, UnicodeError):
        return False
    return not body or (len(body) == 1 and isinstance(body[0], ast.Expr)
                        and isinstance(body[0].value, ast.Constant)
                        and isinstance(body[0].value.value, str))


def _copy_merged_writer_file(
    *,
    source: Path,
    sandbox: Path,
    repro_project_dir: Path,
    copied_files: dict[str, tuple[str, str]],
    owner: str,
    expected_paths: set[str],
) -> None:
    relative = source.relative_to(sandbox).as_posix()
    if relative.startswith("tests/"):
        relative = f"execution_units/{safe_label(owner)}/{relative}"
    normalize_python = source.suffix.lower() == ".py" and not relative.startswith("outputs/")
    if normalize_python:
        normalized_content = (
            source.read_text(encoding="utf-8-sig")
            .replace("\r\n", "\n")
            .replace("\r", "\n")
            .encode("utf-8")
        )
        content_hash = hashlib.sha256(normalized_content).hexdigest()
    else:
        content_hash = _streaming_file_sha256(source)
    previous = copied_files.get(relative)
    target = repro_project_dir / Path(relative)
    if previous is not None and previous[0] != content_hash:
        if normalize_python and _documentation_only_package(source) and _documentation_only_package(target):
            # A package shared by private submodules can have two descriptions.
            # Keep both descriptions separately; only its package docstring is
            # normalized. Imports, assignments and any executable code still
            # take the collision error below. Original execution bytes remain
            # in execution_records, rather than being recertified as this file.
            for origin, origin_owner in ((target, previous[1]), (source, owner)):
                note = f"task_notes/package_descriptions/{safe_label(origin_owner)}/{relative}.txt"
                note_path = repro_project_dir / note
                if not note_path.exists():
                    write_text(note_path, origin.read_text(encoding="utf-8-sig"))
                    expected_paths.add(note)
            write_text(target, '"""Package assembled from task-private modules; descriptions are in task_notes/package_descriptions."""\n')
            copied_files[relative] = (_streaming_file_sha256(target), "assembled_package")
            expected_paths.add(relative)
            return
        raise RuntimeError(
            "execution-unit package collision for "
            f"{relative}: {previous[1]} and {owner} supplied different content"
        )
    if normalize_python:
        _copy_python_without_bom(source, target)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    copied_files[relative] = (content_hash, owner)
    expected_paths.add(relative)

def _writer_package_files(sandbox: Path) -> list[Path]:
    """Return portable runtime files, including original paper inputs when present."""

    excluded_roots = {
        ".git",
        ".geng_execution",
        ".geng_runtime",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "repair_logs",
        "venv",
        "writer_progress",
        ".tox",
        "node_modules",
    }
    excluded_root_files = {
        "README.md",
        "config.json",
        "config_smoke.json",
        "environment_request.json",
        "environment.lock.json",
        "artifact_lineage.json",
        "execution_unit.json",
        "execution_unit_result.json",
        "execution_plan.json",
        "package_manifest.json",
        "project_manifest.json",
        "project_portability_manifest.json",
        "repro_project_manifest.json",
        "reproducibility_manifest.json",
        "requirements.txt",
        "writer_environment.lock.json",
        "run_experiment.py",
        "source_inventory.json",
        "task_agent_result.json",
        "task_agent_result.md",
        "tasks_manifest.json",
    }
    root_resolved = sandbox.resolve()
    result: list[Path] = []
    for path in sorted(sandbox.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative_path = path.relative_to(sandbox)
        # Only the top-level tasks/ tree is copied by _task_owned_files.
        # An architecture may also assign modules such as src/tasks/ber.py;
        # these are private dependencies, not a duplicate of the task scaffold.
        if relative_path.parts[0].casefold() == "tasks":
            continue
        if relative_path.parts[0] == PAPER_EVIDENCE_DIR:
            # Digitisation and other reproduction code may read the original
            # PDF/page images at runtime. Keep these exact inputs while leaving
            # role packets and machine-local analysis metadata in the audit.
            if len(relative_path.parts) < 3 or relative_path.parts[1] not in {
                "source", "full_paper_pages",
            }:
                continue
            if (relative_path.parts[1] == "full_paper_pages"
                    and path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}):
                continue
        if any(part.casefold() in excluded_roots for part in relative_path.parts):
            continue
        relative = relative_path.as_posix()
        if len(relative_path.parts) == 1 and relative in excluded_root_files:
            continue
        if path.suffix.lower() in {".pyc", ".pyo"}:
            continue
        if path.name.casefold() in {
            ".env",
            "credentials.json",
            "secrets.json",
            "token.json",
        }:
            continue
        try:
            path.resolve().relative_to(root_resolved)
        except ValueError:
            continue
        result.append(path)
    return result

def _portable_environment_lock(case_runtime: CaseRuntime) -> dict[str, Any]:
    lock = case_runtime.lock if isinstance(case_runtime.lock, dict) else {}
    interpreter = lock.get("interpreter") if isinstance(lock.get("interpreter"), dict) else {}
    return {
        "schema_version": "1.0",
        "kind": "geng.reproduction_environment.lock",
        "source_environment_hash": case_runtime.environment_hash,
        "request_hash": lock.get("request_hash"),
        "resolution_hash": lock.get("resolution_hash"),
        "ready": bool(lock.get("ready", True)),
        "trusted_sources": lock.get("index", {}),
        "source_policy": lock.get("source_policy", {}),
        "interpreter": {
            key: interpreter.get(key)
            for key in (
                "python_full_version",
                "implementation",
                "marker_environment",
            )
            if key in interpreter
        },
        "requirements": lock.get("requirements", []),
        # Requested requirements alone are not a lock: host-reused and
        # transitive packages also affect the executable scientific environment.
        "installed_distributions": lock.get("installed_distributions", []),
    }

def _build_artifact_lineage(
    *,
    repro_project_dir: Path,
    execution_plan: dict[str, Any],
    task_records: list[dict[str, Any]],
    require_lineage: bool,
) -> dict[str, Any]:
    entries_by_artifact: dict[str, dict[str, Any]] = {}
    observations: list[dict[str, Any]] = []
    processed_sandboxes: set[str] = set()
    for record in task_records:
        raw_sandbox = str(record.get("sandbox") or "").strip()
        if not raw_sandbox:
            continue
        sandbox = Path(raw_sandbox)
        sandbox_key = str(sandbox.resolve())
        if sandbox_key in processed_sandboxes:
            continue
        processed_sandboxes.add(sandbox_key)
        unit_result = _read_optional_json_object(sandbox / "execution_unit_result.json")
        raw_lineage = unit_result.get("artifact_lineage")
        for raw_entry in raw_lineage if isinstance(raw_lineage, list) else []:
            if not isinstance(raw_entry, dict):
                continue
            artifact_id = str(raw_entry.get("artifact_id") or "").strip()
            raw_path = str(raw_entry.get("path") or "").strip().replace("\\", "/")
            if not artifact_id or not raw_path:
                observations.append({"code": "incomplete_artifact_description", "task_id": record.get("task_id"), "entry": raw_entry})
                continue
            portable_path = PurePosixPath(raw_path)
            if (
                portable_path.is_absolute()
                or PureWindowsPath(raw_path).is_absolute()
                or PureWindowsPath(raw_path).drive
                or ".." in portable_path.parts
            ):
                raise RuntimeError(f"material artifact {artifact_id!r} uses an unsafe path: {raw_path}")
            unit_id = str(record.get("execution_unit_id") or "").strip()
            asset_root = PurePosixPath("execution_units") / safe_label(unit_id)
            if (
                not unit_id
                or tuple(portable_path.parts[: len(asset_root.parts)])
                != asset_root.parts
            ):
                observations.append({"code": "artifact_namespace_differs", "artifact_id": artifact_id,
                                     "path": raw_path, "expected_namespace": asset_root.as_posix()})
            relative = Path(*portable_path.parts)
            source = sandbox / relative
            target = repro_project_dir / relative
            try:
                source.resolve().relative_to(sandbox.resolve())
                target.resolve().relative_to(repro_project_dir.resolve())
            except ValueError:
                raise RuntimeError(f"material artifact {artifact_id!r} escapes its execution unit")
            if not source.is_file() or not target.is_file():
                observations.append({"code": "declared_artifact_unavailable", "artifact_id": artifact_id, "path": raw_path})
                continue
            item = {
                "artifact_id": artifact_id,
                "path": relative.as_posix(),
                "sha256": _streaming_file_sha256(target),
                "bytes": target.stat().st_size,
                "producer_task_id": str(raw_entry.get("producer_task_id") or "") or None,
                "consumer_task_ids": sorted(
                    {
                        str(value)
                        for value in raw_entry.get("consumer_task_ids", [])
                        if str(value)
                    }
                ) if isinstance(raw_entry.get("consumer_task_ids"), list) else [],
                "execution_unit_id": str(record.get("execution_unit_id") or "") or None,
            }
            previous = entries_by_artifact.get(artifact_id)
            if previous is not None and (
                previous["path"] != item["path"]
                or previous["sha256"] != item["sha256"]
            ):
                observations.append({"code": "multiple_artifact_descriptions", "previous": previous, "current": item})
            entries_by_artifact[artifact_id] = item

    return {
        "schema_version": "2.0",
        "artifacts": [entries_by_artifact[key] for key in sorted(entries_by_artifact)],
        "observations": observations,
        "declared_task_inputs": [
            {"task_ids": unit.get("task_ids", []), "depends_on": unit.get("depends_on", [])}
            for unit in execution_plan.get("execution_units", [])
        ],
    }


def _writer_snapshot_hash(analysis_hash: str, runtime_hash: str) -> str:
    payload = f"{analysis_hash}::{runtime_hash}".encode("ascii")
    return hashlib.sha256(payload).hexdigest()

def _write_final_shared_project_files(
    repro_project_dir: Path,
    task_records: list[dict[str, Any]],
) -> None:
    write_text(
        repro_project_dir / "README.md",
        "# Task-writer reproduction project\n\n"
        "This project was assembled from autonomous per-task Codex writer sandboxes. "
        "Each task delivered its own code, artifacts, and self-review before host aggregation.\n",
    )
    write_json(
        repro_project_dir / "config.json",
        {
            "run_profile": "full",
            "task_writer_mode": True,
            "task_statuses": {str(r.get("task_id")): r.get("task_writer_status") for r in task_records},
        },
    )
    write_json(repro_project_dir / "config_smoke.json", {"run_profile": "smoke", "task_writer_mode": True, "smoke": True})

def _task_manifest_with_configs(task_manifest: dict[str, Any]) -> dict[str, Any]:
    manifest = json.loads(json.dumps(task_manifest))
    for entry in manifest.get("tasks", []):
        if not isinstance(entry, dict):
            continue
        module = str(entry.get("module") or "")
        if module:
            entry["config_full"] = f"configs/{module}_config.json"
            entry["config_smoke"] = f"configs/{module}_config_smoke.json"
    return manifest

def _read_requirement_names(path: Path) -> list[str]:
    names: list[str] = []
    for raw in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        names.append(line)
    return names

def _copy_python_without_bom(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source.read_text(encoding="utf-8-sig"), encoding="utf-8", newline="\n")

def _format_requirements(requirements: list[str]) -> str:
    seen: set[str] = set()
    lines: list[str] = []
    for item in requirements:
        key = item.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        lines.append(item.strip())
    return "\n".join(lines) + ("\n" if lines else "")
