"""Merge writer sandboxes and freeze the portable reproduction package."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from .agentic_foundation import install_foundation_snapshot
from .case_runtime import CaseRuntime
from .outputs import write_json, write_text
from .paper_evidence import safe_label
from .project_portability import (
    ProjectPortabilityError,
    build_source_inventory,
    validate_repro_project_portability,
)
from .task_writer_files import _read_optional_json_object, _task_owned_files, _task_result_file_path
from .task_writer_support import PAPER_EVIDENCE_DIR, _manifest_from_project, _prune_unexpected_files


def _freeze_repro_project_package(
    *,
    repro_project_dir: Path,
    output_dir: Path,
    audit_path: Path,
    task_manifest: dict[str, Any],
    expected_paths: set[str],
    analysis_snapshot_hash: str,
    foundation_snapshot_hash: str,
    environment_hash: str,
    run_smoke: bool,
    python_executable: Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Freeze the final tree only after its last package mutation.

    ``source_inventory.json`` deliberately excludes its own bytes.  Everything
    else, including outputs, tests and binary checkpoints, is hashed before the
    portability audit and text-compatible project manifest are committed.
    """

    from .delivery_environment import export_installation
    expected_paths.update(export_installation(repro_project_dir, python_executable=python_executable))
    source_inventory = build_source_inventory(repro_project_dir)
    write_json(repro_project_dir / "source_inventory.json", source_inventory)
    portability = validate_repro_project_portability(
        repro_project_dir,
        run_smoke=run_smoke,
        python_executable=python_executable,
        smoke_command=["python", "run_experiment.py", "config_smoke.json"],
        smoke_timeout_s=120.0,
        raise_on_error=False,
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
    if run_smoke:
        from .environment_rebuild import verify_clean_environment
        portability["clean_environment"] = verify_clean_environment(
            repro_project_dir, cache_dir=output_dir / "audit" / "clean_environments",
            python_executable=python_executable,
        )
        if not portability["clean_environment"].get("verified"):
            portability.setdefault("warnings", []).append({
                "code": "clean_environment_unverified", "severity": "warning",
                "message": "Clean-environment delivery was not verified; retain the independent scientific results.",
            })
    # Persist the full smoke diagnostics before failing.  Relocation uses a
    # temporary copy, so otherwise task-level errors disappear with that copy
    # and callers receive only the aggregate return code.
    write_json(audit_path, portability)
    if not portability.get("portable"):
        raise ProjectPortabilityError(portability)
    manifest = _manifest_from_project(
        repro_project_dir=repro_project_dir,
        expected_paths=expected_paths,
        task_manifest=task_manifest,
        round_no=1,
    )
    manifest["_meta"]["mode"] = "task_writers"
    manifest["_meta"]["analysis_snapshot_hash"] = analysis_snapshot_hash
    manifest["_meta"]["foundation_snapshot_hash"] = foundation_snapshot_hash or None
    manifest["_meta"]["environment_lock_hash"] = environment_hash or None
    inventory = portability.get("inventory")
    manifest["_meta"]["source_inventory_sha256"] = (
        inventory.get("inventory_sha256") if isinstance(inventory, dict) else None
    )
    write_json(output_dir / "repro_project_manifest.json", manifest)
    return manifest, portability

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

    for name in ("outputs", "repair_logs"):
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
    foundation: dict[str, Any] | None = None,
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
    if foundation is not None:
        expected_paths.update(install_foundation_snapshot(repro_project_dir, foundation))
    canonical_frozen = {
        str(item.get("path") or "").replace("\\", "/")
        for item in _read_optional_json_object(repro_project_dir / "foundation_manifest.json").get("frozen_files", [])
        if isinstance(item, dict)
    }
    configs_dir = repro_project_dir / "configs"
    configs_dir.mkdir(parents=True, exist_ok=True)
    combined_requirements: list[str] = ["numpy", "matplotlib"]
    if foundation is not None:
        combined_requirements.clear()
    copied_task_files: dict[str, tuple[str, str]] = {}
    processed_sandboxes: set[str] = set()
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
                if source.relative_to(sandbox).as_posix() in canonical_frozen:
                    continue
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
        source_output = sandbox / "outputs" / output_subdir
        target_output = repro_project_dir / "outputs" / output_subdir
        if target_output.exists() or target_output.is_symlink():
            _remove_packaged_path(target_output)
        if source_output.exists():
            shutil.copytree(source_output, target_output, ignore=shutil.ignore_patterns("paper_target*"))
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
    if source.suffix.lower() == ".py":
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
    if previous is not None and previous[0] != content_hash:
        raise RuntimeError(
            "execution-unit package collision for "
            f"{relative}: {previous[1]} and {owner} supplied different content"
        )
    target = repro_project_dir / Path(relative)
    if source.suffix.lower() == ".py":
        _copy_python_without_bom(source, target)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    copied_files[relative] = (content_hash, owner)
    expected_paths.add(relative)

def _writer_package_files(sandbox: Path) -> list[Path]:
    """Return portable Writer-owned files outside task outputs and frozen science."""

    frozen_paths = {
        str(item.get("path") or "").replace("\\", "/")
        for item in _read_optional_json_object(sandbox / "foundation_manifest.json").get("frozen_files", [])
        if isinstance(item, dict)
    }
    excluded_roots = {
        ".git",
        ".geng_execution",
        ".geng_runtime",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        PAPER_EVIDENCE_DIR,
        "outputs",
        "repair_logs",
        "tasks",
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
        "foundation_manifest.json",
        "package_manifest.json",
        "project_manifest.json",
        "project_portability_manifest.json",
        "repro_project_manifest.json",
        "reproducibility_manifest.json",
        "requirements.txt",
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
        if any(part.casefold() in excluded_roots for part in relative_path.parts):
            continue
        relative = relative_path.as_posix()
        if relative in frozen_paths:
            continue
        if len(relative_path.parts) == 1 and relative in excluded_root_files:
            continue
        if relative.startswith("configs/foundation"):
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
                continue
            portable_path = PurePosixPath(raw_path)
            if (
                portable_path.is_absolute()
                or PureWindowsPath(raw_path).is_absolute()
                or PureWindowsPath(raw_path).drive
                or ".." in portable_path.parts
            ):
                if require_lineage:
                    raise RuntimeError(
                        f"material artifact {artifact_id!r} uses a non-portable path: {raw_path}"
                    )
                continue
            unit_id = str(record.get("execution_unit_id") or "").strip()
            asset_root = PurePosixPath("execution_units") / safe_label(unit_id)
            if (
                not unit_id
                or tuple(portable_path.parts[: len(asset_root.parts)])
                != asset_root.parts
            ):
                if require_lineage:
                    raise RuntimeError(
                        f"material artifact {artifact_id!r} must be persisted under "
                        f"{asset_root.as_posix()}/: {raw_path}"
                    )
                continue
            relative = Path(*portable_path.parts)
            source = sandbox / relative
            target = repro_project_dir / relative
            try:
                source.resolve().relative_to(sandbox.resolve())
                target.resolve().relative_to(repro_project_dir.resolve())
            except ValueError:
                if require_lineage:
                    raise RuntimeError(
                        f"material artifact {artifact_id!r} escapes its execution unit"
                    )
                continue
            if not source.is_file() or not target.is_file():
                if require_lineage:
                    raise RuntimeError(
                        f"material artifact {artifact_id!r} was not persisted into the final project: {raw_path}"
                    )
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
                raise RuntimeError(
                    f"material artifact {artifact_id!r} has conflicting packaged identities"
                )
            entries_by_artifact[artifact_id] = item

    dependencies: dict[tuple[str, str, str], dict[str, Any]] = {}
    raw_units = execution_plan.get("execution_units")
    for unit in (raw_units if isinstance(raw_units, list) else []):
        if not isinstance(unit, dict):
            continue
        raw_dependencies = unit.get("dependencies")
        for dependency in raw_dependencies if isinstance(raw_dependencies, list) else []:
            if not isinstance(dependency, dict):
                continue
            key = (
                str(dependency.get("artifact_id") or ""),
                str(dependency.get("producer_task_id") or ""),
                str(dependency.get("consumer_task_id") or ""),
            )
            if all(key):
                dependencies[key] = dependency
    strong_requirements: dict[str, dict[str, set[str]]] = {}
    for unit in (raw_units if isinstance(raw_units, list) else []):
        if not isinstance(unit, dict):
            continue
        relationships = unit.get("relationships")
        for relationship in relationships if isinstance(relationships, list) else []:
            if (
                not isinstance(relationship, dict)
                or str(relationship.get("strength") or "") != "strong"
            ):
                continue
            producer = str(relationship.get("producer_task_id") or "")
            consumers = {
                str(value)
                for value in relationship.get("consumer_task_ids", [])
                if str(value)
            } if isinstance(relationship.get("consumer_task_ids"), list) else set()
            if not consumers:
                consumers = {
                    str(value)
                    for value in relationship.get("task_ids", [])
                    if str(value) and str(value) != producer
                } if isinstance(relationship.get("task_ids"), list) else set()
            for artifact_id in (
                relationship.get("artifact_ids")
                if isinstance(relationship.get("artifact_ids"), list)
                else []
            ):
                key = str(artifact_id)
                if not key:
                    continue
                requirement = strong_requirements.setdefault(
                    key,
                    {"producers": set(), "consumers": set()},
                )
                if producer:
                    requirement["producers"].add(producer)
                requirement["consumers"].update(consumers)

    for (artifact_id, producer, consumer), dependency in dependencies.items():
        if str(dependency.get("strength") or "") == "strong":
            requirement = strong_requirements.setdefault(
                artifact_id,
                {"producers": set(), "consumers": set()},
            )
            requirement["producers"].add(producer)
            requirement["consumers"].add(consumer)

    for artifact_id, requirement in sorted(strong_requirements.items()):
        item = entries_by_artifact.get(artifact_id)
        if item is None:
            if require_lineage:
                raise RuntimeError(
                    f"strong relationship artifact {artifact_id!r} is missing from execution_unit_result.json"
                )
            continue
        producers = set(requirement["producers"])
        expected_producer = next(iter(producers)) if len(producers) == 1 else None
        if require_lineage and len(producers) > 1:
            raise RuntimeError(
                f"strong relationship artifact {artifact_id!r} has multiple producers"
            )
        if require_lineage and item.get("producer_task_id") not in {None, expected_producer}:
            raise RuntimeError(
                f"artifact {artifact_id!r} names producer {item.get('producer_task_id')!r}, expected {expected_producer!r}"
            )
        if item.get("producer_task_id") is None and expected_producer is not None:
            item["producer_task_id"] = expected_producer
        consumers = set(item.get("consumer_task_ids") or [])
        consumers.update(requirement["consumers"])
        item["consumer_task_ids"] = sorted(consumers)

    return {
        "schema_version": "1.0",
        "artifacts": [entries_by_artifact[key] for key in sorted(entries_by_artifact)],
        "strong_dependency_count": sum(
            1
            for dependency in dependencies.values()
            if str(dependency.get("strength") or "") == "strong"
        ),
        "strong_artifact_count": len(strong_requirements),
    }

def _writer_snapshot_hash(analysis_hash: str, foundation_hash: str) -> str:
    payload = f"{analysis_hash}::{foundation_hash}".encode("ascii")
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
