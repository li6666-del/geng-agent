"""Run singleton and compound task-writer Codex state machines."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, Callable

from .agentic_foundation import _assert_foundation_sandbox_layout_safe, foundation_violations, restore_foundation_snapshot
from .case_environment import EnvironmentPolicyError
from .case_runtime import CaseRuntime, read_environment_request, requirements_missing_from_lock
from .codex_runner import run_codex_subprocess
from .execution_receipts import ExecutionBroker, trusted_input_snapshot
from .foundation_revision import read_foundation_revision_request, validate_foundation_revision_request
from .config import get_config_value
from .outputs import write_json, write_text
from .paper_evidence import safe_label
from .security import redact_text
from .task_writer_contracts import DEFAULT_MAX_EVIDENCE_RERUNS, TASK_WRITER_TERMINAL_STATUS
from .task_writer_delivery import _collect_task_writer_delivery
from .task_writer_execution_binding import _load_task_execution_binding
from .task_writer_prompts import _build_execution_unit_continuation_brief, _build_execution_unit_writer_brief, _build_task_writer_brief, _build_task_writer_continuation_brief
from .task_writer_sandbox import _prepare_execution_unit_writer_sandbox, _prepare_task_writer_sandbox
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
from .verification_result import rerun_evidence_path_issues, task_verification_issues, writer_revision_allowed


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
    base_prompt = _build_execution_unit_writer_brief(
        unit=unit,
        members=members,
        facts=facts,
        experiment_index=experiment_index,
        paper_context_json=paper_context_json,
        paper_thesis=paper_thesis,
        bindings=bindings,
        run_repro=run_repro,
        review_feedback=review_feedback,
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

    if reuse_existing:
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
                requested: dict[str, dict[str, Any]] = {}
                for (index, task, _entry), record in zip(members, existing_records):
                    action, returned = _attach_task_reporter_review(
                        callback=task_review_callback,
                        index=index,
                        task=task,
                        record=record,
                        session_round=session_round,
                    )
                    if action == "writer_revision" and isinstance(returned, dict):
                        requested[str(record.get("task_id") or "")] = returned
                if not requested:
                    return existing_records
                seen_rerun_requests.update(
                    _execution_unit_rerun_fingerprints(requested, sandbox)
                )
                evidence_based_reruns = 1
                current_feedback = requested
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
            if session_round == 1
            else _build_execution_unit_continuation_brief(
                base_prompt=base_prompt,
                unit_id=unit_id,
                session_round=session_round,
                review_feedback=current_feedback,
            )
        )
        # Some case volumes expose sub-second mtimes with a coarse rounding
        # boundary. Active compound outputs were emptied immediately above, so
        # a one-second allowance cannot admit a previous generation but avoids
        # rejecting files written in the first filesystem tick of this session.
        session_started_at = time.time()
        writer_status = _run_task_writer_codex_session(
            label=(label_base if session_round == 1 else f"{label_base}_continue_{session_round:03d}"),
            prompt=prompt,
            sandbox=sandbox,
            audit_dir=audit_dir,
            case_runtime=case_runtime,
            request_source=f"execution_unit_writer:{unit_id}",
            require_execution_receipt=run_repro,
        )
        if writer_status.get("error_kind") in {
            "environment_request",
            "environment_request_invalid",
            "foundation_revision",
        }:
            return [
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

        requested_feedback: dict[str, dict[str, Any]] = {}
        if run_repro and task_review_callback is not None:
            for (index, task, _entry), record in zip(members, records):
                action, returned = _attach_task_reporter_review(
                    callback=task_review_callback,
                    index=index,
                    task=task,
                    record=record,
                    session_round=session_round,
                )
                if action == "writer_revision" and isinstance(returned, dict):
                    requested_feedback[str(record.get("task_id") or "")] = returned

        if required_change_baseline is not None:
            current_state = _writer_source_config_fingerprint(sandbox)
            if current_state == required_change_baseline:
                for record in records:
                    task_id = str(record.get("task_id") or "")
                    feedback = requested_feedback.get(task_id) or current_feedback.get(task_id)
                    if feedback:
                        _terminalize_rerun_request(
                            record=record,
                            verification=feedback,
                            stop_reason="execution_unit_continuation_without_source_change",
                            uncertainty=(
                                "The compound Writer changed neither unit source nor configuration; "
                                "the host retained the latest scientifically honest result."
                            ),
                        )
                return records
            required_change_baseline = None
        if not run_repro or task_review_callback is None:
            return records
        if not requested_feedback:
            return records

        fingerprints = _execution_unit_rerun_fingerprints(requested_feedback, sandbox)
        repeated_request = bool(fingerprints) and fingerprints.issubset(
            seen_rerun_requests
        )
        if repeated_request or evidence_based_reruns >= rerun_budget:
            stop_reason = (
                "repeated_execution_unit_rerun_request_without_new_causal_plan"
                if repeated_request
                else "external_rerun_budget_exhausted"
            )
            for record in records:
                feedback = requested_feedback.get(str(record.get("task_id") or ""))
                if feedback:
                    _terminalize_rerun_request(
                        record=record,
                        verification=feedback,
                        stop_reason=stop_reason,
                        uncertainty=(
                            "The execution unit stopped because another full shared run lacked "
                            "a new paper-grounded causal change."
                        ),
                    )
            return records
        seen_rerun_requests.update(fingerprints)
        evidence_based_reruns += 1
        current_feedback = requested_feedback
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
        case_runtime=case_runtime,
        execution_unit_id=unit_id,
    )
    session_round = 1
    seen_rerun_requests: set[str] = set()
    evidence_based_reruns = 0
    rerun_budget = _external_writer_rerun_budget()
    required_change_baseline: str | None = None
    if reuse_existing:
        archive_round = _next_writer_progress_round(sandbox)
        if runtime_refresh_required:
            session_round = archive_round
        elif task_review_callback is None:
            existing_record = _collect_task_writer_delivery(
                index=index,
                task=task,
                manifest_entry=manifest_entry,
                sandbox=sandbox,
                writer_status={'ok': True, 'source': 'resumed_existing_delivery'},
            )
            existing_record['analysis_snapshot_hash'] = analysis_snapshot_hash
            existing_record['writer_session_count'] = max(1, archive_round)
            if existing_record.get('task_writer_status') == TASK_WRITER_TERMINAL_STATUS:
                return existing_record
        if not runtime_refresh_required and task_review_callback is not None:
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
                if review_action in {"terminal", "failed"}:
                    return existing_record
                if review_action == "writer_revision":
                    evidence = (
                        returned_feedback.get("rerun_evidence")
                        if isinstance(returned_feedback, dict)
                        else None
                    )
                    seen_rerun_requests.add(_rerun_evidence_fingerprint(evidence, _writer_progress_fingerprint(sandbox)))
                    evidence_based_reruns = 1
                    review_feedback = returned_feedback
                    required_change_baseline = _record_source_config_fingerprint(
                        existing_record,
                        sandbox,
                    )
        if not runtime_refresh_required:
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
        session_started_at = time.time()
        writer_status = _run_task_writer_codex_session(
            label=label,
            prompt=prompt,
            sandbox=sandbox,
            audit_dir=audit_dir,
            case_runtime=case_runtime,
            request_source=f"task_writer:{task_id}",
            require_execution_receipt=run_repro,
        )

        if writer_status.get("error_kind") in {
            "environment_request",
            "environment_request_invalid",
            "foundation_revision",
        }:
            return {
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
            }

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
        if required_change_baseline is not None:
            current_state = _record_source_config_fingerprint(record, sandbox)
            if current_state == required_change_baseline:
                _terminalize_rerun_request(
                    record=record,
                    verification=review_feedback,
                    stop_reason="writer_continuation_without_source_change",
                    uncertainty=(
                        "The Reporter-authorized continuation changed neither task source "
                        "nor run configuration; the flow stopped instead of spending "
                        "another unchanged scientific run."
                    ),
                )
                return _complete_task_writer_runtime_refresh(
                    record=record,
                    marker=refresh_marker,
                    required=runtime_refresh_required,
                )
            required_change_baseline = None
        if not run_repro or task_review_callback is None:
            return _complete_task_writer_runtime_refresh(
                record=record,
                marker=refresh_marker,
                required=runtime_refresh_required,
            )
        review_action, returned_feedback = _attach_task_reporter_review(
            callback=task_review_callback,
            index=index,
            task=task,
            record=record,
            session_round=session_round,
        )
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
            if rerun_fingerprint in seen_rerun_requests:
                _terminalize_rerun_request(
                    record=record,
                    verification=returned_feedback,
                    stop_reason="repeated_rerun_request_without_new_causal_plan",
                    uncertainty=(
                        "The same causal rerun request recurred after one Writer attempt; "
                        "the flow stopped instead of repeating unchanged work."
                    ),
                )
                return _complete_task_writer_runtime_refresh(
                    record=record,
                    marker=refresh_marker,
                    required=runtime_refresh_required,
                )
            if (
                evidence_based_reruns >= rerun_budget
            ):
                _terminalize_rerun_request(
                    record=record,
                    verification=returned_feedback,
                    stop_reason="external_rerun_budget_exhausted",
                    uncertainty=(
                        "The externally configured operational rerun budget was exhausted; "
                        "the latest scientific result was retained for reporting."
                    ),
                )
                return _complete_task_writer_runtime_refresh(
                    record=record,
                    marker=refresh_marker,
                    required=runtime_refresh_required,
                )
            seen_rerun_requests.add(rerun_fingerprint)
            evidence_based_reruns += 1
            review_feedback = returned_feedback
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

def _attach_task_reporter_review(
    *,
    callback: Callable[[int, dict[str, Any], dict[str, Any], int], dict[str, Any]],
    index: int,
    task: dict[str, Any],
    record: dict[str, Any],
    session_round: int,
) -> tuple[str, dict[str, Any] | None]:
    expected_task_id = str(task.get("task_id") or record.get("task_id") or "")
    try:
        task_reporter = callback(index, task, record, session_round)
    except Exception as exc:
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
        path_issues = rerun_evidence_path_issues(
            verification,
            task_reporter.get("workspace"),
        )
        if path_issues:
            warnings = record.setdefault("delivery_warnings", [])
            if isinstance(warnings, list):
                warnings.extend(path_issues)
            _terminalize_rerun_request(
                record=record,
                verification=verification,
                stop_reason="untrusted_rerun_paper_evidence",
                uncertainty=(
                    "Reporter suggested a rerun without trusted existing paper evidence; "
                    "the host declined it and retained a terminal outcome."
                ),
            )
        elif writer_revision_allowed(verification, expected_task_id):
            request = _reporter_foundation_revision(record, task_reporter, verification)
            if request is not None:
                record["foundation_revision_request"] = request
                record["task_reporter_terminal"] = False
                return "terminal", None
            return "writer_revision", verification
        else:
            # A malformed rerun request is not a reason to burn another full run.
            _terminalize_rerun_request(
                record=record,
                verification=verification,
                stop_reason="incomplete_causal_rerun_plan",
                uncertainty=(
                    "Reporter suggested a rerun without a complete causal plan; "
                    "the host recorded a terminal outcome."
                ),
            )
    issues = task_verification_issues(record.get("task_verification"), expected_task_id)
    if issues:
        warnings = record.setdefault("delivery_warnings", [])
        if isinstance(warnings, list):
            warnings.extend(issues)
    final_verification = record.get("task_verification")
    record["task_reporter_successful"] = (
        isinstance(final_verification, dict)
        and final_verification.get("outcome") in {
            "reproduced",
            "reproduced_with_assumptions",
        }
    )
    record["task_reporter_terminal"] = True
    return "terminal", None


def _reporter_foundation_revision(record, reporter, verification):
    """Route a concrete frozen-module correction to its owner, not back to Writer."""
    sandbox = Path(str(record.get("sandbox") or ""))
    architecture_path = sandbox / PAPER_EVIDENCE_DIR / "analysis_artifacts" / "scientific_architecture.json"
    if not architecture_path.is_file() or not (sandbox / "foundation_manifest.json").is_file():
        return None
    architecture = json.loads(architecture_path.read_text(encoding="utf-8-sig"))
    evidence = verification.get("rerun_evidence") or {}
    targets = [str(p).replace("\\", "/") for p in evidence.get("change_targets", [])]
    ids = [str(c.get("id")) for c in architecture.get("components", [])
           if isinstance(c, dict) and any(str(c.get("module") or "__missing__") in target or str(c.get("id") or "__missing__") == target for target in targets)]
    if not ids:
        return None
    plan_path = architecture_path.with_name("execution_plan.json")
    plan = json.loads(plan_path.read_text(encoding="utf-8-sig")) if plan_path.is_file() else None
    workspace = Path(str(reporter.get("workspace") or ""))
    try:
        request = validate_foundation_revision_request({"component_ids": ids,
            "paper_evidence_files": evidence.get("paper_evidence_files", []),
            "causal_change": evidence.get("proposed_change") or evidence.get("causal_change") or evidence.get("local_observation"),
            "predicted_effect": evidence.get("expected_effect") or evidence.get("predicted_effect")},
            architecture=architecture, evidence_root=workspace, execution_plan=plan)
    except (ValueError, OSError):
        return None
    request["evidence_root"] = str(workspace)
    return request

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
        case_runtime.python_executable if case_runtime is not None else Path(sys.executable).absolute()
    )
    python_dir = selected_python.parent
    runtime_env = {
        "GENG_PYTHON": str(selected_python),
        "GENG_PYTHON_EXECUTABLE": str(selected_python),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    if case_runtime is not None:
        runtime_env["VIRTUAL_ENV"] = str(case_runtime.venv_dir)
    evidence_before = trusted_input_snapshot(sandbox, (PAPER_EVIDENCE_DIR,))
    with ExecutionBroker(sandbox, audit_dir, selected_python,
                         environment_hash=case_runtime.environment_hash if case_runtime else "") as broker:
        runtime_env["GENG_EXECUTION_BROKER"] = broker.session_id
        prompt += ("\n\nHost-observed execution: use `$GENG_PYTHON run_task.py --task TASK_ID --config configs/TASK_ID.full.json --mode full` "
                   "(use your actual manifest config path). Invoke the selected Python using your shell's syntax. "
                   "Smoke uses --mode smoke. Declare consumed persistent checkpoint/data files with repeatable --input RELATIVE_PATH. "
                   "The host records the real process exit, source/config/input/output hashes; raw Python executions are exploratory "
                   "and cannot establish delivery provenance. Wait for an in-flight execution; do not launch it again after a CLI polling timeout. "
                   "If the observed execution fails, inspect its stderr_tail and repair before submitting another run. "
                   "Do not write execution_receipt.json yourself. Final notes may be added after the run; changing source/config/results requires a new receipt.\n")
        status = run_codex_subprocess(
            role="task_writer", work_dir=sandbox, prompt=prompt, audit_dir=audit_dir,
            label=label, sandbox="workspace-write",
            command_override=get_config_value("GENG_CODEX_TASK_WRITER_CMD"),
            image_paths=sorted(path.resolve() for path in (sandbox / PAPER_EVIDENCE_DIR / "full_paper_pages").glob("paper_page_*.png") if path.is_file()),
            extra_env=runtime_env, path_prepend=[python_dir],
        )
    status.update(execution_receipts_required=require_execution_receipt, execution_audit_dir=str(audit_dir))
    try:
        # This no-follow walk must precede every read of Writer-controlled
        # files, including a dependency request or requirements.txt.
        _assert_foundation_sandbox_layout_safe(sandbox)
    except (OSError, RuntimeError) as exc:
        return {
            **status,
            "ok": False,
            "error_kind": "environment_request_invalid",
            "blocked_reason": "task writer sandbox contains an unsafe filesystem entry",
        }
    if trusted_input_snapshot(sandbox, (PAPER_EVIDENCE_DIR,)) != evidence_before:
        return {**status, "ok": False, "error_kind": "evidence_modified",
                "blocked_reason": "Writer changed trusted paper inputs; the delivery cannot establish reproduction"}
    try:
        requests = read_environment_request(sandbox=sandbox, source=request_source)
    except EnvironmentPolicyError as exc:
        return {
            **status,
            "ok": False,
            "error_kind": "environment_request_invalid",
            "blocked_reason": "task writer produced an invalid dependency request",
        }
    if requests:
        return {
            **status,
            "ok": False,
            "error_kind": "environment_request",
            "blocked_reason": "task writer requested a host-managed case dependency",
            "environment_requests": [
                {
                    "requirement": item.requirement,
                    "import_names": list(item.import_names),
                    "requested_by": item.requested_by,
                    "reason": item.reason,
                    "capability": item.capability,
                    "import_names_explicit": item.import_names_explicit,
                }
                for item in requests
            ],
        }
    if case_runtime is not None:
        try:
            requests = requirements_missing_from_lock(
                sandbox / "requirements.txt",
                case_runtime.lock,
                source=f"{request_source}:requirements.txt",
            )
        except EnvironmentPolicyError as exc:
            return {
                **status,
                "ok": False,
                "error_kind": "environment_request_invalid",
                "blocked_reason": "task writer requirements are invalid or unsafe",
            }
        if requests:
            return {
                **status,
                "ok": False,
                "error_kind": "environment_request",
                "blocked_reason": "task writer declared a dependency absent from the active case lock",
                "environment_requests": [
                    {
                        "requirement": item.requirement,
                        "import_names": list(item.import_names),
                        "requested_by": item.requested_by,
                        "reason": item.reason,
                        "capability": item.capability,
                        "import_names_explicit": item.import_names_explicit,
                    }
                    for item in requests
                ],
            }
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
