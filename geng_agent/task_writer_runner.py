"""Run singleton and compound task-writer Codex state machines."""

from __future__ import annotations

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from pathlib import Path
from typing import Any, Callable

from .agentic_foundation import _assert_foundation_sandbox_layout_safe, foundation_violations, restore_foundation_snapshot
from .case_runtime import CaseRuntime
from .codex_runner import run_codex_subprocess
from .execution_receipts import ExecutionBroker, trusted_input_snapshot, find_host_execution
from .foundation_revision import read_foundation_revision_request
from .config import get_config_value
from .outputs import write_json, write_text
from .progress import PipelineCancelled
from .paper_evidence import safe_label
from .security import redact_text
from .task_writer_contracts import DEFAULT_MAX_EVIDENCE_RERUNS, TASK_WRITER_TERMINAL_STATUS
from .task_writer_delivery import _collect_task_writer_delivery
from .task_writer_execution_binding import _load_task_execution_binding
from .task_writer_prompts import _build_execution_unit_continuation_brief, _build_execution_unit_writer_brief, _build_task_writer_brief, _build_task_writer_continuation_brief
from .task_writer_sandbox import _prepare_execution_unit_writer_sandbox, _prepare_task_writer_sandbox
from .task_writer_inputs import write_writer_input, unique_image_paths
from .task_writer_state import (
    _archive_execution_unit_delivery,
    _archive_nonterminal_writer_delivery,
    _complete_execution_unit_runtime_refresh,
    _complete_task_writer_runtime_refresh,
    _next_writer_progress_round,
    _record_source_config_fingerprint,
    _rerun_evidence_fingerprint,
    _task_writer_runtime_refresh_marker,
    _terminalize_rerun_request,
    _writer_source_config_fingerprint,
    _writer_progress_fingerprint,
)
from .task_writer_support import PAPER_EVIDENCE_DIR, _restore_trusted_files
from .task_writer_units import _execution_unit_sandbox, _public_execution_unit
from .verification_result import rerun_evidence_path_issues, task_verification_issues
from .writer_recovery import localize_writer_feedback, writer_recovery_context
from .writer_environment import ensure_writer_environment, snapshot_writer_environment
from .task_recovery import recovery_state_id, recover_writer_stall, resolve_revision_owner
from .supervisor import NodeFailure, StageBlocked, REPLAY_REQUIRED, current_supervisor, supervised_call


def _external_writer_rerun_budget() -> int:
    """Return a generous emergency cap, optionally overridden by configuration."""

    raw = get_config_value("GENG_TASK_WRITER_MAX_EVIDENCE_RERUNS")
    if not raw:
        return DEFAULT_MAX_EVIDENCE_RERUNS
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MAX_EVIDENCE_RERUNS
    return value if value >= 0 else DEFAULT_MAX_EVIDENCE_RERUNS

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


