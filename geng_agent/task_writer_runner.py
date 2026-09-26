"""Run each planner-owned task with its Writer and independent Reporter."""

from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from pathlib import Path
from typing import Any, Callable

from .case_runtime import CaseRuntime
from .codex_runner import run_codex_subprocess
from .execution_receipts import ExecutionBroker, trusted_input_snapshot
from .config import get_config_value
from .outputs import write_json, write_text
from .progress import PipelineCancelled
from .paper_evidence import safe_label
from .security import redact_text
from .task_writer_contracts import TASK_WRITER_TERMINAL_STATUS
from .task_writer_delivery import _collect_task_writer_delivery
from .task_writer_execution_binding import _load_task_execution_binding
from .task_writer_prompts import _build_task_writer_brief, _build_task_writer_continuation_brief
from .task_writer_sandbox import _prepare_task_writer_sandbox
from .task_writer_inputs import write_writer_input, unique_image_paths
from .task_writer_state import _archive_nonterminal_writer_delivery, _complete_task_writer_runtime_refresh, _next_writer_progress_round, _task_writer_runtime_refresh_marker, _terminalize_rerun_request
from .task_writer_support import PAPER_EVIDENCE_DIR, _restore_trusted_files
from .verification_result import task_verification_issues
from .writer_recovery import localize_writer_feedback, writer_recovery_context
from .writer_environment import ensure_writer_environment, snapshot_writer_environment
from .task_recovery import resolve_revision_owner
from .supervisor import StageBlocked, REPLAY_REQUIRED, current_supervisor, supervised_call



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




class _PreparedReporterResult(dict):
    """In-memory routing for this callback/replay, never a persisted authority."""

    revision_route: dict[str, Any] | None = None
    prepared_state: str | None = None


def _obtain_reporter_review(callback, index, task, record, round_no):
    reporter = callback(index, task, record, round_no)
    return _prepare_reporter_review(reporter, callback, index, task, record, round_no)


def _prepare_reporter_review(reporter, callback, index, task, record, round_no):
    while isinstance(reporter, dict) and reporter.get("ok"):
        verification = reporter.get("task_verification")
        if not isinstance(verification, dict) or verification.get("host_action") != "rerun_writer":
            break
        if isinstance(reporter, _PreparedReporterResult):
            return reporter
        supervisor = current_supervisor()
        if supervisor is not None:
            supervisor._check_cancelled()
        try:
            route = resolve_revision_owner(record=record, reporter=reporter, verification=verification)
        except PipelineCancelled:
            raise
        except Exception as exc:
            route = {"action": "unavailable", "reason": "moderator_dispatch_unavailable",
                     "error": redact_text(f"{type(exc).__name__}: {exc}")}
        if route["action"] == "repair_reporter":
            record["task_reporter"] = reporter
            record["moderator_review_request"] = dict(route.get("decision") or {})
            reporter = callback(index, task, record, round_no)
            continue
        prepared = _PreparedReporterResult(reporter)
        prepared.revision_route = route
        return prepared
    return reporter


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




