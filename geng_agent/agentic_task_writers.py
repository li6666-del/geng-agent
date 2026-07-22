from __future__ import annotations

import ast
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
import hashlib
import json
import shutil
import sys
from pathlib import Path
from typing import Any, Callable

from .agentic_foundation import (
    foundation_violations,
    install_foundation_snapshot,
    restore_foundation_snapshot,
    validate_foundation_bundle,
)
from .verification_result import (
    FINAL_MATCHED_STATUS,
    WRITER_REVIEW_STATUS,
    partition_writer_delivery_issues,
    verification_result_issues,
    writer_delivery_issues,
)
from .task_writer_support import (
    PAPER_EVIDENCE_DIR,
    CODEX_PROJECT_BACKEND,
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
from .codex_runner import DEFAULT_CODEX_TIMEOUT_SECONDS, run_codex_subprocess
from .config import get_config_value
from .io_runtime import BACKEND_RUNTIME_API_DOC, IO_RUNTIME_API_DOC, inject_io_runtime
from .json_utils import pretty_json
from .manifest_utils import expected_generated_paths
from .outputs import inspect_output_artifacts, validate_repro_project, write_json, write_text
from .paper_evidence import facts_for_task, paper_context_for_task, safe_label, thesis_ordering_anchor_for_task
from .security import (
    dependency_policy_prompt_text,
    redact_text,
    static_scan_repro_project,
    validate_requirements,
)
from .stage_cleanup import _clear_stage_outputs
from .task_scripts import build_tasks_manifest, write_task_scaffolding


TASK_WRITER_TERMINAL_STATUS = WRITER_REVIEW_STATUS

WRITER_PAPER_FIDELITY_POLICY = """## Highest law: fidelity to the paper's established facts
Fidelity outranks visual closeness, convenience, prior code, and reporter advice in both an initial implementation and every repair session.

- Treat paper-explicit data, system models, equations, algorithm steps, experiment protocols, baseline identities, metric definitions, axes, and stated scan ranges as immutable constraints. Do not alter, replace, or bypass them merely to make a curve look closer to the target.
- Use this evidence priority: explicit paper statements and figures; deterministic derivations from them; figure-level visual estimates; standard domain assumptions; target-informed calibration; reporter suggestions. Lower-priority evidence may fill a genuine gap but may never overwrite higher-priority evidence.
- When the paper is silent, incomplete, or genuinely ambiguous, make a bold but scientifically plausible implementation or value assumption. Label it `assumed`, explain why it is reasonable, keep it separate from paper facts, and revise it when comparison evidence warrants that.
- An assumed algorithm is acceptable only as an implementation completion for an unspecified step. It may not replace a model, data-generating law, objective, or core algorithm that the paper already defines.
- Reporter feedback is evidence to investigate, not authority over the paper. Reject or reclassify feedback that conflicts with explicit paper evidence. Preserve the faithful main result and keep any conflicting figure-fitting alternative clearly labeled as a diagnostic branch.
"""


def _task_with_experiment_profile(task: dict[str, Any], experiment_index: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(task)
    task_id = str(task.get("task_id") or "")
    experiments = experiment_index.get("experiments") if isinstance(experiment_index, dict) else []
    for experiment in experiments if isinstance(experiments, list) else []:
        if not isinstance(experiment, dict) or str(experiment.get("task_id") or "") != task_id:
            continue
        enriched["experiment_id"] = experiment.get("experiment_id") or task_id
        break
    return enriched


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
    timeout: float = DEFAULT_CODEX_TIMEOUT_SECONDS,
    run_timeout: float = 120.0,
    resume: bool = True,
    review_feedback: dict[str, dict[str, Any]] | None = None,
    force_task_ids: set[str] | None = None,
    task_review_callback: Callable[[int, dict[str, Any], dict[str, Any], int], dict[str, Any]] | None = None,
    foundation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Third-round autonomous per-task Codex writer workflow.

    Each task gets an isolated sandbox and one Codex writer that owns code,
    full execution, and task-level paper comparison. The host does not run a
    separate reviewer and does not repeat the full run after merging.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    audit_dir.mkdir(parents=True, exist_ok=True)
    del run_timeout

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
    analysis_snapshot_hash = _analysis_snapshot_hash(
        paper_path=paper_path,
        artifacts=analysis_artifacts,
    )
    foundation_snapshot_hash = str(foundation["manifest"]["snapshot_hash"]) if foundation is not None else ""
    if foundation_snapshot_hash:
        analysis_snapshot_hash = _writer_snapshot_hash(analysis_snapshot_hash, foundation_snapshot_hash)

    task_manifest = build_tasks_manifest(tasks)
    task_items = [task for task in tasks.get("repro_tasks", []) if isinstance(task, dict)]
    manifest_entries = [entry for entry in task_manifest.get("tasks", []) if isinstance(entry, dict)]
    task_pairs = list(zip(task_items, manifest_entries))
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
    cached_all_deliveries = cached_all_current and all(
        _record_is_valid_current_delivery(record) for record in cached_records
    )
    cached_all_verified = cached_all_current and all(
        _record_has_accepted_task_verification(record) for record in cached_records
    )
    if (
        resume
        and not force_task_ids
        and cached is not None
        and cached_all_current
        and (not run_repro or (cached_runtime_passed and cached_all_deliveries))
        and (task_review_callback is None or cached_all_verified)
    ):
        cached["writer_review_doc"] = {
            "_meta": {"mode": "task_writer_scientific_results"},
            **_task_writer_alignment_summary(cached_records),
            "task_writer_reviews": [_compact_task_writer_review(record) for record in cached_records],
        }
        write_json(audit_dir / "03c_task_writers_resume.json", {"ok": True, "source": "cached artifacts"})
        return cached
    resume_records = (
        _load_task_writer_resume_records(
            audit_dir=audit_dir,
            task_pairs=task_pairs,
            expected_analysis_snapshot_hash=analysis_snapshot_hash,
        )
        if resume
        else {}
    )

    _clear_stage_outputs(output_dir, "manifest", preserve_audit=bool(resume_records))

    task_root = audit_dir / "03c_task_writer_sandboxes"
    if task_root.exists() and not resume_records:
        shutil.rmtree(task_root)
    task_root.mkdir(parents=True, exist_ok=True)
    status: dict[str, Any] = {
        "backend": CODEX_PROJECT_BACKEND,
        "mode": "task_writers",
        "stop_rule": (
            "accepted_by_isolated_task_reporter_or_external_blocker"
            if task_review_callback is not None
            else "ready_for_review_or_external_blocker"
        ),
        "run_repro": bool(run_repro),
        "task_count": len(task_pairs),
        "orchestration": "launch_all_then_wait",
    }
    status["agent_concurrency"] = len(task_pairs)
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
        timeout=timeout,
        initial_records_by_index=resume_records,
        review_feedback=review_feedback,
        force_task_ids=force_task_ids,
        task_review_callback=task_review_callback,
    )
    write_json(audit_dir / "writer_dispatch.json", dispatch_audit)

    _prepare_project_workspace(repro_project_dir, task_manifest)
    expected_paths = _merge_task_writer_deliveries(
        repro_project_dir=repro_project_dir,
        task_manifest=task_manifest,
        expected_paths=set(expected_paths),
        task_records=task_records,
        foundation=foundation,
    )
    _restore_trusted_files(repro_project_dir, task_manifest)
    final_task_manifest = _task_manifest_with_configs(task_manifest)
    write_json(repro_project_dir / "tasks_manifest.json", final_task_manifest)
    validation = validate_repro_project(repro_project_dir)
    validation["host_validation_skipped"] = False
    requirement_warnings = validate_requirements(repro_project_dir)
    security_issues = static_scan_repro_project(repro_project_dir)
    syntax_issues = [
        issue for issue in security_issues if "syntax error" in str(issue.get("message") or "").lower()
    ]
    if syntax_issues:
        validation["python_compiles"] = False
        validation["compile_errors"] = syntax_issues
        validation["host_validation_skipped"] = False
    manifest = _manifest_from_project(
        repro_project_dir=repro_project_dir,
        expected_paths=expected_paths,
        task_manifest=final_task_manifest,
        round_no=1,
    )
    manifest["_meta"]["mode"] = "task_writers"
    manifest["_meta"]["analysis_snapshot_hash"] = analysis_snapshot_hash
    manifest["_meta"]["foundation_snapshot_hash"] = foundation_snapshot_hash or None
    write_json(output_dir / "repro_project_manifest.json", manifest)

    runtime_result = _task_writer_runtime_result(
        task_records=task_records,
        validation=validation,
        requirement_warnings=requirement_warnings,
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
                    "task_reporter_verdict": (
                        record.get("task_verification", {}).get("verdict")
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


def _load_task_writer_resume_records(
    *,
    audit_dir: Path,
    task_pairs: list[tuple[dict[str, Any], dict[str, Any]]],
    expected_analysis_snapshot_hash: str,
) -> dict[int, dict[str, Any]]:
    path = audit_dir / "03c_task_writers_records.json"
    if not path.exists():
        return {}
    try:
        document = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}
    raw_records = document.get("tasks") if isinstance(document, dict) else None
    if not isinstance(raw_records, list):
        return {}
    expected_by_id = {
        str(task.get("task_id") or entry.get("task_id") or f"task_{index}"): index
        for index, (task, entry) in enumerate(task_pairs, start=1)
    }
    records: dict[int, dict[str, Any]] = {}
    for record in raw_records:
        if not isinstance(record, dict):
            continue
        task_id = str(record.get("task_id") or "")
        index = expected_by_id.get(task_id)
        if index is None:
            continue
        expected_sandbox = audit_dir / "03c_task_writer_sandboxes" / f"{index:02d}_{safe_label(task_id)}"
        sandbox = Path(str(record.get("sandbox") or ""))
        if not sandbox.exists() or sandbox.resolve() != expected_sandbox.resolve():
            continue
        if str(record.get("analysis_snapshot_hash") or "") != expected_analysis_snapshot_hash:
            continue
        records[index] = record
    return records


def _record_is_valid_current_delivery(record: dict[str, Any]) -> bool:
    sandbox = Path(str(record.get("sandbox") or ""))
    if not sandbox.is_dir():
        return False
    status = str(record.get("task_writer_status") or "")
    if status not in {WRITER_REVIEW_STATUS, FINAL_MATCHED_STATUS}:
        return False
    if status == FINAL_MATCHED_STATUS:
        verdict = record.get("verification_result")
        if record.get("verification_verified") is not True or not isinstance(verdict, dict):
            return False
        if verdict.get("verdict") != "accepted":
            return False
    blockers, _ = partition_writer_delivery_issues(record.get("result_json"))
    if blockers:
        return False
    task_id = str(record.get('task_id') or '')
    if task_id and _task_execution_binding_issues(
        sandbox=sandbox,
        task_id=task_id,
        result_doc=record.get('result_json'),
    ):
        return False
    return True


def _record_has_accepted_task_verification(record: dict[str, Any]) -> bool:
    verification = record.get("task_verification")
    return isinstance(verification, dict) and verification.get("verdict") == "accepted"


def _dispatch_task_writers(
    *,
    task_pairs: list[tuple[dict[str, Any], dict[str, Any]]],
    facts: dict[str, Any],
    experiment_index: dict[str, Any],
    paper: dict[str, Any],
    paper_path: Path,
    paper_context_json: str,
    paper_images: list[Any] | None,
    paper_thesis: dict[str, Any] | None,
    foundation: dict[str, Any] | None = None,
    analysis_snapshot_hash: str,
    analysis_artifacts: dict[str, Path],
    task_root: Path,
    audit_dir: Path,
    run_repro: bool,
    timeout: float = DEFAULT_CODEX_TIMEOUT_SECONDS,
    initial_records_by_index: dict[int, dict[str, Any]] | None = None,
    review_feedback: dict[str, dict[str, Any]] | None = None,
    force_task_ids: set[str] | None = None,
    task_review_callback: Callable[[int, dict[str, Any], dict[str, Any], int], dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    existing = dict(initial_records_by_index or {})
    feedback_by_id = dict(review_feedback or {})
    forced = {str(item) for item in (force_task_ids or set()) if str(item)}
    by_index: dict[int, dict[str, Any]] = {
        index: record
        for index, record in existing.items()
        if _task_writer_runtime_task_passed(record)
        and str(record.get("task_id") or "") not in forced
        and (task_review_callback is None or _record_has_accepted_task_verification(record))
    }
    pending_indexes = [index for index in range(1, len(task_pairs) + 1) if index not in by_index]
    audit: dict[str, Any] = {
        "policy": "parallel_first",
        "parallel_attempted": len(task_pairs) > 1,
        "task_count": len(task_pairs),
        "runtime_capability": "parallel_subagents",
        "session_timeout_s": float(timeout),
        "overall_runtime_limit_s": None,
        "dispatch_batches": [{
            "batch_id": "stage5_all_task_writers",
            "task_ids": [str(task_pairs[index - 1][0].get("task_id") or f"task_{index}") for index in pending_indexes],
            "launched_before_wait": True,
            "concurrency_limit": len(pending_indexes),
        }] if pending_indexes else [],
        "fallback_reason": None,
        "fallback_evidence_files": [],
        "attempts": [],
        "reused_task_ids": [str(record.get("task_id") or "") for record in by_index.values()],
        "launched_task_ids": [str(task_pairs[index - 1][0].get("task_id") or "") for index in pending_indexes],
    }
    audit_path = audit_dir / "writer_dispatch.json"
    write_json(audit_path, audit)
    futures: dict[Future[dict[str, Any]], int] = {}
    if pending_indexes:
        with ThreadPoolExecutor(max_workers=len(pending_indexes)) as executor:
            for index in pending_indexes:
                task, manifest_entry = task_pairs[index - 1]
                futures[executor.submit(
                    _run_one_task_writer,
                    index=index,
                    reuse_existing=bool(existing.get(index)),
                    task=task,
                    manifest_entry=manifest_entry,
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
                    timeout=timeout,
                    review_feedback=feedback_by_id.get(
                        str(task.get("task_id") or manifest_entry.get("task_id") or "")
                    ),
                    task_review_callback=task_review_callback,
                )] = index
            for future in as_completed(futures):
                index = futures[future]
                try:
                    by_index[index] = future.result()
                except Exception as exc:
                    task, manifest_entry = task_pairs[index - 1]
                    by_index[index] = _failed_task_record(
                        index=index,
                        task_id=str(task.get("task_id") or manifest_entry.get("task_id") or f"task_{index}"),
                        module=str(manifest_entry.get("module") or ""),
                        error=f"{type(exc).__name__}: {exc}",
                    )
                audit["attempts"].append(
                    {
                        "task_id": by_index[index].get("task_id"),
                        "index": index,
                        "writer_error_kind": by_index[index].get("writer_error_kind"),
                        "writer_completed": bool(by_index[index].get("writer_completed")),
                    }
                )
                write_json(audit_path, audit)
    records = [by_index[index] for index in range(1, len(task_pairs) + 1)]
    audit["completed_task_count"] = len(records)
    write_json(audit_path, audit)
    return records, audit


def _run_one_task_writer(
    *,
    index: int,
    reuse_existing: bool,
    task: dict[str, Any],
    manifest_entry: dict[str, Any],
    facts: dict[str, Any],
    experiment_index: dict[str, Any],
    paper: dict[str, Any],
    paper_path: Path,
    paper_context_json: str,
    paper_images: list[Any] | None,
    paper_thesis: dict[str, Any] | None,
    foundation: dict[str, Any] | None = None,
    analysis_snapshot_hash: str,
    analysis_artifacts: dict[str, Path],
    task_root: Path,
    audit_dir: Path,
    run_repro: bool,
    timeout: float = DEFAULT_CODEX_TIMEOUT_SECONDS,
    review_feedback: dict[str, Any] | None = None,
    task_review_callback: Callable[[int, dict[str, Any], dict[str, Any], int], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    task_id = str(task.get("task_id") or manifest_entry.get("task_id") or f"task_{index}")
    module = str(manifest_entry.get("module") or safe_label(task_id))
    task = _task_with_experiment_profile(task, experiment_index)
    base_label = f"03c_task_writer_{index:02d}_{safe_label(task_id)}"
    sandbox = task_root / f"{index:02d}_{safe_label(task_id)}"
    output_subdir = str(manifest_entry.get("output_subdir") or task_id)
    _prepare_task_writer_sandbox(
        sandbox=sandbox,
        task=task,
        manifest_entry=manifest_entry,
        paper=paper,
        paper_path=paper_path,
        facts=facts,
        paper_thesis=paper_thesis,
        analysis_snapshot_hash=analysis_snapshot_hash,
        analysis_artifacts=analysis_artifacts,
        full_paper_images=paper_images,
        reuse_existing=reuse_existing,
        foundation=foundation,
    )
    execution_binding = _load_task_execution_binding(sandbox, task_id)
    base_prompt = _build_task_writer_brief(
        index=index,
        task=task,
        manifest_entry=manifest_entry,
        facts=facts,
        experiment_index=experiment_index,
        paper=paper,
        paper_context_json=paper_context_json,
        paper_thesis=paper_thesis,
        run_repro=run_repro,
        review_feedback=review_feedback,
        foundation_enabled=foundation is not None,
        execution_binding=execution_binding,
    )
    session_round = 1
    if reuse_existing:
        archive_round = _next_writer_progress_round(sandbox)
        if task_review_callback is not None:
            existing_record = _collect_task_writer_delivery(
                index=index,
                task=task,
                manifest_entry=manifest_entry,
                sandbox=sandbox,
                writer_status={"ok": True, "source": "resumed_existing_delivery"},
            )
            existing_record["analysis_snapshot_hash"] = analysis_snapshot_hash
            existing_record["writer_session_count"] = max(1, archive_round)
            if existing_record.get("task_writer_status") == TASK_WRITER_TERMINAL_STATUS:
                review_action, returned_feedback = _attach_task_reporter_review(
                    callback=task_review_callback,
                    index=index,
                    task=task,
                    record=existing_record,
                    session_round=archive_round,
                )
                if review_action in {"accepted", "failed"}:
                    return existing_record
                review_feedback = returned_feedback
        _archive_nonterminal_writer_delivery(
            sandbox=sandbox,
            output_subdir=output_subdir,
            round_no=archive_round,
            session_status={"ok": True, "source": "resumed_nonmatched_delivery"},
        )
        session_round = archive_round + 1

    while True:
        label = base_label if session_round == 1 else f"{base_label}_continue_{session_round:03d}"
        prompt = (
            base_prompt
            if session_round == 1
            else _build_task_writer_continuation_brief(
                base_prompt=base_prompt,
                task_id=task_id,
                module=module,
                session_round=session_round,
                review_feedback=review_feedback,
            )
        )
        writer_status = _run_task_writer_codex_session(
            label=label,
            prompt=prompt,
            sandbox=sandbox,
            audit_dir=audit_dir,
            timeout=timeout,
        )

        if foundation is not None:
            frozen_issues = foundation_violations(sandbox, foundation)
            if frozen_issues:
                restore_foundation_snapshot(sandbox, foundation)
                writer_status = {
                    **writer_status,
                    "ok": False,
                    "error_kind": "foundation_modified",
                    "blocked_reason": "task writer changed the frozen scientific foundation",
                    "foundation_violations": frozen_issues,
                }
        _restore_trusted_files(sandbox, {"version": 1, "tasks": [manifest_entry]})
        record = _collect_task_writer_delivery(
            index=index,
            task=task,
            manifest_entry=manifest_entry,
            sandbox=sandbox,
            writer_status=writer_status,
        )
        if (
            run_repro
            and record.get('writer_error_kind') == 'shared_component_bypassed'
            and writer_status.get('ok')
        ):
            record['analysis_snapshot_hash'] = analysis_snapshot_hash
            record['writer_session_count'] = session_round
            binding_blockers = [
                str(item)
                for item in record.get('delivery_blockers', [])
                if str(item).startswith('shared_component_bypassed:')
            ]
            review_feedback = {
                'error_kind': 'shared_component_bypassed',
                'verdict': 'revise',
                'revision_target': 'writer',
                'differences': binding_blockers,
                'feedback': [
                    'Use every shared_implementation component directly in the real scientific path.',
                    'Import each declared component module, or a shared Foundation composition entrypoint whose src import graph reaches it; task-private heads may compose with, but must not replace, those shared components.',
                    'Update component_usage with exact module, callable, usage, and evidence_files entries.',
                ],
            }
            _archive_nonterminal_writer_delivery(
                sandbox=sandbox,
                output_subdir=output_subdir,
                round_no=session_round,
                session_status={
                    **writer_status,
                    'ok': False,
                    'error_kind': 'shared_component_bypassed',
                    'issues': binding_blockers,
                },
            )
            session_round += 1
            continue
        record["analysis_snapshot_hash"] = analysis_snapshot_hash
        record["writer_session_count"] = session_round
        if not run_repro or not record.get("writer_completed"):
            return record
        if record.get("task_writer_status") != TASK_WRITER_TERMINAL_STATUS:
            _archive_nonterminal_writer_delivery(
                sandbox=sandbox,
                output_subdir=output_subdir,
                round_no=session_round,
                session_status=writer_status,
            )
            session_round += 1
            continue
        if task_review_callback is None:
            return record
        review_action, returned_feedback = _attach_task_reporter_review(
            callback=task_review_callback,
            index=index,
            task=task,
            record=record,
            session_round=session_round,
        )
        if review_action in {"accepted", "failed"}:
            return record
        if review_action == "writer_revision":
            review_feedback = returned_feedback
            _archive_nonterminal_writer_delivery(
                sandbox=sandbox,
                output_subdir=output_subdir,
                round_no=session_round,
                session_status=writer_status,
            )
            session_round += 1
            continue
        record["task_writer_status"] = "failed"
        record["writer_error_kind"] = "task_reporter_unresolved"
        record["blocked_reason"] = "task reporter returned an unresolved non-writer revision"
        return record


def _attach_task_reporter_review(
    *,
    callback: Callable[[int, dict[str, Any], dict[str, Any], int], dict[str, Any]],
    index: int,
    task: dict[str, Any],
    record: dict[str, Any],
    session_round: int,
) -> tuple[str, dict[str, Any] | None]:
    task_reporter = callback(index, task, record, session_round)
    record["task_reporter"] = task_reporter
    verification = task_reporter.get("task_verification") if isinstance(task_reporter, dict) else None
    if isinstance(verification, dict):
        record["task_verification"] = verification
    if not isinstance(task_reporter, dict) or not task_reporter.get("ok"):
        record["task_writer_status"] = "failed"
        record["writer_error_kind"] = "task_reporter_failed"
        record["blocked_reason"] = (
            task_reporter.get("error")
            if isinstance(task_reporter, dict)
            else "task reporter callback failed"
        )
        return "failed", None
    if isinstance(verification, dict) and verification.get("verdict") == "accepted":
        record["task_reporter_accepted"] = True
        return "accepted", None
    if isinstance(verification, dict) and verification.get("revision_target") == "writer":
        return "writer_revision", verification
    record["task_writer_status"] = "failed"
    record["writer_error_kind"] = "task_reporter_unresolved"
    record["blocked_reason"] = "task reporter returned an unresolved non-writer revision"
    return "failed", None


def _next_writer_progress_round(sandbox: Path) -> int:
    progress_root = sandbox / "writer_progress"
    rounds: list[int] = []
    if progress_root.is_dir():
        for path in progress_root.iterdir():
            if not path.is_dir() or not path.name.startswith("round_"):
                continue
            try:
                rounds.append(int(path.name.split("_", 1)[1]))
            except (TypeError, ValueError):
                continue
    return max(rounds, default=0) + 1


def _run_task_writer_codex_session(
    *,
    label: str,
    prompt: str,
    sandbox: Path,
    audit_dir: Path,
    timeout: float = DEFAULT_CODEX_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    write_text(audit_dir / f"{label}_brief.md", prompt)
    python_dir = Path(sys.executable).resolve().parent
    return run_codex_subprocess(
        role="task_writer",
        work_dir=sandbox,
        prompt=prompt,
        audit_dir=audit_dir,
        label=label,
        sandbox="workspace-write",
        timeout=timeout,
        command_override=get_config_value("GENG_CODEX_TASK_WRITER_CMD"),
        image_paths=sorted(
            path.resolve()
            for path in (sandbox / PAPER_EVIDENCE_DIR / "full_paper_pages").glob("paper_page_*.png")
            if path.is_file()
        ),
        extra_env={"GENG_PYTHON_EXECUTABLE": sys.executable, "PYTHONDONTWRITEBYTECODE": "1"},
        path_prepend=[python_dir],
    )


def _build_task_writer_continuation_brief(
    *,
    base_prompt: str,
    task_id: str,
    module: str,
    session_round: int,
    review_feedback: dict[str, Any] | None = None,
) -> str:
    feedback_text = pretty_json(review_feedback) if review_feedback else "None"
    return f"""# Mandatory continuation: session {session_round}

The previous Codex session for `{task_id}` ended without a valid `ready_for_review` delivery, or the independent reporter reported a possible material paper mismatch. Continue in the existing sandbox; do not restart the implementation and do not merely rewrite the previous explanation.

{WRITER_PAPER_FIDELITY_POLICY}

Before acting:
1. Read the existing task code, configs, outputs, and `writer_progress/` archives.
2. Inspect the latest local CSV/summary/PNG against the complete paper evidence.
3. Classify every reporter item before editing: (a) a paper-grounded violation of an explicit fact or failure of the core claim; (b) a reasonable choice inside paper-silent or ambiguous space; or (c) a non-material numerical, statistical, visual, or presentation difference.
4. Create a concrete modification plan only for category (a). For category (b), keep or revise the explicit assumption according to evidence. For category (c), record the caveat without changing faithful code merely to satisfy the reporter.
5. Run a fresh full with `python -m tasks.{module} config.json` after a meaningful code, model, parameter, or configuration change. Never rerun unchanged code solely to answer non-blocking feedback.
6. Keep iterating while a paper-grounded material blocker remains and a concrete change is available. Do not emit `explained_gap`, `failed`, or final `matched` as a scientific terminal state.
7. Write `task_agent_result.json` with status `ready_for_review` once the latest successful full respects explicit paper facts, supports the task's core claim, and leaves only disclosed assumptions or non-material differences.

## Isolated task reporter feedback
```json
{feedback_text}
```

Investigate every reported difference, but do not obey it blindly. Fix and rerun only for a material, paper-grounded blocker. If a suggestion conflicts with explicit paper evidence, preserve the faithful implementation and document that conflict. If it concerns an acceptable paper-silent assumption or a non-material difference, retain it as a caveat and resubmit without manufacturing scientific activity.

The original task brief follows.

{base_prompt}
"""


def _archive_nonterminal_writer_delivery(
    *,
    sandbox: Path,
    output_subdir: str,
    round_no: int,
    session_status: dict[str, Any],
) -> None:
    progress_dir = sandbox / "writer_progress" / f"round_{round_no:03d}"
    progress_dir.mkdir(parents=True, exist_ok=True)
    for filename in ("task_agent_result.json", "task_agent_result.md"):
        source, _ = _task_result_file_path(sandbox, output_subdir, filename)
        if not source.is_file():
            continue
        shutil.copy2(source, progress_dir / filename)
        source.unlink()
    write_json(
        progress_dir / "session_status.json",
        {
            "terminal": False,
            "reason": "writer session ended without ready_for_review",
            "session_status": session_status,
        },
    )


def _collect_task_writer_delivery(
    *,
    index: int,
    task: dict[str, Any],
    manifest_entry: dict[str, Any],
    sandbox: Path,
    writer_status: dict[str, Any],
) -> dict[str, Any]:
    """Collect writer-owned outputs without repairing or format-gating them."""
    task_id = str(task.get("task_id") or manifest_entry.get("task_id") or f"task_{index}")
    module = str(manifest_entry.get("module") or "")
    output_subdir = str(manifest_entry.get("output_subdir") or task.get("task_id") or "task")
    result_path, _ = _task_result_file_path(sandbox, output_subdir, "task_agent_result.json")
    markdown_path, _ = _task_result_file_path(sandbox, output_subdir, "task_agent_result.md")
    result_doc = _read_optional_json_object(result_path)
    reported_status = str(result_doc.get("status") or "")
    delivery_issues = writer_delivery_issues(result_doc)
    delivery_blockers, delivery_warnings = partition_writer_delivery_issues(result_doc)
    binding_issues = _task_execution_binding_issues(
        sandbox=sandbox,
        task_id=task_id,
        result_doc=result_doc,
    )
    if binding_issues:
        binding_blockers = [f'shared_component_bypassed: {issue}' for issue in binding_issues]
        delivery_issues.extend(binding_blockers)
        delivery_blockers.extend(binding_blockers)
        writer_status = {
            **writer_status,
            'ok': False,
            'error_kind': 'shared_component_bypassed',
            'blocked_reason': '; '.join(binding_issues),
            'shared_component_issues': binding_issues,
        }
    delivery_usable = not delivery_blockers
    status = TASK_WRITER_TERMINAL_STATUS if delivery_usable else "failed"

    artifacts = inspect_output_artifacts(sandbox, subdir=output_subdir)
    local_images = _collect_writer_images(
        sandbox=sandbox,
        output_subdir=output_subdir,
        declared=result_doc.get("local_image_paths"),
        fallback_pattern="*.png",
        exclude_names={"paper_target_crop.png", "paper_target_locator.png"},
    )
    return {
        "index": index,
        "task_id": task_id,
        "module": module,
        "output_subdir": output_subdir,
        "sandbox": str(sandbox),
        "writer_status": writer_status,
        "writer_completed": delivery_usable,
        "task_writer_status": status,
        "writer_reported_status": reported_status or None,
        "delivery_validation_issues": delivery_issues,
        "delivery_blockers": delivery_blockers,
        "delivery_warnings": delivery_warnings,
        "process_warning": None if writer_status.get("ok") else (writer_status.get("error") or writer_status.get("blocked_reason") or "writer process ended after producing a usable delivery"),
        "result_json": result_doc,
        "result_json_path": str(result_path) if result_path.exists() else None,
        "result_markdown_path": str(markdown_path) if markdown_path.exists() else None,
        "execution_summary": result_doc.get("execution_summary", {}),
        "artifacts": artifacts,
        "local_images": local_images,
        "writer_error_kind": writer_status.get("error_kind"),
        "blocked_reason": writer_status.get("blocked_reason"),
    }


def _read_optional_json_object(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _load_task_execution_binding(sandbox: Path, task_id: str) -> dict[str, Any] | None:
    '''Load the task-scoped scientific execution contract from its sandbox copy.'''

    architecture = _read_optional_json_object(
        sandbox
        / PAPER_EVIDENCE_DIR
        / 'analysis_artifacts'
        / 'scientific_architecture.json'
    )
    return _task_execution_binding_from_architecture(architecture, task_id)


def _task_execution_binding_from_architecture(
    architecture: Any,
    task_id: str,
) -> dict[str, Any] | None:
    '''Resolve a 1.1 binding to concrete component execution records.

    Architecture 1.0 deliberately returns ``None`` so existing cases retain
    their legacy writer prompt and delivery behavior.
    '''

    if not isinstance(architecture, dict) or str(architecture.get('schema_version') or '') != '1.1':
        return None
    raw_components = architecture.get('components')
    components_by_id = {
        str(item.get('id')): item
        for item in raw_components
        if isinstance(item, dict) and str(item.get('id') or '')
    } if isinstance(raw_components, list) else {}
    raw_bindings = architecture.get('bindings')
    binding = next(
        (
            item
            for item in raw_bindings
            if isinstance(item, dict)
            and str(item.get('task_id') or '') == str(task_id)
        ),
        None,
    ) if isinstance(raw_bindings, list) else None
    configuration_issues: list[str] = []
    bound_components: list[dict[str, Any]] = []
    if not isinstance(binding, dict):
        configuration_issues.append(f'no scientific_architecture/1.1 binding exists for task {task_id}')
    else:
        component_ids = binding.get('components')
        if not isinstance(component_ids, list):
            configuration_issues.append('binding.components must be a list of component IDs')
            component_ids = []
        for raw_component_id in component_ids:
            component_id = str(raw_component_id or '')
            component = components_by_id.get(component_id)
            if not isinstance(component, dict):
                label = component_id or '<empty>'
                configuration_issues.append(f'binding refers to unknown component {label}')
                continue
            execution = component.get('execution')
            bound_components.append(
                {
                    'component_id': component_id,
                    'module': str(component.get('module') or ''),
                    'callable': str(component.get('callable') or ''),
                    'execution': dict(execution) if isinstance(execution, dict) else {},
                }
            )
    return {
        'schema_version': '1.1',
        'task_id': str(task_id),
        'experiment_id': str(binding.get('experiment_id') or '') if isinstance(binding, dict) else '',
        'consistency_group': str(binding.get('consistency_group') or '') if isinstance(binding, dict) else '',
        'components': bound_components,
        'configuration_issues': configuration_issues,
    }


def _task_execution_binding_issues(
    *,
    sandbox: Path,
    task_id: str,
    result_doc: Any,
    execution_binding: dict[str, Any] | None = None,
) -> list[str]:
    '''Apply the low-false-positive static gate for architecture 1.1.'''

    contract = execution_binding or _load_task_execution_binding(sandbox, task_id)
    if not isinstance(contract, dict) or str(contract.get('schema_version') or '') != '1.1':
        return []
    issues = [str(item) for item in contract.get('configuration_issues', []) if str(item)]
    components = [item for item in contract.get('components', []) if isinstance(item, dict)]
    usage_items = result_doc.get('component_usage') if isinstance(result_doc, dict) else None
    usage_by_id: dict[str, dict[str, Any]] = {}
    if not isinstance(usage_items, list):
        issues.append('task_agent_result.json must contain component_usage for every bound component')
        usage_items = []
    for index, item in enumerate(usage_items):
        if not isinstance(item, dict):
            issues.append(f'component_usage[{index}] must be an object')
            continue
        component_id = str(item.get('component_id') or '')
        if not component_id:
            issues.append(f'component_usage[{index}].component_id is empty')
        elif component_id in usage_by_id:
            issues.append(f'component_usage contains duplicate component {component_id}')
        else:
            usage_by_id[component_id] = item

    expected_ids = {str(item.get('component_id') or '') for item in components}
    for unexpected in sorted(set(usage_by_id) - expected_ids):
        issues.append(f'component_usage declares unbound component {unexpected}')

    source_facts = _inspect_task_execution_source(sandbox, task_id)
    imported_modules = source_facts['imported_modules']
    reachable_task_files = source_facts['reachable_task_files']
    for component in components:
        component_id = str(component.get('component_id') or '')
        module = str(component.get('module') or '')
        callable_name = str(component.get('callable') or '')
        execution = component.get('execution') if isinstance(component.get('execution'), dict) else {}
        usage = usage_by_id.get(component_id)
        if not isinstance(usage, dict):
            issues.append(f'component_usage is missing bound component {component_id}')
        else:
            if str(usage.get('module') or '') != module:
                issues.append(f'{component_id}: component_usage.module must equal declared module {module}')
            if str(usage.get('callable') or '') != callable_name:
                issues.append(f'{component_id}: component_usage.callable must equal declared callable {callable_name}')
            usage_kind = str(usage.get('usage') or '')
            if usage_kind not in {'in_scientific_path', 'reference_only', 'not_used'}:
                issues.append(
                    f'{component_id}: usage must be in_scientific_path, reference_only, or not_used'
                )
            evidence = usage.get('evidence_files')
            evidence_items = (
                [str(item).strip() for item in evidence if str(item).strip()]
                if isinstance(evidence, list)
                else []
            )
            if not evidence_items:
                issues.append(f'{component_id}: evidence_files must identify the task scientific path')
            for raw_evidence in evidence_items:
                relative = _sandbox_evidence_source(sandbox, raw_evidence)
                if relative is None:
                    issues.append(
                        f'{component_id}: evidence file {raw_evidence!r} must exist inside the sandbox'
                    )
                elif relative.casefold() not in reachable_task_files:
                    issues.append(
                        f'{component_id}: evidence file {raw_evidence!r} is not in the assigned task import closure'
                    )
            if execution.get('shared_implementation') is True and usage_kind != 'in_scientific_path':
                declared = usage_kind or 'undeclared'
                issues.append(
                    f'{component_id}: shared_implementation must be used in_scientific_path, not {declared}'
                )

        expected_import = _normalize_python_module(module)
        if not expected_import:
            issues.append(f'{component_id}: declared component module is empty')
        elif expected_import not in imported_modules:
            issues.append(
                f'{component_id}: expected module {expected_import} is not reachable from the assigned task entry through task-local and src import graphs'
            )
        elif callable_name and not _declared_callable_is_called(
            source_facts,
            module=expected_import,
            callable_name=callable_name,
        ):
            issues.append(
                f'{component_id}: declared callable {expected_import}.{callable_name} is imported but not called from the assigned task scientific path'
            )
    return _dedupe_strings(issues)


def _normalize_python_module(module: str) -> str:
    value = str(module or '').strip().replace('\\', '/').lstrip('./')
    if value.endswith('/__init__.py'):
        value = value[:-12]
    elif value.endswith('.py'):
        value = value[:-3]
    return value.strip('/').replace('/', '.')


def _inspect_task_execution_source(sandbox: Path, task_id: str) -> dict[str, Any]:
    '''Inspect only the source closure rooted at the assigned task entrypoint.'''

    module_paths, module_names = _task_module_index(sandbox)
    entry = _assigned_task_entrypoint(sandbox, task_id)
    imported_modules: set[str] = set()
    reachable_task_files: set[str] = set()
    pending = [entry.resolve()] if entry is not None else []
    visited: set[Path] = set()
    while pending:
        path = pending.pop()
        if path in visited:
            continue
        visited.add(path)
        module_name = module_names.get(path)
        if not module_name:
            continue
        try:
            tree = ast.parse(path.read_text(encoding='utf-8-sig'), filename=str(path))
        except (OSError, UnicodeError, SyntaxError):
            continue
        reachable_task_files.add(path.relative_to(sandbox.resolve()).as_posix().casefold())
        imported = _imports_from_local_module(
            tree,
            module_name=module_name,
            is_package=path.name == '__init__.py',
        )
        imported_modules.update(imported)
        for imported_name in imported:
            candidate = module_paths.get(imported_name)
            if candidate is not None and candidate not in visited:
                pending.append(candidate)
    reachable_src_modules = _reachable_local_src_modules(sandbox, imported_modules)
    callable_usage = _static_callable_usage(
        sandbox,
        entry=entry,
        reachable_task_paths=visited,
        reachable_src_modules=reachable_src_modules,
    )
    return {
        'imported_modules': reachable_src_modules,
        'direct_imported_modules': imported_modules,
        'reachable_task_files': reachable_task_files,
        'called_symbols': callable_usage,
    }


def _assigned_task_entrypoint(sandbox: Path, task_id: str) -> Path | None:
    '''Prefer the trusted manifest entry, then fall back to a task-id filename.'''

    manifest = _read_optional_json_object(sandbox / 'tasks_manifest.json')
    raw_entries = manifest.get('tasks') if isinstance(manifest, dict) else None
    for entry in raw_entries if isinstance(raw_entries, list) else []:
        if not isinstance(entry, dict) or str(entry.get('task_id') or '') != str(task_id):
            continue
        candidates: list[str] = []
        script = str(entry.get('script') or '').strip()
        module = str(entry.get('module') or '').strip()
        if script:
            candidates.append(script)
        if module:
            candidates.append(f"tasks/{module.replace('.', '/')}.py")
        for raw in candidates:
            path = _safe_task_source_path(sandbox, raw)
            if path is not None:
                return path

    raw_task_id = str(task_id or '').strip()
    fallback_names = [raw_task_id]
    slug = ''.join(
        character if character.isalnum() or character == '_' else '_'
        for character in raw_task_id
    ).strip('_').lower()
    if slug and slug[0].isdigit():
        slug = f't_{slug}'
    if slug and slug not in fallback_names:
        fallback_names.append(slug)
    for name in fallback_names:
        path = _safe_task_source_path(sandbox, f'tasks/{name}.py')
        if path is not None:
            return path
    return None


def _safe_task_source_path(sandbox: Path, raw: str) -> Path | None:
    value = str(raw or '').strip().replace('\\', '/')
    if not value:
        return None
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = sandbox / candidate
    try:
        resolved = candidate.resolve()
        resolved.relative_to((sandbox / 'tasks').resolve())
    except (OSError, ValueError):
        return None
    if not resolved.is_file() or resolved.is_symlink() or resolved.suffix.lower() != '.py':
        return None
    return resolved


def _task_module_index(sandbox: Path) -> tuple[dict[str, Path], dict[Path, str]]:
    module_paths: dict[str, Path] = {}
    module_names: dict[Path, str] = {}
    for path in _task_source_files(sandbox):
        resolved = path.resolve()
        relative = path.relative_to(sandbox).with_suffix('')
        parts = list(relative.parts)
        if parts and parts[-1] == '__init__':
            parts.pop()
        module_name = '.'.join(parts)
        if not module_name:
            continue
        module_names[resolved] = module_name
        module_paths[module_name] = resolved
        if module_name.startswith('tasks.'):
            module_paths.setdefault(module_name[len('tasks.'):], resolved)
    return module_paths, module_names


def _sandbox_evidence_source(sandbox: Path, raw: str) -> str | None:
    '''Resolve an optional line suffix and require a real sandbox file.'''

    value = str(raw or '').strip().replace('\\', '/')
    prefix, separator, suffix = value.rpartition(':')
    compact_suffix = suffix.replace('-', '')
    if separator and (
        suffix.casefold() == 'line'
        or compact_suffix.isdigit()
        or (suffix[:1].casefold() == 'l' and suffix[1:].isdigit())
    ):
        value = prefix
    fragment_prefix, fragment, fragment_line = value.rpartition('#L')
    if fragment and fragment_line.isdigit():
        value = fragment_prefix
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = sandbox / candidate
    try:
        resolved = candidate.resolve()
        relative = resolved.relative_to(sandbox.resolve())
    except (OSError, ValueError):
        return None
    if not resolved.is_file() or resolved.is_symlink():
        return None
    return relative.as_posix()


def _declared_callable_is_called(
    source_facts: dict[str, Any],
    *,
    module: str,
    callable_name: str,
) -> bool:
    target = '.'.join(
        part
        for part in (
            _normalize_python_module(module),
            str(callable_name or '').strip().replace(':', '.').strip('.'),
        )
        if part
    )
    if not target:
        return False
    for raw_symbol in source_facts.get('called_symbols', set()):
        symbol = str(raw_symbol or '')
        if symbol == target or symbol.startswith(f'{target}.'):
            return True
        if target.endswith('.__call__') and symbol == target[:-9]:
            return True
    return False


def _static_callable_usage(
    sandbox: Path,
    *,
    entry: Path | None,
    reachable_task_paths: set[Path],
    reachable_src_modules: set[str],
) -> set[str]:
    '''Return call targets reachable from the assigned task entry symbols.'''

    _task_paths, task_names = _task_module_index(sandbox)
    src_paths = _src_module_index(sandbox)
    selected: dict[str, Path] = {}
    for path in reachable_task_paths:
        module_name = task_names.get(path.resolve())
        if module_name:
            selected[module_name] = path.resolve()
    for module_name, path in src_paths.items():
        if module_name in reachable_src_modules or any(
            value.startswith(f'{module_name}.')
            for value in reachable_src_modules
        ):
            selected[module_name] = path.resolve()

    graph: dict[str, set[str]] = {}
    aliases: dict[str, str] = {}
    for module_name, path in selected.items():
        analysis = _analyze_static_module(path, module_name)
        aliases.update(analysis['aliases'])
        for owner, targets in analysis['graph'].items():
            graph.setdefault(owner, set()).update(targets)

    if entry is None:
        return set()
    entry_module = task_names.get(entry.resolve())
    if not entry_module:
        return set()
    roots = {f'{entry_module}.__module__'}
    return _walk_static_calls(roots, graph=graph, aliases=aliases)


def _src_module_index(sandbox: Path) -> dict[str, Path]:
    module_paths: dict[str, Path] = {}
    src_root = sandbox / 'src'
    if not src_root.is_dir():
        return module_paths
    for path in src_root.rglob('*.py'):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(sandbox).with_suffix('')
        parts = list(relative.parts)
        if parts and parts[-1] == '__init__':
            parts.pop()
        module_name = '.'.join(parts)
        if module_name:
            module_paths[module_name] = path.resolve()
    return module_paths


def _analyze_static_module(path: Path, module_name: str) -> dict[str, Any]:
    graph: dict[str, set[str]] = {}
    aliases: dict[str, str] = {}
    try:
        tree = ast.parse(path.read_text(encoding='utf-8-sig'), filename=str(path))
    except (OSError, UnicodeError, SyntaxError):
        return {
            'graph': graph,
            'aliases': aliases,
        }
    is_package = path.name == '__init__.py'
    module_aliases: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbol = f'{module_name}.{node.name}'
            module_aliases[node.name] = symbol
        elif isinstance(node, ast.ClassDef):
            module_aliases[node.name] = f'{module_name}.{node.name}'

    module_owner = f'{module_name}.__module__'
    module_scanner = _StaticCallScanner(
        owner=module_owner,
        aliases=module_aliases,
        graph=graph,
        module_name=module_name,
        is_package=is_package,
    )
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for default in list(node.args.defaults) + [
                item for item in node.args.kw_defaults if item is not None
            ]:
                module_scanner.visit(default)
            continue
        if isinstance(node, ast.ClassDef):
            continue
        module_scanner.visit(node)
    module_aliases = module_scanner.aliases

    for local_name, target in module_aliases.items():
        if not local_name or '.' in local_name:
            continue
        exported = f'{module_name}.{local_name}'
        if target and target != exported:
            aliases[exported] = target

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            owner = f'{module_name}.{node.name}'
            _analyze_static_function(
                node,
                owner=owner,
                base_aliases=module_aliases,
                graph=graph,
                module_name=module_name,
                is_package=is_package,
            )
            continue
        if not isinstance(node, ast.ClassDef):
            continue
        class_symbol = f'{module_name}.{node.name}'
        method_symbols = {
            item.name: f'{class_symbol}.{item.name}'
            for item in node.body
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        if '__init__' in method_symbols:
            graph.setdefault(class_symbol, set()).add(method_symbols['__init__'])
        class_aliases = dict(module_aliases)
        class_aliases.update(method_symbols)
        for item in node.body:
            if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            owner = method_symbols[item.name]
            function_aliases = dict(class_aliases)
            positional = list(item.args.posonlyargs) + list(item.args.args)
            if positional:
                function_aliases[positional[0].arg] = class_symbol
            _analyze_static_function(
                item,
                owner=owner,
                base_aliases=function_aliases,
                graph=graph,
                module_name=module_name,
                is_package=is_package,
            )
    return {
        'graph': graph,
        'aliases': aliases,
    }


def _analyze_static_function(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    owner: str,
    base_aliases: dict[str, str],
    graph: dict[str, set[str]],
    module_name: str,
    is_package: bool,
) -> None:
    aliases = dict(base_aliases)
    positional = list(node.args.posonlyargs) + list(node.args.args)
    defaults = list(node.args.defaults)
    for argument, default in zip(positional[-len(defaults):], defaults):
        target = _static_reference(default, aliases)
        if target:
            aliases[argument.arg] = target
    for argument, default in zip(node.args.kwonlyargs, node.args.kw_defaults):
        if default is None:
            continue
        target = _static_reference(default, aliases)
        if target:
            aliases[argument.arg] = target
    scanner = _StaticCallScanner(
        owner=owner,
        aliases=aliases,
        graph=graph,
        module_name=module_name,
        is_package=is_package,
    )
    for statement in node.body:
        scanner.visit(statement)


class _StaticCallScanner(ast.NodeVisitor):
    def __init__(
        self,
        *,
        owner: str,
        aliases: dict[str, str],
        graph: dict[str, set[str]],
        module_name: str,
        is_package: bool,
    ) -> None:
        self.owner = owner
        self.aliases = dict(aliases)
        self.graph = graph
        self.module_name = module_name
        self.is_package = is_package

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        return

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        return

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        return

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            bound = alias.asname or alias.name.split('.')[0]
            self.aliases[bound] = alias.name if alias.asname else alias.name.split('.')[0]

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        base = _static_import_base(
            node,
            module_name=self.module_name,
            is_package=self.is_package,
        )
        for alias in node.names:
            if alias.name == '*':
                continue
            target = '.'.join(part for part in (base, alias.name) if part)
            self.aliases[alias.asname or alias.name] = target

    def visit_Assign(self, node: ast.Assign) -> None:
        self.visit(node.value)
        value = _static_reference(node.value, self.aliases)
        if not value:
            return
        for target in node.targets:
            name = _static_reference(target, self.aliases)
            if name:
                self.aliases[name] = value
            if isinstance(target, ast.Name):
                self.aliases[target.id] = value

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is None:
            return
        self.visit(node.value)
        value = _static_reference(node.value, self.aliases)
        if not value:
            return
        name = _static_reference(node.target, self.aliases)
        if name:
            self.aliases[name] = value
        if isinstance(node.target, ast.Name):
            self.aliases[node.target.id] = value

    def visit_Call(self, node: ast.Call) -> None:
        target = _static_reference(node.func, self.aliases)
        if target:
            self.graph.setdefault(self.owner, set()).add(target)
        self.generic_visit(node)


def _static_reference(node: ast.AST | None, aliases: dict[str, str]) -> str:
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        parent = _static_reference(node.value, aliases)
        value = f'{parent}.{node.attr}' if parent else node.attr
        return aliases.get(value, value)
    if isinstance(node, ast.Call):
        return _static_reference(node.func, aliases)
    if isinstance(node, ast.Subscript):
        return _static_reference(node.value, aliases)
    return ''


def _static_import_base(
    node: ast.ImportFrom,
    *,
    module_name: str,
    is_package: bool,
) -> str:
    base = str(node.module or '')
    if not node.level:
        return base
    package = module_name if is_package else module_name.rpartition('.')[0]
    package_parts = package.split('.') if package else []
    trim = max(0, node.level - 1)
    if trim:
        package_parts = package_parts[:-trim] if trim <= len(package_parts) else []
    prefix = '.'.join(package_parts)
    return '.'.join(part for part in (prefix, base) if part)


def _canonical_static_symbol(symbol: str, aliases: dict[str, str]) -> str:
    current = str(symbol or '')
    seen: set[str] = set()
    while current and current not in seen:
        seen.add(current)
        parts = current.split('.')
        replacement = ''
        suffix: list[str] = []
        for length in range(len(parts), 0, -1):
            prefix = '.'.join(parts[:length])
            target = aliases.get(prefix)
            if target:
                replacement = target
                suffix = parts[length:]
                break
        if not replacement:
            break
        current = '.'.join([replacement, *suffix])
    return current


def _walk_static_calls(
    roots: set[str],
    *,
    graph: dict[str, set[str]],
    aliases: dict[str, str],
) -> set[str]:
    called: set[str] = set()
    pending = list(roots)
    visited: set[str] = set()
    while pending:
        raw_owner = pending.pop()
        owner = _canonical_static_symbol(raw_owner, aliases)
        if owner in visited:
            continue
        visited.add(owner)
        targets = set(graph.get(owner, set()))
        if owner != raw_owner:
            targets.update(graph.get(raw_owner, set()))
        for raw_target in targets:
            target = _canonical_static_symbol(raw_target, aliases)
            if not target:
                continue
            called.add(target)
            if target not in visited:
                pending.append(target)
    return called


def _reachable_local_src_modules(sandbox: Path, roots: set[str]) -> set[str]:
    '''Follow static imports through local Foundation modules under src/.'''

    src_root = sandbox / 'src'
    module_paths: dict[str, Path] = {}
    if src_root.is_dir():
        for path in src_root.rglob('*.py'):
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(sandbox).with_suffix('')
            parts = list(relative.parts)
            if parts and parts[-1] == '__init__':
                parts.pop()
            module_name = '.'.join(parts)
            if module_name:
                module_paths[module_name] = path

    reachable = set(roots)
    pending = list(roots)
    visited: set[str] = set()
    while pending:
        module_name = pending.pop()
        if module_name in visited:
            continue
        visited.add(module_name)
        path = module_paths.get(module_name)
        if path is None:
            continue
        try:
            tree = ast.parse(path.read_text(encoding='utf-8-sig'), filename=str(path))
        except (OSError, UnicodeError, SyntaxError):
            continue
        imported = _imports_from_local_module(
            tree,
            module_name=module_name,
            is_package=path.name == '__init__.py',
        )
        for candidate in imported:
            if candidate not in reachable:
                reachable.add(candidate)
                pending.append(candidate)
    return reachable


def _imports_from_local_module(
    tree: ast.AST,
    *,
    module_name: str,
    is_package: bool,
) -> set[str]:
    imported: set[str] = set()
    package = module_name if is_package else module_name.rpartition('.')[0]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        base = str(node.module or '')
        if node.level:
            package_parts = package.split('.') if package else []
            trim = max(0, node.level - 1)
            if trim:
                package_parts = package_parts[:-trim] if trim <= len(package_parts) else []
            prefix = '.'.join(package_parts)
            base = '.'.join(value for value in (prefix, base) if value)
        if base:
            imported.add(base)
        for alias in node.names:
            if alias.name != '*':
                imported.add('.'.join(value for value in (base, alias.name) if value))
    return imported


def _dedupe_strings(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value)
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _collect_writer_images(
    *,
    sandbox: Path,
    output_subdir: str,
    declared: Any,
    fallback_pattern: str,
    exclude_names: set[str] | None = None,
) -> list[str]:
    output_dir = sandbox / "outputs" / output_subdir
    candidates: list[Path] = []
    values = declared if isinstance(declared, list) else [declared] if isinstance(declared, str) else []
    for raw in values:
        path = Path(str(raw))
        candidates.extend([path] if path.is_absolute() else [sandbox / path, output_dir / path.name])
    candidates.extend(sorted(output_dir.glob(fallback_pattern)) if output_dir.exists() else [])
    excluded = {name.lower() for name in (exclude_names or set())}
    result: list[str] = []
    seen: set[str] = set()
    for path in candidates:
        if not path.is_file() or path.suffix.lower() != ".png" or path.name.lower() in excluded:
            continue
        key = str(path.resolve()).lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(str(path.resolve()))
    return result


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
    for name in ("channel.py", "modulation.py", "metrics.py", "simulation.py"):
        write_text(sandbox / "src" / name, '"""Task-private workflow stub; prefer tasks/<module>_lib.py."""\n')
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


def _build_task_writer_brief(
    *,
    index: int,
    task: dict[str, Any],
    manifest_entry: dict[str, Any],
    facts: dict[str, Any],
    experiment_index: dict[str, Any],
    paper: dict[str, Any],
    paper_context_json: str,
    paper_thesis: dict[str, Any] | None,
    run_repro: bool,
    review_feedback: dict[str, Any] | None = None,
    foundation_enabled: bool = False,
    execution_binding: dict[str, Any] | None = None,
) -> str:
    task_id = str(task.get("task_id") or manifest_entry.get("task_id") or f"task_{index}")
    module = str(manifest_entry.get("module") or "")
    output_subdir = str(manifest_entry.get("output_subdir") or task_id)
    task_context = paper_context_for_task(paper=paper, task=task)
    task_facts = facts_for_task(facts, task)
    ordering_anchor = thesis_ordering_anchor_for_task(paper_thesis, task)
    feedback_text = pretty_json(review_feedback) if review_feedback else "None"
    full_instruction = (
        f"Run your full task with `python -m tasks.{module} config.json` after each meaningful fix."
        if run_repro
        else "Do not run full config because --run-repro is disabled; prepare the code but do not write a final task result."
    )
    execution_binding_section = ''
    component_usage_template = ''
    hardware_instruction = (
        'Inspect the available hardware yourself and choose CPU, CUDA, memory use, batch size, and parallelism appropriate to the task. '
        'For Monte Carlo, batched matrix operations, large sweeps, or a CPU full likely to take minutes, prefer a real Torch CUDA implementation when CUDA is available.'
    )
    if isinstance(execution_binding, dict):
        component_usage_example = [
            {
                'component_id': str(component.get('component_id') or ''),
                'module': str(component.get('module') or ''),
                'callable': str(component.get('callable') or ''),
                'usage': 'in_scientific_path',
                'evidence_files': [f'tasks/{module}.py:line'],
            }
            for component in execution_binding.get('components', [])
            if isinstance(component, dict)
        ]
        component_usage_key = json.dumps('component_usage')
        component_usage_template = (
            f'  {component_usage_key}: {pretty_json(component_usage_example)},'
        )
        execution_binding_section = f'''## Mandatory scientific execution binding (architecture 1.1)
The resolved component contract for this task is:
```json
{pretty_json(execution_binding)}
```

- Consume the listed `module` / `callable` implementations in the real computation that produces the submitted CSV, summary, and figure. A task may import a declared component itself or import a shared Foundation composition entrypoint whose local `src/**/*.py` import graph reaches it.
- Every component with `execution.shared_implementation=true` must be reported as `in_scientific_path`. An audit-only call, shape check, reference comparison, or unused import does not count.
- Do not mirror or rewrite a shared trainable model under `tasks/`. Reuse its Foundation model/trainer/checkpoint path so all bound tasks execute the same implementation.
- Add `component_usage` to `task_agent_result.json`, with one exact entry per bound component:
```json
{pretty_json(component_usage_example)}
```
'''
        hardware_instruction = (
            'Follow each bound component execution.primary_framework and execution.device_policy exactly. '
            'Do not substitute Torch, CUDA, NumPy, CPU, or another framework/device heuristic for the architecture contract. '
            'Record evidence that expensive computation ran under the declared policy.'
        )
    ownership_instruction = (
        "The shared `src/**/*.py`, Foundation tests, and `configs/foundation*` files are frozen. "
        "Import and reuse them, but never edit, delete, replace, or shadow them. Put all task-private science and helpers under `tasks/`."
        if foundation_enabled
        else "You may create or edit any task-private code, config, helper, dependency, and output needed for this task."
    )
    return f"""# Role: autonomous Codex task writer

You own exactly one reproduction task. Write the code, run the assigned full experiment, compare the result directly with the complete paper, and keep revising and rerunning while a paper-grounded material scientific blocker remains. Your handoff is `ready_for_review`; only the independent reporter may grant final `matched`.

{WRITER_PAPER_FIDELITY_POLICY}

## Ownership
- Assigned task_id: `{task_id}`
- Assigned module: `tasks.{module}`
- Output directory: `outputs/{output_subdir}/`
- You own the task-private portion of this isolated sandbox. {ownership_instruction}
- Do not edit `src/_io.py`, `src/_backend.py`, `run_experiment.py`, `tasks_manifest.json`, `tasks/__init__.py`, or any other task module.
- Read your binding in `scientific_architecture.json` when present and preserve its shared shapes, units, normalization, component identities, and invariants.
- {full_instruction}
- You may run smoke with `python -m tasks.{module} config_smoke.json`.
- Run Python and dependencies directly. The host does not guard commands, allocate hardware, define scientific thresholds, or interrupt your scientific loop.
- {hardware_instruction}
- Calling `_backend.select_backend()` is not GPU acceleration by itself. If CUDA is selected, the expensive computation must actually run on CUDA tensors. If CPU is selected despite available CUDA, record a concrete task-specific reason.
- There is no cycle limit. Keep iterating toward the paper until the result is honestly ready for independent review; external process failures are handled by the host.

{execution_binding_section}

## Paper-faithful core-claim objective
Your target is a faithful implementation that supports the assigned figure's core scientific claim. Pursue close numerical and visual agreement, but never trade away explicit paper facts to obtain it. Reproduce every observable detail that can be grounded or responsibly inferred:
- scientific content: all curves, baselines, parameter settings, sample regimes, statistics, ordering, crossings, slopes, saturation points, extrema, and annotations;
- axes: variables, units, transforms, linear/log scale, limits, tick locations, and normalization;
- presentation: subplot structure, aspect ratio, legend entries/order/location, labels, markers, line styles, colors, error bars, reference lines, and captions visible in the target;
- numerical agreement: compare key coordinates, relative gaps, thresholds, and curve shapes, using tolerances appropriate to the paper evidence and its visual resolution.

The core claim normally consists of the claimed method identity, comparison direction, ordering, trend, crossing or threshold region, scaling behavior, gain/loss region, or other conclusion the figure is used to establish. Exact pixels, styling, undisclosed nuisance parameters, and small numerical offsets are secondary unless the paper's claim depends on them. The paper itself is the authority. If the upstream task description conflicts with the paper, follow the paper and record the correction.

## Mandatory self-iteration protocol
You are not a one-shot report writer. You are the coder, runner, and first-pass reviewer for this task. Missing parameters, modeling uncertainty, non-identifiability, or an imperfect result are reasons to investigate rather than stop reflexively; only a paper-grounded material blocker should trigger scientific changes and another full.

For each cycle:
1. Before writing code, open `paper_evidence/analysis_artifacts/manifest.json`, verify the finalized facts/tasks/index are present, and inspect the copied original paper plus the complete rendered-page index. Task-scoped facts and text previews are navigation hints only and may be incomplete.
2. Inspect the assigned task, finalized upstream artifacts, and the full paper. Build your checklist from the actual target figure: identity, panels, curves, baselines, equations, parameters, axes, scales, statistics, annotations, and style.
3. Resolve missing parameters in this order: finalized facts/tasks/thesis/index; copied original paper under `paper_evidence/source/`; captions and neighboring text; equations, tables, appendices, references, and all available page images. Record the source for every recovered value.
   Read `repro_tasks.json` `_meta.fact_gap_handoff` as prior-search context. Its unresolved entries are navigation aids, not permission to stop: inspect the full paper yourself, then make an explicit testable assumption only when the evidence remains unavailable. Read optional `analysis_warnings.json` the same way: warnings identify uncertain upstream evidence but never override the paper or excuse an incomplete search.
4. Only after that search fails, make a bold but scientifically plausible explicit value or implementation assumption. Put it in code/config and `parameter_resolution`; never silently invent it or relabel it as a paper fact. An assumption may complete an unspecified step but must not replace an explicit model, data-generating law, objective, or core algorithm.
5. Implement or revise the task code and configuration. Do not digitize or hard-code the target curves as the simulated result.
6. Run smoke only as a quick sanity check, then run full directly with `python -m tasks.{module} config.json` when enabled.
7. Inspect the local CSV/summary/PNG side by side with the paper figure. First verify fidelity to explicit facts and support for the core claim; then classify remaining differences as material blockers, acceptable paper-silent assumptions, or non-material numerical/presentation differences.
8. For every material blocker, form a concrete hypothesis about equations, parameters, assumptions, normalization, statistics, backend precision, axis scaling, or baseline implementation. Modify code/config/model and run full again. Do not modify faithful scientific code solely for typography or pixel-level agreement.
9. Record each cycle in `task_agent_result.md`: evidence searched, recovered parameters or assumptions, backend/device, command, duration, return code, the core-claim comparison, material blockers, non-material caveats, the concrete modification plan, changed files, and the result after modification.

Do not stop while a material scientific blocker remains and an evidence-based change is available. Conversely, do not keep changing a paper-faithful implementation after the core claim is supported merely because exact values, undisclosed choices, or presentation details could still be debated. Never rerun unchanged code merely to consume a cycle. Do not emit `explained_gap`, self-declare `failed`, or claim final `matched`; hand off only as `ready_for_review` after a successful full and direct paper comparison.

## Comparison-driven iteration loop
Do not score, rank, or maintain a hypothesis queue. There is no fixed iteration count. Use the direct loop below until explicit paper facts are respected and the core claim is supported:

1. Run full and compare the local CSV/summary/PNG with the paper figure across explicit model/data/algorithm fidelity, core-claim support, curves/baselines, numerical shape, axes/scales, statistics, annotations, and style.
2. If a material scientific blocker remains, write a concrete modification plan before editing. State the paper evidence, what appears wrong, which parameter/equation/baseline/config choice will change, which files will change, and what direction the result should move.
3. Apply that modification plan. Keep each iteration coherent enough that its effect can be understood; do not change unrelated parts merely to create activity.
4. Run smoke if useful, then run a fresh full. Compare the same figure details again and record whether each targeted difference improved, worsened, or stayed unchanged.
5. Keep, revert, or revise the change based on the comparison, then write the next modification plan and repeat.

When a value or implementation detail is missing, search the complete paper first. If it is still absent, choose a scientifically plausible value, algorithm, or small range, label it as assumed, run it, and revise it from the comparison result. For a material assumption, prefer a small sensitivity check when practical so the core claim is not supported only at one finely tuned point. Never repeat an unchanged full and never count prose, metadata, locator, or report-only edits as progress.

Your only normal handoff decision is `ready_for_review`: the latest full is successful, explicit paper facts remain intact, the task's core claim is supported, assumptions are disclosed, and no paper-grounded material blocker remains. Before handing off, revisit the complete paper, assumptions, equations, parameter ranges, baseline definitions, numerical methods, and statistics. Do not delay handoff for non-material styling differences or merely because another undocumented implementation is conceivable. External Codex/runtime failure is handled by the host and must not be converted into a scientific conclusion.

Stopping rule:
- If an explicit paper fact is materially violated, or the core claim is unsupported, and a concrete paper-grounded change is available, continue iterating and rerun full.
- If the latest full respects explicit facts, supports the core claim, and only disclosed paper-silent assumptions or non-material differences remain, write the review delivery.
- Never hide or hand-wave a material gap. Record it and use it to drive the next evidence-based modification; disclose non-material gaps as caveats without manufacturing another run.
- If this Codex session ends before `ready_for_review`, the host will reopen the same sandbox and require you to continue.
- The reporter may return concrete differences. The host then reopens this same sandbox; accepted tasks are not rerun.

## Required final files
- `task_agent_result.md`: Chinese audit log of your implementation and scientific iteration. It will not be appended to the final report.
- `task_agent_result.json`: write this delivery only after a successful full and direct paper comparison; it is a strict JSON object with:
```json
{{
{component_usage_template}
  "task_id": "{task_id}",
  "status": "ready_for_review",
  "summary": "one Chinese sentence",
  "differences": [],
  "possible_causes": [],
  "remaining_uncertainties": [],
  "evidence_files": [],
  "local_image_paths": [],
  "parameter_resolution": [
    {{"name": "parameter", "value": "value", "source": "paper|derived|assumed", "evidence": "file/page/equation or assumption rationale"}}
  ],
  "detail_comparison": {{
    "curves_and_baselines": "comparison and tolerance",
    "axes_and_scales": "comparison and tolerance",
    "numerical_shape": "comparison and tolerance",
    "statistics": "comparison and tolerance",
    "annotations_and_style": "comparison and tolerance"
  }},
  "iteration_records": [
    {{
      "full_run_index": 1,
      "comparison": ["measured local-vs-paper differences after this full"],
      "modification_plan": ["specific changes proposed after this comparison; empty only when terminal"],
      "changes_applied": ["code/config/model/parameter changes applied before the next full"],
      "outcome": "improved|worsened|unchanged|continuing|ready_for_review"
    }}
  ],
  "execution_summary": {{
    "commands": [],
    "full_run_count": 0,
    "last_returncode": null,
    "cuda_available": false,
    "backend_requested": "auto|cpu|cuda",
    "backend": "cpu|cuda|other",
    "device": "human-readable device name",
    "actual_compute_device_evidence": "how the expensive computation was verified on this device",
    "backend_choice_reason": "task-specific reason",
    "full_durations_s": []
  }}
}}
```
- A delivery without a successful full execution, local result images, and a concrete comparison summary is invalid and will be relaunched.

## Independent reporter feedback from a previous delivery
```json
{feedback_text}
```
If feedback is present, investigate every reported difference against the paper's evidence hierarchy. Fix and rerun for a material paper-grounded blocker. Do not alter explicit paper facts, do not blindly obey speculative feedback, and do not rerun unchanged code for an acceptable assumption or non-material caveat.

## Trusted runtime APIs
{IO_RUNTIME_API_DOC}

{BACKEND_RUNTIME_API_DOC}

## Dependency policy
{dependency_policy_prompt_text()}

## Mandatory complete inputs
- `paper_evidence/index.json`
- the copied original paper path recorded by `paper_evidence/index.json` under `paper_source.relative_path`
- `paper_evidence/analysis_artifacts/manifest.json`
- `paper_evidence/analysis_artifacts/engineering_facts.json`
- `paper_evidence/analysis_artifacts/repro_tasks.json`
- `paper_evidence/analysis_artifacts/experiment_index.json`
- `paper_evidence/analysis_artifacts/scientific_architecture.json` when present; it is mandatory in workflow v2
- `paper_evidence/analysis_artifacts/paper_thesis.json` when present
- `paper_evidence/analysis_artifacts/analysis_warnings.json` when present
- `paper_evidence/full_paper_pages/index.json` and every page image listed there

## Task-scoped navigation aids
- `paper_evidence/01_{safe_label(task_id)}/evidence.json`
- `paper_evidence/01_{safe_label(task_id)}/context.md`
## Task JSON
```json
{pretty_json(task)}
```

## Manifest entry
```json
{pretty_json(manifest_entry)}
```

## Task-scoped facts preview (not the information boundary)
```json
{pretty_json(task_facts)}
```

## Paper thesis / ordering anchor
{ordering_anchor or "None"}

## Task paper context
{task_context[:12000]}

## Truncated paper-context preview (read the copied paper for complete context)
{paper_context_json[:8000]}

## Experiment index
```json
{pretty_json(experiment_index)[:8000]}
```
"""


def _task_result_file_path(sandbox: Path, output_subdir: str, filename: str) -> tuple[Path, bool]:
    root_path = sandbox / filename
    if root_path.exists():
        return root_path, False
    output_path = sandbox / "outputs" / output_subdir / filename
    if output_path.exists():
        return output_path, True
    return root_path, False


def _merge_task_writer_deliveries(
    *,
    repro_project_dir: Path,
    task_manifest: dict[str, Any],
    expected_paths: set[str],
    task_records: list[dict[str, Any]],
    foundation: dict[str, Any] | None = None,
) -> set[str]:
    _write_final_shared_project_files(
        repro_project_dir, task_records, write_placeholders=foundation is None
    )
    if foundation is not None:
        expected_paths.update(install_foundation_snapshot(repro_project_dir, foundation))
    configs_dir = repro_project_dir / "configs"
    configs_dir.mkdir(parents=True, exist_ok=True)
    combined_requirements: list[str] = ["numpy", "matplotlib"]
    if foundation is not None:
        combined_requirements.clear()
    copied_task_files: dict[str, tuple[str, str]] = {}
    for record in task_records:
        sandbox = Path(str(record.get("sandbox") or ""))
        module = str(record.get("module") or "")
        output_subdir = str(record.get("output_subdir") or record.get("task_id") or "")
        if not sandbox.exists():
            continue
        for source in _task_owned_files(sandbox):
            relative = source.relative_to(sandbox).as_posix()
            content_hash = hashlib.sha256(source.read_bytes()).hexdigest()
            previous = copied_task_files.get(relative)
            if previous is not None and previous[0] != content_hash:
                raise RuntimeError(
                    "task writer source collision for "
                    f"{relative}: {previous[1]} and {record.get('task_id')} supplied different content"
                )
            target = repro_project_dir / Path(relative)
            if source.suffix.lower() == ".py":
                _copy_python_without_bom(source, target)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
            copied_task_files[relative] = (content_hash, str(record.get("task_id") or module))
            expected_paths.add(relative)
        for config_name, target_name in (
            ("config.json", f"{module}_config.json"),
            ("config_smoke.json", f"{module}_config_smoke.json"),
        ):
            source = sandbox / config_name
            if source.exists():
                target = configs_dir / target_name
                shutil.copy2(source, target)
                expected_paths.add(f"configs/{target_name}")
        source_output = sandbox / "outputs" / output_subdir
        if source_output.exists():
            target_output = repro_project_dir / "outputs" / output_subdir
            if target_output.exists():
                shutil.rmtree(target_output)
            shutil.copytree(source_output, target_output, ignore=shutil.ignore_patterns("paper_target*"))
        result_dir = repro_project_dir / "outputs" / output_subdir
        result_dir.mkdir(parents=True, exist_ok=True)
        for name in ("task_agent_result.json", "task_agent_result.md"):
            source, _ = _task_result_file_path(sandbox, output_subdir, name)
            if source.exists():
                shutil.copy2(source, result_dir / name)
        req_path = sandbox / "requirements.txt"
        if req_path.exists():
            combined_requirements.extend(_read_requirement_names(req_path))
    write_text(repro_project_dir / "requirements.txt", _format_requirements(combined_requirements))
    _prune_unexpected_files(repro_project_dir, expected_paths)
    return expected_paths


def _task_source_files(sandbox: Path) -> list[Path]:
    """Return every task-owned Python module, including transitive helpers.

    A writer sandbox contains exactly one task scaffold. Copying the complete
    task package is safer than guessing a single ``<module>_lib.py`` filename
    and keeps imports such as ``from tasks import _fig6_full_dd`` intact.
    """

    return [path for path in _task_owned_files(sandbox) if path.suffix.lower() == ".py"]


def _task_owned_files(sandbox: Path) -> list[Path]:
    """Return the complete task package dependency closure without following links."""

    task_root = sandbox / "tasks"
    if not task_root.is_dir():
        return []
    safe: list[Path] = []
    task_root_resolved = task_root.resolve()
    for path in sorted(task_root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(task_root).as_posix()
        if relative == "__init__.py" or "__pycache__" in path.parts or path.suffix.lower() in {".pyc", ".pyo"}:
            continue
        try:
            path.resolve().relative_to(task_root_resolved)
        except ValueError:
            continue
        safe.append(path)
    return safe


def _writer_snapshot_hash(analysis_hash: str, foundation_hash: str) -> str:
    payload = f"{analysis_hash}::{foundation_hash}".encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def _write_final_shared_project_files(
    repro_project_dir: Path,
    task_records: list[dict[str, Any]],
    *,
    write_placeholders: bool = True,
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
    placeholder_names = ("channel.py", "modulation.py", "metrics.py", "simulation.py") if write_placeholders else ()
    for name in placeholder_names:
        write_text(
            repro_project_dir / "src" / name,
            '"""Shared placeholder for task-writer mode; task-specific logic lives in tasks/*.py."""\n',
        )


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


def _task_writer_runtime_result(
    *,
    task_records: list[dict[str, Any]],
    validation: dict[str, Any],
    requirement_warnings: list[dict[str, Any]],
    security_issues: list[dict[str, Any]],
    manifest_issues: list[dict[str, Any]] | None = None,
    requirement_issues: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    del manifest_issues, requirement_issues
    passed = sum(1 for record in task_records if _task_writer_runtime_task_passed(record))
    delivered = sum(1 for record in task_records if record.get("writer_completed"))
    total = len(task_records)
    valid_task_ids = [str(record.get("task_id")) for record in task_records if _task_writer_runtime_task_passed(record)]
    valid_csv_files: list[str] = []
    valid_png_files: list[str] = []
    valid_summary_json_files: list[str] = []
    for record in task_records:
        if not _task_writer_runtime_task_passed(record):
            continue
        artifacts = record.get("artifacts") if isinstance(record.get("artifacts"), dict) else {}
        output_subdir = str(record.get("output_subdir") or record.get("task_id") or "")
        csv_files = artifacts.get("csv_files") if isinstance(artifacts.get("csv_files"), list) else []
        png_files = artifacts.get("png_files") if isinstance(artifacts.get("png_files"), list) else []
        summary_files = artifacts.get("summary_json_files") if isinstance(artifacts.get("summary_json_files"), list) else []
        valid_csv_files.extend(f"{output_subdir}/{item}" for item in csv_files if isinstance(item, str))
        valid_png_files.extend(f"{output_subdir}/{item}" for item in png_files if isinstance(item, str))
        valid_summary_json_files.extend(
            f"{output_subdir}/{item}" for item in summary_files if isinstance(item, str)
        )
    blocking_security = any(
        "syntax error" in str(issue.get("message") or "").lower()
        for issue in security_issues
        if isinstance(issue, dict)
    )
    all_checks_passed = (
        total > 0
        and passed == total
        and validation.get("required_files_present") is True
        and validation.get("python_compiles") is not False
        and validation.get("local_imports_resolve") is True
        and not blocking_security
    )
    return {
        "enabled": True,
        "passed": bool(all_checks_passed),
        "run_profile": "task_writer_full",
        "repair_backend": "codex_task_writers",
        "per_task_orchestration": True,
        "host_repeated_full": False,
        "tasks_total": total,
        "tasks_passed": passed,
        "coverage": f"{passed}/{total}",
        "deliveries_passed": delivered,
        "delivery_coverage": f"{delivered}/{total}",
        "partial_success": {
            "has_partial_output": bool(0 < passed < total),
            "valid_task_ids": valid_task_ids,
            "valid_csv_files": valid_csv_files,
            "valid_png_files": valid_png_files,
            "valid_summary_json_files": valid_summary_json_files,
        },
        "per_task": [
            {
                "task_id": record.get("task_id"),
                "module": record.get("module"),
                "passed": _task_writer_runtime_task_passed(record),
                "writer_completed": bool(record.get("writer_completed")),
                "task_writer_status": record.get("task_writer_status"),
                "writer_error_kind": record.get("writer_error_kind"),
                "blocked_reason": record.get("blocked_reason"),
                "task_reporter_verdict": (
                    record.get("task_verification", {}).get("verdict")
                    if isinstance(record.get("task_verification"), dict)
                    else None
                ),
                "task_reporter_revision_target": (
                    record.get("task_verification", {}).get("revision_target")
                    if isinstance(record.get("task_verification"), dict)
                    else None
                ),
                "execution_summary": record.get("execution_summary"),
                "artifacts": record.get("artifacts"),
            }
            for record in task_records
        ],
        "validation": validation,
        "requirements_warnings": requirement_warnings,
        "requirements_issues": [],
        "security_issues": security_issues,
    }


def _task_writer_runtime_task_passed(record: dict[str, Any]) -> bool:
    return bool(record.get("writer_completed")) and str(record.get("task_writer_status") or "") in {
        WRITER_REVIEW_STATUS,
        FINAL_MATCHED_STATUS,
    }


def apply_verified_result(
    *,
    task_records: list[dict[str, Any]],
    verification_result: dict[str, Any],
    output_dir: Path,
    audit_dir: Path,
    repro_project_dir: Path,
) -> dict[str, Any]:
    """Grant final matched after direct independent paper comparison."""

    expected_task_ids = [str(record.get("task_id") or "") for record in task_records]
    result_issues = verification_result_issues(verification_result, expected_task_ids)
    if result_issues:
        raise ValueError("cannot grant matched from an invalid verification result: " + "; ".join(result_issues))
    if not verification_result.get("all_accepted"):
        raise ValueError("cannot grant matched while any task requires revision")

    for record in task_records:
        task_id = str(record.get("task_id") or "")
        delivery_blockers, _ = partition_writer_delivery_issues(record.get("result_json"))
        if delivery_blockers:
            raise ValueError(
                f"cannot grant matched from an invalid writer delivery for {task_id}: "
                + "; ".join(delivery_blockers)
            )

    verdict_by_id = {
        str(item.get("task_id")): item
        for item in verification_result.get("tasks", [])
        if isinstance(item, dict) and str(item.get("task_id") or "")
    }
    for record in task_records:
        task_id = str(record.get("task_id") or "")
        task_verdict = verdict_by_id.get(task_id)
        if not isinstance(task_verdict, dict) or task_verdict.get("verdict") != "accepted":
            raise ValueError(f"cannot grant matched without accepted verdict for {task_id}")
        record["task_writer_status"] = FINAL_MATCHED_STATUS
        record["verification_result"] = task_verdict
        record["verification_verified"] = True

    previous_runtime = _read_optional_json_object(output_dir / "runtime_result.json")
    runtime_result = _task_writer_runtime_result(
        task_records=task_records,
        validation=(
            previous_runtime.get("validation")
            if isinstance(previous_runtime.get("validation"), dict)
            else {"host_validation_skipped": True}
        ),
        requirement_warnings=(
            previous_runtime.get("requirements_warnings")
            if isinstance(previous_runtime.get("requirements_warnings"), list)
            else []
        ),
        security_issues=(
            previous_runtime.get("security_issues")
            if isinstance(previous_runtime.get("security_issues"), list)
            else []
        ),
    )
    runtime_result["verification_verified"] = True
    runtime_result["verification_mode"] = "direct_paper_comparison"
    write_json(output_dir / "runtime_result.json", runtime_result)
    write_json(
        audit_dir / "03c_task_writers_records.json",
        {"verification_result": verification_result, "tasks": task_records},
    )
    status_path = audit_dir / "03c_task_writers_status.json"
    status = _read_optional_json_object(status_path)
    status.update(
        {
            "stop_class": "verified_matched",
            "stopped_reason": "all tasks passed direct independent paper verification",
            "runtime": {"passed": True, "coverage": runtime_result.get("coverage")},
            "tasks": [
                {
                    "task_id": record.get("task_id"),
                    "status": FINAL_MATCHED_STATUS,
                    "verification_verified": True,
                }
                for record in task_records
            ],
        }
    )
    write_json(status_path, status)
    config_path = repro_project_dir / "config.json"
    config = _read_optional_json_object(config_path)
    config["task_statuses"] = {
        str(record.get("task_id")): FINAL_MATCHED_STATUS for record in task_records
    }
    config["verification_verified"] = True
    write_json(config_path, config)
    return runtime_result


def _task_writer_alignment_summary(task_records: list[dict[str, Any]]) -> dict[str, Any]:
    if not task_records:
        return {
            "overall_alignment": "inconclusive",
            "overall_result_credibility": "low",
            "overall_summary": "没有可审查的复现任务。",
        }
    if any(_task_writer_blocked_by_codex(record) for record in task_records):
        return {
            "overall_alignment": "inconclusive",
            "overall_result_credibility": "low",
            "overall_summary": "至少一个 Codex task writer 因额度或限流被阻塞，不能把缺失任务视为科学复现失败。",
        }
    if any(not record.get("writer_completed") for record in task_records):
        return {
            "overall_alignment": "inconclusive",
            "overall_result_credibility": "low",
            "overall_summary": "部分自治 writer 未正常完成，不能给出强复现结论。",
        }
    statuses = {str(record.get("task_writer_status") or "failed") for record in task_records}
    if statuses <= {WRITER_REVIEW_STATUS, FINAL_MATCHED_STATUS}:
        return {
            "overall_alignment": "candidate",
            "overall_result_credibility": "medium",
            "overall_summary": "所有自治 writer 均已完成 full 并提交待独立审查的结果。",
        }
    return {
        "overall_alignment": "inconclusive",
        "overall_result_credibility": "low",
        "overall_summary": "至少一个 writer 遭遇外部进程错误，任务尚未完成。",
    }


def _compact_task_writer_review(record: dict[str, Any]) -> dict[str, Any]:
    result = record.get("result_json") if isinstance(record.get("result_json"), dict) else {}
    return {
        "task_id": record.get("task_id"),
        "task_writer_status": record.get("task_writer_status"),
        "writer_completed": record.get("writer_completed"),
        "summary": result.get("summary"),
        "differences": result.get("differences", []),
        "possible_causes": result.get("possible_causes", []),
        "remaining_uncertainties": result.get("remaining_uncertainties", []),
        "evidence_files": result.get("evidence_files", []),
        "writer_error_kind": record.get("writer_error_kind"),
        "blocked_reason": record.get("blocked_reason"),
    }


def _task_writer_concurrency(task_count: int, requested: int | None, *, run_repro: bool = False) -> int:
    del requested, run_repro
    return max(1, task_count)


def _task_writer_stop_class(task_records: list[dict[str, Any]]) -> str:
    if not task_records:
        return "no_tasks"
    if any(_task_writer_blocked_by_codex(record) for record in task_records):
        return "blocked_by_codex"
    if any(not record.get("writer_completed") for record in task_records):
        return "writer_failures"
    if any(record.get("task_writer_status") == "failed" for record in task_records):
        return "external_failures"
    return "ready_for_review"


def _task_writer_stopped_reason(task_records: list[dict[str, Any]]) -> str:
    stop_class = _task_writer_stop_class(task_records)
    return {
        "no_tasks": "no reproduction tasks were available",
        "blocked_by_codex": "one or more Codex task writers were blocked by usage limits or rate limits",
        "writer_failures": "one or more autonomous task writers did not complete",
        "external_failures": "one or more task writers stopped because of an external process failure",
        "ready_for_review": "all task writers submitted successful full results for independent verification",
    }.get(stop_class, stop_class)


def _failed_task_record(*, index: int, task_id: str, module: str, error: str) -> dict[str, Any]:
    return {
        "index": index,
        "task_id": task_id,
        "module": module,
        "task_writer_status": "failed",
        "writer_completed": False,
        "writer_status": {"ok": False, "error": redact_text(error)[:1000]},
        "result_json": {"task_id": task_id, "status": "failed", "summary": redact_text(error)[:500]},
        "local_images": [],
    }


def _task_writer_blocked_by_codex(record: dict[str, Any]) -> bool:
    return str(record.get("writer_error_kind") or "") in {
        "codex_usage_limit",
        "codex_rate_limit",
    }