def _execution_unit_rerun_fingerprints(
    feedback_by_task_id: dict[str, dict[str, Any]],
    sandbox: Path | None = None,
) -> set[str]:
    return {
        json.dumps(
            {
                "task_id": task_id,
                "rerun_evidence": _rerun_evidence_fingerprint(
                    feedback.get("rerun_evidence")
                    , _writer_progress_fingerprint(sandbox) if sandbox is not None else ""
                ),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        for task_id, feedback in sorted(feedback_by_task_id.items())
    }


class _PreparedReporterResult(dict):
    """In-memory routing for this callback/replay, never a persisted authority."""

    revision_route: dict[str, Any] | None = None
    prepared_state: str | None = None


def _obtain_reporter_review(callback, index, task, record, round_no):
    reporter = callback(index, task, record, round_no)
    return _prepare_reporter_review(reporter, callback, index, task, record, round_no)


def _prepare_reporter_review(reporter, callback, index, task, record, round_no,
                             *, clarification_allowed=True):
    if not isinstance(reporter, dict) or not reporter.get("ok"):
        return reporter
    verification = reporter.get("task_verification")
    task_id = str(task.get("task_id") or record.get("task_id") or "")
    if not isinstance(verification, dict) or verification.get("host_action") != "rerun_writer":
        return reporter
    if isinstance(reporter, _PreparedReporterResult):
        # Only an exceptional decision carries in-memory authority, bound to
        # the exact evidence generation. Explicit routes retain the raw result.
        if reporter.prepared_state == recovery_state_id(record, "revision_owner"):
            return reporter
    prepared = _PreparedReporterResult(reporter)
    route = resolve_revision_owner(record=record, reporter=reporter, verification=verification)
    if route["action"] == "repair_reporter" and clarification_allowed:
        record["task_reporter"] = reporter
        record["moderator_review_request"] = dict(route.get("decision") or {})
        clarified = callback(index, task, record, round_no)
        return _prepare_reporter_review(clarified, callback, index, task, record, round_no,
                                         clarification_allowed=False)
    if route["action"] == "repair_reporter":
        route = {**route, "action": "stop", "reason": "moderator_reporter_clarification_exhausted"}
    if not route.get("decision"):
        return reporter
    prepared.prepared_state = recovery_state_id(record, "revision_owner")
    prepared.revision_route = route
    return prepared


def _review_task_records(
    *,
    work: list[tuple[int, dict[str, Any], dict[str, Any], int]],
    callback: Callable[[int, dict[str, Any], dict[str, Any], int], dict[str, Any]],
) -> list[tuple[str, dict[str, Any] | None]]:
    """Collect independent reviews before any feedback mutates Writer inputs."""
    if not work:
        return []
    with ThreadPoolExecutor(max_workers=len(work)) as executor:
        futures = [
            executor.submit(copy_context().run, _obtain_reporter_review, callback, index, task, record, round_no)
            for index, task, record, round_no in work
        ]
    # Waiting for the entire batch also protects shared Writer input snapshots
    # on cache misses and resume. One failed callback does not discard siblings.
    return [
        _attach_task_reporter_review(
            callback=lambda *_args, result=future: result.result(),
            index=index, task=task, record=record, session_round=round_no,
        )
        for (index, task, record, round_no), future in zip(work, futures)
    ]


def _review_execution_unit_tasks(
    *,
    members: list[tuple[int, dict[str, Any], dict[str, Any]]],
    records: list[dict[str, Any]],
    callback: Callable[[int, dict[str, Any], dict[str, Any], int], dict[str, Any]],
    session_round: int,
) -> dict[str, dict[str, Any]]:
    """Review each logical task concurrently against the completed Writer output.

    Reporters own separate workspaces. Wait for all of them before attaching
    decisions and localizing feedback into the shared Writer sandbox, so neither
    feedback handling nor a Writer continuation can change another review's input.
    """
    if len(members) != len(records):
        raise ValueError("each logical task must have a Writer delivery before review")
    reviews = _review_task_records(
        work=[
            (index, task, record, session_round)
            for (index, task, _entry), record in zip(members, records)
        ],
        callback=callback,
    )
    requested: dict[str, dict[str, Any]] = {}
    for record, (action, returned) in zip(records, reviews):
        if action == "writer_revision" and isinstance(returned, dict):
            requested[str(record.get("task_id") or "")] = returned
    return requested


def _recover_writer_exceptions(*, work, trigger, writer_budget_available, callback, session_round):
    """One recovery entry for repeated requests and ineffective continuations.

    Decisions and optional Reporter clarifications run independently. Only once
    all callbacks finish may feedback files be localized in a shared sandbox.
    """
    def prepare(index, task, record, verification):
        try:
            route = recover_writer_stall(record=record, verification=verification, trigger=trigger,
                                         writer_budget_available=writer_budget_available)
        except PipelineCancelled:
            raise
        except Exception as exc:
            record["moderator_error"] = redact_text(f"{type(exc).__name__}: {exc}")[:1000]
            return {"action": "stop", "reason": "moderator_recovery_failed"}
        if route["action"] == "repair_reporter" and callback is not None:
            record["moderator_review_request"] = dict(route.get("decision") or {})
            try:
                reporter = _obtain_reporter_review(callback, index, task, record, session_round)
            except PipelineCancelled:
                raise
            except Exception as exc:
                reporter = {"ok": False, "error": redact_text(f"{type(exc).__name__}: {exc}")[:1000]}
            return {"action": "reporter_result", "reporter": reporter}
        return route

    if not work:
        return {}
    with ThreadPoolExecutor(max_workers=len(work)) as executor:
        futures = [executor.submit(copy_context().run, prepare, *item) for item in work]
    requested = {}
    for (index, task, record, verification), future in zip(work, futures):
        route = future.result()
        task_id = str(record.get("task_id") or task.get("task_id") or "")
        if route["action"] == "reporter_result":
            action, feedback = _attach_task_reporter_review(
                callback=lambda *_args, value=route["reporter"]: value,
                index=index, task=task, record=record, session_round=session_round,
                writer_revision_permitted=writer_budget_available,
            )
            if action == "writer_revision" and writer_budget_available:
                requested[task_id] = feedback
            elif action == "writer_revision":
                _terminalize_rerun_request(record=record, verification=feedback,
                    stop_reason="external_rerun_budget_exhausted",
                    uncertainty="Reporter clarification cannot increase the operational Writer rerun budget.")
            continue
        if route["action"] == "revise_foundation":
            record["foundation_revision_request"] = route["request"]
            record["task_reporter_terminal"] = False
            continue
        if route["action"] == "revise_writer" and writer_budget_available:
            reporter = record.get("task_reporter") or {}
            requested[task_id] = localize_writer_feedback(
                route["feedback"], reporter_root=Path(str(reporter.get("workspace") or record.get("sandbox") or "")),
                sandbox=Path(str(record.get("sandbox") or "")),
                output_subdir=str(record.get("output_subdir") or task_id),
            )
            continue
        _terminalize_rerun_request(record=record, verification=record.get("task_verification") or verification,
            stop_reason=trigger,
            uncertainty="The recovery coordinator stopped this repair; no new full run is authorized and the scientific conclusion is retained.")
    return requested


def _supervise_writer_delivery(*, node_id, produce, archive, tasks, sandbox, analysis_snapshot_hash, reconcile_first=False):
    """Approve one actual Writer handoff, retaining local failure evidence.

    Called inside each existing worker thread, never around the dispatcher.
    The original Writer is the only actor allowed to repair scientific files.
    """
    latest = None
    reconciled = None
    supervisor = current_supervisor()
    dispatch_assignment = (supervisor.current_instruction("tool:writers:" + node_id.rsplit(":round:", 1)[0])
                           if supervisor is not None else None)
    instructions = dispatch_assignment
    attempt = 1
    handoff_failed = False
    recheck_on_retry = False

    def operation():
        nonlocal latest, reconciled, handoff_failed, recheck_on_retry, reconcile_first
        if reconcile_first and reconciled is None:
            reconciled = produce(None, 0)
        reconcile_first = False
        if reconciled is not None:
            latest = reconciled
        elif recheck_on_retry:
            candidate = produce(instructions, 0)
            candidate_status, candidate_records = candidate
            ready = bool(candidate_records) and not candidate_status.get("error_kind") and all(
                record.get("writer_completed") and isinstance(record.get("host_execution"), dict)
                and record["host_execution"].get("passed") is True for record in candidate_records)
            latest = candidate if ready else produce(instructions, attempt)
        else:
            latest = produce(instructions, attempt)
        reconciled = None
        recheck_on_retry = False
        handoff_failed = False
        status, records = latest
        routed = status.get("error_kind") in {"environment_request", "environment_refresh", "foundation_revision"}
        if current_supervisor() is not None and not routed and not any(
            record.get("writer_completed") for record in records
        ):
            handoff_failed = True
            raise NodeFailure("Writer produced no usable delivery for downstream review", result={
                "writer_status": status, "task_records": records})
        return latest

    def repair(decision):
        nonlocal instructions, attempt, recheck_on_retry
        preserved = {}
        if latest is not None:
            status, records = latest
            if status.get("execution_receipts_required") and status.get("execution_audit_dir") and records:
                try:
                    observed = {
                        str(record["task_id"]): find_host_execution(
                            sandbox, Path(status["execution_audit_dir"]), str(record["task_id"]))
                        for record in records
                    }
                    if all(item.get("passed") is True for item in observed.values()):
                        preserved = {task_id: item["run_id"] for task_id, item in observed.items()}
                except (OSError, ValueError, KeyError):
                    preserved = {}
            if not preserved:
                archive(status, attempt)
        instructions = {**decision, "preserved_full_receipts": preserved}
        # A failed mechanical handoff may already be recoverable from the
        # same full. Reinspect locally before reopening the original Writer.
        recheck_on_retry = bool(preserved) and handoff_failed
        attempt += 1

    def reconcile(_state):
        nonlocal reconciled
        # Attempt zero only recollects the current files and checks host-owned
        # receipts. An invalid/missing delivery is diagnosed before any new run.
        reconciled = produce(None, 0)
        return REPLAY_REQUIRED

    try:
        return supervised_call(
            node_id, operation,
            inputs={"owner": "task_writer", "tasks": tasks, "analysis_snapshot_hash": analysis_snapshot_hash,
                    "dispatch_assignment": dispatch_assignment,
                    "instruction": "Review this Writer handoff before independent Reporter review. Do not make or override the scientific verdict."},
            evidence_roots={"writer": sandbox},
            summarize=lambda value: {"writer_status": value[0], "task_records": [{
                key: record.get(key) for key in ("task_id", "writer_completed", "writer_error_kind",
                    "task_writer_status", "execution_summary", "result_json", "artifacts")
            } for record in value[1]]},
            repair=repair,
            reconcile=reconcile,
        )
    except StageBlocked as exc:
        if latest is None:
            raise
        for record in latest[1]:
            record["supervisor_blocked"] = exc.decision
            record["blocked_reason"] = str(exc)
            record["task_reporter_terminal"] = False
        return latest


def _supervisor_writer_prompt(prompt, instructions):
    if not instructions:
        return prompt
    return prompt + ("\n\n## Project supervisor repair assignment\n"
        "Repair only the assigned task's implementation or handoff. Preserve the frozen Foundation, "
        "paper goals, and acceptance criteria. A changed executable input or output requires a new host receipt.\n"
        + json.dumps(instructions, ensure_ascii=False))


def _run_one_execution_unit_writer(
    *,
    unit: dict[str, Any],
    reuse_existing: bool,
    runtime_refresh_required: bool,
    facts: dict[str, Any],
    experiment_index: dict[str, Any],
    paper: dict[str, Any],
    paper_path: Path,
    paper_context_json: str,
    paper_images: list[Any] | None,
    paper_thesis: dict[str, Any] | None,
    foundation: dict[str, Any] | None,
    analysis_snapshot_hash: str,
    analysis_artifacts: dict[str, Path],
    task_root: Path,
    audit_dir: Path,
    run_repro: bool,
    review_feedback: dict[str, dict[str, Any]],
    task_review_callback: Callable[[int, dict[str, Any], dict[str, Any], int], dict[str, Any]] | None,
    case_runtime: CaseRuntime | None,
) -> list[dict[str, Any]]:
    members = [
        (index, _task_with_experiment_profile(task, experiment_index), entry)
        for index, task, entry in unit["members"]
    ]
    unit_id = str(unit["unit_id"])
    sandbox = _execution_unit_sandbox(task_root, unit_id)
    refresh_marker = _task_writer_runtime_refresh_marker(sandbox)
    if runtime_refresh_required and reuse_existing and sandbox.is_dir():
        write_json(
            refresh_marker,
            {
                "schema_version": 1,
                "reason": "scientific_handoff_match_combined_snapshot_changed",
                "execution_unit_id": unit_id,
                "target_analysis_snapshot_hash": analysis_snapshot_hash,
            },
        )
    _prepare_execution_unit_writer_sandbox(
        sandbox=sandbox,
        unit=unit,
        members=members,
        paper=paper,
        paper_path=paper_path,
        facts=facts,
        paper_thesis=paper_thesis,
        analysis_snapshot_hash=analysis_snapshot_hash,
        analysis_artifacts=analysis_artifacts,
        full_paper_images=paper_images,
        foundation=foundation,
        reuse_existing=reuse_existing,
    )
    bindings = {
        str(task.get("task_id") or entry.get("task_id") or ""): _load_task_execution_binding(
            sandbox,
            str(task.get("task_id") or entry.get("task_id") or ""),
        )
        for _index, task, entry in members
    }
    write_writer_input(sandbox=sandbox, members=members, facts=facts,
                       experiment_index=experiment_index, bindings=bindings, paper=paper,
                       case_runtime=case_runtime, unit=unit)
    base_prompt = _build_execution_unit_writer_brief(
        unit=unit,
        members=members,
        facts=facts,
        experiment_index=experiment_index,
        paper_context_json=paper_context_json,
        paper_thesis=paper_thesis,
        bindings=bindings,
        run_repro=run_repro,
        review_feedback={},
        foundation_enabled=foundation is not None,
        case_runtime=case_runtime,
    )
    session_round = _next_writer_progress_round(sandbox) if reuse_existing else 1
    rerun_budget = _external_writer_rerun_budget()
    evidence_based_reruns = 0
    seen_rerun_requests: set[str] = set()
    required_change_baseline: str | None = None
    current_feedback = {
        task_id: value
        for task_id, value in review_feedback.items()
        if task_id in {str(task.get("task_id") or entry.get("task_id") or "") for _, task, entry in members}
    }
    recovery_reason = "environment_or_runtime_refresh" if runtime_refresh_required else "resume_incomplete_delivery"

    reconcile_first = bool(reuse_existing and current_supervisor() is not None
                           and not runtime_refresh_required and not current_feedback)
    if reuse_existing and not reconcile_first:
        existing_records: list[dict[str, Any]] = []
        for index, task, entry in members:
            record = _collect_task_writer_delivery(
                index=index,
                task=task,
                manifest_entry=entry,
                sandbox=sandbox,
                writer_status={"ok": True, "source": "resumed_existing_execution_unit"},
                allow_root_result_fallback=False,
            )
            record["analysis_snapshot_hash"] = analysis_snapshot_hash
            record["writer_session_count"] = max(1, session_round)
            record["execution_unit_id"] = unit_id
            record["execution_unit_member_count"] = len(members)
            existing_records.append(record)
        if runtime_refresh_required:
            _archive_execution_unit_delivery(
                sandbox=sandbox,
                members=members,
                execution_unit_id=unit_id,
                round_no=session_round,
                session_status={"ok": True, "source": "runtime_refresh_required"},
            )
            session_round += 1
        elif all(
            record.get("task_writer_status") == TASK_WRITER_TERMINAL_STATUS
            for record in existing_records
        ):
            if task_review_callback is not None:
                requested = _review_execution_unit_tasks(
                    members=members,
                    records=existing_records,
                    callback=task_review_callback,
                    session_round=session_round,
                )
                if any(record.get("foundation_revision_request") for record in existing_records):
                    return existing_records
                if not requested:
                    return existing_records
                if rerun_budget <= 0:
                    _recover_writer_exceptions(
                        work=[(index, task, record, requested[str(record["task_id"])])
                              for (index, task, _entry), record in zip(members, existing_records)
                              if str(record.get("task_id") or "") in requested],
                        trigger="external_rerun_budget_exhausted", writer_budget_available=False,
                        callback=task_review_callback, session_round=session_round,
                    )
                    return existing_records
                seen_rerun_requests.update(
                    _execution_unit_rerun_fingerprints(requested, sandbox)
                )
                evidence_based_reruns = 1
                current_feedback = requested
                recovery_reason = "reporter_causal_revision"
            elif not current_feedback:
                return existing_records
            required_change_baseline = _writer_source_config_fingerprint(sandbox)
            _archive_execution_unit_delivery(
                sandbox=sandbox,
                members=members,
                execution_unit_id=unit_id,
                round_no=session_round,
                session_status={"ok": True, "source": "resumed_execution_unit_revision"},
            )
            session_round += 1
        else:
            # A recovered compound sandbox may contain a partial delivery from an
            # interrupted Writer. Start the continuation from an empty active
            # artifact generation so a second failure cannot promote those files.
            _archive_execution_unit_delivery(
                sandbox=sandbox,
                members=members,
                execution_unit_id=unit_id,
                round_no=session_round,
                session_status={"ok": False, "source": "resumed_incomplete_execution_unit"},
            )
            session_round += 1

    while True:
        label_base = f"03c_execution_unit_{int(unit.get('unit_index') or 1):02d}_{safe_label(unit_id)}"
        prompt = (
            base_prompt
            if session_round == 1 and not current_feedback
            else _build_execution_unit_continuation_brief(
                base_prompt=base_prompt,
                unit_id=unit_id,
                session_round=session_round,
                review_feedback=current_feedback,
                run_repro=run_repro,
                recovery_context=writer_recovery_context(sandbox, reason=recovery_reason,
                    task_ids=[str(task.get("task_id") or entry.get("task_id") or "") for _, task, entry in members]),
            )
        )
        # Previous outputs were archived above. The collector only tolerates
        # one floating-point timestamp step; actual full receipts remain bound.
        def produce_delivery(supervisor_feedback, supervisor_attempt):
            # Only a host-validated full allows old artifact timestamps. The
            # collector still verifies source/config/input/output hashes after
            # this continuation, so scientific edits require a new full receipt.
            preserve_full = bool((supervisor_feedback or {}).get("preserved_full_receipts"))
            session_started_at = None if supervisor_attempt == 0 or preserve_full else time.time()
            if supervisor_attempt == 0:
                writer_status = _inspect_task_writer_completion(
                    status={"ok": True, "reconciled_delivery": True}, sandbox=sandbox, audit_dir=audit_dir,
                    case_runtime=case_runtime, request_source=f"execution_unit_writer:{unit_id}",
                    require_execution_receipt=run_repro)
            else:
                writer_status = _run_task_writer_codex_session(
                    label=((label_base if session_round == 1 else f"{label_base}_continue_{session_round:03d}")
                           + (f"_supervisor_{supervisor_attempt:03d}" if supervisor_attempt > 1 else "")),
                    prompt=_supervisor_writer_prompt(prompt, supervisor_feedback),
                    sandbox=sandbox,
                    audit_dir=audit_dir,
                    case_runtime=case_runtime,
                    request_source=f"execution_unit_writer:{unit_id}",
                    require_execution_receipt=run_repro,
                )
            if writer_status.get("error_kind") in {
                "environment_request",
                "environment_refresh",
                "environment_request_invalid",
                "sandbox_inspection_failed",
                "foundation_revision",
            }:
                return writer_status, [
                    {
                        "index": index,
                        "task_id": str(task.get("task_id") or entry.get("task_id") or f"task_{index}"),
                        "module": str(entry.get("module") or ""),
                        "output_subdir": str(entry.get("output_subdir") or task.get("task_id") or ""),
                        "sandbox": str(sandbox),
                        "execution_unit_id": unit_id,
                        "task_writer_status": "blocked_environment",
                        "writer_completed": False,
                        "writer_error_kind": writer_status.get("error_kind"),
                        "writer_status": writer_status,
                        "environment_requests": writer_status.get("environment_requests", []),
                        "foundation_revision_request": writer_status.get("foundation_revision_request"),
                        "analysis_snapshot_hash": analysis_snapshot_hash,
                        "writer_session_count": session_round,
                        "runtime_refresh_required": bool(runtime_refresh_required),
                        "runtime_refresh_completed": False,
                        "environment_refresh_required": bool(runtime_refresh_required),
                        "environment_refresh_completed": False,
                    }
                    for index, task, entry in members
                ]
            if foundation is not None:
                frozen_issues = foundation_violations(sandbox, foundation)
                if frozen_issues:
                    restore_foundation_snapshot(sandbox, foundation)
                    writer_status = {
                        **writer_status,
                        "ok": False,
                        "error_kind": "foundation_modified",
                        "blocked_reason": "execution-unit writer changed the frozen scientific Foundation",
                        "foundation_violations": frozen_issues,
                    }
            unit_manifest = {
                "version": 1,
                "execution_plan_version": "1.0",
                "execution_units": [_public_execution_unit(unit)],
                "tasks": [entry for _index, _task, entry in members],
            }
            _restore_trusted_files(sandbox, unit_manifest)
            write_json(sandbox / "execution_unit.json", _public_execution_unit(unit))
            records: list[dict[str, Any]] = []
            for index, task, entry in members:
                record = _collect_task_writer_delivery(
                    index=index,
                    task=task,
                    manifest_entry=entry,
                    sandbox=sandbox,
                    writer_status=writer_status,
                    require_stopping_assessment=False,
                    allow_root_result_fallback=False,
                    fresh_since=session_started_at,
                )
                record["analysis_snapshot_hash"] = analysis_snapshot_hash
                record["writer_session_count"] = session_round
                record["execution_unit_id"] = unit_id
                record["execution_unit_member_count"] = len(members)
                records.append(record)

            _complete_execution_unit_runtime_refresh(
                records=records,
                marker=refresh_marker,
                required=runtime_refresh_required,
                writer_status=writer_status,
            )

            return writer_status, records

        writer_status, records = _supervise_writer_delivery(
            node_id=f"writer:{unit_id}:round:{session_round}", produce=produce_delivery,
            archive=lambda status, _attempt: _archive_execution_unit_delivery(
                sandbox=sandbox, members=members, execution_unit_id=unit_id,
                round_no=_next_writer_progress_round(sandbox), session_status=status),
            tasks=[task for _index, task, _entry in members], sandbox=sandbox,
            analysis_snapshot_hash=analysis_snapshot_hash, reconcile_first=reconcile_first,
        )
        reconcile_first = False
        if any(record.get("supervisor_blocked") for record in records):
            return records
        if writer_status.get("error_kind") in {"environment_request", "environment_refresh", "environment_request_invalid", "foundation_revision", "sandbox_inspection_failed"}:
            return records

        requested_feedback: dict[str, dict[str, Any]] = {}
        if run_repro and task_review_callback is not None:
            requested_feedback = _review_execution_unit_tasks(
                members=members,
                records=records,
                callback=task_review_callback,
                session_round=session_round,
            )
        if any(record.get("foundation_revision_request") for record in records):
            return records

        recovery_applied = False
        if required_change_baseline is not None:
            current_state = _writer_source_config_fingerprint(sandbox)
            if current_state == required_change_baseline:
                requested_feedback = _recover_writer_exceptions(
                    work=[(index, task, record, record.get("task_verification") or requested_feedback[str(record["task_id"])])
                          for (index, task, _entry), record in zip(members, records)
                          if str(record.get("task_id") or "") in requested_feedback],
                    trigger="execution_unit_continuation_without_source_change",
                    writer_budget_available=evidence_based_reruns < rerun_budget,
                    callback=task_review_callback, session_round=session_round,
                )
                if any(record.get("foundation_revision_request") for record in records) or not requested_feedback:
                    return records
                recovery_applied = True
            required_change_baseline = None
        if not run_repro or task_review_callback is None:
            return records
        if not requested_feedback:
            return records

        fingerprints = _execution_unit_rerun_fingerprints(requested_feedback, sandbox)
        repeated_request = bool(fingerprints) and fingerprints.issubset(
            seen_rerun_requests
        )
        if not recovery_applied and (repeated_request or evidence_based_reruns >= rerun_budget):
            stop_reason = (
                "repeated_execution_unit_rerun_request_without_new_causal_plan"
                if repeated_request
                else "external_rerun_budget_exhausted"
            )
            requested_feedback = _recover_writer_exceptions(
                work=[(index, task, record, record.get("task_verification") or requested_feedback[str(record["task_id"])])
                      for (index, task, _entry), record in zip(members, records)
                      if str(record.get("task_id") or "") in requested_feedback],
                trigger=stop_reason, writer_budget_available=evidence_based_reruns < rerun_budget,
                callback=task_review_callback, session_round=session_round,
            )
            if any(record.get("foundation_revision_request") for record in records) or not requested_feedback:
                return records
            fingerprints = _execution_unit_rerun_fingerprints(requested_feedback, sandbox)
        seen_rerun_requests.update(fingerprints)
        evidence_based_reruns += 1
        current_feedback = requested_feedback
        recovery_reason = "reporter_causal_revision"
        required_change_baseline = _writer_source_config_fingerprint(sandbox)
        _archive_execution_unit_delivery(
            sandbox=sandbox,
            members=members,
            execution_unit_id=unit_id,
            round_no=session_round,
            session_status=writer_status,
        )
        session_round += 1

def _run_one_task_writer(
    *,
    index: int,
    execution_unit_id: str | None = None,
    reuse_existing: bool,
    runtime_refresh_required: bool = False,
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
    review_feedback: dict[str, Any] | None = None,
    task_review_callback: Callable[[int, dict[str, Any], dict[str, Any], int], dict[str, Any]] | None = None,
    case_runtime: CaseRuntime | None = None,
) -> dict[str, Any]:
    task_id = str(task.get("task_id") or manifest_entry.get("task_id") or f"task_{index}")
    module = str(manifest_entry.get("module") or safe_label(task_id))
    task = _task_with_experiment_profile(task, experiment_index)
    base_label = f"03c_task_writer_{index:02d}_{safe_label(task_id)}"
    sandbox = task_root / f"{index:02d}_{safe_label(task_id)}"
    output_subdir = str(manifest_entry.get("output_subdir") or task_id)
    unit_id = str(execution_unit_id or f"unit_task_{index:02d}_{safe_label(task_id)}")
    refresh_marker = _task_writer_runtime_refresh_marker(sandbox)
    if runtime_refresh_required and reuse_existing and sandbox.is_dir():
        write_json(
            refresh_marker,
            {
                "schema_version": 1,
                "reason": "scientific_handoff_match_combined_snapshot_changed",
                "task_id": task_id,
                "target_analysis_snapshot_hash": analysis_snapshot_hash,
            },
        )
        refresh_archive_round = _next_writer_progress_round(sandbox)
        _archive_nonterminal_writer_delivery(
            sandbox=sandbox,
            output_subdir=output_subdir,
            round_no=refresh_archive_round,
            session_status={
                "ok": True,
                "source": "runtime_refresh_required",
            },
        )
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
        execution_unit_id=unit_id,
    )
    execution_binding = _load_task_execution_binding(sandbox, task_id)
    write_writer_input(sandbox=sandbox, members=[(index, task, manifest_entry)], facts=facts,
                       experiment_index=experiment_index, bindings={task_id: execution_binding},
                       paper=paper, case_runtime=case_runtime)
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
        review_feedback=None,
        foundation_enabled=foundation is not None,
        execution_binding=execution_binding,
        case_runtime=case_runtime,
        execution_unit_id=unit_id,
    )
    session_round = 1
    seen_rerun_requests: set[str] = set()
    evidence_based_reruns = 0
    rerun_budget = _external_writer_rerun_budget()
    required_change_baseline: str | None = None
    last_reporter: dict[str, Any] | None = None
    recovery_reason = "environment_or_runtime_refresh" if runtime_refresh_required else "resume_incomplete_delivery"
    reconcile_first = bool(reuse_existing and current_supervisor() is not None
                           and not runtime_refresh_required and not review_feedback)
    if reuse_existing and not reconcile_first:
        archive_round = _next_writer_progress_round(sandbox)
        if runtime_refresh_required:
            session_round = archive_round
        elif task_review_callback is None and not review_feedback:
            existing_record = _collect_task_writer_delivery(
                index=index,
                task=task,
                manifest_entry=manifest_entry,
                sandbox=sandbox,
                writer_status={'ok': True, 'source': 'resumed_existing_delivery'},
            )
            existing_record['analysis_snapshot_hash'] = analysis_snapshot_hash
            existing_record['writer_session_count'] = max(1, archive_round)
            existing_record['execution_unit_id'] = unit_id
            if existing_record.get('task_writer_status') == TASK_WRITER_TERMINAL_STATUS:
                return existing_record
        if not runtime_refresh_required and task_review_callback is not None and not review_feedback:
            existing_record = _collect_task_writer_delivery(
                index=index,
                task=task,
                manifest_entry=manifest_entry,
                sandbox=sandbox,
                writer_status={"ok": True, "source": "resumed_existing_delivery"},
            )
            existing_record["analysis_snapshot_hash"] = analysis_snapshot_hash
            existing_record["writer_session_count"] = max(1, archive_round)
            existing_record["execution_unit_id"] = unit_id
            if existing_record.get("task_writer_status") == TASK_WRITER_TERMINAL_STATUS:
                review_action, returned_feedback = _attach_task_reporter_review(
                    callback=task_review_callback,
                    index=index,
                    task=task,
                    record=existing_record,
                    session_round=archive_round,
                )
                last_reporter = existing_record.get("task_reporter")
                if review_action in {"terminal", "failed"}:
                    return existing_record
                if review_action == "writer_revision":
                    if rerun_budget <= 0:
                        _recover_writer_exceptions(
                            work=[(index, task, existing_record, existing_record.get("task_verification") or returned_feedback)],
                            trigger="external_rerun_budget_exhausted", writer_budget_available=False,
                            callback=task_review_callback, session_round=archive_round,
                        )
                        return existing_record
                    evidence = (
                        returned_feedback.get("rerun_evidence")
                        if isinstance(returned_feedback, dict)
                        else None
                    )
                    seen_rerun_requests.add(_rerun_evidence_fingerprint(evidence, _writer_progress_fingerprint(sandbox)))
                    evidence_based_reruns = 1
                    review_feedback = returned_feedback
                    recovery_reason = "reporter_causal_revision"
                    required_change_baseline = _record_source_config_fingerprint(
                        existing_record,
                        sandbox,
                    )
        if not runtime_refresh_required:
            if review_feedback:
                recovery_reason = "requested_delivery_revision"
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
            if session_round == 1 and not review_feedback
            else _build_task_writer_continuation_brief(
                base_prompt=base_prompt,
                task_id=task_id,
                module=module,
                session_round=session_round,
                review_feedback=review_feedback,
                run_repro=run_repro,
                recovery_context=writer_recovery_context(sandbox, reason=recovery_reason, task_ids=[task_id]),
            )
        )
        def produce_delivery(supervisor_feedback, supervisor_attempt):
            # Only a host-validated full allows old artifact timestamps. The
            # collector still verifies source/config/input/output hashes after
            # this continuation, so scientific edits require a new full receipt.
            preserve_full = bool((supervisor_feedback or {}).get("preserved_full_receipts"))
            session_started_at = None if supervisor_attempt == 0 or preserve_full else time.time()
            if supervisor_attempt == 0:
                writer_status = _inspect_task_writer_completion(
                    status={"ok": True, "reconciled_delivery": True}, sandbox=sandbox, audit_dir=audit_dir,
                    case_runtime=case_runtime, request_source=f"task_writer:{task_id}",
                    require_execution_receipt=run_repro)
            else:
                writer_status = _run_task_writer_codex_session(
                    label=label + (f"_supervisor_{supervisor_attempt:03d}" if supervisor_attempt > 1 else ""),
                    prompt=_supervisor_writer_prompt(prompt, supervisor_feedback),
                    sandbox=sandbox,
                    audit_dir=audit_dir,
                    case_runtime=case_runtime,
                    request_source=f"task_writer:{task_id}",
                    require_execution_receipt=run_repro,
                )

            if writer_status.get("error_kind") in {
                "environment_request",
                "environment_refresh",
                "environment_request_invalid",
                "sandbox_inspection_failed",
                "foundation_revision",
            }:
                return writer_status, [{
                    "index": index,
                    "task_id": task_id,
                    "module": module,
                    "output_subdir": output_subdir,
                    "sandbox": str(sandbox),
                    "task_writer_status": "blocked_environment",
                    "writer_completed": False,
                    "writer_error_kind": writer_status.get("error_kind"),
                    "writer_status": writer_status,
                    "environment_requests": writer_status.get("environment_requests", []),
                    "foundation_revision_request": writer_status.get("foundation_revision_request"),
                    "analysis_snapshot_hash": analysis_snapshot_hash,
                    "writer_session_count": session_round,
                }]

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
                require_stopping_assessment=False,
                fresh_since=session_started_at,
            )
            record["analysis_snapshot_hash"] = analysis_snapshot_hash
            record["writer_session_count"] = session_round
            record["execution_unit_id"] = unit_id
            return writer_status, [record]

        writer_status, delivery_records = _supervise_writer_delivery(
            node_id=f"writer:{unit_id}:round:{session_round}", produce=produce_delivery,
            archive=lambda status, _attempt: _archive_nonterminal_writer_delivery(
                sandbox=sandbox, output_subdir=output_subdir,
                round_no=_next_writer_progress_round(sandbox), session_status=status),
            tasks=[task], sandbox=sandbox, analysis_snapshot_hash=analysis_snapshot_hash,
            reconcile_first=reconcile_first,
        )
        reconcile_first = False
        record = delivery_records[0]
        if record.get("supervisor_blocked") or writer_status.get("error_kind") in {
            "environment_request", "environment_refresh", "environment_request_invalid", "foundation_revision", "sandbox_inspection_failed",
        }:
            return record
        recovered_feedback = None
        if required_change_baseline is not None:
            current_state = _record_source_config_fingerprint(record, sandbox)
            if current_state == required_change_baseline:
                if isinstance(last_reporter, dict):
                    record["task_reporter"] = last_reporter
                recovery_feedback = (last_reporter or {}).get("task_verification") or review_feedback
                recovered = _recover_writer_exceptions(
                    work=[(index, task, record, recovery_feedback)],
                    trigger="writer_continuation_without_source_change",
                    writer_budget_available=evidence_based_reruns < rerun_budget,
                    callback=task_review_callback, session_round=session_round,
                )
                recovered_feedback = recovered.get(task_id)
                if not recovered_feedback:
                    return _complete_task_writer_runtime_refresh(
                        record=record, marker=refresh_marker, required=runtime_refresh_required,
                    )
            required_change_baseline = None
        if not run_repro or task_review_callback is None:
            return _complete_task_writer_runtime_refresh(
                record=record,
                marker=refresh_marker,
                required=runtime_refresh_required,
            )
        if recovered_feedback:
            review_action, returned_feedback = "writer_revision", recovered_feedback
        else:
            review_action, returned_feedback = _attach_task_reporter_review(
                callback=task_review_callback, index=index, task=task, record=record, session_round=session_round,
            )
        last_reporter = record.get("task_reporter")
        if review_action in {"terminal", "failed"}:
            return _complete_task_writer_runtime_refresh(
                record=record,
                marker=refresh_marker,
                required=runtime_refresh_required,
            )
        if review_action == "writer_revision":
            evidence = (
                returned_feedback.get("rerun_evidence")
                if isinstance(returned_feedback, dict)
                else None
            )
            rerun_fingerprint = _rerun_evidence_fingerprint(evidence, _writer_progress_fingerprint(sandbox))
            repeated = rerun_fingerprint in seen_rerun_requests
            if not recovered_feedback and (repeated or evidence_based_reruns >= rerun_budget):
                recovered = _recover_writer_exceptions(
                    work=[(index, task, record, record.get("task_verification") or returned_feedback)],
                    trigger="repeated_rerun_request_without_new_causal_plan" if repeated else "external_rerun_budget_exhausted",
                    writer_budget_available=evidence_based_reruns < rerun_budget,
                    callback=task_review_callback, session_round=session_round,
                )
                returned_feedback = recovered.get(task_id)
                if not returned_feedback:
                    return _complete_task_writer_runtime_refresh(
                        record=record, marker=refresh_marker, required=runtime_refresh_required,
                    )
                rerun_fingerprint = _rerun_evidence_fingerprint(
                    returned_feedback.get("rerun_evidence"), _writer_progress_fingerprint(sandbox),
                )
            seen_rerun_requests.add(rerun_fingerprint)
            evidence_based_reruns += 1
            review_feedback = returned_feedback
            recovery_reason = "reporter_causal_revision"
            required_change_baseline = _record_source_config_fingerprint(
                record,
                sandbox,
            )
            _archive_nonterminal_writer_delivery(
                sandbox=sandbox,
                output_subdir=output_subdir,
                round_no=session_round,
                session_status=writer_status,
            )
            session_round += 1
            continue
        return _complete_task_writer_runtime_refresh(
            record=record,
            marker=refresh_marker,
            required=runtime_refresh_required,
        )