def _supervise_writer_delivery(*, node_id, produce, archive, tasks, sandbox, analysis_snapshot_hash, reconcile_first=False):
    """Forward an actual Writer handoff, retaining local failure evidence.

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

    def operation():
        nonlocal latest, reconciled, reconcile_first
        latest = reconciled if reconciled is not None else (
            produce(None, 0) if reconcile_first else produce(instructions, attempt))
        reconciled = None
        reconcile_first = False
        return latest

    def repair(decision):
        nonlocal instructions, attempt
        instructions = decision
        attempt += 1

    def reconcile(_state):
        nonlocal reconciled
        # Attempt zero recollects current files and observations without running
        # the model or interpreting whether the science passed.
        reconciled = produce(None, 0)
        return REPLAY_REQUIRED

    try:
        return supervised_call(
            node_id, operation,
            inputs={"owner": "task_writer", "tasks": tasks, "analysis_snapshot_hash": analysis_snapshot_hash,
                    "dispatch_assignment": dispatch_assignment,
                    "instruction": "Pass all available Writer materials and exceptions to the independent Reporter."},
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
        "Repair only the assigned task's implementation or handoff. Preserve the "
        "paper goals, and acceptance criteria. Changes to executable inputs or scientific results require a new host receipt. "
        "A documentation-only repair, including delivery_readme.md, must use the existing code/results without "
        "rerunning the experiment or changing its scientific verdict.\n"
        + json.dumps(instructions, ensure_ascii=False))



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
    analysis_snapshot_hash: str,
    analysis_artifacts: dict[str, Path],
    task_root: Path,
    audit_dir: Path,
    run_repro: bool,
    review_feedback: dict[str, Any] | None = None,
    task_review_callback: Callable[[int, dict[str, Any], dict[str, Any], int], dict[str, Any]] | None = None,
    case_runtime: CaseRuntime | None = None,
    upstream_records: list[dict] | None = None,
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
        execution_unit_id=unit_id,
    )
    from .task_inputs import copy_upstream_inputs
    copy_upstream_inputs(sandbox, task, upstream_records or [])
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
        execution_binding=execution_binding,
        case_runtime=case_runtime,
        execution_unit_id=unit_id,
    )
    session_round = _next_writer_progress_round(sandbox) if reuse_existing else 1
    while True:
        supervisor = current_supervisor()
        if supervisor is not None:
            supervisor._check_cancelled()
        label = base_label + f"_round_{session_round:03d}"
        prompt = base_prompt if not review_feedback and not reuse_existing else _build_task_writer_continuation_brief(
            base_prompt=base_prompt, task_id=task_id, module=module,
            session_round=session_round, review_feedback=review_feedback, run_repro=run_repro,
            recovery_context=writer_recovery_context(sandbox, reason="agent_requested_continuation", task_ids=[task_id]))

        def produce_delivery(instructions, attempt):
            if attempt == 0:
                status = _inspect_task_writer_completion(status={"ok": True, "source": "resumed_delivery"},
                    sandbox=sandbox, audit_dir=audit_dir, case_runtime=case_runtime,
                    request_source=f"task_writer:{task_id}", require_execution_receipt=run_repro)
            else:
                status = _run_task_writer_codex_session(label=label + f"_attempt_{attempt}",
                    prompt=_supervisor_writer_prompt(prompt, instructions), sandbox=sandbox,
                    audit_dir=audit_dir, case_runtime=case_runtime,
                    request_source=f"task_writer:{task_id}", require_execution_receipt=run_repro)
            record = _collect_task_writer_delivery(index=index, task=task, manifest_entry=manifest_entry,
                sandbox=sandbox, writer_status=status, require_stopping_assessment=False)
            record.update(analysis_snapshot_hash=analysis_snapshot_hash, writer_session_count=session_round,
                          execution_unit_id=unit_id)
            return status, [record]

        status, records = _supervise_writer_delivery(
            node_id=f"writer:{unit_id}:round:{session_round}", produce=produce_delivery,
            archive=lambda *_: None, tasks=[task], sandbox=sandbox,
            analysis_snapshot_hash=analysis_snapshot_hash,
            reconcile_first=bool(reuse_existing and not review_feedback and not runtime_refresh_required))
        record = records[0]
        if status.get("error_kind") == "environment_refresh":
            return record
        # All notes and observed failures reach Reporter without a content gate.
        if not run_repro or task_review_callback is None:
            return _complete_task_writer_runtime_refresh(record=record, marker=refresh_marker, required=runtime_refresh_required)
        action, feedback = _attach_task_reporter_review(callback=task_review_callback,
            index=index, task=task, record=record, session_round=session_round)
        if action != "writer_revision":
            return _complete_task_writer_runtime_refresh(record=record, marker=refresh_marker, required=runtime_refresh_required)
        _archive_nonterminal_writer_delivery(sandbox=sandbox, output_subdir=output_subdir,
                                            round_no=session_round, session_status=status)
        review_feedback = feedback
        reuse_existing = True
        session_round += 1


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
            warnings.append("task Reporter failed; preserving the error for the report editor")
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
        if route["action"] == "revise_writer":
            feedback = localize_writer_feedback(
                route["feedback"], reporter_root=Path(str(task_reporter.get("workspace") or "")),
                sandbox=Path(str(record.get("sandbox") or "")),
                output_subdir=str(record.get("output_subdir") or expected_task_id),
            )
            return "writer_revision", feedback
        if route["action"] != "stop":
            record["coordination_status"] = "pending"
            record.setdefault("coordination_observations", []).append(route)
            return "pending", None
        _terminalize_rerun_request(
            record=record, verification=verification,
            stop_reason=route.get("reason") or "moderator_stopped_revision",
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
        "CUDA_VISIBLE_DEVICES": "",
    }
    if case_runtime is not None:
        runtime_env["VIRTUAL_ENV"] = str(selected_python.parent.parent)
    evidence_before = None
    evidence_error = None
    try:
        evidence_before = trusted_input_snapshot(sandbox, (PAPER_EVIDENCE_DIR,))
    except Exception as exc:
        evidence_error = redact_text(f"{type(exc).__name__}: {exc}")
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
                   "The Writer shell hides CUDA to prevent out-of-band GPU use; this does not mean the host lacks a GPU. "
                   "Inspect nvidia-smi and use a brokered smoke run to test CUDA. Declare `--device cpu` for "
                   "CPU execution or `--device gpu` for CUDA execution on every smoke/full launch. "
                   "Independent GPU tasks run concurrently on the same device; the host does not reserve a GPU slot. "
                   "Do not edit submitted source, config, or inputs until the run completes. "
                   "Use the broker for CUDA execution so its results and errors reach the Reporter and moderator. "
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
    if evidence_error:
        status["paper_evidence_snapshot_error"] = evidence_error
    return _inspect_task_writer_completion(status=status, sandbox=sandbox, audit_dir=audit_dir,
        case_runtime=case_runtime, request_source=request_source,
        require_execution_receipt=require_execution_receipt, evidence_before=evidence_before)


def _inspect_task_writer_completion(*, status, sandbox, audit_dir, case_runtime,
                                    request_source, require_execution_receipt, evidence_before=None):
    """Forward the session and file observations without approving its contents."""
    status.update(execution_receipts_required=require_execution_receipt, execution_audit_dir=str(audit_dir))
    if evidence_before is not None:
        try:
            evidence_after = trusted_input_snapshot(sandbox, (PAPER_EVIDENCE_DIR,))
        except Exception as exc:
            status["paper_evidence_snapshot_error"] = redact_text(f"{type(exc).__name__}: {exc}")
            return status
        changed = sorted(path for path, digest in evidence_before.items()
                         if evidence_after.get(path) != digest)
        added = sorted(set(evidence_after) - set(evidence_before))
        if added:
            # Reporter receives a fresh copy of the original paper, not this
            # Writer directory. Extra scratch files are observations, not a
            # reason to suppress an otherwise usable scientific handoff.
            status["paper_evidence_added_files"] = added
        if changed:
            status["paper_evidence_changed_files"] = changed
    return status
