from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
import json
import os
import secrets
import shutil
import time
from pathlib import Path
from typing import Any

from .task_writer_support import (
    CODEX_PROJECT_BACKEND,
    _load_cached_task_writer_workflow,
    _manifest_from_project,
    _manifest_disk_paths,
    _prepare_project_workspace,
    _prune_unexpected_files,
    _render_writer_python_cmd_wrapper,
    _render_writer_python_sh_wrapper,
    _resolve_writer_real_python,
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
from .task_contract import build_task_contract_draft, contract_hash
from .resource_runtime import ResourceBroker
from .resource_scheduler import WriterConcurrencyController, build_resource_plan, detect_hardware


TASK_WRITER_STATUSES = {"matched", "explained_gap", "failed"}


def _task_with_experiment_profile(task: dict[str, Any], experiment_index: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(task)
    task_id = str(task.get("task_id") or "")
    experiments = experiment_index.get("experiments") if isinstance(experiment_index, dict) else []
    for experiment in experiments if isinstance(experiments, list) else []:
        if not isinstance(experiment, dict) or str(experiment.get("task_id") or "") != task_id:
            continue
        enriched["experiment_id"] = experiment.get("experiment_id") or task_id
        enriched["reproducibility_mode"] = experiment.get("reproducibility_mode") or "proxy_only"
        criteria = experiment.get("acceptance_criteria")
        if isinstance(criteria, list) and criteria:
            enriched["acceptance_criteria"] = criteria
        enriched["feasibility"] = experiment.get("feasibility") or {}
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
    paper_thesis: dict[str, Any] | None,
    output_dir: Path,
    audit_dir: Path,
    repro_project_dir: Path,
    run_repro: bool,
    timeout: float = 1800.0,
    run_timeout: float = 120.0,
    resume: bool = True,
    paper_memory: dict[str, Any] | None = None,
    memory_snapshot_hash: str = "",
) -> dict[str, Any]:
    """Third-round autonomous per-task Codex writer workflow.

    Each task gets an isolated sandbox and one Codex writer that owns code,
    full execution, and task-level paper comparison. The host does not run a
    separate reviewer and does not repeat the full run after merging.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    audit_dir.mkdir(parents=True, exist_ok=True)
    timeout = max(1.0, float(timeout or 1800.0))
    run_timeout = max(1.0, float(run_timeout or 120.0))

    task_manifest = build_tasks_manifest(tasks)
    task_items = [task for task in tasks.get("repro_tasks", []) if isinstance(task, dict)]
    manifest_entries = [entry for entry in task_manifest.get("tasks", []) if isinstance(entry, dict)]
    task_pairs = list(zip(task_items, manifest_entries))
    expected_paths = expected_generated_paths([item["script"] for item in manifest_entries])

    cached = _load_cached_task_writer_workflow(
        output_dir=output_dir,
        repro_project_dir=repro_project_dir,
        run_repro=run_repro,
        memory_snapshot_hash=memory_snapshot_hash,
    )
    cached_runtime_passed = bool((cached or {}).get("runtime_result", {}).get("passed"))
    if resume and cached is not None and (not run_repro or cached_runtime_passed):
        cached_records = cached.get("task_records") if isinstance(cached.get("task_records"), list) else []
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
            expected_memory_snapshot_hash=memory_snapshot_hash,
        )
        if resume
        else {}
    )

    _clear_stage_outputs(output_dir, "manifest", preserve_audit=bool(resume_records))

    task_root = audit_dir / "03c_task_writer_sandboxes"
    if task_root.exists() and not resume_records:
        shutil.rmtree(task_root)
    task_root.mkdir(parents=True, exist_ok=True)
    resource_root = audit_dir / "03c_task_writer_resources"
    if resource_root.exists():
        shutil.rmtree(resource_root)
    resource_root.mkdir(parents=True, exist_ok=True)
    hardware_snapshot = detect_hardware()
    resource_plan = build_resource_plan(
        task_count=len(task_pairs),
        requested_writer_concurrency=len(task_pairs),
        hardware=hardware_snapshot,
    )
    resource_plan_path = audit_dir / "resource_plan.json"
    resource_events_path = audit_dir / "resource_events.jsonl"
    write_json(audit_dir / "hardware_snapshot.json", hardware_snapshot)
    write_json(resource_plan_path, resource_plan)
    write_text(resource_events_path, "")
    status: dict[str, Any] = {
        "backend": CODEX_PROJECT_BACKEND,
        "mode": "task_writers",
        "stop_rule": "matched_or_evidenced_gap_or_failed",
        "run_repro": bool(run_repro),
        "task_count": len(task_pairs),
        "resource_plan": str(resource_plan_path),
    }
    writer_plan = resource_plan["writer"]
    status["agent_concurrency"] = int(writer_plan["initial_concurrency"])
    status["agent_concurrency_max"] = int(writer_plan["max_concurrency"])
    write_json(audit_dir / "03c_task_writers_start.json", status)
    resource_broker = ResourceBroker(
        plan=resource_plan,
        events_path=resource_events_path,
        state_path=resource_root / "resource_state.json",
    )
    resource_broker.start()
    try:
        task_records, dispatch_audit = _dispatch_task_writers(
            task_pairs=task_pairs,
            facts=facts,
            experiment_index=experiment_index,
            paper=paper,
            paper_path=paper_path,
            paper_context_json=paper_context_json,
            paper_thesis=paper_thesis,
            paper_memory=paper_memory,
            memory_snapshot_hash=memory_snapshot_hash,
            task_root=task_root,
            audit_dir=audit_dir,
            timeout=timeout,
            run_timeout=run_timeout,
            run_repro=run_repro,
            resource_plan=resource_plan,
            resource_plan_path=resource_plan_path,
            resource_broker=resource_broker,
            initial_records_by_index=resume_records,
        )
    finally:
        resource_broker.stop()
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
    manifest = _manifest_from_project(
        repro_project_dir=repro_project_dir,
        expected_paths=expected_paths,
        task_manifest=final_task_manifest,
        round_no=1,
    )
    manifest["_meta"]["mode"] = "task_writers"
    manifest["_meta"]["memory_snapshot_hash"] = memory_snapshot_hash
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
    expected_memory_snapshot_hash: str,
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
        if str(record.get("memory_snapshot_hash") or expected_memory_snapshot_hash) != expected_memory_snapshot_hash:
            continue
        records[index] = record
    return records


def _guard_token_from_record(record: dict[str, Any] | None) -> str | None:
    if not isinstance(record, dict):
        return None
    run_records = record.get("run_records") if isinstance(record.get("run_records"), list) else []
    for item in reversed(run_records):
        if isinstance(item, dict) and isinstance(item.get("guard_token"), str) and item["guard_token"]:
            return item["guard_token"]
    raw_run_log = str(record.get("run_log_path") or "")
    if not raw_run_log:
        return None
    run_log = Path(raw_run_log)
    if not run_log.is_file():
        return None
    for item in reversed(_read_jsonl(run_log)):
        if isinstance(item.get("guard_token"), str) and item["guard_token"]:
            return item["guard_token"]
    return None


def _dispatch_task_writers(
    *,
    task_pairs: list[tuple[dict[str, Any], dict[str, Any]]],
    facts: dict[str, Any],
    experiment_index: dict[str, Any],
    paper: dict[str, Any],
    paper_path: Path,
    paper_context_json: str,
    paper_thesis: dict[str, Any] | None,
    paper_memory: dict[str, Any] | None,
    memory_snapshot_hash: str,
    task_root: Path,
    audit_dir: Path,
    timeout: float,
    run_timeout: float,
    run_repro: bool,
    resource_plan: dict[str, Any],
    resource_plan_path: Path,
    resource_broker: ResourceBroker | None,
    initial_records_by_index: dict[int, dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    writer_plan = resource_plan["writer"]
    controller = WriterConcurrencyController(writer_plan)
    retries = max(0, int(writer_plan.get("capacity_retries") or 0))
    retry_base = max(0.0, float(writer_plan.get("retry_base_seconds") or 0.0))
    existing = dict(initial_records_by_index or {})
    by_index: dict[int, dict[str, Any]] = {
        index: record for index, record in existing.items() if _task_writer_runtime_task_passed(record)
    }
    pending: list[dict[str, Any]] = []
    for index in range(1, len(task_pairs) + 1):
        if index in by_index:
            continue
        previous = existing.get(index)
        previous_attempt = max(1, int((previous or {}).get("attempt") or 1)) if previous else 0
        previous_writer_ok = bool(((previous or {}).get("writer_status") or {}).get("ok"))
        pending.append(
            {
                "index": index,
                "attempt": previous_attempt + 1 if previous else 1,
                "ready_at": 0.0,
                "capacity_retry": 0,
                "reuse_existing": bool(previous),
                "resume_record": previous if previous_writer_ok else None,
            }
        )
    guard_tokens = {
        index: _guard_token_from_record(existing.get(index)) or secrets.token_hex(16)
        for index in range(1, len(task_pairs) + 1)
    }
    futures: dict[Future[dict[str, Any]], dict[str, Any]] = {}
    capacity_not_before = 0.0
    audit: dict[str, Any] = {
        "policy": "parallel_first",
        "parallel_attempted": len(task_pairs) > 1,
        "task_count": len(task_pairs),
        "runtime_capability": "parallel_subagents",
        "runtime_limit": {
            "initial_concurrency": controller.current,
            "max_concurrency": controller.maximum,
            "resource_plan": str(resource_plan_path),
        },
        "dispatch_batches": [],
        "fallback_reason": (
            f"adaptive runtime concurrency limit starts {controller.current} of {len(task_pairs)} task writers"
            if len(task_pairs) > controller.current
            else None
        ),
        "fallback_evidence_files": (
            ["audit/resource_plan.json"] if len(task_pairs) > controller.current else []
        ),
        "attempts": [],
        "concurrency_events": [],
        "reused_task_ids": [str(record.get("task_id") or "") for record in by_index.values()],
        "repair_task_ids": [
            str(task_pairs[int(item["index"]) - 1][0].get("task_id") or "") for item in pending
        ],
    }
    audit_path = audit_dir / "writer_dispatch.json"

    def flush() -> None:
        write_json(audit_path, audit)

    def failed_record(index: int, exc: Exception) -> dict[str, Any]:
        task, manifest_entry = task_pairs[index - 1]
        task_id = str(task.get("task_id") or manifest_entry.get("task_id") or f"task_{index}")
        return _failed_task_record(
            index=index,
            task_id=task_id,
            module=str(manifest_entry.get("module") or ""),
            error=f"{type(exc).__name__}: {exc}",
        )

    flush()
    with ThreadPoolExecutor(max_workers=max(1, controller.maximum)) as executor:
        while pending or futures:
            now = time.monotonic()
            launched: list[dict[str, Any]] = []
            while len(futures) < controller.current and now >= capacity_not_before:
                ready_index = next((i for i, item in enumerate(pending) if float(item["ready_at"]) <= now), None)
                if ready_index is None:
                    break
                item = pending.pop(ready_index)
                index = int(item["index"])
                attempt = int(item["attempt"])
                capacity_retry = int(item.get("capacity_retry") or 0)
                task, manifest_entry = task_pairs[index - 1]
                future = executor.submit(
                    _run_one_task_writer,
                    index=index,
                    attempt=attempt,
                    reuse_existing=bool(item.get("reuse_existing")) or attempt > 1,
                    resume_record=item.get("resume_record"),
                    guard_token=guard_tokens[index],
                    task=task,
                    manifest_entry=manifest_entry,
                    facts=facts,
                    experiment_index=experiment_index,
                    paper=paper,
                    paper_path=paper_path,
                    paper_context_json=paper_context_json,
                    paper_thesis=paper_thesis,
                    paper_memory=paper_memory,
                    memory_snapshot_hash=memory_snapshot_hash,
                    task_root=task_root,
                    audit_dir=audit_dir,
                    timeout=timeout,
                    run_timeout=run_timeout,
                    run_repro=run_repro,
                    resource_broker=resource_broker,
                )
                futures[future] = item
                launched.append(
                    {
                        "task_id": str(task.get("task_id") or manifest_entry.get("task_id") or f"task_{index}"),
                        "index": index,
                        "attempt": attempt,
                        "capacity_retry": capacity_retry,
                    }
                )
            if launched:
                audit["dispatch_batches"].append(
                    {
                        "batch_id": f"stage5_batch_{len(audit['dispatch_batches']) + 1:02d}",
                        "task_ids": [item["task_id"] for item in launched],
                        "launched_before_wait": True,
                        "concurrency_limit": controller.current,
                        "writer_attempts": launched,
                        "time": time.time(),
                    }
                )
                flush()
            if not futures:
                if pending:
                    next_ready = max(capacity_not_before, min(float(item["ready_at"]) for item in pending))
                    delay = max(0.0, next_ready - time.monotonic())
                    time.sleep(min(1.0, delay) if delay > 0 else 0.05)
                continue
            done, _ = wait(set(futures), timeout=1.0, return_when=FIRST_COMPLETED)
            if not done:
                continue
            for future in done:
                item = futures.pop(future)
                index = int(item["index"])
                attempt = int(item["attempt"])
                capacity_retry = int(item.get("capacity_retry") or 0)
                try:
                    record = future.result()
                except Exception as exc:
                    record = failed_record(index, exc)
                record["attempt"] = attempt
                task_id = str(record.get("task_id") or f"task_{index}")
                capacity_error = _task_writer_capacity_blocked(record)
                audit["attempts"].append(
                    {
                        "task_id": task_id,
                        "index": index,
                        "attempt": attempt,
                        "writer_error_kind": record.get("writer_error_kind"),
                        "writer_completed": bool(record.get("writer_completed")),
                        "completed_at": time.time(),
                    }
                )
                if capacity_error and capacity_retry < retries:
                    before, after = controller.record_capacity_error()
                    delay = retry_base * (2**capacity_retry)
                    retry_ready_at = time.monotonic() + delay
                    capacity_not_before = max(capacity_not_before, retry_ready_at)
                    pending.append(
                        {
                            "index": index,
                            "attempt": attempt + 1,
                            "ready_at": retry_ready_at,
                            "capacity_retry": capacity_retry + 1,
                            "reuse_existing": True,
                            "resume_record": None,
                        }
                    )
                    audit["concurrency_events"].append(
                        {
                            "event": "capacity_backoff",
                            "task_id": task_id,
                            "attempt": attempt,
                            "capacity_retry": capacity_retry + 1,
                            "before": before,
                            "after": after,
                            "retry_delay_s": delay,
                            "scope": "global",
                            "global_not_before_monotonic": capacity_not_before,
                            "pending_task_count": len(pending),
                            "time": time.time(),
                        }
                    )
                else:
                    by_index[index] = record
                    writer_ok = bool(record.get("writer_completed"))
                    if writer_ok:
                        before, after = controller.record_success()
                        if after != before:
                            audit["concurrency_events"].append(
                                {
                                    "event": "stable_success_increase",
                                    "task_id": task_id,
                                    "before": before,
                                    "after": after,
                                    "time": time.time(),
                                }
                            )
                    else:
                        controller.record_other_failure()
                flush()
    records = [by_index[index] for index in range(1, len(task_pairs) + 1)]
    audit["final_concurrency"] = controller.current
    audit["completed_task_count"] = len(records)
    flush()
    return records, audit


def _run_one_task_writer(
    *,
    index: int,
    attempt: int,
    reuse_existing: bool,
    resume_record: dict[str, Any] | None,
    guard_token: str,
    task: dict[str, Any],
    manifest_entry: dict[str, Any],
    facts: dict[str, Any],
    experiment_index: dict[str, Any],
    paper: dict[str, Any],
    paper_path: Path,
    paper_context_json: str,
    paper_thesis: dict[str, Any] | None,
    paper_memory: dict[str, Any] | None,
    memory_snapshot_hash: str,
    task_root: Path,
    audit_dir: Path,
    timeout: float,
    run_timeout: float,
    run_repro: bool,
    resource_broker: ResourceBroker | None,
) -> dict[str, Any]:
    task_id = str(task.get("task_id") or manifest_entry.get("task_id") or f"task_{index}")
    module = str(manifest_entry.get("module") or safe_label(task_id))
    task = _task_with_experiment_profile(task, experiment_index)
    base_label = f"03c_task_writer_{index:02d}_{safe_label(task_id)}"
    label = base_label if attempt <= 1 else f"{base_label}_attempt_{attempt:02d}"
    sandbox = task_root / f"{index:02d}_{safe_label(task_id)}"
    output_subdir = str(manifest_entry.get("output_subdir") or task_id)
    run_log = sandbox / "task_agent_runs.jsonl"
    _prepare_task_writer_sandbox(
        sandbox=sandbox,
        task=task,
        manifest_entry=manifest_entry,
        paper=paper,
        paper_path=paper_path,
        facts=facts,
        paper_thesis=paper_thesis,
        paper_memory=paper_memory,
        memory_snapshot_hash=memory_snapshot_hash,
        reuse_existing=reuse_existing,
    )
    if resource_broker is None:
        raise RuntimeError("task writer resource broker is not running")
    broker_channel = resource_broker.register_channel(
        task_id=task_id,
        channel_dir=sandbox / ".geng_resource_broker",
    )
    timeout_state_path = sandbox / ".geng_task_writer_timeout_state.json"
    writer_status_history = list((resume_record or {}).get("writer_status_history") or [])
    if resume_record is None:
        prompt = _build_task_writer_brief(
            index=index,
            task=task,
            manifest_entry=manifest_entry,
            facts=facts,
            experiment_index=experiment_index,
            paper=paper,
            paper_context_json=paper_context_json,
            paper_thesis=paper_thesis,
            run_timeout=run_timeout,
            run_repro=run_repro,
            retry_attempt=attempt,
        )
        writer_status = _run_task_writer_codex_session(
            label=label,
            prompt=prompt,
            sandbox=sandbox,
            audit_dir=audit_dir,
            task_id=task_id,
            module=module,
            output_subdir=output_subdir,
            run_log=run_log,
            allow_full=run_repro,
            run_timeout=run_timeout,
            memory_snapshot_hash=memory_snapshot_hash,
            broker_channel=broker_channel,
            resource_broker=resource_broker,
            timeout_state_path=timeout_state_path,
            guard_token=guard_token,
            timeout=timeout,
        )
        writer_status_history.append({"label": label, "repair_kind": "initial", "status": writer_status})
    else:
        writer_status = resume_record.get("writer_status") if isinstance(resume_record.get("writer_status"), dict) else {}

    _restore_trusted_files(sandbox, {"version": 1, "tasks": [manifest_entry]})
    record = _collect_task_writer_delivery(
        index=index,
        task=task,
        manifest_entry=manifest_entry,
        sandbox=sandbox,
        writer_status=writer_status,
        run_log=run_log,
    )
    record["writer_status_history"] = writer_status_history
    record["attempt"] = attempt
    return record


def _run_task_writer_codex_session(
    *,
    label: str,
    prompt: str,
    sandbox: Path,
    audit_dir: Path,
    task_id: str,
    module: str,
    output_subdir: str,
    run_log: Path,
    allow_full: bool,
    run_timeout: float,
    memory_snapshot_hash: str,
    broker_channel: dict[str, str],
    resource_broker: ResourceBroker,
    timeout_state_path: Path,
    guard_token: str,
    timeout: float,
) -> dict[str, Any]:
    write_text(audit_dir / f"{label}_brief.md", prompt)
    guard = _prepare_task_writer_python_guard(
        audit_dir=audit_dir,
        label=label,
        task_id=task_id,
        module=module,
        output_subdir=output_subdir,
        run_log=run_log,
        allow_full=allow_full,
        run_timeout=run_timeout,
        contract_path=sandbox / "task_contract.json",
        memory_snapshot_hash=memory_snapshot_hash,
        resource_channel_dir=Path(broker_channel["channel_dir"]),
        resource_channel_token=broker_channel["token"],
        resource_wait_timeout=float(resource_broker.plan["execution"].get("resource_wait_timeout_seconds") or 1800.0),
        timeout_state_path=timeout_state_path,
        guard_token=guard_token,
    )
    return run_codex_subprocess(
        role="task_writer",
        work_dir=sandbox,
        prompt=prompt,
        audit_dir=audit_dir,
        label=label,
        sandbox="workspace-write",
        timeout=timeout,
        command_override=get_config_value("GENG_CODEX_TASK_WRITER_CMD"),
        extra_env=guard["env"],
        path_prepend=[guard["bin_dir"]],
        timeout_state_path=timeout_state_path,
    )


def _collect_task_writer_delivery(
    *,
    index: int,
    task: dict[str, Any],
    manifest_entry: dict[str, Any],
    sandbox: Path,
    writer_status: dict[str, Any],
    run_log: Path,
) -> dict[str, Any]:
    """Collect writer-owned outputs without repairing or format-gating them."""
    task_id = str(task.get("task_id") or manifest_entry.get("task_id") or f"task_{index}")
    module = str(manifest_entry.get("module") or "")
    output_subdir = str(manifest_entry.get("output_subdir") or task.get("task_id") or "task")
    result_path, _ = _task_result_file_path(sandbox, output_subdir, "task_agent_result.json")
    markdown_path, _ = _task_result_file_path(sandbox, output_subdir, "task_agent_result.md")
    contract_path = sandbox / "task_contract.json"

    result_doc = _read_optional_json_object(result_path)
    contract_doc = _read_optional_json_object(contract_path)
    status = str(result_doc.get("status") or "failed")
    if status not in TASK_WRITER_STATUSES or not writer_status.get("ok"):
        status = "failed"

    artifacts = inspect_output_artifacts(sandbox, subdir=output_subdir)
    local_images = _collect_writer_images(
        sandbox=sandbox,
        output_subdir=output_subdir,
        declared=result_doc.get("local_image_paths"),
        fallback_pattern="*.png",
        exclude_names={"paper_target_crop.png", "paper_target_locator.png"},
    )
    run_records = _read_jsonl(run_log)
    full_runs = [item for item in run_records if item.get("profile") == "full"]

    return {
        "index": index,
        "task_id": task_id,
        "module": module,
        "output_subdir": output_subdir,
        "sandbox": str(sandbox),
        "writer_status": writer_status,
        "writer_completed": bool(writer_status.get("ok")),
        "task_writer_status": status,
        "result_json": result_doc,
        "result_json_path": str(result_path) if result_path.exists() else None,
        "result_markdown_path": str(markdown_path) if markdown_path.exists() else None,
        "task_contract_path": str(contract_path) if contract_path.exists() else None,
        "task_contract": contract_doc,
        "task_contract_hash": contract_hash(contract_doc) if contract_doc else None,
        "reproducibility_mode": str(contract_doc.get("reproducibility_mode") or task.get("reproducibility_mode") or "unknown"),
        "run_log_path": str(run_log) if run_log.exists() else None,
        "run_records": run_records,
        "full_run": full_runs[-1] if full_runs else None,
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
    paper_memory: dict[str, Any] | None,
    memory_snapshot_hash: str,
    reuse_existing: bool = False,
) -> None:
    if reuse_existing and sandbox.exists():
        return
    if sandbox.exists():
        shutil.rmtree(sandbox)
    sandbox.mkdir(parents=True, exist_ok=True)
    single_manifest = {"version": 1, "tasks": [manifest_entry]}
    inject_io_runtime(sandbox)
    write_task_scaffolding(sandbox, single_manifest)
    _write_minimal_shared_project_files(sandbox, task, manifest_entry)
    write_json(
        sandbox / "task_contract.json",
        build_task_contract_draft(task, memory_snapshot_hash=memory_snapshot_hash),
    )
    _write_paper_evidence_bundle(
        repro_project_dir=sandbox,
        paper_path=paper_path,
        paper=paper,
        facts=facts,
        tasks={"repro_tasks": [task]},
        paper_thesis=paper_thesis,
        paper_memory=paper_memory,
        memory_snapshot_hash=memory_snapshot_hash,
    )


def _write_minimal_shared_project_files(sandbox: Path, task: dict[str, Any], manifest_entry: dict[str, Any]) -> None:
    task_id = str(task.get("task_id") or manifest_entry.get("task_id") or "task")
    module = str(manifest_entry.get("module") or "task")
    write_text(sandbox / "README.md", f"# Task writer sandbox\n\nTask: `{task_id}`\n")
    write_text(sandbox / "requirements.txt", "numpy\nmatplotlib\n")
    write_json(sandbox / "config.json", {"run_profile": "full", "task_id": task_id, "seed": 1})
    write_json(sandbox / "config_smoke.json", {"run_profile": "smoke", "task_id": task_id, "seed": 1, "smoke": True})
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
    run_timeout: float,
    run_repro: bool,
    retry_attempt: int = 1,
) -> str:
    task_id = str(task.get("task_id") or manifest_entry.get("task_id") or f"task_{index}")
    module = str(manifest_entry.get("module") or "")
    output_subdir = str(manifest_entry.get("output_subdir") or task_id)
    task_context = paper_context_for_task(paper=paper, facts=facts, task=task)
    task_facts = facts_for_task(facts, task)
    ordering_anchor = thesis_ordering_anchor_for_task(paper_thesis, task)
    full_instruction = (
        f"Run your full task with `python -m tasks.{module} config.json` after each meaningful fix."
        if run_repro
        else "Do not run full config because --run-repro is disabled; produce code and mark the result failed/skipped."
    )
    retry_instruction = (
        f"This is capacity-retry attempt {retry_attempt}. Preserve and inspect the existing sandbox, prior run log, code, and artifacts before continuing."
        if retry_attempt > 1
        else "This is the first writer attempt for this task."
    )
    return f"""# Role: autonomous Codex task writer

You own exactly one reproduction task. There is no separate reviewer. You must write the code, run your assigned task, compare the output to the paper evidence, revise if needed, then leave your final scientific conclusion for the dedicated report agent.

{retry_instruction}

## Hard boundaries
- Assigned task_id: `{task_id}`
- Assigned module: `tasks.{module}`
- Output directory: `outputs/{output_subdir}/`
- You may edit only: `README.md`, `requirements.txt`, `config.json`, `config_smoke.json`, `task_contract.json`, `tasks/{module}.py`, optional `tasks/{module}_lib.py`, your result files, and files under `outputs/{output_subdir}/`.
- Do not edit `src/_io.py`, `src/_backend.py`, `run_experiment.py`, `tasks_manifest.json`, `tasks/__init__.py`, or any other task module.
- Do not run `python run_experiment.py config.json`; the Python guard rejects dispatcher full runs and other task modules.
- {full_instruction}
- You may run smoke with `python -m tasks.{module} config_smoke.json`.
- Full runs use the sandbox-local Python guard; do not bypass it.
- There is no cycle limit. Keep iterating toward the paper until you reach a scientifically justified terminal state.

## Mandatory self-iteration protocol
You are not a one-shot report writer. You are the coder, runner, and reviewer for this task. Work in repeated cycles until the result is either matched, explained by a defensible gap, or genuinely failed.

For each cycle:
1. Before writing code, inspect and finalize `task_contract.json`. It is the authoritative mapping from paper facts to inputs, equations, outputs, invariants, backend, resource request, seed, and acceptance criteria. Keep `task_id`, `experiment_id`, and `memory_snapshot_hash` intact. Set `resources.execution_class`, CPU cores, RAM, GPU count, and VRAM conservatively; use `cpu_light` only for genuinely small analytic/plotting jobs, `cpu_heavy` for large CPU simulations, `gpu` for CUDA full runs, and `unknown` when evidence is insufficient.
2. Implement or revise the task code/config against that contract.
3. Run smoke only as a quick sanity check.
4. Run full with `python -m tasks.{module} config.json` when `--run-repro` is enabled. The guard rejects full until the contract validates.
5. Inspect the local CSV/summary/PNG and compare them with the contract and paper evidence images/text.
6. If the result does not match the paper claim, first assume your implementation, configuration, proxy model, axis scaling, normalization, baseline, seed, or plotting could be wrong. Form a concrete repair hypothesis, modify code or config, and run full again.
7. Record each cycle in `task_agent_result.md`: contract change, command, return code, changed files, observed mismatch, repair hypothesis, and next decision. This file is audit evidence, not part of the final human report.

Do not stop after the first imperfect output. A mismatch must trigger another concrete hypothesis, code/config/model change, and rerun while a plausible repair remains. Never rerun an unchanged full merely to consume another cycle: every repeated full must follow a meaningful implementation/configuration change or test a new hypothesis. Report `explained_gap` only after plausible repairs are exhausted and you can name the remaining difference, cause, and evidence. Report `failed` only for a real blocker such as runtime failure, missing essential paper information, timeout, dependency failure, or no usable artifacts. Report `matched` only when local artifacts support the paper trend, scale, ordering, and baseline comparison for this task.

Reproducibility-mode rule from the host contract:
- `native_full` and `scaled_full` may end as `matched` when all acceptance criteria are met (for scaled runs, state the preserved scale assumptions).
- `proxy_only` must not claim complete reproduction; use `explained_gap` and identify what the proxy does and does not establish.
- `environment_blocked` and `upstream_patch_required` normally end as `failed` unless the blocker is actually resolved and the contract is updated with evidence.

Stopping rule:
- If a cycle reaches `matched`, stop immediately and write the final files.
- If the result is not matched and a plausible implementation/configuration/model repair remains, continue iterating and rerun full after that meaningful change.
- Stop as `explained_gap` only when usable artifacts exist, the remaining mismatch is evidenced, and you can explain why further local code/config changes cannot resolve it.
- Stop as `failed` only when the task lacks usable artifacts or an external blocker prevents further progress.
- The host will not repair JSON, paths, BOMs, contracts, result fields, or missing files after you exit. You alone own a complete scientific delivery.

## Required final files
- `task_contract.json`: validated experiment contract used by every full run.
- `task_agent_result.md`: Chinese audit log of your implementation and scientific iteration. It will not be appended to the final report.
- `task_agent_result.json`: strict JSON object with:
```json
{{
  "task_id": "{task_id}",
  "status": "matched|explained_gap|failed",
  "summary": "one Chinese sentence",
  "differences": [],
  "possible_causes": [],
  "remaining_uncertainties": [],
  "evidence_files": [],
  "local_image_paths": []
}}
```
- If `status == "explained_gap"`, `differences`, `possible_causes`, `remaining_uncertainties`, and `evidence_files` must all be non-empty.
- If `status == "matched"`, cite the local CSV/PNG/summary and paper evidence that support the match.
- If `status == "failed"`, explain whether the blocker is runtime, missing paper details, timeout, dependency, or modeling uncertainty.

## Trusted runtime APIs
{IO_RUNTIME_API_DOC}

{BACKEND_RUNTIME_API_DOC}

## Dependency policy
{dependency_policy_prompt_text()}

## Task evidence files
- `paper_evidence/index.json`
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

## Relevant facts
```json
{pretty_json(task_facts)}
```

## Paper thesis / ordering anchor
{ordering_anchor or "None"}

## Task paper context
{task_context[:12000]}

## Full paper context excerpt
{paper_context_json[:8000]}

## Experiment index
```json
{pretty_json(experiment_index)[:8000]}
```
"""


def _prepare_task_writer_python_guard(
    *,
    audit_dir: Path,
    label: str,
    task_id: str,
    module: str,
    output_subdir: str,
    run_log: Path,
    allow_full: bool,
    run_timeout: float,
    contract_path: Path,
    memory_snapshot_hash: str,
    resource_channel_dir: Path,
    resource_channel_token: str,
    resource_wait_timeout: float,
    timeout_state_path: Path,
    guard_token: str | None = None,
) -> dict[str, Any]:
    bin_dir = audit_dir / f"{label}_python_guard"
    if bin_dir.exists():
        shutil.rmtree(bin_dir)
    bin_dir.mkdir(parents=True, exist_ok=True)
    real_python = _resolve_writer_real_python()
    guard_token = guard_token or secrets.token_hex(16)
    guard_script = bin_dir / "task_writer_python_guard.py"
    write_text(guard_script, _render_task_writer_python_guard(real_python, guard_token, run_timeout))
    shutil.copyfile(Path(__file__).with_name("resource_runtime.py"), bin_dir / "resource_runtime.py")
    write_text(
        bin_dir / "sitecustomize.py",
        "from resource_runtime import enforce_torch_cuda_fraction\n"
        "enforce_torch_cuda_fraction()\n",
    )
    for name in ("python", "python3", "py"):
        if os.name == "nt":
            write_text(bin_dir / f"{name}.cmd", _render_writer_python_cmd_wrapper(guard_script, real_python))
        else:
            wrapper = bin_dir / name
            write_text(wrapper, _render_writer_python_sh_wrapper(guard_script, real_python))
            wrapper.chmod(0o755)
    shim_python = bin_dir / ("python.cmd" if os.name == "nt" else "python")
    env = {
        "GENG_WRITER_SELFTEST_MODE": "task_writer_full",
        "GENG_TASK_WRITER_MODULE": module,
        "GENG_TASK_WRITER_TASK_ID": task_id,
        "GENG_TASK_WRITER_OUTPUT_SUBDIR": output_subdir,
        "GENG_TASK_WRITER_RUN_LOG": str(run_log),
        "GENG_TASK_WRITER_BROKER_CHANNEL": str(resource_channel_dir),
        "GENG_TASK_WRITER_BROKER_TOKEN": resource_channel_token,
        "GENG_TASK_WRITER_RESOURCE_WAIT_TIMEOUT": str(max(1.0, float(resource_wait_timeout))),
        "GENG_TASK_WRITER_TIMEOUT_STATE": str(timeout_state_path),
        "GENG_TASK_WRITER_ALLOW_FULL": "1" if allow_full else "0",
        "GENG_TASK_CONTRACT_PATH": str(contract_path),
        "GENG_TASK_MEMORY_SNAPSHOT_HASH": memory_snapshot_hash or "unavailable",
        "PYTHON": str(shim_python),
        "GENG_PYTHON": str(shim_python),
    }
    return {
        "bin_dir": bin_dir,
        "env": env,
        "real_python": real_python,
        "shim_python": shim_python,
        "run_log": run_log,
        "guard_token": guard_token,
    }


def _render_task_writer_python_guard(real_python: str, guard_token: str, run_timeout: float) -> str:
    return f'''from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time

from resource_runtime import (
    RESOURCE_LIMIT_RETURN_CODE,
    ResourceUnavailable,
    acquire_resource_lease,
    run_guarded_process,
    subprocess_environment,
    timeout_exclusion,
)

REAL_PYTHON = {real_python!r}
GUARD_TOKEN = {guard_token!r}
RUN_TIMEOUT_S = {float(run_timeout or 0.0)!r}
MODULE = os.environ.get("GENG_TASK_WRITER_MODULE", "")
OUTPUT_SUBDIR = os.environ.get("GENG_TASK_WRITER_OUTPUT_SUBDIR", "")
RUN_LOG = Path(os.environ.get("GENG_TASK_WRITER_RUN_LOG", "task_agent_runs.jsonl"))
BROKER_CHANNEL = Path(os.environ.get("GENG_TASK_WRITER_BROKER_CHANNEL", ".geng_resource_broker"))
BROKER_TOKEN = os.environ.get("GENG_TASK_WRITER_BROKER_TOKEN", "")
RESOURCE_WAIT_TIMEOUT_S = float(os.environ.get("GENG_TASK_WRITER_RESOURCE_WAIT_TIMEOUT", "1800"))
TIMEOUT_STATE = Path(os.environ.get("GENG_TASK_WRITER_TIMEOUT_STATE", ".geng_task_writer_timeout_state.json"))
ALLOW_FULL = os.environ.get("GENG_TASK_WRITER_ALLOW_FULL") == "1"
CONTRACT_PATH = Path(os.environ.get("GENG_TASK_CONTRACT_PATH", "task_contract.json"))
EXPECTED_MEMORY_SNAPSHOT_HASH = os.environ.get("GENG_TASK_MEMORY_SNAPSHOT_HASH", "unavailable")


def _base(value: str) -> str:
    return os.path.basename(str(value).replace("\\\\", "/")).lower()


def _strip_py_launcher_version(args: list[str]) -> list[str]:
    if args and args[0].startswith("-"):
        version = args[0][1:]
        if version and all(ch.isdigit() or ch == "." for ch in version):
            return args[1:]
    return args


def _parse_allowed(args: list[str]) -> tuple[bool, str, str]:
    args = _strip_py_launcher_version(list(args))
    config = ""
    if len(args) == 3 and args[0] == "-m" and args[1] == f"tasks.{{MODULE}}":
        config = args[2]
    elif len(args) == 2 and _base(args[0]) == f"{{MODULE}}.py":
        config = args[1]
    else:
        return False, "", "only the assigned task module may be executed"
    config_base = _base(config)
    if config_base == "config_smoke.json":
        return True, "smoke", ""
    if config_base == "config.json" and ALLOW_FULL:
        return True, "full", ""
    if config_base == "config.json":
        return False, "", "full config is disabled because --run-repro was not requested"
    return False, "", "only config_smoke.json or config.json are allowed"


def _artifact_snapshot() -> dict:
    root = Path("outputs") / OUTPUT_SUBDIR
    result = {{"output_subdir": OUTPUT_SUBDIR, "csv_files": [], "png_files": [], "summary_json_files": []}}
    if not root.exists():
        return result
    result["csv_files"] = sorted(str(path.as_posix()) for path in root.glob("*.csv"))
    result["png_files"] = sorted(str(path.as_posix()) for path in root.glob("*.png"))
    result["summary_json_files"] = sorted(str(path.as_posix()) for path in root.glob("summary*.json"))
    return result


def _append_run_log(record: dict) -> None:
    RUN_LOG.parent.mkdir(parents=True, exist_ok=True)
    with RUN_LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\\n")


def _load_contract() -> tuple[dict, str]:
    try:
        value = json.loads(CONTRACT_PATH.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        return {{}}, f"cannot read task contract: {{type(exc).__name__}}: {{exc}}"
    if not isinstance(value, dict):
        return {{}}, "task contract must be a JSON object"
    if value.get("schema_version") != "1.0":
        return value, "task contract schema_version must be 1.0"
    if value.get("task_id") != os.environ.get("GENG_TASK_WRITER_TASK_ID", value.get("task_id")):
        return value, "task contract task_id does not match assigned task"
    for field in ("task_id", "experiment_id", "memory_snapshot_hash"):
        if not isinstance(value.get(field), str) or not value[field].strip():
            return value, f"task contract requires non-empty string {{field}}"
    if value.get("memory_snapshot_hash") != EXPECTED_MEMORY_SNAPSHOT_HASH:
        return value, "task contract memory_snapshot_hash does not match this workflow"
    if value.get("reproducibility_mode") not in {{"native_full", "scaled_full", "proxy_only", "environment_blocked", "upstream_patch_required"}}:
        return value, "task contract reproducibility_mode is invalid"
    for field in ("algorithm_steps", "acceptance_criteria"):
        items = value.get(field)
        if not isinstance(items, list) or not items or not all(isinstance(item, str) and item.strip() for item in items):
            return value, f"task contract requires non-empty string list {{field}}"
    outputs = value.get("outputs")
    if not isinstance(outputs, list) or not outputs:
        return value, "task contract requires non-empty outputs"
    for item in outputs:
        if not isinstance(item, dict) or not isinstance(item.get("path_pattern"), str) or not item["path_pattern"].strip():
            return value, "task contract output requires path_pattern"
        if item.get("kind") not in {{"csv", "png", "json", "text", "other"}} or not isinstance(item.get("required"), bool):
            return value, "task contract output kind/required is invalid"
    backend = value.get("backend")
    if not isinstance(backend, dict) or backend.get("requested") not in {{"auto", "cpu", "gpu"}} or not isinstance(backend.get("allow_cpu_fallback"), bool):
        return value, "task contract backend is invalid"
    resources = value.get("resources")
    if not isinstance(resources, dict):
        return value, "task contract resources are required"
    if resources.get("execution_class") not in {{"cpu_light", "cpu_heavy", "gpu", "unknown"}}:
        return value, "task contract resources.execution_class is invalid"
    if isinstance(resources.get("cpu_cores"), bool) or not isinstance(resources.get("cpu_cores"), int) or resources["cpu_cores"] < 1:
        return value, "task contract resources.cpu_cores must be a positive integer"
    for field in ("ram_gb", "vram_gb"):
        item = resources.get(field)
        if isinstance(item, bool) or not isinstance(item, (int, float)) or item < 0 or (field == "ram_gb" and item <= 0):
            return value, f"task contract resources.{{field}} is invalid"
    if isinstance(resources.get("gpu_count"), bool) or not isinstance(resources.get("gpu_count"), int) or resources["gpu_count"] < 0:
        return value, "task contract resources.gpu_count must be a non-negative integer"
    if resources.get("confidence") not in {{"low", "medium", "high"}}:
        return value, "task contract resources.confidence is invalid"
    if (backend.get("requested") == "gpu" or resources.get("execution_class") == "gpu") and resources.get("gpu_count", 0) < 1:
        return value, "GPU task contract must request at least one GPU"
    if isinstance(value.get("seed"), bool) or not isinstance(value.get("seed"), int):
        return value, "task contract seed must be an integer"
    return value, ""


def _contract_hash(value: dict) -> str:
    import hashlib
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def main() -> int:
    args = sys.argv[1:]
    allowed, profile, reason = _parse_allowed(args)
    if not allowed:
        print(
            "geng-agent task-writer python guard: " + reason + ". "
            f"Allowed commands: python -m tasks.{{MODULE}} config_smoke.json and "
            f"python -m tasks.{{MODULE}} config.json.",
            file=sys.stderr,
        )
        return 97
    contract, contract_error = _load_contract()
    if profile == "full" and contract_error:
        print("geng-agent task-writer python guard: " + contract_error, file=sys.stderr)
        _append_run_log({{
            "guard": "geng_task_writer_python_guard_v1",
            "guard_token": GUARD_TOKEN,
            "task_module": MODULE,
            "output_subdir": OUTPUT_SUBDIR,
            "command": [REAL_PYTHON, *args],
            "profile": profile,
            "config": args[-1] if args else "",
            "returncode": 98,
            "timed_out": False,
            "duration_s": 0.0,
            "contract_error": contract_error,
            "artifacts": _artifact_snapshot(),
        }})
        return 98
    lease = None
    allocation = {{"execution_class": "smoke", "cpu_cores": 2, "ram_gb": 1.5, "gpu_count": 0, "vram_gb": 0.0, "gpu_indices": []}}
    resource_wait_s = 0.0
    if profile == "full":
        try:
            with timeout_exclusion(TIMEOUT_STATE, "resource_wait"):
                lease, allocation, resource_wait_s = acquire_resource_lease(
                    channel_dir=BROKER_CHANNEL,
                    channel_token=BROKER_TOKEN,
                    contract=contract,
                    task_id=os.environ.get("GENG_TASK_WRITER_TASK_ID", MODULE),
                    wait_timeout_s=RESOURCE_WAIT_TIMEOUT_S,
                )
        except ResourceUnavailable as exc:
            print("geng-agent task-writer resource scheduler: " + str(exc), file=sys.stderr)
            _append_run_log({{
                "guard": "geng_task_writer_python_guard_v2",
                "guard_token": GUARD_TOKEN,
                "task_module": MODULE,
                "output_subdir": OUTPUT_SUBDIR,
                "command": [REAL_PYTHON, *args],
                "profile": profile,
                "config": args[-1] if args else "",
                "returncode": 99,
                "timed_out": False,
                "duration_s": 0.0,
                "resource_error": str(exc),
                "artifacts": _artifact_snapshot(),
            }})
            return 99
    started = time.time()
    returncode = 1
    timed_out = False
    resource_violation = None
    peak_resources = {{}}
    enforcement = {{}}
    try:
        timeout = RUN_TIMEOUT_S if RUN_TIMEOUT_S > 0 else None
        if profile == "full":
            with timeout_exclusion(TIMEOUT_STATE, "full_run"):
                completed = run_guarded_process(
                    command=[REAL_PYTHON, *args],
                    timeout=timeout,
                    env=subprocess_environment(allocation, real_python=REAL_PYTHON),
                    allocation=allocation,
                )
        else:
            completed = run_guarded_process(
                command=[REAL_PYTHON, *args],
                timeout=timeout,
                env=subprocess_environment(allocation, real_python=REAL_PYTHON),
                allocation=allocation,
            )
        returncode = int(completed["returncode"])
        timed_out = bool(completed.get("timed_out"))
        resource_violation = completed.get("resource_violation")
        peak_resources = completed.get("peak_resources") or {{}}
        enforcement = completed.get("enforcement") or {{}}
        if resource_violation:
            print("geng-agent task-writer resource guard: " + str(resource_violation), file=sys.stderr)
    except ResourceUnavailable as exc:
        resource_violation = str(exc)
        returncode = RESOURCE_LIMIT_RETURN_CODE
    finally:
        duration = time.time() - started
        record = {{
            "guard": "geng_task_writer_python_guard_v2",
            "guard_token": GUARD_TOKEN,
            "task_module": MODULE,
            "output_subdir": OUTPUT_SUBDIR,
            "command": [REAL_PYTHON, *args],
            "profile": profile,
            "config": args[-1] if args else "",
            "returncode": returncode,
            "timed_out": timed_out,
            "duration_s": round(duration, 3),
            "resource_wait_s": round(resource_wait_s, 3),
            "resource_allocation": allocation,
            "resource_violation": resource_violation,
            "peak_resources": peak_resources,
            "resource_enforcement": enforcement,
            "artifacts": _artifact_snapshot(),
            "contract_path": str(CONTRACT_PATH),
            "contract_hash": _contract_hash(contract),
        }}
        _append_run_log(record)
        if lease is not None:
            lease.release()
    return returncode


if __name__ == "__main__":
    raise SystemExit(main())
'''


def _task_result_file_path(sandbox: Path, output_subdir: str, filename: str) -> tuple[Path, bool]:
    root_path = sandbox / filename
    if root_path.exists():
        return root_path, False
    output_path = sandbox / "outputs" / output_subdir / filename
    if output_path.exists():
        return output_path, True
    return root_path, False


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            records.append(parsed)
    return records


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
    contracts_dir = repro_project_dir / "contracts"
    contracts_dir.mkdir(parents=True, exist_ok=True)
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
            shutil.copy2(script_source, script_target)
        lib_source = sandbox / "tasks" / f"{module}_lib.py"
        if lib_source.exists():
            lib_target = repro_project_dir / "tasks" / f"{module}_lib.py"
            shutil.copy2(lib_source, lib_target)
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
        contract_source = sandbox / "task_contract.json"
        if contract_source.exists():
            contract_target = contracts_dir / f"{module}.json"
            shutil.copy2(contract_source, contract_target)
            expected_paths.add(f"contracts/{module}.json")
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
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        names.append(line)
    return names


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
    all_checks_passed = total > 0 and passed == total
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
                "full_run": record.get("full_run"),
                "task_contract_path": record.get("task_contract_path"),
                "task_contract_hash": record.get("task_contract_hash"),
                "reproducibility_mode": record.get("reproducibility_mode"),
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
        "matched",
        "explained_gap",
    }


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
    if statuses == {"matched"}:
        return {
            "overall_alignment": "match",
            "overall_result_credibility": "medium",
            "overall_summary": "所有自治 writer 均报告为 matched。",
        }
    if statuses <= {"matched", "explained_gap"} and "explained_gap" in statuses:
        return {
            "overall_alignment": "partial_match",
            "overall_result_credibility": "medium",
            "overall_summary": "所有自治 writer 均完成，但至少一个任务只解释了剩余差异。",
        }
    return {
        "overall_alignment": "inconclusive",
        "overall_result_credibility": "low",
        "overall_summary": "至少一个任务报告 failed，当前结果只能作为失败或待诊断证据。",
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
    del run_repro
    plan = build_resource_plan(task_count=task_count, requested_writer_concurrency=requested)
    return int(plan["writer"]["initial_concurrency"])


def _task_writer_stop_class(task_records: list[dict[str, Any]]) -> str:
    if not task_records:
        return "no_tasks"
    if any(_task_writer_blocked_by_codex(record) for record in task_records):
        return "blocked_by_codex"
    if any(not record.get("writer_completed") for record in task_records):
        return "writer_failures"
    if any(record.get("task_writer_status") == "failed" for record in task_records):
        return "task_failures_reported"
    if any(record.get("task_writer_status") == "explained_gap" for record in task_records):
        return "explained_gaps"
    return "matched"


def _task_writer_stopped_reason(task_records: list[dict[str, Any]]) -> str:
    stop_class = _task_writer_stop_class(task_records)
    return {
        "no_tasks": "no reproduction tasks were available",
        "blocked_by_codex": "one or more Codex task writers were blocked by usage limits or rate limits",
        "writer_failures": "one or more autonomous task writers did not complete",
        "task_failures_reported": "one or more task writers reported failed",
        "explained_gaps": "all completed task writers either matched or explained remaining gaps",
        "matched": "all task writers reported matched",
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
        "run_records": [],
        "local_images": [],
    }


def _task_writer_blocked_by_codex(record: dict[str, Any]) -> bool:
    kind = str(record.get("writer_error_kind") or "")
    return kind in {"codex_usage_limit", "codex_rate_limit"}


def _task_writer_capacity_blocked(record: dict[str, Any]) -> bool:
    return str(record.get("writer_error_kind") or "") == "codex_rate_limit"