def _clear_previous_reporter_decision(record: dict[str, Any]) -> None:
    # A fresh failed review must never leave a previous successful verdict active.
    for key in (
        "task_verification", "task_reporter_terminal", "task_reporter_successful",
        "task_reporter_error_kind", "task_reporter_error",
        "verification_result", "verification_verified", "scientific_outcome",
    ):
        record.pop(key, None)
    if record.get("task_writer_status") == "matched":
        record["task_writer_status"] = TASK_WRITER_TERMINAL_STATUS


def _attach_task_reporter_review(
    *,
    callback: Callable[[int, dict[str, Any], dict[str, Any], int], dict[str, Any]],
    index: int,
    task: dict[str, Any],
    record: dict[str, Any],
    session_round: int,
    writer_revision_permitted: bool = True,
) -> tuple[str, dict[str, Any] | None]:
    expected_task_id = str(task.get("task_id") or record.get("task_id") or "")
    try:
        task_reporter = _obtain_reporter_review(callback, index, task, record, session_round)
    except PipelineCancelled:
        raise
    except Exception as exc:
        _clear_previous_reporter_decision(record)
        message = redact_text(f"{type(exc).__name__}: {exc}")[:1000]
        task_reporter = {
            "ok": False,
            "task_id": expected_task_id,
            "task_verification": {},
            "error": message,
            "error_kind": "task_reporter_callback_failed",
        }
        record["task_reporter"] = task_reporter
        record["task_reporter_error_kind"] = "task_reporter_callback_failed"
        warnings = record.setdefault("delivery_warnings", [])
        if isinstance(warnings, list):
            warnings.append("task Reporter failed; the host will synthesize a terminal outcome")
        return "failed", None

    _clear_previous_reporter_decision(record)
    record["task_reporter"] = task_reporter
    verification = task_reporter.get("task_verification") if isinstance(task_reporter, dict) else None
    if isinstance(verification, dict):
        record["task_verification"] = verification
    if not isinstance(task_reporter, dict) or not task_reporter.get("ok"):
        record["task_reporter_error_kind"] = "task_reporter_failed"
        record["task_reporter_error"] = (
            task_reporter.get("error")
            if isinstance(task_reporter, dict)
            else "task reporter callback failed"
        )
        warnings = record.setdefault("delivery_warnings", [])
        if isinstance(warnings, list):
            warnings.append("task Reporter was unavailable; preserving the Writer delivery")
        return "failed", None
    if not isinstance(verification, dict):
        record["task_reporter_error_kind"] = "task_reporter_missing_result"
        record["task_reporter_error"] = "task reporter produced no usable scientific note"
        warnings = record.setdefault("delivery_warnings", [])
        if isinstance(warnings, list):
            warnings.append("task Reporter produced no note; preserving the Writer delivery")
        return "failed", None
    if verification.get("host_action") == "rerun_writer":
        route = getattr(task_reporter, "revision_route", None)
        if route is None:
            route = resolve_revision_owner(record=record, reporter=task_reporter, verification=verification)
        if route["action"] == "revise_foundation":
            record["foundation_revision_request"] = route["request"]
            record["task_reporter_terminal"] = False
            return "terminal", None
        if route["action"] == "revise_writer" and writer_revision_permitted:
            feedback = localize_writer_feedback(
                route["feedback"], reporter_root=Path(str(task_reporter.get("workspace") or "")),
                sandbox=Path(str(record.get("sandbox") or "")),
                output_subdir=str(record.get("output_subdir") or expected_task_id),
            )
            return "writer_revision", feedback
        _terminalize_rerun_request(
            record=record, verification=verification,
            stop_reason=("external_rerun_budget_exhausted" if not writer_revision_permitted
                         else route.get("reason") or "moderator_stopped_revision"),
            uncertainty="The coordination decision ended this repair; the original Reporter request is preserved.",
        )
    issues = task_verification_issues(record.get("task_verification"), expected_task_id)
    if issues:
        warnings = record.setdefault("delivery_warnings", [])
        if isinstance(warnings, list):
            warnings.extend(issues)
    final_verification = record.get("task_verification")
    from .verification_result import verification_scientifically_successful
    record["task_reporter_successful"] = isinstance(final_verification, dict) and verification_scientifically_successful(final_verification)
    record["task_reporter_terminal"] = True
    return "terminal", None


