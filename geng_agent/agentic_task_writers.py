from __future__ import annotations

import ast
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
import hashlib
import json
import shutil
import sys
import time
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Callable

from .artifact_paths import path_is_link
from .verification_result import (
    FINAL_MATCHED_STATUS,
    WRITER_REVIEW_STATUS,
    partition_writer_delivery_issues,
    rerun_evidence_path_issues,
    task_verification_issues,
    writer_delivery_issues,
)
from .task_writer_support import (
    ANALYSIS_ARTIFACT_DIR,
    PAPER_EVIDENCE_DIR,
    CODEX_PROJECT_BACKEND,
    WRITER_ANALYSIS_SCHEMA_VERSION,
    WRITER_HANDOFF_POLICY_VERSION,
    WRITER_OPTIONAL_ANALYSIS_ARTIFACTS,
    WRITER_REQUIRED_ANALYSIS_ARTIFACTS,
    _analysis_snapshot_hash,
    _collect_writer_analysis_artifacts,
    _load_cached_task_writer_workflow,
    _manifest_from_project,
    _manifest_disk_paths,
    _missing_required_analysis_artifacts,
    _prune_unexpected_files,
    _write_paper_evidence_bundle,
)
from .codex_runner import run_codex_subprocess
from .case_runtime import (
    CaseRuntime,
    EnvironmentRequestRequired,
    environment_request_prompt,
    read_environment_request,
)
from .case_environment import EnvironmentPolicyError, RequirementRequest
from .execution_plan import compile_execution_plan
from .writer_lineage import build_writer_unit_lineage, writer_policy_content_hashes
from .config import get_config_value
from .supervisor import StageBlocked, REPLAY_REQUIRED, supervised_call
from .io_runtime import BACKEND_RUNTIME_API_DOC, IO_RUNTIME_API_DOC, inject_io_runtime
from .json_utils import pretty_json
from .outputs import inspect_output_artifacts, validate_repro_project, write_json, write_text
from .paper_evidence import facts_for_task, paper_context_for_task, safe_label, thesis_ordering_anchor_for_task
from .project_portability import build_source_inventory, validate_repro_project_portability
from .security import (
    dependency_policy_prompt_text,
    redact_text,
    split_static_security_issues,
)
from .scientific_materiality import CORE_RESULT_STOP_POLICY, TERMINAL_SCIENTIFIC_OUTCOMES
from .stage_cleanup import _clear_stage_outputs
from .task_scripts import build_tasks_manifest, write_task_scaffolding
from .task_writer_contracts import TASK_WRITER_TERMINAL_STATUS, WRITER_PAPER_FIDELITY_POLICY
from .task_writer_delivery import _collect_task_writer_delivery, _collect_writer_images
from .task_writer_execution_binding import _load_task_execution_binding, _task_execution_binding_from_architecture

