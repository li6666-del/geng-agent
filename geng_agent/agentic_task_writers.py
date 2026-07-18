from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, as_completed
import json
import shutil
import sys
from pathlib import Path
from typing import Any, Callable

from .verification_result import (
    FINAL_MATCHED_STATUS,
    WRITER_REVIEW_STATUS,
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
from .codex_runner import run_codex_subprocess
from .config import get_config_value
from .io_runtime import BACKEND_RUNTIME_API_DOC, IO_RUNTIME_API_DOC, inject_io_runtime
from .json_utils import pretty_json
from .manifest_utils import expected_generated_paths
from .outputs import inspect_output_artifacts, write_json, write_text
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
    timeout: float = 1800.0,
    run_timeout: float = 120.0,
    resume: bool = True,
    review_feedback: dict[str, dict[str, Any]] | None = None,
    force_task_ids: set[str] | None = None,
    task_review_callback: Callable[[int, dict[str, Any], dict[str, Any], int], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Third-round autonomous per-task Codex writer workflow.

    Each task gets an isolated sandbox and one Codex writer that owns code,
    full execution, and task-level paper comparison. The host does not run a
    separate reviewer and does not repeat the full run after merging.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    audit_dir.mkdir(parents=True, exist_ok=True)
    del timeout, run_timeout

    analysis_artifacts = _collect_writer_analysis_artifacts(output_dir=output_dir)
    missing_analysis_artifacts = _missing_required_analysis_artifacts(analysis_artifacts)
    if missing_analysis_artifacts:
        raise RuntimeError(
            "task writers require finalized first-two-stage artifacts: "
            + ", ".join(missing_analysis_artifacts)
        )
    analysis_snapshot_hash = _analysis_snapshot_hash(
        paper_path=paper_path,
        artifacts=analysis_artifacts,
    )

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
        analysis_snapshot_hash=analysis_snapshot_hash,
        analysis_artifacts=analysis_artifacts,
        task_root=task_root,
        audit_dir=audit_dir,
        run_repro=run_repro,
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
    )
    _restore_trusted_files(repro_project_dir, task_manifest)
    final_task_manifest = _task_manifest_with_configs(task_manifest)
    write_json(repro_project_dir / "tasks_manifest.json", final_task_manifest)
    validation = {
        "required_files_present": True,
        "missing_files": [],
        "python_compiles": True,
        "compile_errors": [],
        "host_validation_skipped": True,
    }
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
    return not writer_delivery_issues(record.get("result_json"))


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
    analysis_snapshot_hash: str,
    analysis_artifacts: dict[str, Path],
    task_root: Path,
    audit_dir: Path,
    run_repro: bool,
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
        "runtime_limit": None,
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
                    analysis_snapshot_hash=analysis_snapshot_hash,
                    analysis_artifacts=analysis_artifacts,
                    task_root=task_root,
                    audit_dir=audit_dir,
                    run_repro=run_repro,
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
    analysis_snapshot_hash: str,
    analysis_artifacts: dict[str, Path],
    task_root: Path,
    audit_dir: Path,
    run_repro: bool,
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
    )
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
        )

        _restore_trusted_files(sandbox, {"version": 1, "tasks": [manifest_entry]})
        record = _collect_task_writer_delivery(
            index=index,
            task=task,
            manifest_entry=manifest_entry,
            sandbox=sandbox,
            writer_status=writer_status,
        )
        record["analysis_snapshot_hash"] = analysis_snapshot_hash
        record["writer_session_count"] = session_round
        if not writer_status.get("ok") or not run_repro:
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
        timeout=None,
        command_override=get_config_value("GENG_CODEX_TASK_WRITER_CMD"),
        image_paths=sorted(
            path.resolve()
            for path in (sandbox / PAPER_EVIDENCE_DIR / "full_paper_pages").glob("paper_page_*.png")
            if path.is_file()
        ),
        extra_env={"GENG_PYTHON_EXECUTABLE": sys.executable},
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
    status = TASK_WRITER_TERMINAL_STATUS if not delivery_issues else "failed"
    if not writer_status.get("ok"):
        status = "failed"

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
        "writer_completed": bool(writer_status.get("ok")),
        "task_writer_status": status,
        "writer_reported_status": reported_status or None,
        "delivery_validation_issues": delivery_issues,
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
        return
    if sandbox.exists():
        shutil.rmtree(sandbox)
    sandbox.mkdir(parents=True, exist_ok=True)
    single_manifest = {"version": 1, "tasks": [manifest_entry]}
    inject_io_runtime(sandbox)
    write_task_scaffolding(sandbox, single_manifest)
    _write_minimal_shared_project_files(sandbox, task, manifest_entry)
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


