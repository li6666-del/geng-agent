from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
import hashlib
import json
import os
import re
import secrets
import shutil
import time
from pathlib import Path
from typing import Any

from .task_writer_support import (
    CODEX_PROJECT_BACKEND,
    _clear_result_review_outputs,
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
from .outputs import inspect_output_artifacts, validate_repro_project, write_json, write_text
from .paper_evidence import facts_for_task, paper_context_for_task, safe_label, thesis_ordering_anchor_for_task
from .schemas import validate_stage
from .security import (
    dependency_policy_prompt_text,
    reconcile_whitelisted_requirements,
    redact_text,
    split_requirement_issues,
    static_scan_repro_project,
    validate_requirements,
)
from .stage_cleanup import _clear_stage_outputs
from .task_scripts import build_tasks_manifest, write_task_scaffolding
from .task_contract import build_task_contract_draft, contract_hash
from .failure_memory import append_failure, load_failures, query_failures
from .revision_router import classify_revision_error, parse_revision_request, validate_revision_request
from .resource_runtime import ResourceBroker
from .resource_scheduler import WriterConcurrencyController, build_resource_plan, detect_hardware


TASK_WRITER_STATUSES = {"matched", "explained_gap", "failed"}
MAX_TASK_WRITER_SELF_ITERATIONS = 5
MAX_TASK_WRITER_HOST_REPAIR_ATTEMPTS = 2


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


def _collect_revision_requests(task_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    requests: list[dict[str, Any]] = []
    for record in task_records:
        request = record.get("revision_request")
        if not isinstance(request, dict):
            continue
        item = dict(request)
        category = str(item.get("category") or "code_or_runtime")
        item["eligible_for_analysis_reentry"] = category in {"analysis_scope", "contract_error"}
        requests.append(item)
    return requests


def _update_failure_memory(path: Path, task_records: list[dict[str, Any]]) -> None:
    for record in task_records:
        status = str(record.get("task_writer_status") or "failed")
        errors = [str(item) for item in record.get("errors", []) if str(item)]
        if status == "matched" and not errors:
            continue
        result = record.get("result_json") if isinstance(record.get("result_json"), dict) else {}
        request = record.get("revision_request") if isinstance(record.get("revision_request"), dict) else {}
        category = str(request.get("category") or "")
        if not category:
            category = classify_revision_error("; ".join(errors) or str(result.get("summary") or status)).value
        append_failure(
            path,
            {
                "task_id": str(record.get("task_id") or "unknown"),
                "scenario": str(record.get("reproducibility_mode") or "unknown"),
                "category": category,
                "status": status,
                "message": str(result.get("summary") or "; ".join(errors) or status),
                "differences": result.get("differences") if isinstance(result.get("differences"), list) else [],
                "repair_hypotheses": result.get("possible_causes") if isinstance(result.get("possible_causes"), list) else [],
                "contract_hash": record.get("task_contract_hash"),
                "evidence_files": result.get("evidence_files") if isinstance(result.get("evidence_files"), list) else [],
            },
        )


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
    result_review: bool,
    rounds: int = MAX_TASK_WRITER_SELF_ITERATIONS,
    timeout: float = 1800.0,
    run_timeout: float = 120.0,
    resume: bool = True,
    agent_concurrency: int | None = None,
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
    rounds = max(1, min(MAX_TASK_WRITER_SELF_ITERATIONS, int(rounds or MAX_TASK_WRITER_SELF_ITERATIONS)))
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
        result_review=result_review,
        memory_snapshot_hash=memory_snapshot_hash,
    )
    cached_runtime_passed = bool((cached or {}).get("runtime_result", {}).get("passed"))
    if resume and cached is not None and (not run_repro or cached_runtime_passed):
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
    _clear_result_review_outputs(output_dir)

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
        requested_writer_concurrency=agent_concurrency,
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
        "rounds_requested": rounds,
        "run_repro": bool(run_repro),
        "result_review": bool(result_review),
        "task_count": len(task_pairs),
        "resource_plan": str(resource_plan_path),
        "rounds": [],
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
            rounds=rounds,
            timeout=timeout,
            run_timeout=run_timeout,
            run_repro=run_repro,
            shared_failure_memory_path=output_dir / "failure_memory.jsonl",
            resource_plan=resource_plan,
            resource_plan_path=resource_plan_path,
            resource_broker=resource_broker,
            initial_records_by_index=resume_records,
        )
    finally:
        resource_broker.stop()
    write_json(audit_dir / "writer_dispatch.json", dispatch_audit)

    revision_requests = _collect_revision_requests(task_records)
    write_json(output_dir / "revision_requests.json", {"requests": revision_requests})
    _update_failure_memory(output_dir / "failure_memory.jsonl", task_records)

    _prepare_project_workspace(repro_project_dir, task_manifest)
    expected_paths = _merge_task_writer_deliveries(
        repro_project_dir=repro_project_dir,
        task_manifest=task_manifest,
        expected_paths=set(expected_paths),
        task_records=task_records,
    )
    _restore_trusted_files(repro_project_dir, task_manifest)
    _normalize_project_text_bom(repro_project_dir)
    final_task_manifest = _task_manifest_with_configs(task_manifest)
    write_json(repro_project_dir / "tasks_manifest.json", final_task_manifest)
    reconcile_whitelisted_requirements(repro_project_dir)
    validation = validate_repro_project(repro_project_dir)
    requirement_issues = validate_requirements(repro_project_dir)
    blocking_requirement_issues, requirement_warnings = split_requirement_issues(requirement_issues)
    security_issues = static_scan_repro_project(repro_project_dir)
    manifest = _manifest_from_project(
        repro_project_dir=repro_project_dir,
        expected_paths=expected_paths,
        task_manifest=final_task_manifest,
        round_no=1,
    )
    manifest["_meta"]["mode"] = "task_writers"
    manifest["_meta"]["memory_snapshot_hash"] = memory_snapshot_hash
    manifest_issues = validate_stage("repro_project_manifest", manifest, required_files=expected_paths)
    write_json(
        audit_dir / "03c_task_writers_manifest_validation.json",
        {"ok": not manifest_issues, "errors": [issue.as_dict() for issue in manifest_issues]},
    )
    write_json(output_dir / "repro_project_manifest.json", manifest)

    runtime_result = _task_writer_runtime_result(
        task_records=task_records,
        validation=validation,
        manifest_issues=[issue.as_dict() for issue in manifest_issues],
        requirement_issues=blocking_requirement_issues,
        requirement_warnings=requirement_warnings,
        security_issues=security_issues,
    )
    write_json(output_dir / "runtime_result.json", runtime_result)

    review_doc: dict[str, Any] | None = None
    if result_review:
        markdown = _render_task_writer_result_review(task_records)
        alignment_summary = _task_writer_alignment_summary(task_records)
        write_text(output_dir / "result_review.md", markdown)
        review_doc = {
            "_meta": {"markdown_review": True, "mode": "task_writer_self_review"},
            "markdown": markdown,
            **alignment_summary,
            "task_writer_reviews": [_compact_task_writer_review(record) for record in task_records],
        }
        result_review_result = {
            "enabled": True,
            "passed": True,
            "mode": "codex_task_writer_self_review",
            "result_review_markdown_path": str(output_dir / "result_review.md"),
            **alignment_summary,
            "task_count": len(task_records),
            "task_statuses": [
                {
                    "task_id": record.get("task_id"),
                    "status": record.get("task_writer_status"),
                    "structural_ok": record.get("structural_ok"),
                    "writer_error_kind": record.get("writer_error_kind"),
                    "blocked_reason": record.get("blocked_reason"),
                    "warnings": record.get("warnings", []),
                }
                for record in task_records
            ],
            "note": "No independent reviewer was launched; task writers performed their own comparisons.",
        }
    else:
        result_review_result = {
            "enabled": False,
            "passed": None,
            "reason": "result review disabled by --no-result-review",
            "mode": "codex_task_writer_self_review",
        }

    write_json(
        audit_dir / "03c_task_writers_records.json",
        {"dispatch_policy": dispatch_audit, "tasks": task_records},
    )
    status.update(
        {
            "rounds_run": 1,
            "best_round": 1,
            "stop_class": _task_writer_stop_class(task_records),
            "stopped_reason": _task_writer_stopped_reason(task_records),
            "validation": validation,
            "manifest_errors": [issue.as_dict() for issue in manifest_issues],
            "runtime": {
                "passed": runtime_result.get("passed"),
                "coverage": runtime_result.get("coverage"),
            },
            "result_review": {
                "enabled": result_review_result.get("enabled"),
                "passed": result_review_result.get("passed"),
                "mode": result_review_result.get("mode"),
            },
            "revision_requests": revision_requests,
            "tasks": [
                {
                    "task_id": record.get("task_id"),
                    "status": record.get("task_writer_status"),
                    "structural_ok": record.get("structural_ok"),
                    "writer_error_kind": record.get("writer_error_kind"),
                    "blocked_reason": record.get("blocked_reason"),
                    "errors": record.get("errors", []),
                    "warnings": record.get("warnings", []),
                }
                for record in task_records
            ],
        }
    )
    write_json(audit_dir / "03c_task_writers_status.json", status)
    return {
        "manifest": manifest,
        "runtime_result": runtime_result,
        "result_review_result": result_review_result,
        "result_review_doc": review_doc,
        "written_files": [str(path) for path in _manifest_disk_paths(manifest, repro_project_dir)],
        "status": status,
        "revision_requests": revision_requests,
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
        contract_path = sandbox / "task_contract.json"
        try:
            parsed_contract = json.loads(contract_path.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
        contract = parsed_contract if isinstance(parsed_contract, dict) else {}
        if str(contract.get("task_id") or "") != task_id:
            continue
        if str(contract.get("memory_snapshot_hash") or "") != (expected_memory_snapshot_hash or "unavailable"):
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
    rounds: int,
    timeout: float,
    run_timeout: float,
    run_repro: bool,
    shared_failure_memory_path: Path,
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
                    rounds=rounds,
                    timeout=timeout,
                    run_timeout=run_timeout,
                    run_repro=run_repro,
                    shared_failure_memory_path=shared_failure_memory_path,
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
                        "structural_ok": bool(record.get("structural_ok")),
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
                    writer_ok = bool((record.get("writer_status") or {}).get("ok")) and bool(
                        record.get("structural_ok")
                    )
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
    rounds: int,
    timeout: float,
    run_timeout: float,
    run_repro: bool,
    shared_failure_memory_path: Path,
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
        shared_failure_memory_path=shared_failure_memory_path,
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
    repair_history = list((resume_record or {}).get("host_repair_history") or [])
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
            rounds=rounds,
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
    record, deterministic_actions = _host_repair_and_validate_task_writer(
        index=index,
        task=task,
        manifest_entry=manifest_entry,
        sandbox=sandbox,
        writer_status=writer_status,
        run_repro=run_repro,
        run_log=run_log,
        guard_token=guard_token,
        expected_memory_snapshot_hash=memory_snapshot_hash,
    )
    if deterministic_actions:
        repair_history.append(
            {
                "repair_kind": "deterministic",
                "attempt": 0,
                "actions": deterministic_actions,
                "remaining_errors": record.get("errors", []),
            }
        )

    for repair_no in range(1, MAX_TASK_WRITER_HOST_REPAIR_ATTEMPTS + 1):
        if _task_writer_runtime_task_passed(record) or _task_writer_blocked_by_codex(record):
            break
        repair_kind = _classify_task_writer_repair(record)
        repair_label = f"{label}_host_repair_{repair_no:02d}_{repair_kind}"
        repair_prompt = _build_task_writer_host_repair_brief(
            task_id=task_id,
            module=module,
            output_subdir=output_subdir,
            repair_kind=repair_kind,
            errors=record.get("errors", []),
            warnings=record.get("warnings", []),
            rounds=rounds,
            full_enabled=run_repro,
        )
        metadata_snapshot = (
            _snapshot_metadata_repair_state(sandbox=sandbox, output_subdir=output_subdir)
            if repair_kind == "metadata"
            else None
        )
        scope_actions: list[dict[str, Any]] = []
        try:
            writer_status = _run_task_writer_codex_session(
                label=repair_label,
                prompt=repair_prompt,
                sandbox=sandbox,
                audit_dir=audit_dir,
                task_id=task_id,
                module=module,
                output_subdir=output_subdir,
                run_log=run_log,
                allow_full=bool(run_repro and repair_kind == "execution"),
                run_timeout=run_timeout,
                memory_snapshot_hash=memory_snapshot_hash,
                broker_channel=broker_channel,
                resource_broker=resource_broker,
                timeout_state_path=timeout_state_path,
                guard_token=guard_token,
                timeout=timeout,
            )
        finally:
            if metadata_snapshot is not None:
                scope_actions = _restore_metadata_repair_state(
                    sandbox=sandbox,
                    output_subdir=output_subdir,
                    snapshot=metadata_snapshot,
                )
        writer_status_history.append(
            {"label": repair_label, "repair_kind": repair_kind, "status": writer_status}
        )
        _restore_trusted_files(sandbox, {"version": 1, "tasks": [manifest_entry]})
        record, deterministic_actions = _host_repair_and_validate_task_writer(
            index=index,
            task=task,
            manifest_entry=manifest_entry,
            sandbox=sandbox,
            writer_status=writer_status,
            run_repro=run_repro,
            run_log=run_log,
            guard_token=guard_token,
            expected_memory_snapshot_hash=memory_snapshot_hash,
        )
        repair_history.append(
            {
                "repair_kind": repair_kind,
                "attempt": repair_no,
                "full_allowed": bool(run_repro and repair_kind == "execution"),
                "actions": [*scope_actions, *deterministic_actions],
                "remaining_errors": record.get("errors", []),
            }
        )
        if not writer_status.get("ok"):
            break

    record["writer_status_history"] = writer_status_history
    record["host_repair_history"] = repair_history
    record["attempt"] = attempt
    write_json(
        audit_dir / f"{label}_host_repairs.json",
        {
            "task_id": task_id,
            "structural_ok": record.get("structural_ok"),
            "history": repair_history,
        },
    )
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


def _host_repair_and_validate_task_writer(
    *,
    index: int,
    task: dict[str, Any],
    manifest_entry: dict[str, Any],
    sandbox: Path,
    writer_status: dict[str, Any],
    run_repro: bool,
    run_log: Path,
    guard_token: str,
    expected_memory_snapshot_hash: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Apply host-owned normalizations, then perform task-local structural gates."""
    output_subdir = str(manifest_entry.get("output_subdir") or task.get("task_id") or "task")
    actions = _apply_deterministic_task_writer_repairs(sandbox=sandbox, output_subdir=output_subdir)

    requirements_path = sandbox / "requirements.txt"
    requirements_before = requirements_path.read_bytes() if requirements_path.is_file() else None
    added_requirements = reconcile_whitelisted_requirements(sandbox)
    requirements_after = requirements_path.read_bytes() if requirements_path.is_file() else None
    if requirements_after != requirements_before:
        actions.append(
            {
                "kind": "requirements_reconciled",
                "file": "requirements.txt",
                "packages_added": added_requirements,
            }
        )

    record = _validate_task_writer_delivery(
        index=index,
        task=task,
        manifest_entry=manifest_entry,
        sandbox=sandbox,
        writer_status=writer_status,
        run_repro=run_repro,
        trusted_run_log_path=run_log,
        guard_token=guard_token,
        expected_memory_snapshot_hash=expected_memory_snapshot_hash,
    )
    validation = validate_repro_project(sandbox)
    requirement_issues = validate_requirements(sandbox)
    blocking_requirements, requirement_warnings = split_requirement_issues(requirement_issues)
    security_issues = static_scan_repro_project(sandbox)

    errors = list(record.get("errors") or [])
    warnings = list(record.get("warnings") or [])
    errors.extend(f"missing required project file: {path}" for path in validation.get("missing_files", []))
    errors.extend(
        f"python compile error in {item.get('file')}: {item.get('error')}"
        for item in validation.get("compile_errors", [])
        if isinstance(item, dict)
    )
    errors.extend(_format_host_issue("blocking requirement issue", item) for item in blocking_requirements)
    errors.extend(_format_host_issue("security issue", item) for item in security_issues)
    warnings.extend(_format_host_issue("requirement warning", item) for item in requirement_warnings)

    record["errors"] = _dedupe_text(errors)
    record["warnings"] = _dedupe_text(warnings)
    record["structural_ok"] = not record["errors"]
    record["sandbox_validation"] = {
        "project": validation,
        "blocking_requirement_issues": blocking_requirements,
        "requirement_warnings": requirement_warnings,
        "security_issues": security_issues,
    }
    return record, actions


def _apply_deterministic_task_writer_repairs(*, sandbox: Path, output_subdir: str) -> list[dict[str, Any]]:
    """Repair serialization-only defects without invoking Codex or running Python."""
    actions: list[dict[str, Any]] = []
    normalized_project_files = _normalize_project_text_bom(sandbox)
    if normalized_project_files:
        actions.append({"kind": "utf8_bom_removed", "files": normalized_project_files})

    metadata_names = (
        "task_agent_result.json",
        "task_agent_result.md",
        "paper_target_figure.json",
        "task_revision_request.json",
    )
    metadata_paths: list[Path] = []
    for name in metadata_names:
        metadata_paths.extend((sandbox / name, sandbox / "outputs" / output_subdir / name))

    output_bom_files: list[str] = []
    for path in dict.fromkeys(metadata_paths):
        if not path.is_file():
            continue
        raw = path.read_bytes()
        if b"\xef\xbb\xbf" not in raw:
            continue
        path.write_bytes(raw.replace(b"\xef\xbb\xbf", b""))
        output_bom_files.append(_relative_display_path(path, sandbox))
    if output_bom_files:
        actions.append({"kind": "utf8_bom_removed", "files": output_bom_files})

    for filename in ("task_agent_result.json", "paper_target_figure.json", "task_revision_request.json"):
        for path in (sandbox / filename, sandbox / "outputs" / output_subdir / filename):
            changes = _normalize_task_writer_json_file(
                path=path,
                sandbox=sandbox,
                output_subdir=output_subdir,
                filename=filename,
            )
            if changes:
                actions.append(
                    {
                        "kind": "json_metadata_normalized",
                        "file": _relative_display_path(path, sandbox),
                        "changes": changes,
                    }
                )
    return actions


def _normalize_task_writer_json_file(
    *,
    path: Path,
    sandbox: Path,
    output_subdir: str,
    filename: str,
) -> list[str]:
    if not path.is_file():
        return []
    try:
        document = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return []
    if not isinstance(document, dict):
        return []

    changes: list[str] = []
    list_path_keys = {"evidence_files", "local_image_paths", "paper_image_paths"}
    scalar_path_keys = {"crop_path", "locator_path", "image_path"}
    for key in list_path_keys:
        value = document.get(key)
        if not isinstance(value, list):
            continue
        normalized = [
            _normalize_writer_declared_path_text(
                sandbox=sandbox,
                output_subdir=output_subdir,
                raw_path=item,
            )
            if isinstance(item, str)
            else item
            for item in value
        ]
        if normalized != value:
            document[key] = normalized
            changes.append(f"normalized {key} paths")
    for key in scalar_path_keys:
        value = document.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        normalized = _normalize_writer_declared_path_text(
            sandbox=sandbox,
            output_subdir=output_subdir,
            raw_path=value,
        )
        if normalized != value:
            document[key] = normalized
            changes.append(f"normalized {key} path")

    if filename == "paper_target_figure.json":
        confidence = document.get("confidence")
        score = _confidence_score(confidence)
        if score is not None:
            label = "low" if score < 0.5 else "medium" if score < 0.8 else "high"
            if confidence != label:
                document["confidence"] = label
                document.setdefault("confidence_score", score)
                changes.append("normalized confidence to low/medium/high")
        for key in ("source_page", "source_pages"):
            value = document.get(key)
            if isinstance(value, float) and value.is_integer():
                document[key] = int(value)
                changes.append(f"normalized {key} integer")
            elif isinstance(value, list):
                normalized_pages = [int(item) if isinstance(item, float) and item.is_integer() else item for item in value]
                if normalized_pages != value:
                    document[key] = normalized_pages
                    changes.append(f"normalized {key} integers")

    if changes:
        write_json(path, document)
    return changes


def _normalize_writer_declared_path_text(*, sandbox: Path, output_subdir: str, raw_path: str) -> str:
    raw = str(raw_path).strip().strip('"')
    if not raw:
        return raw
    resolved = _resolve_writer_declared_path(sandbox=sandbox, output_subdir=output_subdir, raw_path=raw)
    candidate = resolved or Path(raw)
    if candidate.is_absolute() and _path_is_inside(candidate, sandbox):
        return candidate.resolve().relative_to(sandbox.resolve()).as_posix()
    return raw.replace("\\", "/")


def _confidence_score(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        score = float(value)
    elif isinstance(value, str):
        try:
            score = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    return score if 0 <= score <= 1 else None


def _relative_display_path(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path)


def _snapshot_metadata_repair_state(*, sandbox: Path, output_subdir: str) -> dict[str, bytes]:
    snapshot: dict[str, bytes] = {}
    for path in sandbox.rglob("*"):
        if (
            not path.is_file()
            or not _metadata_repair_path_is_protected(path, sandbox, output_subdir)
            or _metadata_repair_path_is_mutable(path, sandbox, output_subdir)
        ):
            continue
        snapshot[path.relative_to(sandbox).as_posix()] = path.read_bytes()
    return snapshot


def _restore_metadata_repair_state(
    *,
    sandbox: Path,
    output_subdir: str,
    snapshot: dict[str, bytes],
) -> list[dict[str, Any]]:
    restored: list[str] = []
    removed: list[str] = []
    for path in list(sandbox.rglob("*")):
        if (
            not path.is_file()
            or not _metadata_repair_path_is_protected(path, sandbox, output_subdir)
            or _metadata_repair_path_is_mutable(path, sandbox, output_subdir)
        ):
            continue
        relative = path.relative_to(sandbox).as_posix()
        if relative not in snapshot:
            path.unlink()
            removed.append(relative)
    for relative, content in snapshot.items():
        path = sandbox / Path(relative)
        if not path.is_file() or path.read_bytes() != content:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            restored.append(relative)
    if not restored and not removed:
        return []
    return [
        {
            "kind": "metadata_scope_enforced",
            "restored_files": restored,
            "removed_unapproved_files": removed,
        }
    ]


def _metadata_repair_path_is_mutable(path: Path, sandbox: Path, output_subdir: str) -> bool:
    relative = path.relative_to(sandbox)
    if ".geng_resource_broker" in relative.parts or "__pycache__" in relative.parts:
        return True
    if path.name in {
        "task_agent_result.json",
        "task_agent_result.md",
        "paper_target_figure.json",
        "task_revision_request.json",
        "task_agent_runs.jsonl",
        ".geng_task_writer_timeout_state.json",
    }:
        return True
    output_root = sandbox / "outputs" / output_subdir
    if _path_is_inside(path, output_root) and path.suffix.lower() == ".png":
        name = path.stem.lower()
        return any(token in name for token in ("paper", "crop", "locator"))
    return False


def _metadata_repair_path_is_protected(path: Path, sandbox: Path, output_subdir: str) -> bool:
    relative = path.relative_to(sandbox)
    if relative.parts and relative.parts[0] in {"tasks", "src", "configs"}:
        return path.suffix.lower() in {".py", ".json"}
    if len(relative.parts) == 1:
        return (
            path.name in {"requirements.txt", "task_contract.json", "tasks_manifest.json", "run_experiment.py"}
            or path.name.startswith("config")
        )
    output_root = sandbox / "outputs" / output_subdir
    if not _path_is_inside(path, output_root):
        return False
    if path.suffix.lower() in {".csv", ".png"}:
        return True
    return path.suffix.lower() == ".json" and path.name.startswith("summary")


def _format_host_issue(prefix: str, issue: Any) -> str:
    if not isinstance(issue, dict):
        return f"{prefix}: {issue}"
    location = str(issue.get("file") or "")
    line = str(issue.get("line") or "")
    if line:
        location = f"{location}:{line}" if location else f"line {line}"
    message = str(issue.get("message") or issue.get("error") or issue)
    return f"{prefix}{f' in {location}' if location else ''}: {message}"


def _dedupe_text(items: list[Any]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        text = str(item).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _classify_task_writer_repair(record: dict[str, Any]) -> str:
    error_text = "\n".join(str(item).lower() for item in record.get("errors", []))
    execution_markers = (
        "missing assigned task script",
        "missing valid local",
        "missing local output image",
        "invalid artifact",
        "task_contract",
        "full run",
        "compile error",
        "syntax error",
        "missing required project file",
        "blocking requirement issue",
        "security issue",
        "dependency",
    )
    if any(marker in error_text for marker in execution_markers):
        return "execution"
    metadata_markers = (
        "task_agent_result",
        "paper_target_figure",
        "paper image path",
        "paper target image",
        "confidence",
        "source_page",
        "bbox_norm",
        "task_revision_request",
    )
    if any(marker in error_text for marker in metadata_markers):
        return "metadata"
    return "execution" if str(record.get("task_writer_status") or "") == "failed" else "metadata"


def _build_task_writer_host_repair_brief(
    *,
    task_id: str,
    module: str,
    output_subdir: str,
    repair_kind: str,
    errors: list[Any],
    warnings: list[Any],
    rounds: int,
    full_enabled: bool,
) -> str:
    error_lines = "\n".join(f"- {item}" for item in errors) or "- none"
    warning_lines = "\n".join(f"- {item}" for item in warnings) or "- none"
    if repair_kind == "metadata":
        repair_instructions = f"""This is a metadata-only repair. Do not run full and do not run `python -m tasks.{module} config.json`.
Do not change scientific code, generated data, or valid images. Correct only missing or malformed result/report/locator fields and in-sandbox path references. The guard has full disabled for this session."""
    elif full_enabled:
        repair_instructions = f"""This is a task-local execution repair. Inspect the existing code, data, contract, run log, and artifacts; preserve everything already valid.
Fix only this assigned task. Run `python -m tasks.{module} config_smoke.json` as a quick gate, then you must rerun `python -m tasks.{module} config.json` after correcting code, data, artifacts, or full evidence. Never run the dispatcher or another task. Iterate up to {rounds} cycles, stopping as soon as this task has valid evidence and a defensible final status."""
    else:
        repair_instructions = f"""This is a task-local code repair while full execution is disabled for the workflow. Fix only `tasks.{module}` and its private configuration, but do not run full. Use smoke only for syntax and shape checks, and report the final status as failed/skipped when full evidence cannot be produced."""
    return f"""# Host-directed repair for one task

Continue in the existing sandbox. This is not a fresh task and other tasks must not be touched or rerun.

- Assigned task_id: `{task_id}`
- Assigned module: `tasks.{module}`
- Assigned output directory: `outputs/{output_subdir}/`
- Repair class: `{repair_kind}`

{repair_instructions}

## Host validation errors to clear
{error_lines}

## Host warnings to preserve or clean up when relevant
{warning_lines}

## Required final delivery
- Keep `task_agent_result.json` and `task_agent_result.md` complete and readable.
- A `matched` result needs non-empty `evidence_files`.
- An `explained_gap` result needs non-empty `differences`, `possible_causes`, `remaining_uncertainties`, and `evidence_files`.
- Keep `paper_target_figure.json` and its writer-created crop/locator image valid.
- Use relative POSIX-style paths inside the sandbox.
- Finish only this repair; do not recreate the project or touch another task.
"""


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
    shared_failure_memory_path: Path,
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
    prior_failures = query_failures(load_failures(shared_failure_memory_path, strict=False), task_id=str(task.get("task_id") or ""))
    write_text(
        sandbox / "failure_memory.jsonl",
        "".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in prior_failures),
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
    rounds: int,
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

You own exactly one reproduction task. There is no separate reviewer. You must write the code, run your assigned task, compare the output to the paper evidence, revise if needed, then leave a final human-readable comparison for the host.

{retry_instruction}

## Hard boundaries
- Assigned task_id: `{task_id}`
- Assigned module: `tasks.{module}`
- Output directory: `outputs/{output_subdir}/`
- You may edit only: `README.md`, `requirements.txt`, `config.json`, `config_smoke.json`, `task_contract.json`, `task_revision_request.json`, `tasks/{module}.py`, optional `tasks/{module}_lib.py`, your result files, and files under `outputs/{output_subdir}/`. Treat `failure_memory.jsonl` as read-only prior experience.
- Do not edit `src/_io.py`, `src/_backend.py`, `run_experiment.py`, `tasks_manifest.json`, `tasks/__init__.py`, or any other task module.
- Do not run `python run_experiment.py config.json`; the Python guard rejects dispatcher full runs and other task modules.
- {full_instruction}
- You may run smoke with `python -m tasks.{module} config_smoke.json`.
- Full runs use the sandbox-local Python guard; do not bypass it.
- Required internal science iterations: keep iterating until `matched`, up to {rounds} cycles maximum.

## Mandatory self-iteration protocol
You are not a one-shot report writer. You are the coder, runner, and reviewer for this task. Work in repeated cycles until the result is either matched, explained by a defensible gap, or genuinely failed.

For each cycle:
1. Before writing code, inspect and finalize `task_contract.json`. It is the authoritative mapping from paper facts to inputs, equations, outputs, invariants, backend, resource request, seed, and acceptance criteria. Keep `task_id`, `experiment_id`, and `memory_snapshot_hash` intact. Set `resources.execution_class`, CPU cores, RAM, GPU count, and VRAM conservatively; use `cpu_light` only for genuinely small analytic/plotting jobs, `cpu_heavy` for large CPU simulations, `gpu` for CUDA full runs, and `unknown` when evidence is insufficient.
2. Implement or revise the task code/config against that contract.
3. Run smoke only as a quick sanity check.
4. Run full with `python -m tasks.{module} config.json` when `--run-repro` is enabled. The guard rejects full until the contract validates.
5. Inspect the local CSV/summary/PNG and compare them with the contract and paper evidence images/text.
6. If the result does not match the paper claim, first assume your implementation, configuration, proxy model, axis scaling, normalization, baseline, seed, or plotting could be wrong. Form a concrete repair hypothesis, modify code or config, and run full again.
7. Create or refresh the paper-side comparison image for this task:
   - Prefer a tight crop of the exact target figure/subfigure from the rendered `paper_page_*.png` evidence.
   - If the exact crop is uncertain, create a small locator image that shows the relevant page/region with a visible red rectangle around the believed target.
   - Do not use an unannotated full `paper_page_*.png` as the final paper comparison image.
8. Record each cycle in `task_agent_result.md`: contract change, command, return code, changed files, observed mismatch, repair hypothesis, target-paper-figure crop/locator status, and next decision.

Do not stop after the first imperfect output. A first mismatch should normally trigger at least one code/config repair and rerun. Report `explained_gap` only after you have tried plausible implementation/config/model fixes and can name the remaining gap with evidence. Report `failed` only for a real blocker such as runtime failure, missing essential paper information, timeout, dependency failure, or no valid artifacts. Report `matched` only when local artifacts support the paper trend, scale, ordering, and baseline comparison for this task.

Reproducibility-mode rule from the host contract:
- `native_full` and `scaled_full` may end as `matched` when all acceptance criteria are met (for scaled runs, state the preserved scale assumptions).
- `proxy_only` must not claim complete reproduction; use `explained_gap` and identify what the proxy does and does not establish.
- `environment_blocked` and `upstream_patch_required` normally end as `failed` unless the blocker is actually resolved and the contract is updated with evidence.

Stopping rule:
- If a cycle reaches `matched`, stop immediately and write the final files.
- If a cycle is not `matched`, continue to the next repair/rerun cycle until cycle {rounds}.
- Do not choose `explained_gap` before the final allowed cycle unless an essential paper detail is provably unavailable and no code/config change could test it.
- At cycle {rounds}, if the result is still not a complete match, write the strongest honest conclusion: `explained_gap` when artifacts exist and the remaining difference is evidenced; `failed` when the task lacks usable artifacts or is blocked.

## Required final files
- `task_contract.json`: validated experiment contract used by every full run.
- `task_agent_result.md`: Chinese, human-readable comparison report.
- `paper_target_figure.json`: JSON object describing how you located the paper-side figure or claim evidence, with fields such as `target_figure`, `source_page` or `source_pages`, `bbox_norm`, `confidence`, `contains_only_target`, `fallback_used`, `reason`, and `paper_image_paths`. Use a single `source_page` plus one four-number `bbox_norm` for one-page figure evidence. For a claim/formula spread across multiple pages, use `source_pages` (or a `source_page` list) and either omit `bbox_norm` or provide a page-keyed object whose values are four-number boxes.
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
  "local_image_paths": [],
  "paper_image_paths": []
}}
```
- `paper_image_paths` must list your task-specific paper comparison image(s), preferably under `outputs/{output_subdir}/` and relative to the sandbox root. Use the tight crop when confident; otherwise use the red-box locator image. Do not list raw `paper_page_1.png` / `paper_page_2.png` style full-page files, and do not simply rename a full paper page as a crop.
- If `status == "explained_gap"`, `differences`, `possible_causes`, `remaining_uncertainties`, and `evidence_files` must all be non-empty.
- If `status == "matched"`, cite the local CSV/PNG/summary and paper evidence that support the match.
- If `status == "failed"`, explain whether the blocker is runtime, missing paper details, timeout, dependency, or modeling uncertainty.
- Create `task_revision_request.json` only when the task or contract is impossible because the upstream analysis scope is wrong or the experiment contract is invalid. It must contain `task_id`, `scenario`, `error`, non-empty `requested_changes`, `reentry_count`, and optional category `analysis_scope|contract_error|code_or_runtime|environment`. Do not request upstream revision for an ordinary code bug that you can fix inside your five cycles.

## Paper target figure image guidance
- You may use Python with Pillow/PyMuPDF/OpenCV-like array logic if available, but keep dependencies within the allowed whitelist.
- Recommended output names: `outputs/{output_subdir}/paper_target_crop.png` for a confident crop, or `outputs/{output_subdir}/paper_target_locator.png` for a red-box fallback.
- The final report is assembled from `outputs/{output_subdir}/`; put the paper-side PNG there so it remains self-contained after host aggregation.
- The paper-side image is for human comparison in the final Word report. Favor readability over showing an entire page.
- Keep original rendered paper pages untouched under `paper_evidence/`; write your derived crop/locator under `outputs/{output_subdir}/`.

## Trusted runtime APIs
{IO_RUNTIME_API_DOC}

{BACKEND_RUNTIME_API_DOC}

## Dependency policy
{dependency_policy_prompt_text()}

## Task evidence files
- `paper_evidence/index.json`
- `paper_evidence/01_{safe_label(task_id)}/evidence.json`
- `paper_evidence/01_{safe_label(task_id)}/context.md`
- `failure_memory.jsonl` (prior failures for this task; read before choosing a repair hypothesis)

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


def _validate_task_writer_delivery(
    *,
    index: int,
    task: dict[str, Any],
    manifest_entry: dict[str, Any],
    sandbox: Path,
    writer_status: dict[str, Any],
    run_repro: bool,
    trusted_run_log_path: Path,
    guard_token: str,
    expected_memory_snapshot_hash: str,
) -> dict[str, Any]:
    task_id = str(task.get("task_id") or manifest_entry.get("task_id") or f"task_{index}")
    module = str(manifest_entry.get("module") or "")
    output_subdir = str(manifest_entry.get("output_subdir") or task_id)
    errors: list[str] = []
    result_path, result_path_fallback = _task_result_file_path(sandbox, output_subdir, "task_agent_result.json")
    md_path, md_path_fallback = _task_result_file_path(sandbox, output_subdir, "task_agent_result.md")
    result_doc: dict[str, Any] = {}
    if result_path.exists():
        try:
            parsed = json.loads(result_path.read_text(encoding="utf-8-sig"))
            if isinstance(parsed, dict):
                result_doc = parsed
            else:
                errors.append("task_agent_result.json must contain an object")
        except Exception as exc:
            errors.append(f"task_agent_result.json is invalid JSON: {type(exc).__name__}: {exc}")
    else:
        errors.append("missing task_agent_result.json")
    if not md_path.exists() or len(md_path.read_text(encoding="utf-8", errors="replace").strip()) < 40:
        errors.append("missing or too-short task_agent_result.md")

    warnings: list[str] = []
    if result_path_fallback:
        warnings.append("task_agent_result.json found under outputs/<task>; accepted as fallback")
    if md_path_fallback:
        warnings.append("task_agent_result.md found under outputs/<task>; accepted as fallback")

    status = str(result_doc.get("status") or "failed")
    if status not in TASK_WRITER_STATUSES:
        errors.append(f"invalid task writer status: {status}")
        status = "failed"
    if str(result_doc.get("task_id") or task_id) != task_id:
        errors.append("task_agent_result.json task_id does not match assigned task")

    if status == "explained_gap":
        for key in ("differences", "possible_causes", "remaining_uncertainties", "evidence_files"):
            if not _non_empty_list(result_doc.get(key)):
                errors.append(f"explained_gap requires non-empty {key}")
    if status == "matched" and not _non_empty_list(result_doc.get("evidence_files")):
        errors.append("matched requires non-empty evidence_files")

    script_path = sandbox / "tasks" / f"{module}.py"
    if not script_path.exists() or not script_path.is_file():
        errors.append(f"missing assigned task script: tasks/{module}.py")

    contract_path = sandbox / "task_contract.json"
    contract_doc: dict[str, Any] = {}
    if contract_path.exists():
        try:
            parsed_contract = json.loads(contract_path.read_text(encoding="utf-8-sig"))
            if isinstance(parsed_contract, dict):
                contract_doc = parsed_contract
                errors.extend(
                    f"task_contract.json {issue.path}: {issue.message}"
                    for issue in validate_stage("task_contract", contract_doc)
                )
                if str(contract_doc.get("task_id") or "") != task_id:
                    errors.append("task_contract.json task_id does not match assigned task")
                if str(contract_doc.get("memory_snapshot_hash") or "") != (expected_memory_snapshot_hash or "unavailable"):
                    errors.append("task_contract.json memory_snapshot_hash does not match workflow snapshot")
            else:
                errors.append("task_contract.json must contain an object")
        except Exception as exc:
            errors.append(f"task_contract.json is invalid JSON: {type(exc).__name__}: {exc}")
    else:
        errors.append("missing task_contract.json")

    mode = str(contract_doc.get("reproducibility_mode") or "native_full")
    if status == "matched" and mode == "proxy_only":
        status = "explained_gap"
        result_doc["status"] = status
        result_doc.setdefault("differences", []).append("The execution contract classifies this task as proxy_only, so it cannot establish full paper reproduction.")
        result_doc.setdefault("possible_causes", []).append("Required fidelity, facts, or environment evidence is incomplete in the experiment contract.")
        result_doc.setdefault("remaining_uncertainties", []).append("Whether a native full implementation would preserve the reported scale and ordering.")
        result_doc.setdefault("evidence_files", []).append("task_contract.json")
        warnings.append("matched was downgraded to explained_gap because task_contract is proxy_only")
    elif status == "matched" and mode in {"environment_blocked", "upstream_patch_required"}:
        status = "failed"
        result_doc["status"] = status
        errors.append(f"matched is invalid while task contract mode is {mode}")

    revision_path, revision_fallback = _task_result_file_path(sandbox, output_subdir, "task_revision_request.json")
    revision_request: dict[str, Any] | None = None
    if revision_path.exists():
        try:
            raw_revision = json.loads(revision_path.read_text(encoding="utf-8"))
            revision_issues = validate_revision_request(raw_revision)
            if revision_issues:
                errors.extend(f"task_revision_request.json {item['path']}: {item['message']}" for item in revision_issues)
            else:
                parsed_revision = parse_revision_request(raw_revision)
                revision_request = parsed_revision.model_dump(mode="json")
                revision_request["category"] = classify_revision_error(parsed_revision).value
                if revision_request["task_id"] != task_id:
                    errors.append("task_revision_request.json task_id does not match assigned task")
        except Exception as exc:
            errors.append(f"task_revision_request.json is invalid: {type(exc).__name__}: {exc}")
    if revision_fallback:
        warnings.append("task_revision_request.json found under outputs/<task>; accepted as fallback")

    run_records = _read_jsonl(trusted_run_log_path)
    trusted_records = [item for item in run_records if _is_trusted_guard_record(item, guard_token, module, output_subdir)]
    full_runs = [item for item in trusted_records if item.get("profile") == "full"]
    successful_full_runs = [item for item in full_runs if item.get("returncode") == 0]
    final_contract_hash = contract_hash(contract_doc) if contract_doc else None
    if successful_full_runs and final_contract_hash and successful_full_runs[-1].get("contract_hash") != final_contract_hash:
        errors.append("final task_contract.json differs from the contract used by the last successful full run")
    if run_repro and successful_full_runs and not _run_record_has_required_artifacts(successful_full_runs[-1]):
        warnings.append("trusted full run did not record required CSV/PNG/summary artifacts")
    if run_records and not trusted_records:
        warnings.append("task_agent_runs.jsonl contains no trusted guard records; ignoring it for host validation")

    artifacts = inspect_output_artifacts(sandbox, subdir=output_subdir)
    if run_repro and status in {"matched", "explained_gap"}:
        if not artifacts.get("has_csv"):
            errors.append("missing valid local CSV artifact")
        if not artifacts.get("has_png"):
            errors.append("missing valid local PNG artifact")
        if not artifacts.get("has_summary_json"):
            errors.append("missing valid local summary.json artifact")
        for invalid in artifacts.get("invalid_files", []):
            errors.append(f"invalid artifact: {invalid}")

    paper_locator_path, paper_locator_fallback = _task_result_file_path(sandbox, output_subdir, "paper_target_figure.json")
    paper_locator_doc: dict[str, Any] = {}
    if paper_locator_path.exists():
        try:
            parsed_locator = json.loads(paper_locator_path.read_text(encoding="utf-8-sig"))
            if isinstance(parsed_locator, dict):
                paper_locator_doc = parsed_locator
            else:
                errors.append("paper_target_figure.json must contain an object")
        except Exception as exc:
            errors.append(f"paper_target_figure.json is invalid JSON: {type(exc).__name__}: {exc}")
    elif status in {"matched", "explained_gap"}:
        errors.append("missing paper_target_figure.json")
    if paper_locator_fallback:
        warnings.append("paper_target_figure.json found under outputs/<task>; accepted as fallback")
    if status in {"matched", "explained_gap"} and paper_locator_doc:
        errors.extend(_validate_paper_locator_doc(paper_locator_doc))

    paper_images, paper_image_warnings, paper_image_errors = _task_paper_image_paths(
        sandbox=sandbox,
        output_subdir=output_subdir,
        result_doc=result_doc,
        locator_doc=paper_locator_doc,
    )
    warnings.extend(paper_image_warnings)
    if status in {"matched", "explained_gap"}:
        errors.extend(paper_image_errors)
    local_images = _task_local_image_paths(
        sandbox,
        output_subdir,
        result_doc=result_doc,
        paper_images=paper_images,
    )
    if not paper_images:
        if status in {"matched", "explained_gap"}:
            errors.append("missing writer-provided paper target image")
    if run_repro and not local_images:
        errors.append("missing local output image")

    if result_doc and result_path.exists():
        write_json(result_path, result_doc)

    structural_ok = not errors
    return {
        "index": index,
        "task_id": task_id,
        "module": module,
        "output_subdir": output_subdir,
        "sandbox": str(sandbox),
        "writer_status": writer_status,
        "task_writer_status": status,
        "result_json": result_doc,
        "result_json_path": str(result_path) if result_path.exists() else None,
        "result_markdown_path": str(md_path) if md_path.exists() else None,
        "paper_locator_path": str(paper_locator_path) if paper_locator_path.exists() else None,
        "paper_locator": paper_locator_doc,
        "task_contract_path": str(contract_path) if contract_path.exists() else None,
        "task_contract": contract_doc,
        "task_contract_hash": final_contract_hash,
        "reproducibility_mode": mode,
        "revision_request_path": str(revision_path) if revision_path.exists() else None,
        "revision_request": revision_request,
        "run_log_path": str(trusted_run_log_path) if trusted_run_log_path.exists() else None,
        "run_records": trusted_records,
        "full_run": full_runs[-1] if full_runs else None,
        "artifacts": artifacts,
        "local_images": local_images,
        "paper_images": paper_images,
        "structural_ok": structural_ok,
        "errors": errors,
        "warnings": warnings,
        "writer_error_kind": writer_status.get("error_kind") if isinstance(writer_status, dict) else None,
        "blocked_reason": writer_status.get("blocked_reason") if isinstance(writer_status, dict) else None,
    }


def _task_result_file_path(sandbox: Path, output_subdir: str, filename: str) -> tuple[Path, bool]:
    root_path = sandbox / filename
    if root_path.exists():
        return root_path, False
    output_path = sandbox / "outputs" / output_subdir / filename
    if output_path.exists():
        return output_path, True
    return root_path, False


def _non_empty_list(value: Any) -> bool:
    return isinstance(value, list) and any(str(item).strip() for item in value)


def _validate_paper_locator_doc(locator_doc: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for key in ("target_figure", "reason"):
        if not isinstance(locator_doc.get(key), str) or not str(locator_doc.get(key)).strip():
            errors.append(f"paper_target_figure.json requires non-empty {key}")
    confidence = locator_doc.get("confidence")
    confidence_is_text = isinstance(confidence, str) and bool(confidence.strip())
    confidence_is_score = (
        not isinstance(confidence, bool)
        and isinstance(confidence, (int, float))
        and 0 <= float(confidence) <= 1
    )
    if not confidence_is_text and not confidence_is_score:
        errors.append("paper_target_figure.json requires non-empty confidence or a score in [0, 1]")
    source_page = locator_doc.get("source_pages", locator_doc.get("source_page"))
    if not _valid_source_page_spec(source_page):
        errors.append("paper_target_figure.json requires source_page")
    if not isinstance(locator_doc.get("fallback_used"), bool):
        errors.append("paper_target_figure.json requires boolean fallback_used")
    if not isinstance(locator_doc.get("contains_only_target"), bool):
        errors.append("paper_target_figure.json requires boolean contains_only_target")
    if not _non_empty_list(locator_doc.get("paper_image_paths")) and not any(
        isinstance(locator_doc.get(key), str) and str(locator_doc.get(key)).strip()
        for key in ("crop_path", "locator_path", "image_path")
    ):
        errors.append("paper_target_figure.json requires paper_image_paths or a crop/locator/image path")
    bbox = locator_doc.get("bbox_norm")
    errors.extend(_validate_bbox_norm(bbox))
    return errors


def _valid_source_page_spec(value: Any) -> bool:
    if isinstance(value, list):
        return bool(value) and all(_valid_single_source_page(item) for item in value)
    return _valid_single_source_page(value)


def _valid_single_source_page(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, str)) and bool(str(value).strip())


def _validate_bbox_norm(bbox: Any) -> list[str]:
    if bbox is None:
        return []
    if _is_bbox_box(bbox):
        return []
    if isinstance(bbox, list) and bbox and all(_is_bbox_box(item) for item in bbox):
        return []
    if isinstance(bbox, dict) and bbox and all(
        isinstance(key, str) and key.strip() and _is_bbox_box(value)
        for key, value in bbox.items()
    ):
        return []
    return [
        "paper_target_figure.json bbox_norm must be a list of four numbers, "
        "a list of boxes, or a page-keyed object of boxes"
    ]


def _is_bbox_box(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 4
        and all(not isinstance(item, bool) and isinstance(item, (int, float)) and 0 <= item <= 1 for item in value)
    )


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


def _is_trusted_guard_record(record: dict[str, Any], guard_token: str, module: str, output_subdir: str) -> bool:
    return (
        record.get("guard") in {"geng_task_writer_python_guard_v1", "geng_task_writer_python_guard_v2"}
        and record.get("guard_token") == guard_token
        and record.get("task_module") == module
        and record.get("output_subdir") == output_subdir
    )


def _run_record_has_required_artifacts(record: dict[str, Any]) -> bool:
    artifacts = record.get("artifacts") if isinstance(record.get("artifacts"), dict) else {}
    csv_files = artifacts.get("csv_files") if isinstance(artifacts.get("csv_files"), list) else []
    png_files = artifacts.get("png_files") if isinstance(artifacts.get("png_files"), list) else []
    summary_files = artifacts.get("summary_json_files") if isinstance(artifacts.get("summary_json_files"), list) else []
    return bool(csv_files and png_files and summary_files)


def _task_paper_image_paths(
    *,
    sandbox: Path,
    output_subdir: str,
    result_doc: dict[str, Any],
    locator_doc: dict[str, Any],
) -> tuple[list[str], list[str], list[str]]:
    warnings: list[str] = []
    errors: list[str] = []
    raw_paths: list[str] = []

    raw_paths.extend(_string_list(result_doc.get("paper_image_paths")))
    if not raw_paths:
        raw_paths.extend(_string_list(locator_doc.get("paper_image_paths")))
    for key in ("crop_path", "locator_path", "image_path"):
        value = locator_doc.get(key)
        if isinstance(value, str) and value.strip():
            raw_paths.append(value)

    if not raw_paths:
        return [], warnings, ["task_agent_result.json paper_image_paths is empty"]

    resolved_paths: list[str] = []
    seen: set[str] = set()
    raw_page_hashes = _raw_rendered_paper_page_hashes(sandbox)
    for raw in raw_paths:
        path = _resolve_writer_declared_path(sandbox=sandbox, output_subdir=output_subdir, raw_path=raw)
        if path is None:
            errors.append(f"paper image path does not exist: {raw}")
            continue
        if not _path_is_inside(path, sandbox):
            errors.append(f"paper image path must stay inside task sandbox: {raw}")
            continue
        if _is_raw_rendered_paper_page(path):
            errors.append(f"paper image path must be a writer-created crop or locator, not raw page: {raw}")
            continue
        if not _looks_like_png(path):
            errors.append(f"paper image path is not a valid PNG: {raw}")
            continue
        digest = _file_sha256(path)
        if digest and digest in raw_page_hashes:
            errors.append(f"paper image path appears to be an unmodified rendered paper page: {raw}")
            continue
        normalized = _copy_paper_image_to_output_dir(sandbox=sandbox, output_subdir=output_subdir, path=path)
        if normalized != path:
            warnings.append(f"paper image copied into outputs/{output_subdir}: {path.name}")
            path = normalized
        key = str(path.resolve()).lower()
        if key in seen:
            continue
        seen.add(key)
        resolved_paths.append(str(path.resolve()))

    if not resolved_paths and raw_paths:
        warnings.append("writer declared paper_image_paths but none were usable")
    return resolved_paths, warnings, errors


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _resolve_writer_declared_path(*, sandbox: Path, output_subdir: str, raw_path: str) -> Path | None:
    raw = str(raw_path).strip().strip('"')
    if not raw:
        return None
    candidate = Path(raw)
    candidates: list[Path] = []
    if candidate.is_absolute():
        candidates.append(candidate)
    else:
        candidates.extend([sandbox / candidate, sandbox / "outputs" / output_subdir / candidate])
        if candidate.parent == Path("."):
            candidates.append(sandbox / "outputs" / output_subdir / candidate.name)
    for path in candidates:
        if path.exists() and path.is_file():
            return path
    return None


def _path_is_inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _is_raw_rendered_paper_page(path: Path) -> bool:
    return bool(re.fullmatch(r"paper_page_\d+\.png", path.name))


def _raw_rendered_paper_page_hashes(sandbox: Path) -> set[str]:
    hashes: set[str] = set()
    evidence_root = sandbox / "paper_evidence"
    if not evidence_root.exists():
        return hashes
    for path in evidence_root.rglob("paper_page_*.png"):
        if path.is_file() and _is_raw_rendered_paper_page(path):
            digest = _file_sha256(path)
            if digest:
                hashes.add(digest)
    return hashes


def _file_sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _looks_like_png(path: Path) -> bool:
    try:
        return path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    except OSError:
        return False


def _copy_paper_image_to_output_dir(*, sandbox: Path, output_subdir: str, path: Path) -> Path:
    output_dir = sandbox / "outputs" / output_subdir
    try:
        path.resolve().relative_to(output_dir.resolve())
        return path
    except ValueError:
        pass
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = safe_label(path.stem) or "paper_target_image"
    target = output_dir / f"{stem}{path.suffix or '.png'}"
    if target.exists() and target.resolve() != path.resolve():
        for index in range(2, 1000):
            candidate = output_dir / f"{stem}_{index}{path.suffix or '.png'}"
            if not candidate.exists():
                target = candidate
                break
    if target.resolve() != path.resolve():
        shutil.copy2(path, target)
    return target


def _task_local_image_paths(
    sandbox: Path,
    output_subdir: str,
    *,
    result_doc: dict[str, Any] | None = None,
    paper_images: list[str] | None = None,
) -> list[str]:
    excluded = _resolved_path_keys(paper_images or [])
    declared_paths = _string_list((result_doc or {}).get("local_image_paths"))
    declared_images: list[str] = []
    seen: set[str] = set()
    for raw in declared_paths:
        path = _resolve_writer_declared_path(sandbox=sandbox, output_subdir=output_subdir, raw_path=raw)
        if path is None:
            continue
        if not _path_is_inside(path, sandbox) or not _looks_like_png(path):
            continue
        if _is_paper_evidence_output_image(path, sandbox=sandbox, excluded=excluded):
            continue
        key = str(path.resolve()).lower()
        if key in seen:
            continue
        seen.add(key)
        declared_images.append(str(path.resolve()))
    if declared_images:
        return declared_images

    output_dir = sandbox / "outputs" / output_subdir
    if not output_dir.exists():
        return []
    local_images: list[str] = []
    for path in sorted(output_dir.glob("*.png")):
        if not path.is_file():
            continue
        if _is_paper_evidence_output_image(path, sandbox=sandbox, excluded=excluded):
            continue
        local_images.append(str(path.resolve()))
    return local_images


def _resolved_path_keys(paths: list[str]) -> set[str]:
    keys: set[str] = set()
    for raw in paths:
        try:
            keys.add(str(Path(raw).resolve()).lower())
        except OSError:
            continue
    return keys


def _is_paper_evidence_output_image(path: Path, *, sandbox: Path, excluded: set[str]) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return False
    if str(resolved).lower() in excluded:
        return True
    try:
        resolved.relative_to((sandbox / "paper_evidence").resolve())
        return True
    except ValueError:
        pass
    name = path.name.lower()
    if _is_raw_rendered_paper_page(path):
        return True
    return bool(re.search(r"(^|[_-])paper([_-]|$)|(^|[_-])locator([_-]|$)", name))


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
            shutil.copytree(source_output, target_output)
        result_dir = repro_project_dir / "outputs" / output_subdir
        result_dir.mkdir(parents=True, exist_ok=True)
        for name in ("task_agent_result.json", "task_agent_result.md", "paper_target_figure.json", "task_revision_request.json"):
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


def _normalize_project_text_bom(project_dir: Path) -> list[str]:
    """Remove UTF-8 BOMs introduced by Windows shell writers before host scans."""
    normalized: list[str] = []
    text_suffixes = {".py", ".json", ".md", ".txt", ".csv", ".toml", ".yaml", ".yml"}
    for path in project_dir.rglob("*"):
        relative = path.relative_to(project_dir)
        if relative.parts and relative.parts[0] in {"outputs", "paper_evidence"}:
            continue
        if not path.is_file() or (path.suffix.lower() not in text_suffixes and path.name != "requirements.txt"):
            continue
        raw = path.read_bytes()
        if b"\xef\xbb\xbf" not in raw:
            continue
        path.write_bytes(raw.replace(b"\xef\xbb\xbf", b""))
        normalized.append(relative.as_posix())
    return normalized


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
    manifest_issues: list[dict[str, Any]],
    requirement_issues: list[dict[str, Any]],
    requirement_warnings: list[dict[str, Any]],
    security_issues: list[dict[str, Any]],
) -> dict[str, Any]:
    passed = sum(1 for record in task_records if _task_writer_runtime_task_passed(record))
    delivered = sum(1 for record in task_records if record.get("structural_ok"))
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
    all_checks_passed = (
        total > 0
        and passed == total
        and validation.get("required_files_present")
        and validation.get("python_compiles")
        and not manifest_issues
        and not requirement_issues
        and not security_issues
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
                "delivery_ok": bool(record.get("structural_ok")),
                "task_writer_status": record.get("task_writer_status"),
                "writer_error_kind": record.get("writer_error_kind"),
                "blocked_reason": record.get("blocked_reason"),
                "full_run": record.get("full_run"),
                "task_contract_path": record.get("task_contract_path"),
                "task_contract_hash": record.get("task_contract_hash"),
                "reproducibility_mode": record.get("reproducibility_mode"),
                "revision_request": record.get("revision_request"),
                "artifacts": record.get("artifacts"),
                "errors": record.get("errors", []),
                "warnings": record.get("warnings", []),
            }
            for record in task_records
        ],
        "validation": validation,
        "manifest_issues": manifest_issues,
        "requirements_issues": requirement_issues,
        "requirements_warnings": requirement_warnings,
        "security_issues": security_issues,
    }


def _task_writer_runtime_task_passed(record: dict[str, Any]) -> bool:
    return bool(record.get("structural_ok")) and str(record.get("task_writer_status") or "") in {
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
    if any(not record.get("structural_ok") for record in task_records):
        return {
            "overall_alignment": "inconclusive",
            "overall_result_credibility": "low",
            "overall_summary": "部分任务未通过主持人结构验收，不能给出强复现结论。",
        }
    statuses = {str(record.get("task_writer_status") or "failed") for record in task_records}
    if statuses == {"matched"}:
        return {
            "overall_alignment": "match",
            "overall_result_credibility": "medium",
            "overall_summary": "所有任务均由自治 writer 报告为 matched，并通过主持人结构验收。",
        }
    if statuses <= {"matched", "explained_gap"} and "explained_gap" in statuses:
        return {
            "overall_alignment": "partial_match",
            "overall_result_credibility": "medium",
            "overall_summary": "任务均完成结构验收，但至少一个任务只解释了剩余差异。",
        }
    return {
        "overall_alignment": "inconclusive",
        "overall_result_credibility": "low",
        "overall_summary": "至少一个任务报告 failed，当前结果只能作为失败或待诊断证据。",
    }


def _render_task_writer_result_review(task_records: list[dict[str, Any]]) -> str:
    sections: list[str] = []
    appendix_sections: list[str] = []
    for index, record in enumerate(task_records, start=1):
        task_id = str(record.get("task_id") or f"task_{index}")
        result = record.get("result_json") if isinstance(record.get("result_json"), dict) else {}
        lines = [f"## {index}. {task_id}", ""]
        lines.extend(
            [
                f"**Writer 结论：** `{record.get('task_writer_status', 'failed')}`",
                f"**主持人结构验收：** {'通过' if record.get('structural_ok') else '失败'}",
                f"**可复现模式：** `{record.get('reproducibility_mode', 'unknown')}`",
            ]
        )
        if record.get("errors"):
            lines.append("**结构问题：** " + "；".join(str(item) for item in record.get("errors", [])))
        if record.get("blocked_reason"):
            lines.append(f"**执行阻塞：** {record.get('blocked_reason')}")
        lines.append("")

        lines.extend(_image_comparison_markdown(record=record, result=result))
        lines.extend(["### 简短审查结论", ""])
        lines.append(str(result.get("summary") or "Writer 未提供简短结论。"))
        lines.append("")
        lines.extend(_result_list_section("关键差异", result.get("differences"), default="未报告明显差异。"))
        lines.extend(_result_list_section("可能原因", result.get("possible_causes"), default="未报告可能原因。"))
        lines.extend(_result_list_section("剩余不确定性", result.get("remaining_uncertainties"), default="未报告剩余不确定性。"))
        lines.extend(_result_list_section("证据文件", result.get("evidence_files"), default="未列出证据文件。"))
        lines.extend(["", f"完整 writer 自审原文见附录 A{index}。"])

        md = _read_optional_text(record.get("result_markdown_path"))
        appendix_lines = [f"### A{index}. {task_id}", ""]
        if md:
            appendix_lines.append(md.strip())
        else:
            appendix_lines.extend(_fallback_writer_review_lines(result))
        appendix_sections.append("\n".join(appendix_lines).strip() + "\n")
        sections.append("\n".join(lines).strip() + "\n")

    if appendix_sections:
        sections.append("## 附录：Writer 自审原文\n")
        sections.extend(appendix_sections)
    return "\n".join(sections).strip() + "\n"


def _image_comparison_markdown(*, record: dict[str, Any], result: dict[str, Any]) -> list[str]:
    paper_images = [str(path) for path in record.get("paper_images", []) or [] if str(path).strip()]
    paper_image_keys = _resolved_path_keys(paper_images)
    sandbox = Path(str(record.get("sandbox") or "."))
    local_images = [
        str(path)
        for path in record.get("local_images", []) or []
        if str(path).strip()
        and not _is_paper_evidence_output_image(Path(str(path)), sandbox=sandbox, excluded=paper_image_keys)
    ]
    figure_label = _human_figure_label_from_record(record, result)
    paper_caption = "论文原图" if not figure_label else f"论文原图：{figure_label}"
    lines = ["### 图像对比", ""]
    if local_images and paper_images:
        lines.extend(["| 本地复现图 | 论文原图 |", "|---|---|"])
        row_count = max(len(local_images), len(paper_images))
        for row_index in range(row_count):
            local_cell = _markdown_image_cell(
                local_images[row_index] if row_index < len(local_images) else "",
                "本地复现图" if row_count == 1 else f"本地复现图 {row_index + 1}",
            )
            paper_cell = _markdown_image_cell(
                paper_images[row_index] if row_index < len(paper_images) else "",
                paper_caption if row_count == 1 else f"{paper_caption} {row_index + 1}",
            )
            lines.append(f"| {local_cell} | {paper_cell} |")
        lines.append("")
        return lines

    single_images = [("本地复现图", path) for path in local_images] or [(paper_caption, path) for path in paper_images]
    if not single_images:
        lines.extend(["无可用图片。", ""])
        return lines
    for caption, path in single_images:
        lines.append(_markdown_image_cell(path, caption))
        lines.append("")
    return lines


def _markdown_image_cell(path: str, caption: str) -> str:
    if not path:
        return "无可用图片"
    return f"![{caption}]({path})"


def _result_list_section(title: str, values: Any, *, default: str) -> list[str]:
    lines = [f"### {title}", ""]
    if isinstance(values, list):
        items = [str(item) for item in values if str(item).strip()]
    elif values:
        items = [str(values)]
    else:
        items = []
    if not items:
        items = [default]
    lines.extend(f"- {item}" for item in items)
    lines.append("")
    return lines


def _human_figure_label(task_id: str) -> str:
    match = re.search(r"fig(?:ure)?[._\s:-]*([0-9]+)[._\s:-]*\(?([a-z])?\)?\b", task_id, re.I)
    if not match:
        return ""
    number = match.group(1)
    letter = match.group(2)
    return f"Fig. {number}({letter.lower()})" if letter else f"Fig. {number}"


def _human_figure_label_from_record(record: dict[str, Any], result: dict[str, Any]) -> str:
    candidates = [
        str(record.get("task_id") or ""),
        str(result.get("task_id") or ""),
        str(result.get("summary") or ""),
    ]
    for candidate in candidates:
        label = _human_figure_label(candidate)
        if label:
            return label
    return ""


def _fallback_writer_review_lines(result: dict[str, Any]) -> list[str]:
    lines = [str(result.get("summary") or "Writer 未提供可读正文。"), ""]
    for title, key in (
        ("差异", "differences"),
        ("可能原因", "possible_causes"),
        ("仍不确定的信息", "remaining_uncertainties"),
        ("证据文件", "evidence_files"),
    ):
        lines.extend([f"#### {title}", ""])
        values = result.get(key)
        if isinstance(values, list) and values:
            lines.extend(f"- {item}" for item in values)
        else:
            lines.append("- 未列出")
        lines.append("")
    return lines


def _read_optional_text(path_text: Any) -> str:
    if not path_text:
        return ""
    path = Path(str(path_text))
    if not path.exists() or not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def _compact_task_writer_review(record: dict[str, Any]) -> dict[str, Any]:
    result = record.get("result_json") if isinstance(record.get("result_json"), dict) else {}
    return {
        "task_id": record.get("task_id"),
        "task_writer_status": record.get("task_writer_status"),
        "structural_ok": record.get("structural_ok"),
        "summary": result.get("summary"),
        "differences": result.get("differences", []),
        "possible_causes": result.get("possible_causes", []),
        "remaining_uncertainties": result.get("remaining_uncertainties", []),
        "evidence_files": result.get("evidence_files", []),
        "errors": record.get("errors", []),
        "warnings": record.get("warnings", []),
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
    if any(not record.get("structural_ok") for record in task_records):
        return "structural_failures"
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
        "structural_failures": "one or more task writers did not satisfy the delivery contract",
        "task_failures_reported": "one or more task writers reported failed",
        "explained_gaps": "all structurally valid task writers either matched or explained remaining gaps",
        "matched": "all task writers reported matched and passed structural validation",
    }.get(stop_class, stop_class)


def _failed_task_record(*, index: int, task_id: str, module: str, error: str) -> dict[str, Any]:
    return {
        "index": index,
        "task_id": task_id,
        "module": module,
        "task_writer_status": "failed",
        "structural_ok": False,
        "errors": [redact_text(error)[:1000]],
        "writer_status": {"ok": False, "error": redact_text(error)[:1000]},
        "result_json": {"task_id": task_id, "status": "failed", "summary": redact_text(error)[:500]},
        "run_records": [],
        "warnings": [],
        "local_images": [],
        "paper_images": [],
    }


def _task_writer_blocked_by_codex(record: dict[str, Any]) -> bool:
    kind = str(record.get("writer_error_kind") or "")
    return kind in {"codex_usage_limit", "codex_rate_limit"}


def _task_writer_capacity_blocked(record: dict[str, Any]) -> bool:
    return str(record.get("writer_error_kind") or "") == "codex_rate_limit"