from .task_writer_files import (
    _read_optional_json_object,
    _task_owned_files,
    _task_result_file_path,
    _task_source_files,
    _writer_delivery_path_is_fresh,
)
from .task_writer_packaging import (
    _build_artifact_lineage,
    _clear_previous_packaged_runtime_files,
    _copy_merged_writer_file,
    _copy_python_without_bom,
    _expected_paths_from_project_manifest,
    _format_requirements,
    _freeze_repro_project_package,
    _merge_task_writer_deliveries,
    _package_task_directories,
    _portable_environment_lock,
    _read_requirement_names,
    _remove_packaged_path,
    _streaming_file_sha256,
    _task_manifest_with_configs,
    _write_final_shared_project_files,
    _writer_package_files,
    _writer_snapshot_hash,
)
from .task_writer_prompts import (
    _build_task_writer_brief,
    _build_task_writer_continuation_brief,
)
from .task_writer_results import (
    _compact_task_writer_review,
    _task_writer_alignment_summary,
    _task_writer_blocked_by_codex,
    _task_writer_runtime_result,
    _task_writer_runtime_task_passed,
    _task_writer_stop_class,
    _task_writer_stopped_reason,
    apply_verified_result,
)
from .task_writer_sandbox import (
    _ensure_unit_asset_namespace,
    _prepare_task_writer_sandbox,
    _remove_legacy_writer_scoring_state,
    _write_minimal_shared_project_files,
)
from .task_writer_units import (
    _execution_unit_sandbox,
    _execution_unit_work_items,
    _public_execution_unit,
)
from .task_writer_state import _archive_nonterminal_writer_delivery, _checkpoint_partial_task_writer_records, _complete_task_writer_runtime_refresh, _load_task_writer_resume_records, _move_writer_generation_to_archive, _next_writer_progress_round, _record_has_terminal_task_verification, _record_is_valid_current_delivery, _sandbox_analysis_handoff_hash, _task_writer_record_refresh_pending, _task_writer_record_refresh_reusable, _task_writer_resume_layouts, _task_writer_resume_sandbox_is_safe, _task_writer_runtime_refresh_marker, _task_writer_runtime_refresh_pending, _terminalize_rerun_request, _trusted_preserved_evidence_file
from .task_writer_runner import (
    _attach_task_reporter_review,
    _run_one_task_writer,
    _run_task_writer_codex_session,
    _task_with_experiment_profile,
)
from .task_writer_dispatch import (
    _dispatch_task_writers,
    _failed_task_record,
    _refresh_cached_task_reporters,
    _reporter_callback_with_replay,
    _task_writer_concurrency,
)


def _final_package_file_validation(
    *, repro_project_dir: Path, expected_paths: set[str], validation: dict[str, Any],
) -> dict[str, Any]:
    """Refresh file-existence observations after assembly, without a science verdict."""
    def present(relative: Any) -> bool:
        if not isinstance(relative, str) or not relative:
            return False
        path = PurePosixPath(relative.replace("\\", "/"))
        if path.is_absolute() or PureWindowsPath(relative).drive or ".." in path.parts:
            return False
        return (repro_project_dir / relative).is_file()

    observations = list(validation.get("observations") or [])
    declared = set(expected_paths)
    declared.update(path for path in (validation.get("missing_files") or []) if isinstance(path, str))
    declared.update(item["path"] for item in observations if isinstance(item, dict)
                    and item.get("code") == "declared_package_file_missing"
                    and isinstance(item.get("path"), str))
    missing = sorted(path for path in declared if not present(path))
    observations = [item for item in observations if not (
        isinstance(item, dict) and item.get("code") == "declared_package_file_missing"
        and present(item.get("path")))]
    recorded = {item.get("path") for item in observations if isinstance(item, dict)
                and item.get("code") == "declared_package_file_missing"}
    observations.extend({"code": "declared_package_file_missing", "path": path}
                        for path in missing if path not in recorded)
    return {**validation, "required_files_present": not missing,
            "missing_files": missing, "observations": observations}


def _refresh_cached_package_validation(
    *, cached: dict[str, Any], repro_project_dir: Path, output_dir: Path, audit_dir: Path,
) -> dict[str, Any]:
    """Refresh final delivery metadata after the existing cache integrity check."""
    runtime = dict(cached.get("runtime_result") or {})
    validation = _final_package_file_validation(
        repro_project_dir=repro_project_dir,
        expected_paths=_expected_paths_from_project_manifest(cached.get("manifest") or {}),
        validation=dict(runtime.get("validation") or {}),
    )
    runtime["validation"] = validation
    cached["runtime_result"] = runtime
    status_path = audit_dir / "03c_task_writers_status.json"
    status = {**_read_optional_json_object(status_path), **(cached.get("status") or {}),
              "validation": validation}
    cached["status"] = status
    write_json(output_dir / "runtime_result.json", runtime)
    write_json(status_path, status)
    for name in ("03c_project_portability.json", "03c_project_portability_final.json"):
        path = audit_dir / name
        if not path.is_file() or path_is_link(path):
            continue
        record = _read_optional_json_object(path)
        refreshed = _final_package_file_validation(
            repro_project_dir=repro_project_dir, expected_paths=set(),
            validation={"observations": record.get("observations") or []},
        )
        if record.get("observations") != refreshed["observations"]:
            write_json(path, {**record, "observations": refreshed["observations"]})
    return cached