def _run_task_writer_codex_session(
    *,
    label: str,
    prompt: str,
    sandbox: Path,
    audit_dir: Path,
    case_runtime: CaseRuntime | None = None,
    request_source: str = "task_writer",
    require_execution_receipt: bool = True,
) -> dict[str, Any]:
    write_text(audit_dir / f"{label}_brief.md", prompt)
    selected_python = (
        ensure_writer_environment(sandbox, case_runtime)
        if case_runtime is not None else Path(sys.executable).absolute()
    )
    python_dir = selected_python.parent
    runtime_env = {
        "GENG_PYTHON": str(selected_python),
        "GENG_PYTHON_EXECUTABLE": str(selected_python),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    if case_runtime is not None:
        runtime_env["VIRTUAL_ENV"] = str(selected_python.parent.parent)
    evidence_before = trusted_input_snapshot(sandbox, (PAPER_EVIDENCE_DIR,))
    with ExecutionBroker(sandbox, audit_dir, selected_python,
                         environment_hash=case_runtime.environment_hash if case_runtime else "",
                         shared_runtime_python=(case_runtime.python_executable if case_runtime else None),
                         expected_installed_distributions=(
                             case_runtime.lock.get("installed_distributions")
                             if case_runtime and "installed_distributions" in case_runtime.lock else None),
                         allow_full=require_execution_receipt) as broker:
        runtime_env["GENG_EXECUTION_BROKER"] = broker.session_id
        prompt += ("\n\nHost-observed execution: invoke the selected Python using your shell's syntax and use the task's actual config path. "
                   "Use `run_task.py --task TASK_ID --status` to inspect an in-flight or completed execution, "
                   "including its host-owned log paths. Add --submit to a launch command for prompt return. "
                   "Declare consumed persistent checkpoint/data files with repeatable --input RELATIVE_PATH. "
                   "The host records the real process exit, source/config/input/output hashes; raw Python executions are exploratory "
                   "and cannot establish delivery provenance. Wait for an in-flight execution; do not launch it again after a CLI polling timeout. "
                   "If the observed execution fails, inspect its stderr_tail and repair before submitting another run. "
                   "Do not write execution_receipt.json yourself. Final notes may be added after the run; changing source/config/results requires a new receipt.\n")
        if not require_execution_receipt:
            prompt += "Full execution is disabled for this preparation session. Do not submit a full experiment.\n"
        status = run_codex_subprocess(
            role="task_writer", work_dir=sandbox, prompt=prompt, audit_dir=audit_dir,
            label=label, sandbox="workspace-write",
            command_override=get_config_value("GENG_CODEX_TASK_WRITER_CMD"),
            image_paths=unique_image_paths(sorted(path.resolve() for path in (sandbox / PAPER_EVIDENCE_DIR / "full_paper_pages").glob("paper_page_*.png") if path.is_file())),
            extra_env=runtime_env, path_prepend=[python_dir],
            workspace_network_access=case_runtime is not None,
            workspace_writable_roots=[selected_python.parent.parent] if case_runtime else None,
        )
    if case_runtime is not None:
        try:
            snapshot_writer_environment(sandbox, case_runtime)
        except (OSError, ValueError) as exc:
            status["writer_environment_snapshot_error"] = f"{type(exc).__name__}: {exc}"
    if broker.environment_refresh_required is True:
        return {**status, "ok": False, "error_kind": "environment_refresh",
                "blocked_reason": "Shared Python changed after case preparation; the host will refresh the environment before continuing."}
    return _inspect_task_writer_completion(status=status, sandbox=sandbox, audit_dir=audit_dir,
        case_runtime=case_runtime, request_source=request_source,
        require_execution_receipt=require_execution_receipt, evidence_before=evidence_before)


def _inspect_task_writer_completion(*, status, sandbox, audit_dir, case_runtime,
                                    request_source, require_execution_receipt, evidence_before=None):
    """Read-only hard checks shared by a new session and local reconciliation."""
    status.update(execution_receipts_required=require_execution_receipt, execution_audit_dir=str(audit_dir))
    try:
        # This no-follow walk must precede every read of Writer-controlled
        # files, including a dependency request or requirements.txt.
        _assert_foundation_sandbox_layout_safe(sandbox)
    except (OSError, RuntimeError) as exc:
        return {
            **status,
            "ok": False,
            "error_kind": "sandbox_inspection_failed",
            "blocked_reason": f"Writer sandbox inspection failed: {type(exc).__name__}: {redact_text(str(exc))}",
            "inspection_error": {
                "type": type(exc).__name__, "message": redact_text(str(exc)),
                "path": str(getattr(exc, "filename", "") or ""),
                "errno": getattr(exc, "errno", None), "winerror": getattr(exc, "winerror", None),
            },
        }
    if evidence_before is not None:
        evidence_after = trusted_input_snapshot(sandbox, (PAPER_EVIDENCE_DIR,))
        changed = sorted(path for path, digest in evidence_before.items()
                         if evidence_after.get(path) != digest)
        added = sorted(set(evidence_after) - set(evidence_before))
        if added:
            # Reporter receives a fresh copy of the original paper, not this
            # Writer directory. Extra scratch files are observations, not a
            # reason to suppress an otherwise usable scientific handoff.
            status["paper_evidence_added_files"] = added
        if changed:
            status = {**status, "ok": False, "error_kind": "evidence_modified",
                      "blocked_reason": "Original paper evidence changed during Writer execution",
                      "paper_evidence_changed_files": changed}
    architecture_path = sandbox / PAPER_EVIDENCE_DIR / "analysis_artifacts" / "scientific_architecture.json"
    if (sandbox / "foundation_revision_request.json").is_file() and architecture_path.is_file():
        plan_path = architecture_path.with_name("execution_plan.json")
        try:
            request = read_foundation_revision_request(sandbox=sandbox,
                architecture=json.loads(architecture_path.read_text(encoding="utf-8-sig")),
                execution_plan=json.loads(plan_path.read_text(encoding="utf-8-sig")) if plan_path.is_file() else None)
        except (ValueError, OSError) as exc:
            return {**status, "ok": False, "error_kind": "foundation_revision_invalid", "blocked_reason": str(exc)}
        if request:
            request["evidence_root"] = str(sandbox)
            return {**status, "ok": False, "error_kind": "foundation_revision", "foundation_revision_request": request}
    return status
