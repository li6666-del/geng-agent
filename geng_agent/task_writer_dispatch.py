"""Dispatch independent task-writer execution units with checkpointing."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from contextvars import copy_context
from pathlib import Path
from threading import Lock
from typing import Any, Callable

from .agent_activity import record_agent_cached
from .case_runtime import CaseRuntime
from .outputs import write_json
from .paper_evidence import safe_label
from .security import redact_text
from .task_writer_results import _task_writer_runtime_task_passed
from .task_writer_runner import (
    _review_task_records,
    _run_one_execution_unit_writer,
    _run_one_task_writer,
    _task_with_experiment_profile,
)
from .task_writer_state import _checkpoint_partial_task_writer_records, _task_writer_record_refresh_pending, _task_writer_record_refresh_reusable
from .task_writer_units import _execution_unit_sandbox, _execution_unit_work_items


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
    initial_records_by_index: dict[int, dict[str, Any]] | None = None,
    review_feedback: dict[str, dict[str, Any]] | None = None,
    force_task_ids: set[str] | None = None,
    task_review_callback: Callable[[int, dict[str, Any], dict[str, Any], int], dict[str, Any]] | None = None,
    case_runtime: CaseRuntime | None = None,
    execution_plan: dict[str, Any] | None = None,
    snapshot_hashes: dict[str, str] | None = None,
    snapshot_finalizer: Callable[[dict[str, Any], list[dict[str, Any]]], None] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    existing = dict(initial_records_by_index or {})
    feedback_by_id = dict(review_feedback or {})
    forced = {str(item) for item in (force_task_ids or set()) if str(item)}
    units = _execution_unit_work_items(task_pairs, execution_plan)
    by_index: dict[int, dict[str, Any]] = {}
    pending_units: list[dict[str, Any]] = []
    reused_unit_ids: list[str] = []
    cached_review_unit_ids: set[str] = set()
    for unit in units:
        members = unit["members"]
        reusable = all(
            index in existing
            and (
                _task_writer_runtime_task_passed(existing[index])
                or existing[index].get("scientific_stop_reason") == "foundation_revision_unresolved"
                or isinstance(existing[index].get("foundation_revision_request"), dict)
            )
            and (
                _task_writer_record_refresh_reusable(existing[index])
                or isinstance(existing[index].get("foundation_revision_request"), dict)
            )
            and str(existing[index].get("task_id") or "") not in forced
            for index, _task, _entry in members
        )
        if reusable:
            # A terminal old verdict cannot establish that today's Reporter
            # prompt, source snapshot or report assets are still current.
            needs_review = task_review_callback is not None and not any(
                isinstance(existing[index].get("foundation_revision_request"), dict)
                or existing[index].get("scientific_stop_reason") == "foundation_revision_unresolved"
                for index, _task, _entry in members
            )
            if needs_review:
                cached_review_unit_ids.add(str(unit["unit_id"]))
                pending_units.append(unit)
            else:
                for index, _task, _entry in members:
                    by_index[index] = existing[index]
                reused_unit_ids.append(str(unit["unit_id"]))
            record_agent_cached(
                role="task_writer", label=str(unit["unit_id"]),
                work_dir=Path(str(existing[members[0][0]].get("sandbox") or task_root)),
            )
        else:
            pending_units.append(unit)
    launched_task_ids = [
        str(task.get("task_id") or entry.get("task_id") or "")
        for unit in pending_units
        for _index, task, entry in unit["members"]
    ]
    audit: dict[str, Any] = {
        "policy": "parallel_first",
        "parallel_attempted": len(pending_units) > 1,
        "task_count": len(task_pairs),
        "logical_task_count": len(task_pairs),
        "execution_unit_count": len(units),
        "runtime_capability": "parallel_subagents",
        "codex_sessions_unbounded": True,
        "codex_session_policy": "unbounded_until_exit_or_user_stop",
        "dispatch_batches": [{
            "batch_id": "stage5_all_execution_unit_writers",
            "execution_unit_ids": [str(unit["unit_id"]) for unit in pending_units],
            "task_ids": launched_task_ids,
            "launched_before_wait": True,
            "concurrency_limit": len(pending_units),
        }] if pending_units else [],
        "fallback_reason": None,
        "fallback_evidence_files": [],
        "attempts": [],
        "reused_task_ids": [str(record.get("task_id") or "") for record in by_index.values()],
        "reused_execution_unit_ids": reused_unit_ids,
        "cached_writer_reporter_validation_unit_ids": sorted(cached_review_unit_ids),
        "launch_counts_describe": "scheduled_unit_workflows_not_model_processes",
        "launched_execution_unit_ids": [str(unit["unit_id"]) for unit in pending_units],
        "launched_task_ids": launched_task_ids,
    }
    audit_path = audit_dir / "writer_dispatch.json"
    write_json(audit_path, audit)
    futures: dict[Future[Any], dict[str, Any]] = {}
    if pending_units:
        with ThreadPoolExecutor(max_workers=len(pending_units)) as executor:
            for unit in pending_units:
                members = unit["members"]
                if len(members) == 1:
                    index, task, manifest_entry = members[0]
                    existing_record = existing.get(index)
                    future = executor.submit(
                        copy_context().run,
                        _resume_or_run_writer,
                        writer_runner=_run_one_task_writer,
                        cached_members=members if str(unit["unit_id"]) in cached_review_unit_ids else [],
                        cached_records=existing,
                        index=index,
                        execution_unit_id=str(unit["unit_id"]),
                        reuse_existing=bool(existing_record),
                        runtime_refresh_required=bool(
                            isinstance(existing_record, dict)
                            and _task_writer_record_refresh_pending(existing_record)
                        ),
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
                        analysis_snapshot_hash=(snapshot_hashes or {}).get(str(unit["unit_id"]), analysis_snapshot_hash),
                        analysis_artifacts=analysis_artifacts,
                        task_root=task_root,
                        audit_dir=audit_dir,
                        run_repro=run_repro,
                        review_feedback=feedback_by_id.get(
                            str(task.get("task_id") or manifest_entry.get("task_id") or "")
                        ),
                        task_review_callback=task_review_callback,
                        case_runtime=case_runtime,
                    )
                else:
                    reuse_existing_unit = all(
                        index in existing for index, _task, _entry in members
                    )
                    future = executor.submit(
                        copy_context().run,
                        _resume_or_run_writer,
                        writer_runner=_run_one_execution_unit_writer,
                        cached_members=members if str(unit["unit_id"]) in cached_review_unit_ids else [],
                        cached_records=existing,
                        unit=unit,
                        reuse_existing=reuse_existing_unit,
                        runtime_refresh_required=any(
                            _task_writer_record_refresh_pending(existing[index])
                            for index, _task, _entry in members
                            if index in existing
                        ),
                        facts=facts,
                        experiment_index=experiment_index,
                        paper=paper,
                        paper_path=paper_path,
                        paper_context_json=paper_context_json,
                        paper_images=paper_images,
                        paper_thesis=paper_thesis,
                        foundation=foundation,
                        analysis_snapshot_hash=(snapshot_hashes or {}).get(str(unit["unit_id"]), analysis_snapshot_hash),
                        analysis_artifacts=analysis_artifacts,
                        task_root=task_root,
                        audit_dir=audit_dir,
                        run_repro=run_repro,
                        review_feedback=feedback_by_id,
                        task_review_callback=task_review_callback,
                        case_runtime=case_runtime,
                    )
                futures[future] = unit
            for future in as_completed(futures):
                unit = futures[future]
                members = unit["members"]
                try:
                    result = future.result()
                    unit_records = result if isinstance(result, list) else [result]
                    member_index_by_task_id = {
                        str(task.get("task_id") or entry.get("task_id") or ""): index
                        for index, task, entry in members
                    }
                    for record in unit_records:
                        if not isinstance(record, dict):
                            continue
                        record_index = record.get("index")
                        if not isinstance(record_index, int):
                            record_index = member_index_by_task_id.get(
                                str(record.get("task_id") or "")
                            )
                        if not isinstance(record_index, int) and len(members) == 1:
                            record_index = members[0][0]
                        if isinstance(record_index, int):
                            record["index"] = record_index
                            record.setdefault("execution_unit_id", str(unit["unit_id"]))
                            by_index[record_index] = record
                    missing_members = [
                        (index, task, entry)
                        for index, task, entry in members
                        if index not in by_index
                    ]
                    if missing_members:
                        missing_ids = [
                            str(task.get("task_id") or entry.get("task_id") or index)
                            for index, task, entry in missing_members
                        ]
                        raise RuntimeError(
                            "execution-unit Writer returned no delivery for logical tasks: "
                            + ", ".join(missing_ids)
                        )
                    if snapshot_finalizer is not None:
                        snapshot_finalizer(unit, [by_index[index] for index, _task, _entry in members])
                except Exception as exc:
                    for index, task, manifest_entry in members:
                        failed_task_id = str(
                            task.get("task_id")
                            or manifest_entry.get("task_id")
                            or f"task_{index}"
                        )
                        sandbox = (
                            task_root / f"{index:02d}_{safe_label(failed_task_id)}"
                            if len(members) == 1
                            else _execution_unit_sandbox(task_root, str(unit["unit_id"]))
                        )
                        by_index[index] = _failed_task_record(
                            index=index,
                            task_id=failed_task_id,
                            module=str(manifest_entry.get("module") or ""),
                            output_subdir=str(
                                manifest_entry.get("output_subdir") or failed_task_id
                            ),
                            sandbox=sandbox,
                            error=f"{type(exc).__name__}: {exc}",
                        )
                        by_index[index]["execution_unit_id"] = str(unit["unit_id"])
                audit["attempts"].append(
                    {
                        "execution_unit_id": str(unit["unit_id"]),
                        "task_ids": [
                            str(task.get("task_id") or entry.get("task_id") or "")
                            for _index, task, entry in members
                        ],
                        "writer_error_kinds": [
                            by_index[index].get("writer_error_kind")
                            for index, _task, _entry in members
                            if index in by_index
                        ],
                        "writer_completed": all(
                            bool(by_index[index].get("writer_completed"))
                            for index, _task, _entry in members
                            if index in by_index
                        ),
                    }
                )
                write_json(audit_path, audit)
                _checkpoint_partial_task_writer_records(
                    audit_dir=audit_dir,
                    dispatch_audit=audit,
                    records_by_index=by_index,
                )
    records = [by_index[index] for index in range(1, len(task_pairs) + 1)]
    audit["completed_task_count"] = len(records)
    audit["completed_execution_unit_count"] = len(units)
    write_json(audit_path, audit)
    return records, audit


def _cached_reporter_work(index, task, record, experiment_index):
    try:
        round_no = max(1, int(record.get("writer_session_count") or 1))
    except (TypeError, ValueError):
        round_no = 1
    return index, _task_with_experiment_profile(task, experiment_index), record, round_no


def _resume_or_run_writer(*, writer_runner, cached_members, cached_records, **kwargs):
    """Validate resumed reviews concurrently with the other unit workflows.

    Use the validated records directly: preparing an already finished sandbox
    would change Reporter inputs just to find out whether a review is reusable.
    Only an evidence-backed revision enters the Writer continuation machinery.
    """
    if cached_members:
        records = [cached_records[index] for index, _task, _entry in cached_members]
        callback = kwargs["task_review_callback"]
        reviews = _review_task_records(
            work=[
                _cached_reporter_work(index, task, record, kwargs["experiment_index"])
                for (index, task, _entry), record in zip(cached_members, records)
            ],
            callback=callback,
        )
        revisions = {
            str(record["task_id"]): feedback
            for record, (action, feedback) in zip(records, reviews)
            if action == "writer_revision" and isinstance(feedback, dict)
        }
        if not revisions:
            return records[0] if len(cached_members) == 1 else records
        kwargs["task_review_callback"] = _reporter_callback_with_replay(
            callback, {str(record["task_id"]): record.get("task_reporter") for record in records},
        )
        if len(cached_members) == 1:
            kwargs["review_feedback"] = revisions.get(str(records[0]["task_id"]))
        else:
            kwargs["review_feedback"] = {**(kwargs.get("review_feedback") or {}), **revisions}
    return writer_runner(**kwargs)


def _refresh_cached_task_reporters(
    *,
    task_pairs: list[tuple[dict[str, Any], dict[str, Any]]],
    cached_records: list[dict[str, Any]],
    experiment_index: dict[str, Any],
    task_review_callback: Callable[
        [int, dict[str, Any], dict[str, Any], int], dict[str, Any]
    ],
) -> tuple[
    list[dict[str, Any]],
    dict[str, Any],
    dict[str, dict[str, Any]],
    dict[str, Any],
]:
    """Revalidate Reporters while leaving cached Writer deliveries untouched.

    Reporter cache validity belongs to the Reporter itself: the Writer layer
    must call the callback even when an older task verification is terminal.
    Logical Reporters are independent, so cache checks and cache-miss refreshes
    retain the normal parallel-first behavior.
    """

    records_by_id = {
        str(record.get("task_id") or ""): record
        for record in cached_records
        if isinstance(record, dict) and str(record.get("task_id") or "")
    }
    work: list[tuple[int, dict[str, Any], dict[str, Any], int]] = []
    for index, (task, entry) in enumerate(task_pairs, start=1):
        task_id = str(task.get("task_id") or entry.get("task_id") or f"task_{index}")
        record = records_by_id.get(task_id)
        if record is None:
            raise ValueError(f"cached Writer record is missing logical task {task_id}")
        record["index"] = index
        work.append(_cached_reporter_work(index, task, record, experiment_index))

    by_index: dict[int, dict[str, Any]] = {}
    replay_by_task_id: dict[str, Any] = {}
    revision_feedback: dict[str, dict[str, Any]] = {}
    actions: list[dict[str, Any]] = []

    reviews = _review_task_records(work=work, callback=task_review_callback)
    for (index, task, record, _round_no), (action, feedback) in zip(work, reviews):
        task_id = str(record.get("task_id") or task.get("task_id") or f"task_{index}")
        reporter_result = record.get("task_reporter")
        by_index[index] = record
        replay_by_task_id[task_id] = reporter_result
        if action == "writer_revision" and isinstance(feedback, dict):
            revision_feedback[task_id] = feedback
        actions.append(
            {
                "index": index,
                "task_id": task_id,
                "action": action,
                "reporter_cached": (
                    reporter_result.get("cached")
                    if isinstance(reporter_result, dict)
                    else None
                ),
            }
        )

    refreshed_records = [by_index[index] for index in range(1, len(work) + 1)]
    audit = {
        "policy": "parallel_cached_writer_reporter_validation",
        "scientific_writer_artifacts_reused": True,
        "writer_sessions_launched": 0,
        "parallel_attempted": len(work) > 1,
        "reporter_count": len(work),
        "writer_revision_task_ids": sorted(revision_feedback),
        "actions": sorted(actions, key=lambda item: int(item["index"])),
    }
    return refreshed_records, replay_by_task_id, revision_feedback, audit


def _reporter_callback_with_replay(
    callback: Callable[[int, dict[str, Any], dict[str, Any], int], dict[str, Any]],
    replay_by_task_id: dict[str, Any],
) -> Callable[[int, dict[str, Any], dict[str, Any], int], dict[str, Any]]:
    """Replay one prevalidated Reporter result before invoking it again.

    A cached Reporter may request a Writer continuation. The existing Writer
    state machine must see that exact decision once, without launching another
    Reporter into the same audit round. Subsequent calls, after the Writer has
    changed science, go to the real callback and receive a fresh cache/input
    decision.
    """

    pending = dict(replay_by_task_id)
    lock = Lock()

    def wrapped(
        index: int,
        task: dict[str, Any],
        record: dict[str, Any],
        session_round: int,
    ) -> dict[str, Any]:
        task_id = str(task.get("task_id") or record.get("task_id") or f"task_{index}")
        with lock:
            if task_id in pending:
                return pending.pop(task_id)
        return callback(index, task, record, session_round)

    return wrapped

def _task_writer_concurrency(task_count: int, requested: int | None, *, run_repro: bool = False) -> int:
    del requested, run_repro
    return max(1, task_count)

def _failed_task_record(
    *,
    index: int,
    task_id: str,
    module: str,
    output_subdir: str,
    sandbox: Path,
    error: str,
) -> dict[str, Any]:
    return {
        "index": index,
        "task_id": task_id,
        "module": module,
        "output_subdir": output_subdir,
        "sandbox": str(sandbox),
        "task_writer_status": "failed",
        "writer_completed": False,
        "writer_status": {"ok": False, "error": redact_text(error)[:1000]},
        "result_json": {"task_id": task_id, "status": "failed", "summary": redact_text(error)[:500]},
        "execution_summary": {},
        "artifacts": {},
        "local_images": [],
    }