def _commit_cached_task_reporter_refresh(
    *,
    audit_dir: Path,
    cached_records: list[dict[str, Any]],
    reporter_refresh_audit: dict[str, Any],
    cached_status: dict[str, Any] | None,
) -> dict[str, Any]:
    """Atomically replace the authoritative Writer/Reporter audit envelopes."""

    records_path = audit_dir / "03c_task_writers_records.json"
    previous_records = _read_optional_json_object(records_path)
    records_document: dict[str, Any] = {
        "checkpoint": "cached_writer_reporters_revalidated",
        "reporter_refresh": reporter_refresh_audit,
        "previous_verification_superseded": isinstance(
            previous_records.get("verification_result"),
            dict,
        ),
        "tasks": cached_records,
    }
    if isinstance(previous_records.get("dispatch_policy"), dict):
        records_document["dispatch_policy"] = previous_records["dispatch_policy"]
    write_json(records_path, records_document)

    actions_by_id = {
        str(item.get("task_id") or ""): item
        for item in reporter_refresh_audit.get("actions", [])
        if isinstance(item, dict) and str(item.get("task_id") or "")
    }
    all_reporters_terminal = bool(cached_records) and all(
        item.get("action") == "terminal" for item in actions_by_id.values()
    ) and len(actions_by_id) == len(cached_records)
    reporter_outcomes = [
        record.get("task_verification", {}).get("outcome")
        for record in cached_records
        if isinstance(record.get("task_verification"), dict)
    ]
    all_reporters_successful = (
        all_reporters_terminal
        and len(reporter_outcomes) == len(cached_records)
        and all(
            outcome in {"reproduced", "reproduced_with_assumptions"}
            for outcome in reporter_outcomes
        )
    )
    status_path = audit_dir / "03c_task_writers_status.json"
    status = _read_optional_json_object(status_path)
    status.update(cached_status if isinstance(cached_status, dict) else {})
    status.update(
        {
            "backend": CODEX_PROJECT_BACKEND,
            "mode": "task_writers",
            "cached": True,
            "cached_writer_reused": True,
            "task_reporters_revalidated": True,
            "reporter_refresh_complete": True,
            "reporter_refresh_audit": "03c_cached_task_reporters.json",
            "stop_class": (
                "verified_matched"
                if all_reporters_successful
                else (
                    "verified_terminal"
                    if all_reporters_terminal
                    else "reporter_refresh_pending_host_terminalization"
                )
            ),
            "stopped_reason": (
                "all cached-Writer task Reporters reproduced their core conclusions"
                if all_reporters_successful
                else (
                    "all cached-Writer task Reporters reached reportable scientific outcomes"
                    if all_reporters_terminal
                    else "one or more cached-Writer task Reporters require host terminalization"
                )
            ),
            "tasks": [
                {
                    "task_id": record.get("task_id"),
                    "status": record.get("task_writer_status"),
                    "writer_completed": record.get("writer_completed"),
                    "task_reporter_action": actions_by_id.get(
                        str(record.get("task_id") or ""),
                        {},
                    ).get("action"),
                    "task_reporter_cached": actions_by_id.get(
                        str(record.get("task_id") or ""),
                        {},
                    ).get("reporter_cached"),
                    "task_reporter_outcome": (
                        record.get("task_verification", {}).get("outcome")
                        if isinstance(record.get("task_verification"), dict)
                        else None
                    ),
                }
                for record in cached_records
            ],
        }
    )
    write_json(status_path, status)
    return status


