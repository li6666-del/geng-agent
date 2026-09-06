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

from .agentic_foundation import (
    _assert_foundation_sandbox_layout_safe,
    foundation_violations,
    install_foundation_snapshot,
    restore_foundation_snapshot,
    validate_foundation_bundle,
)
from .foundation_snapshot import path_is_foundation_link
from .verification_result import (
    FINAL_MATCHED_STATUS,
    WRITER_REVIEW_STATUS,
    partition_writer_delivery_issues,
    rerun_evidence_path_issues,
    task_verification_issues,
    verification_result_issues,
    writer_revision_allowed,
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
    _prepare_project_workspace,
    _prune_unexpected_files,
    _restore_trusted_files,
    _write_paper_evidence_bundle,
)
from .codex_runner import run_codex_subprocess
from .case_runtime import (
    CaseRuntime,
    EnvironmentRequestRequired,
    environment_request_prompt,
    read_environment_request,
    requirements_missing_from_lock,
)
from .case_environment import EnvironmentPolicyError, RequirementRequest
from .execution_plan import compile_execution_plan
from .foundation_revision import FoundationRevisionRequired, collect_pending_foundation_revisions
from .writer_lineage import build_writer_unit_lineage
from .config import get_config_value
from .io_runtime import BACKEND_RUNTIME_API_DOC, IO_RUNTIME_API_DOC, inject_io_runtime
from .json_utils import pretty_json
from .manifest_utils import expected_generated_paths
from .outputs import inspect_output_artifacts, validate_repro_project, write_json, write_text
from .paper_evidence import facts_for_task, paper_context_for_task, safe_label, thesis_ordering_anchor_for_task
from .project_portability import build_source_inventory, validate_repro_project_portability
from .security import (
    FOUNDATION_STATIC_SECURITY_ADVISORY_CATEGORIES,
    dependency_policy_prompt_text,
    redact_text,
    split_static_security_issues,
    split_requirement_issues,
    static_scan_repro_project,
    reconcile_runtime_requirements,
    validate_requirements,
)
from .scientific_materiality import CORE_RESULT_STOP_POLICY, TERMINAL_SCIENTIFIC_OUTCOMES
from .stage_cleanup import _clear_stage_outputs
from .task_scripts import build_tasks_manifest, write_task_scaffolding
from .task_writer_contracts import (
    DEFAULT_MAX_EVIDENCE_RERUNS,
    TASK_WRITER_TERMINAL_STATUS,
    WRITER_PAPER_FIDELITY_POLICY,
)
from .task_writer_delivery import _collect_task_writer_delivery, _collect_writer_images
from .task_writer_execution_binding import (
    _StaticCallScanner,
    _analyze_static_function,
    _analyze_static_module,
    _assigned_task_entrypoint,
    _canonical_static_symbol,
    _declared_callable_is_called,
    _dedupe_strings,
    _imports_from_local_module,
    _inspect_task_execution_source,
    _load_task_execution_binding,
    _normalize_python_module,
    _reachable_local_src_modules,
    _safe_task_source_path,
    _sandbox_evidence_source,
    _src_module_index,
    _static_callable_usage,
    _static_import_base,
    _static_reference,
    _task_execution_binding_from_architecture,
    _task_execution_binding_issues,
    _task_module_index,
    _walk_static_calls,
)
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
    _build_execution_unit_continuation_brief,
    _build_execution_unit_writer_brief,
    _build_task_writer_brief,
    _build_task_writer_continuation_brief,
)
from .task_writer_results import (
    _classify_task_writer_security_issues,
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
    _prepare_execution_unit_writer_sandbox,
    _prepare_task_writer_sandbox,
    _remove_legacy_writer_scoring_state,
    _write_minimal_shared_project_files,
)
from .task_writer_units import (
    _execution_unit_sandbox,
    _execution_unit_work_items,
    _public_execution_unit,
)
from .task_writer_state import (
    _active_writer_artifact_path,
    _archive_execution_unit_delivery,
    _archive_nonterminal_writer_delivery,
    _checkpoint_partial_task_writer_records,
    _complete_execution_unit_runtime_refresh,
    _complete_task_writer_runtime_refresh,
    _load_task_writer_resume_records,
    _move_writer_generation_to_archive,
    _next_writer_progress_round,
    _record_has_terminal_task_verification,
    _record_is_valid_current_delivery,
    _record_source_config_fingerprint,
    _rerun_evidence_fingerprint,
    _sandbox_analysis_handoff_hash,
    _task_environment_requests,
    _task_writer_record_refresh_pending,
    _task_writer_record_refresh_reusable,
    _task_writer_resume_layouts,
    _task_writer_resume_sandbox_is_safe,
    _task_writer_runtime_refresh_marker,
    _task_writer_runtime_refresh_pending,
    _terminalize_rerun_request,
    _trusted_preserved_evidence_file,
    _writer_source_config_fingerprint,
)
from .task_writer_runner import (
    _attach_task_reporter_review,
    _external_writer_rerun_budget,
    _run_one_execution_unit_writer,
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
    run_timeout: float = 120.0,
    resume: bool = True,
    review_feedback: dict[str, dict[str, Any]] | None = None,
    force_task_ids: set[str] | None = None,
    task_review_callback: Callable[[int, dict[str, Any], dict[str, Any], int], dict[str, Any]] | None = None,
    foundation: dict[str, Any] | None = None,
    case_runtime: CaseRuntime | None = None,
    execution_plan: dict[str, Any] | None = None,
    declined_foundation_revision_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Third-round autonomous per-task Codex writer workflow.

    Each task gets an isolated sandbox and one Codex writer that owns code,
    full execution, and task-level paper comparison. The host does not run a
    separate reviewer and does not repeat the full run after merging.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    audit_dir.mkdir(parents=True, exist_ok=True)
    del run_timeout

    execution_plan = (
        execution_plan
        if isinstance(execution_plan, dict)
        else compile_execution_plan(tasks)
    )
    # The Writer handoff hash must include the exact execution contract it is
    # about to follow.  Pipeline callers already persist this file; direct API
    # callers get the same authoritative artifact here.
    write_json(output_dir / "execution_plan.json", execution_plan)
    analysis_artifacts = _collect_writer_analysis_artifacts(output_dir=output_dir)
    missing_analysis_artifacts = _missing_required_analysis_artifacts(analysis_artifacts)
    if missing_analysis_artifacts:
        raise RuntimeError(
            "task writers require finalized first-two-stage artifacts: "
            + ", ".join(missing_analysis_artifacts)
        )
    if foundation is not None:
        foundation_issues = validate_foundation_bundle(foundation)
        if foundation_issues:
            raise RuntimeError(f"task writers require a valid Foundation snapshot: {foundation_issues[:5]}")
    analysis_handoff_hash = _analysis_snapshot_hash(
        paper_path=paper_path,
        artifacts=analysis_artifacts,
    )
    analysis_snapshot_hash = analysis_handoff_hash
    foundation_snapshot_hash = str(foundation["manifest"]["snapshot_hash"]) if foundation is not None else ""
    if foundation_snapshot_hash:
        analysis_snapshot_hash = _writer_snapshot_hash(analysis_snapshot_hash, foundation_snapshot_hash)
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

    def unit_lineage() -> dict[str, dict[str, Any]]:
        return build_writer_unit_lineage(
            task_pairs=task_pairs,
            execution_plan=execution_plan,
            facts=facts,
            experiment_index=experiment_index,
            paper_path=paper_path,
            analysis_artifacts=analysis_artifacts,
            foundation=foundation,
            case_runtime=case_runtime,
            task_root=task_root,
            paper_thesis=paper_thesis,
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
            if evidence_path.is_file() and not path_is_foundation_link(evidence_path):
                evidence = _read_optional_json_object(evidence_path)
                evidence["analysis_snapshot_hash"] = current["snapshot_hash"]
                write_json(evidence_path, evidence)
        write_json(audit_dir / "03c_writer_unit_lineage.json", lineage)
    expected_paths = expected_generated_paths([item["script"] for item in manifest_entries])
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
            declined_foundation_revision_ids=declined_foundation_revision_ids,
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
    cached_foundation_current = (
        foundation is None
        or not foundation_violations(repro_project_dir, foundation)
    )
    cached_writer_reusable = (
        resume
        and not force_task_ids
        and cached is not None
        and cached_all_current
        and cached_resume_all_current
        and cached_refresh_complete
        and cached_foundation_current
        and (not run_repro or (cached_runtime_passed and cached_all_deliveries))
    )
    refreshed_cached_records_by_index: dict[int, dict[str, Any]] = {}
    preserve_cached_report_assets = False
    if cached_writer_reusable:
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
                return cached
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
            return cached
    if refreshed_cached_records_by_index:
        # Carry the Reporter-refreshed records into dispatch. Unaffected
        # execution units remain reusable; only a unit containing a requested
        # Writer revision is forced into its existing continuation path.
        resume_records.update(refreshed_cached_records_by_index)

    _clear_stage_outputs(
        output_dir,
        "manifest",
        preserve_audit=bool(resume),
        preserve_paths={"report_assets"} if preserve_cached_report_assets else None,
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
        "orchestration": "launch_all_then_wait",
    }
    status["agent_concurrency"] = int(execution_plan.get("execution_unit_count") or 0)
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
        foundation=foundation,
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

    foundation_requests = collect_pending_foundation_revisions(
        task_records, foundation, declined_foundation_revision_ids
    )
    if foundation_requests:
        write_json(audit_dir / "03c_task_writers_records.json", {"dispatch_policy": dispatch_audit, "tasks": task_records})
        raise FoundationRevisionRequired(foundation_requests)

    pending_requests = _task_environment_requests(task_records)
    if pending_requests:
        write_json(
            audit_dir / "03c_task_writers_records.json",
            {"dispatch_policy": dispatch_audit, "tasks": task_records},
        )
        raise EnvironmentRequestRequired(
            pending_requests,
            source="task_writers",
        )

    _prepare_project_workspace(repro_project_dir, task_manifest)
    expected_paths = _merge_task_writer_deliveries(
        repro_project_dir=repro_project_dir,
        task_manifest=task_manifest,
        expected_paths=set(expected_paths),
        task_records=task_records,
        foundation=foundation,
        execution_plan=execution_plan,
        case_runtime=case_runtime,
        require_lineage=run_repro,
    )
    _restore_trusted_files(repro_project_dir, task_manifest)
    final_task_manifest = task_manifest
    write_json(repro_project_dir / "tasks_manifest.json", final_task_manifest)
    reconcile_runtime_requirements(
        repro_project_dir,
        runtime_policy=case_runtime.manifest if case_runtime is not None else None,
        runtime_lock=case_runtime.lock if case_runtime is not None else None,
    )
    validation = validate_repro_project(repro_project_dir)
    validation["host_validation_skipped"] = False
    requirement_findings = validate_requirements(
        repro_project_dir,
        runtime_policy=case_runtime.manifest if case_runtime is not None else None,
        runtime_lock=case_runtime.lock if case_runtime is not None else None,
    )
    requirement_issues, requirement_warnings = split_requirement_issues(
        requirement_findings,
        runtime_policy=case_runtime.manifest if case_runtime is not None else None,
        runtime_lock=case_runtime.lock if case_runtime is not None else None,
    )
    foundation_integrity_issues = (
        foundation_violations(repro_project_dir, foundation)
        if foundation is not None
        else []
    )
    if foundation is not None:
        validation["foundation_integrity_checked"] = True
        validation["foundation_integrity_ok"] = not foundation_integrity_issues
        validation["foundation_violations"] = foundation_integrity_issues
    security_issues = _classify_task_writer_security_issues(
        static_scan_repro_project(repro_project_dir),
        foundation=foundation,
        foundation_integrity_issues=foundation_integrity_issues,
    )
    syntax_issues = [
        issue for issue in security_issues if "syntax error" in str(issue.get("message") or "").lower()
    ]
    if syntax_issues:
        validation["python_compiles"] = False
        validation["compile_errors"] = syntax_issues
        validation["host_validation_skipped"] = False
    manifest, portability = _freeze_repro_project_package(
        repro_project_dir=repro_project_dir,
        output_dir=output_dir,
        audit_path=audit_dir / "03c_project_portability.json",
        task_manifest=final_task_manifest,
        expected_paths=expected_paths,
        analysis_snapshot_hash=analysis_snapshot_hash,
        foundation_snapshot_hash=foundation_snapshot_hash,
        environment_hash=environment_hash,
        run_smoke=bool(run_repro),
        python_executable=(
            case_runtime.python_executable if case_runtime is not None else None
        ),
    )
    validation["portable"] = bool(portability.get("portable"))
    validation["relocated_smoke"] = portability.get("smoke", {})

    runtime_result = _task_writer_runtime_result(
        task_records=task_records,
        validation=validation,
        requirement_warnings=requirement_warnings,
        requirement_issues=requirement_issues,
        security_issues=security_issues,
    )
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
        "written_files": [str(path) for path in _manifest_disk_paths(manifest, repro_project_dir)],
        "status": status,
    }