def _write_minimal_shared_project_files(sandbox: Path, task: dict[str, Any], manifest_entry: dict[str, Any]) -> None:
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
    return f"""# Role: autonomous Codex task writer

You own exactly one reproduction task. Write the code, run the assigned full experiment, compare the result directly with the complete paper, and keep revising and rerunning while a paper-grounded material scientific blocker remains. Your handoff is `ready_for_review`; only the independent reporter may grant final `matched`.

{WRITER_PAPER_FIDELITY_POLICY}

## Ownership
- Assigned task_id: `{task_id}`
- Assigned module: `tasks.{module}`
- Output directory: `outputs/{output_subdir}/`
- You own this isolated sandbox. You may create or edit any code, config, helper, dependency, and output needed for this task.
- Do not edit `src/_io.py`, `src/_backend.py`, `run_experiment.py`, `tasks_manifest.json`, `tasks/__init__.py`, or any other task module.
- {full_instruction}
- You may run smoke with `python -m tasks.{module} config_smoke.json`.
- Run Python and dependencies directly. The host does not guard commands, allocate hardware, define scientific thresholds, or interrupt your scientific loop.
- Inspect the available hardware yourself and choose CPU, CUDA, memory use, batch size, and parallelism appropriate to the task. For Monte Carlo, batched matrix operations, large sweeps, or a CPU full likely to take minutes, prefer a real Torch CUDA implementation when CUDA is available.
- Calling `_backend.select_backend()` is not GPU acceleration by itself. If CUDA is selected, the expensive computation must actually run on CUDA tensors. If CPU is selected despite available CUDA, record a concrete task-specific reason.
- There is no cycle limit. Keep iterating toward the paper until the result is honestly ready for independent review; external process failures are handled by the host.

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
) -> set[str]:
    _write_final_shared_project_files(repro_project_dir, task_records)
    configs_dir = repro_project_dir / "configs"
    configs_dir.mkdir(parents=True, exist_ok=True)
    combined_requirements: list[str] = ["numpy", "matplotlib"]
    for record in task_records:
        sandbox = Path(str(record.get("sandbox") or ""))
        module = str(record.get("module") or "")
        output_subdir = str(record.get("output_subdir") or record.get("task_id") or "")
        if not sandbox.exists():
            continue
        script_source = sandbox / "tasks" / f"{module}.py"
        script_target = repro_project_dir / "tasks" / f"{module}.py"
        if script_source.exists():
            _copy_python_without_bom(script_source, script_target)
        lib_source = sandbox / "tasks" / f"{module}_lib.py"
        if lib_source.exists():
            lib_target = repro_project_dir / "tasks" / f"{module}_lib.py"
            _copy_python_without_bom(lib_source, lib_target)
            expected_paths.add(f"tasks/{module}_lib.py")
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


def _write_final_shared_project_files(repro_project_dir: Path, task_records: list[dict[str, Any]]) -> None:
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
    for name in ("channel.py", "modulation.py", "metrics.py", "simulation.py"):
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
        and validation.get("python_compiles") is not False
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
        delivery_issues = writer_delivery_issues(record.get("result_json"))
        if delivery_issues:
            raise ValueError(
                f"cannot grant matched from an invalid writer delivery for {task_id}: "
                + "; ".join(delivery_issues)
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