def run_codex_task_writer_workflow(
    *,
    facts: dict[str, Any],
    tasks: dict[str, Any],
    experiment_index: dict[str, Any],
    paper: dict[str, Any],
    paper_path: Path,
    paper_context_json: str,
    paper_images: list[Any] | None,
    paper_thesis: dict[str, Any] | None,
    output_dir: Path,
    audit_dir: Path,
    repro_project_dir: Path,
    run_repro: bool,
    resume: bool = True,
    review_feedback: dict[str, dict[str, Any]] | None = None,
    force_task_ids: set[str] | None = None,
    task_review_callback: Callable[[int, dict[str, Any], dict[str, Any], int], dict[str, Any]] | None = None,
    case_runtime: CaseRuntime | None = None,
    execution_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Third-round autonomous per-task Codex writer workflow.

    Each task gets an isolated sandbox and one Codex writer that owns code,
    full execution, and task-level paper comparison. The host does not run a
    separate reviewer and does not repeat the full run after merging.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    audit_dir.mkdir(parents=True, exist_ok=True)

    execution_plan = compile_execution_plan(tasks)
    # The Writer handoff hash must include the exact execution contract it is
    # about to follow.  Pipeline callers already persist this file; direct API
    # callers get the same authoritative artifact here.
    write_json(output_dir / "execution_plan.json", execution_plan)
    analysis_artifacts = _collect_writer_analysis_artifacts(output_dir=output_dir)
    analysis_handoff_hash = _analysis_snapshot_hash(
        paper_path=paper_path,
        artifacts=analysis_artifacts,
    )
    analysis_snapshot_hash = analysis_handoff_hash
    environment_hash = case_runtime.environment_hash if case_runtime is not None else ""
    if environment_hash:
        analysis_snapshot_hash = _writer_snapshot_hash(analysis_snapshot_hash, environment_hash)

    task_manifest = _task_manifest_with_configs(
        build_tasks_manifest(tasks, execution_plan=execution_plan)
    )
    task_items = [task for task in tasks.get("repro_tasks", []) if isinstance(task, dict)]
    manifest_entries = [entry for entry in task_manifest.get("tasks", []) if isinstance(entry, dict)]
    task_pairs = list(zip(task_items, manifest_entries))
    task_root = audit_dir / "03c_task_writer_sandboxes"
    # Finalization may discover runtime imports, but it must keep the policy
    # that this invocation handed to its Writers. A new invocation reads anew.
    workflow_policy_hashes = writer_policy_content_hashes()

    def unit_lineage() -> dict[str, dict[str, Any]]:
        return build_writer_unit_lineage(
            task_pairs=task_pairs,
            execution_plan=execution_plan,
            facts=facts,
            experiment_index=experiment_index,
            paper_path=paper_path,
            analysis_artifacts=analysis_artifacts,
            case_runtime=case_runtime,
            task_root=task_root,
            paper_thesis=paper_thesis,
            policy_content_hashes=workflow_policy_hashes,
        )

    lineage = unit_lineage()
    snapshot_hashes = {key: value["snapshot_hash"] for key, value in lineage.items()}
    write_json(audit_dir / "03c_writer_unit_lineage.json", lineage)

    def finalize_unit_snapshot(unit: dict[str, Any], records: list[dict[str, Any]]) -> None:
        # Writers can discover additional actual imports during implementation.
        # Record that consumed runtime closure before the partial checkpoint so
        # the first resume does not invalidate a just-completed unit.
        unit_id = str(unit["unit_id"])
        current = unit_lineage()[unit_id]
        lineage[unit_id] = current
        snapshot_hashes[unit_id] = str(current["snapshot_hash"])
        for record in records:
            record["analysis_snapshot_hash"] = current["snapshot_hash"]
            record["unit_lineage_policy"] = current["inputs"]["policy"][0]
            sandbox = Path(str(record.get("sandbox") or ""))
            evidence_path = sandbox / PAPER_EVIDENCE_DIR / "index.json"
            if evidence_path.is_file() and not path_is_link(evidence_path):
                evidence = _read_optional_json_object(evidence_path)
                evidence["analysis_snapshot_hash"] = current["snapshot_hash"]
                write_json(evidence_path, evidence)
        write_json(audit_dir / "03c_writer_unit_lineage.json", lineage)


    review_feedback = dict(review_feedback or {})
    force_task_ids = {str(item) for item in (force_task_ids or set()) if str(item)}

    cached = _load_cached_task_writer_workflow(
        output_dir=output_dir,
        repro_project_dir=repro_project_dir,
        run_repro=run_repro,
        analysis_snapshot_hash=analysis_snapshot_hash,
    )
    cached_runtime_passed = bool((cached or {}).get("runtime_result", {}).get("passed"))
    cached_records = cached.get("task_records") if isinstance((cached or {}).get("task_records"), list) else []
    cached_task_ids = [str(record.get("task_id") or "") for record in cached_records]
    expected_task_ids = {
        str(task.get("task_id") or entry.get("task_id") or f"task_{index}")
        for index, (task, entry) in enumerate(task_pairs, start=1)
    }
    cached_all_current = (
        len(cached_records) == len(expected_task_ids)
        and len(set(cached_task_ids)) == len(cached_task_ids)
        and set(cached_task_ids) == expected_task_ids
    )
    resume_records = (
        _load_task_writer_resume_records(
            audit_dir=audit_dir,
            task_pairs=task_pairs,
            expected_analysis_snapshot_hash=analysis_snapshot_hash,
            expected_analysis_handoff_hash=analysis_handoff_hash,
            execution_plan=execution_plan,
            expected_snapshot_hashes=snapshot_hashes,
            require_execution_receipts=run_repro,
        )
        if resume
        else {}
    )
    validated_cached_records = [
        resume_records[index]
        for index in range(1, len(task_pairs) + 1)
        if index in resume_records
    ]
    cached_resume_all_current = (
        len(validated_cached_records) == len(task_pairs)
        and {
            str(record.get("task_id") or "")
            for record in validated_cached_records
        }
        == expected_task_ids
    )
    if cached_resume_all_current:
        # The aggregate package cache validates the frozen project. Reporter
        # inputs additionally require the host-validated task sandbox paths
        # and handoff hashes from the resume loader.
        cached_records = validated_cached_records
    cached_all_deliveries = cached_all_current and all(
        _record_is_valid_current_delivery(record) for record in cached_records
    )
    cached_refresh_complete = cached_all_current and all(
        _task_writer_record_refresh_reusable(record) for record in cached_records
    )
    from .task_inputs import dependencies, upstream_input_identity
    cached_by_task = {str(record.get("task_id")): record for record in cached_records}
    cached_inputs_current = all(
        not dependencies(task) or cached_by_task.get(str(task.get("task_id")), {}).get("upstream_input_identity")
        == upstream_input_identity(task, cached_records)
        for task, _entry in task_pairs)
    cached_writer_reusable = (
        resume
        and not force_task_ids
        and cached is not None
        and cached_all_current
        and cached_resume_all_current
        and cached_refresh_complete
        and cached_inputs_current
        and (not run_repro or (cached_runtime_passed and cached_all_deliveries))
    )
    refreshed_cached_records_by_index: dict[int, dict[str, Any]] = {}
    preserve_cached_report_assets = False
    if cached_writer_reusable:
        from .agent_activity import record_agent_cached

        for sandbox in sorted({str(record.get("sandbox") or "") for record in cached_records}):
            if sandbox:
                record_agent_cached(role="task_writer", label=Path(sandbox).name, work_dir=Path(sandbox))
        reporter_refresh_audit: dict[str, Any] | None = None
        if task_review_callback is not None:
            (
                cached_records,
                replay_by_task_id,
                reporter_revisions,
                reporter_refresh_audit,
            ) = _refresh_cached_task_reporters(
                task_pairs=task_pairs,
                cached_records=cached_records,
                experiment_index=experiment_index,
                task_review_callback=task_review_callback,
            )
            cached["task_records"] = cached_records
            write_json(
                audit_dir / "03c_cached_task_reporters.json",
                reporter_refresh_audit,
            )

            if reporter_revisions:
                # Only an evidence-backed Reporter revision re-enters the
                # existing Writer continuation state machine. Replay the
                # already obtained Reporter decisions once so no Reporter is
                # launched twice against the same cached scientific delivery.
                review_feedback.update(reporter_revisions)
                force_task_ids.update(reporter_revisions)
                task_review_callback = _reporter_callback_with_replay(
                    task_review_callback,
                    replay_by_task_id,
                )
                refreshed_cached_records_by_index = {
                    int(record.get("index") or index): record
                    for index, record in enumerate(cached_records, start=1)
                }
                preserve_cached_report_assets = True
            else:
                cached["writer_review_doc"] = {
                    "_meta": {"mode": "task_writer_scientific_results"},
                    **_task_writer_alignment_summary(cached_records),
                    "task_writer_reviews": [
                        _compact_task_writer_review(record)
                        for record in cached_records
                    ],
                }
                refreshed_status = {
                    **(
                        cached.get("status")
                        if isinstance(cached.get("status"), dict)
                        else {}
                    ),
                    "cached": True,
                    "cached_writer_reused": True,
                    "task_reporters_revalidated": True,
                }
                cached["status"] = _commit_cached_task_reporter_refresh(
                    audit_dir=audit_dir,
                    cached_records=cached_records,
                    reporter_refresh_audit=reporter_refresh_audit,
                    cached_status=refreshed_status,
                )
                write_json(
                    audit_dir / "03c_task_writers_resume.json",
                    {
                        "ok": True,
                        "source": "cached_writer_with_independent_reporter_validation",
                        "reporter_refresh": reporter_refresh_audit,
                    },
                )
                return _refresh_cached_package_validation(
                    cached=cached, repro_project_dir=repro_project_dir,
                    output_dir=output_dir, audit_dir=audit_dir,
                )
        else:

            cached["writer_review_doc"] = {
                "_meta": {"mode": "task_writer_scientific_results"},
                **_task_writer_alignment_summary(cached_records),
                "task_writer_reviews": [
                    _compact_task_writer_review(record) for record in cached_records
                ],
            }
            write_json(
                audit_dir / "03c_task_writers_resume.json",
                {"ok": True, "source": "cached artifacts"},
            )
            return _refresh_cached_package_validation(
                cached=cached, repro_project_dir=repro_project_dir,
                output_dir=output_dir, audit_dir=audit_dir,
            )
    if refreshed_cached_records_by_index:
        # Carry the Reporter-refreshed records into dispatch. Unaffected
        # execution units remain reusable; only a unit containing a requested
        # Writer revision is forced into its existing continuation path.
        resume_records.update(refreshed_cached_records_by_index)

    _clear_stage_outputs(
        output_dir,
        "manifest",
        preserve_audit=bool(resume),
        preserve_paths=({"repro_project", "report_assets"}
                        if preserve_cached_report_assets or resume_records else {"repro_project"}),
    )

    task_root = audit_dir / "03c_task_writer_sandboxes"
    if task_root.exists() and not resume:
        shutil.rmtree(task_root)
    task_root.mkdir(parents=True, exist_ok=True)
    status: dict[str, Any] = {
        "backend": CODEX_PROJECT_BACKEND,
        "mode": "task_writers",
        "stop_rule": (
            "terminal_scientific_outcome_or_external_blocker"
            if task_review_callback is not None
            else "ready_for_review_or_external_blocker"
        ),
        "run_repro": bool(run_repro),
        "task_count": len(task_pairs),
        "logical_task_count": len(task_pairs),
        "execution_unit_count": int(execution_plan.get("execution_unit_count") or 0),
        "orchestration": "supervisor_tool_dispatch",
    }
    status["agent_concurrency"] = int(execution_plan.get("execution_unit_count") or 0)
    status["agent_concurrency_kind"] = "planned_writer_units"
    status["planned_task_reporter_count"] = len(task_pairs) if task_review_callback is not None else 0
    status["actual_agent_activity"] = "agent_activity.json"
    write_json(audit_dir / "03c_task_writers_start.json", status)
    task_records, dispatch_audit = _dispatch_task_writers(
        task_pairs=task_pairs,
        facts=facts,
        experiment_index=experiment_index,
        paper=paper,
        paper_path=paper_path,
        paper_context_json=paper_context_json,
        paper_images=paper_images,
        paper_thesis=paper_thesis,
        analysis_snapshot_hash=analysis_snapshot_hash,
        analysis_artifacts=analysis_artifacts,
        task_root=task_root,
        audit_dir=audit_dir,
        run_repro=run_repro,
        initial_records_by_index=resume_records,
        review_feedback=review_feedback,
        force_task_ids=force_task_ids,
        task_review_callback=task_review_callback,
        case_runtime=case_runtime,
        execution_plan=execution_plan,
        snapshot_hashes=snapshot_hashes,
        snapshot_finalizer=finalize_unit_snapshot,
    )
    write_json(audit_dir / "writer_dispatch.json", dispatch_audit)


    shared_runtime_refresh = any(
        record.get("writer_error_kind") == "environment_refresh" for record in task_records
    )
    if shared_runtime_refresh:
        write_json(
            audit_dir / "03c_task_writers_records.json",
            {"dispatch_policy": dispatch_audit, "tasks": task_records},
        )
        pending_validation = {"required_files_present": False, "python_compiles": None,
                              "host_validation_skipped": True, "packaging_completed": False}
        pending_runtime = _task_writer_runtime_result(
            task_records=task_records, validation=pending_validation,
            requirement_warnings=[], requirement_issues=[], security_issues=[],
        )
        pending_runtime["delivery_status"] = "partial"
        raise EnvironmentRequestRequired(
            [],
            source="shared_runtime_refresh",
            partial_result={
                "manifest": {"_meta": {"mode": "task_writers", "packaging_completed": False},
                             "files": [], "tasks": task_manifest.get("tasks", [])},
                "task_records": task_records, "runtime_result": pending_runtime,
                "written_files": [], "writer_review_doc": {
                    **_task_writer_alignment_summary(task_records),
                    "task_writer_reviews": [_compact_task_writer_review(record) for record in task_records]},
                "status": {**status, "stop_class": "pending_environment",
                           "validation": pending_validation},
            },
        )

    def assemble_project():
        packaged_paths, packaged_manifest, portability = _package_task_directories(
            repro_project_dir=repro_project_dir,
            output_dir=output_dir, audit_dir=audit_dir,
            task_manifest=task_manifest, task_records=task_records,
            execution_plan=execution_plan,
            case_runtime=case_runtime, analysis_snapshot_hash=analysis_snapshot_hash,
            environment_hash=environment_hash, require_lineage=run_repro,
        )
        validation = _final_package_file_validation(
            repro_project_dir=repro_project_dir, expected_paths=packaged_paths,
            validation={"python_compiles": None, "host_validation_skipped": True,
                        "packaging_completed": True, "portable": bool(portability.get("portable")),
                        "observations": []},
        )
        return packaged_paths, packaged_manifest, portability, validation, [], [], []

    def continue_after_packaging_failure(decision, error):
        failure = {"node_id": "packaging", "decision": decision,
                   "error": f"{type(error).__name__}: {error}",
                   "package_layout_attempted": "task_directories"}
        write_json(audit_dir / "03c_packaging_blocked.json", failure)
        return (set(),
                {"_meta": {"mode": "task_writers", "packaging_completed": False,
                           "delivery_blocked": failure}, "files": [],
                 "tasks": task_manifest.get("tasks", [])},
                {"portable": False, "delivery_blocked": failure},
                {"required_files_present": False, "python_compiles": None,
                 "host_validation_skipped": True, "packaging_completed": False,
                 "portable": False, "delivery_blocked": failure},
                [], [], [])

    delivery_blocked = None
    try:
        expected_paths, manifest, portability, validation, requirement_warnings, requirement_issues, security_issues = supervised_call(
            "packaging", assemble_project,
            inputs={"owner": "packaging", "analysis_snapshot_hash": analysis_snapshot_hash,
                    "environment_hash": environment_hash,
                    "task_manifest": task_manifest,
                    "task_results": [{key: record.get(key) for key in
                        ("task_id", "writer_completed", "task_verification", "execution_summary", "coordination_status", "coordination_observations")}
                        for record in task_records],
                    "instruction": "Assemble only existing verified artifacts. Retain failed or unreproduced tasks; do not rerun scientific experiments to repair delivery."},
            evidence_roots={"project": repro_project_dir,
                            "writers": audit_dir / "03c_task_writer_sandboxes",
                            "package_stages": audit_dir / "pkg_stages",
                            "package_audits": audit_dir / "pkg_task_manifests"},
            summarize=lambda value: {"manifest": value[1], "portability": value[2], "validation": value[3]},
            degrade=continue_after_packaging_failure,
            reconcile=lambda _state: REPLAY_REQUIRED,
            passthrough=(EnvironmentRequestRequired,),
        )
        if isinstance(portability.get("delivery_blocked"), dict):
            delivery_blocked = portability["delivery_blocked"]
    except StageBlocked as exc:
        # Keep current-run verified scientific results available to the editor.
        # A broken assembled tree must never masquerade as a portable delivery.
        delivery_blocked = {"node_id": exc.node_id, "decision": exc.decision, "error": str(exc)}
        write_json(audit_dir / "03c_packaging_blocked.json", delivery_blocked)
        manifest = {"_meta": {"mode": "task_writers", "delivery_blocked": delivery_blocked},
                    "files": [], "tasks": task_manifest.get("tasks", [])}
        portability = {"portable": False, "delivery_blocked": delivery_blocked}
        validation = {"required_files_present": False, "python_compiles": None,
                      "host_validation_skipped": True, "portable": False,
                      "delivery_blocked": delivery_blocked}
        requirement_warnings, requirement_issues, security_issues = [], [], []

    runtime_result = _task_writer_runtime_result(
        task_records=task_records,
        validation=validation,
        requirement_warnings=requirement_warnings,
        requirement_issues=requirement_issues,
        security_issues=security_issues,
    )
    if delivery_blocked is not None:
        runtime_result["delivery_status"] = "partial"
        runtime_result["engineering_failures"] = [delivery_blocked]
        runtime_result["partial_success"]["has_partial_output"] = bool(task_records)
    write_json(output_dir / "runtime_result.json", runtime_result)
    alignment_summary = _task_writer_alignment_summary(task_records)
    writer_review_doc = {
        "_meta": {"mode": "task_writer_scientific_results"},
        **alignment_summary,
        "task_writer_reviews": [_compact_task_writer_review(record) for record in task_records],
    }

    write_json(
        audit_dir / "03c_task_writers_records.json",
        {"dispatch_policy": dispatch_audit, "tasks": task_records},
    )
    status.update(
        {
            "stop_class": _task_writer_stop_class(task_records),
            "stopped_reason": _task_writer_stopped_reason(task_records),
            "validation": validation,
            "runtime": {
                "passed": runtime_result.get("passed"),
                "coverage": runtime_result.get("coverage"),
            },
            "tasks": [
                {
                    "task_id": record.get("task_id"),
                    "status": record.get("task_writer_status"),
                    "writer_completed": record.get("writer_completed"),
                    "writer_error_kind": record.get("writer_error_kind"),
                    "blocked_reason": record.get("blocked_reason"),
                    "task_reporter_outcome": (
                        record.get("task_verification", {}).get("outcome")
                        if isinstance(record.get("task_verification"), dict)
                        else None
                    ),
                }
                for record in task_records
            ],
        }
    )
    write_json(audit_dir / "03c_task_writers_status.json", status)
    return {
        "manifest": manifest,
        "runtime_result": runtime_result,
        "task_records": task_records,
        "writer_review_doc": writer_review_doc,
        "delivery_blocked": delivery_blocked,
        "written_files": [str(path) for path in _manifest_disk_paths(manifest, repro_project_dir)],
        "status": status,
    }
