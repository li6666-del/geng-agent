"""Task-writer resume, checkpoint, refresh, and archival state."""

from __future__ import annotations

import hashlib
import ast
import json
import shutil
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Callable

from .artifact_paths import path_is_link
from .outputs import write_json
from .paper_evidence import safe_label
from .scientific_materiality import TERMINAL_SCIENTIFIC_OUTCOMES
from .task_writer_contracts import TASK_WRITER_TERMINAL_STATUS
from .task_writer_files import _read_optional_json_object, _task_owned_files, _task_result_file_path
from .task_writer_support import (
    ANALYSIS_ARTIFACT_DIR,
    PAPER_EVIDENCE_DIR,
    WRITER_ANALYSIS_SCHEMA_VERSION,
    WRITER_HANDOFF_POLICY_VERSION,
    WRITER_OPTIONAL_ANALYSIS_ARTIFACTS,
    WRITER_REQUIRED_ANALYSIS_ARTIFACTS,
    _analysis_snapshot_hash,
)
from .task_writer_units import _execution_unit_sandbox, _execution_unit_work_items
from .verification_result import FINAL_MATCHED_STATUS, WRITER_REVIEW_STATUS, task_verification_issues


def _load_task_writer_resume_records(
    *,
    audit_dir: Path,
    task_pairs: list[tuple[dict[str, Any], dict[str, Any]]],
    expected_analysis_snapshot_hash: str,
    expected_analysis_handoff_hash: str = "",
    execution_plan: dict[str, Any] | None = None,
    expected_snapshot_hashes: dict[str, str] | None = None,
    receipt_validator: Callable[[dict[str, Any], Path], bool] | None = None,
    require_execution_receipts: bool = True,
) -> dict[int, dict[str, Any]]:
    path = audit_dir / "03c_task_writers_records.json"
    layouts = _task_writer_resume_layouts(
        audit_dir=audit_dir,
        task_pairs=task_pairs,
        execution_plan=execution_plan,
    )
    expected_by_id = {
        str(layout["task_id"]): index for index, layout in layouts.items()
    }
    records: dict[int, dict[str, Any]] = {}
    invalid_receipt_indexes: set[int] = set()

    def expected_hash(layout: dict[str, Any]) -> str:
        return (expected_snapshot_hashes or {}).get(str(layout["execution_unit_id"]), expected_analysis_snapshot_hash)

    def receipt_is_current(record: dict[str, Any], sandbox: Path) -> bool:
        if not require_execution_receipts or (expected_snapshot_hashes is None and receipt_validator is None):
            return True
        try:
            if receipt_validator is not None:
                return bool(receipt_validator(record, sandbox))
            from .execution_receipts import find_host_execution
            observed = find_host_execution(sandbox, audit_dir, str(record.get("task_id") or ""))
            record["host_execution"] = observed
            return True  # Reporter receives the observation; receipt quality is not a rerun command.
        except (OSError, ValueError, TypeError) as exc:
            record.setdefault("coordination_observations", []).append(f"Receipt observation unavailable: {exc}")
            return True
    raw_records: list[Any] = []
    if path.exists():
        try:
            document = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception:
            document = {}
        candidate_records = document.get("tasks") if isinstance(document, dict) else None
        if isinstance(candidate_records, list):
            raw_records = candidate_records
    for record in raw_records:
        if not isinstance(record, dict):
            continue
        task_id = str(record.get("task_id") or "")
        index = expected_by_id.get(task_id)
        if index is None:
            continue
        layout = layouts[index]
        expected_sandbox = Path(layout["sandbox"])
        sandbox = Path(str(record.get("sandbox") or ""))
        if (
            not _task_writer_resume_sandbox_is_safe(
                audit_dir=audit_dir,
                sandbox=expected_sandbox,
            )
            or not sandbox.exists()
            or path_is_link(sandbox)
            or sandbox.resolve() != expected_sandbox.resolve()
            or (_task_writer_runtime_refresh_pending(expected_sandbox))
        ):
            continue
        if str(record.get("analysis_snapshot_hash") or "") != expected_hash(layout):
            continue
        # Pass the recorded execution to Reporter; observation quality is not
        # a host instruction to rerun the science.
        if not receipt_is_current(record, expected_sandbox):
            invalid_receipt_indexes.add(index)
            continue
        record.setdefault("index", index)
        record.setdefault("execution_unit_id", str(layout["execution_unit_id"]))
        records[index] = record

    recovered_indexes: list[int] = []
    seen_units: set[str] = set()
    for index in sorted(layouts):
        layout = layouts[index]
        unit_id = str(layout["execution_unit_id"])
        if unit_id in seen_units:
            continue
        seen_units.add(unit_id)
        member_indexes = list(layout["member_indexes"])
        if all(member_index in records for member_index in member_indexes):
            continue
        for member_index in member_indexes:
            records.pop(member_index, None)
        expected_sandbox = Path(layout["sandbox"])
        evidence_index = expected_sandbox / PAPER_EVIDENCE_DIR / "index.json"
        if (
            not _task_writer_resume_sandbox_is_safe(
                audit_dir=audit_dir,
                sandbox=expected_sandbox,
            )
            or not evidence_index.is_file()
            or path_is_link(evidence_index)
        ):
            continue
        try:
            evidence = json.loads(evidence_index.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
        if not isinstance(evidence, dict):
            continue
        evidence_tasks = evidence.get("tasks")
        expected_task_ids = list(layout["task_ids"])
        if (
            not isinstance(evidence_tasks, list)
            or [
                str(item.get("task_id") or "")
                for item in evidence_tasks
                if isinstance(item, dict)
            ]
            != expected_task_ids
        ):
            continue
        stored_snapshot_hash = str(evidence.get("analysis_snapshot_hash") or "")
        expected_unit_hash = expected_hash(layout)
        runtime_refresh_required = (
            stored_snapshot_hash != expected_unit_hash
            or _task_writer_runtime_refresh_pending(expected_sandbox)
            or any(member_index in invalid_receipt_indexes for member_index in member_indexes)
        )
        if stored_snapshot_hash != expected_unit_hash and expected_snapshot_hashes is None:
            preserved_handoff_hash = _sandbox_analysis_handoff_hash(
                sandbox=expected_sandbox,
                evidence=evidence,
                expected_task_ids=expected_task_ids,
            )
            if (
                not expected_analysis_handoff_hash
                or preserved_handoff_hash != expected_analysis_handoff_hash
            ):
                continue
        if expected_snapshot_hashes is not None and not all(
            receipt_is_current({"task_id": str(layouts[member_index]["task_id"])}, expected_sandbox)
            for member_index in member_indexes
        ):
            runtime_refresh_required = True
        for member_index in member_indexes:
            member_layout = layouts[member_index]
            records[member_index] = {
                "index": member_index,
                "task_id": str(member_layout["task_id"]),
                "module": str(member_layout["module"]),
                "output_subdir": str(member_layout["output_subdir"]),
                "sandbox": str(expected_sandbox),
                "execution_unit_id": unit_id,
                "execution_unit_member_count": len(member_indexes),
                "analysis_snapshot_hash": expected_unit_hash,
                "recovered_from_sandbox_snapshot": True,
                "snapshot_compatibility_recovery": runtime_refresh_required,
                "runtime_refresh_required": runtime_refresh_required,
                "environment_refresh_required": runtime_refresh_required,
                "stored_analysis_snapshot_hash": stored_snapshot_hash,
                "scientific_handoff_hash": expected_analysis_handoff_hash or None,
                "resume_requires_current_execution_receipt": bool(expected_snapshot_hashes is not None and require_execution_receipts),
            }
            recovered_indexes.append(member_index)

    if recovered_indexes:
        refresh_indexes = [
            index
            for index in recovered_indexes
            if records[index].get("runtime_refresh_required") is True
        ]
        recovery = {
            "schema_version": 1,
            "source": "preserved_task_writer_sandboxes",
            "analysis_snapshot_hash": expected_analysis_snapshot_hash,
            "analysis_handoff_hash": expected_analysis_handoff_hash or None,
            "recovered_indexes": recovered_indexes,
            "runtime_refresh_indexes": refresh_indexes,
            "environment_refresh_indexes": refresh_indexes,
            "reason": (
                "scientific_handoff_match_combined_snapshot_changed"
                if refresh_indexes
                else "combined_snapshot_match"
            ),
            "recovered_task_ids": [
                str(records[index].get("task_id") or "")
                for index in recovered_indexes
            ],
        }
        write_json(audit_dir / "03c_task_writers_sandbox_recovery.json", recovery)
        write_json(
            path,
            {
                "checkpoint": "sandbox_snapshot_recovery",
                "recovery": recovery,
                "tasks": [records[index] for index in sorted(records)],
            },
        )
    return records

def _task_writer_resume_layouts(
    *,
    audit_dir: Path,
    task_pairs: list[tuple[dict[str, Any], dict[str, Any]]],
    execution_plan: dict[str, Any] | None,
) -> dict[int, dict[str, Any]]:
    task_root = audit_dir / "03c_task_writer_sandboxes"
    layouts: dict[int, dict[str, Any]] = {}
    for unit in _execution_unit_work_items(task_pairs, execution_plan):
        members = list(unit["members"])
        unit_id = str(unit["unit_id"])
        sandbox = (
            task_root
            / f"{members[0][0]:02d}_{safe_label(str(members[0][1].get('task_id') or members[0][2].get('task_id') or 'task'))}"
            if len(members) == 1
            else _execution_unit_sandbox(task_root, unit_id)
        )
        task_ids = [
            str(task.get("task_id") or entry.get("task_id") or f"task_{index}")
            for index, task, entry in members
        ]
        member_indexes = [index for index, _task, _entry in members]
        for (index, _task, entry), task_id in zip(members, task_ids):
            layouts[index] = {
                "task_id": task_id,
                "module": str(entry.get("module") or ""),
                "output_subdir": str(entry.get("output_subdir") or task_id),
                "execution_unit_id": unit_id,
                "member_indexes": member_indexes,
                "task_ids": task_ids,
                "sandbox": sandbox,
            }
    return layouts

def _sandbox_analysis_handoff_hash(
    *,
    sandbox: Path,
    evidence: dict[str, Any],
    expected_task_ids: list[str],
) -> str | None:
    """Recompute the immutable paper/analysis handoff from a preserved sandbox."""

    if (
        evidence.get("policy_version") != WRITER_HANDOFF_POLICY_VERSION
        or evidence.get("analysis_schema_version") != WRITER_ANALYSIS_SCHEMA_VERSION
    ):
        return None
    evidence_tasks = evidence.get("tasks")
    if (
        not isinstance(evidence_tasks, list)
        or [
            str(item.get("task_id") or "")
            for item in evidence_tasks
            if isinstance(item, dict)
        ]
        != expected_task_ids
    ):
        return None
    if path_is_link(sandbox):
        return None

    source = evidence.get("paper_source")
    analysis = evidence.get("analysis_artifacts")
    if (
        not isinstance(source, dict)
        or source.get("copied") is not True
        or not isinstance(analysis, dict)
        or analysis.get("complete") is not True
    ):
        return None
    paper_path = _trusted_preserved_evidence_file(
        sandbox=sandbox,
        relative_path=source.get("relative_path"),
        required_prefix=(PAPER_EVIDENCE_DIR, "source"),
    )
    if paper_path is None:
        return None

    artifacts: dict[str, Path] = {}
    for name in (
        *WRITER_REQUIRED_ANALYSIS_ARTIFACTS,
        *WRITER_OPTIONAL_ANALYSIS_ARTIFACTS,
    ):
        artifact = _trusted_preserved_evidence_file(
            sandbox=sandbox,
            relative_path=f"{PAPER_EVIDENCE_DIR}/{ANALYSIS_ARTIFACT_DIR}/{name}",
            required_prefix=(PAPER_EVIDENCE_DIR, ANALYSIS_ARTIFACT_DIR),
        )
        if artifact is None:
            if name in WRITER_REQUIRED_ANALYSIS_ARTIFACTS:
                return None
            continue
        artifacts[name] = artifact

    try:
        return _analysis_snapshot_hash(
            paper_path=paper_path,
            artifacts=artifacts,
        )
    except OSError:
        return None

def _trusted_preserved_evidence_file(
    *,
    sandbox: Path,
    relative_path: Any,
    required_prefix: tuple[str, ...],
) -> Path | None:
    """Resolve one evidence file without accepting links or path traversal."""

    if not isinstance(relative_path, str):
        return None
    normalized = relative_path.strip()
    if (
        not normalized
        or normalized != relative_path
        or "\\" in normalized
        or "\x00" in normalized
        or "//" in normalized
    ):
        return None
    parts = tuple(normalized.split("/"))
    if (
        len(parts) <= len(required_prefix)
        or parts[: len(required_prefix)] != required_prefix
        or any(part in {"", ".", ".."} or ":" in part for part in parts)
    ):
        return None
    try:
        root = sandbox.resolve(strict=True)
    except OSError:
        return None
    cursor = sandbox
    for part in parts:
        cursor = cursor / part
        try:
            if path_is_link(cursor):
                return None
        except OSError:
            return None
    try:
        resolved = cursor.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError):
        return None
    return resolved if resolved.is_file() else None

def _task_writer_resume_sandbox_is_safe(
    *,
    audit_dir: Path,
    sandbox: Path,
) -> bool:
    task_root = audit_dir / "03c_task_writer_sandboxes"
    evidence_root = sandbox / PAPER_EVIDENCE_DIR
    try:
        if any(
            path_is_link(path)
            for path in (audit_dir, task_root, sandbox, evidence_root)
        ):
            return False
        if not sandbox.is_dir() or not evidence_root.is_dir():
            return False
        sandbox.resolve(strict=True).relative_to(task_root.resolve(strict=True))
    except (OSError, ValueError):
        return False
    return True

def _task_writer_runtime_refresh_marker(sandbox: Path) -> Path:
    return sandbox / ".geng_runtime_refresh_pending.json"

def _task_writer_runtime_refresh_pending(sandbox: Path) -> bool:
    marker = _task_writer_runtime_refresh_marker(sandbox)
    try:
        return marker.is_file() and not path_is_link(marker)
    except OSError:
        return False

def _task_writer_record_refresh_pending(record: dict[str, Any]) -> bool:
    runtime_pending = (
        record.get("runtime_refresh_required") is True
        and record.get("runtime_refresh_completed") is not True
    )
    # Older recovered records used one completion bit for the combined
    # runtime/environment snapshot. Accept that evidence while new records
    # persist both fields explicitly.
    environment_completed = (
        record.get("environment_refresh_completed") is True
        or record.get("runtime_refresh_completed") is True
    )
    environment_pending = (
        record.get("environment_refresh_required") is True
        and not environment_completed
    )
    return bool(runtime_pending or environment_pending)

def _task_writer_record_refresh_reusable(record: dict[str, Any]) -> bool:
    if _task_writer_record_refresh_pending(record):
        return False
    raw_sandbox = str(record.get("sandbox") or "").strip()
    if not raw_sandbox:
        return True
    return not _task_writer_runtime_refresh_pending(Path(raw_sandbox))

def _checkpoint_partial_task_writer_records(
    *,
    audit_dir: Path,
    dispatch_audit: dict[str, Any],
    records_by_index: dict[int, dict[str, Any]],
) -> None:
    """Persist each completed parallel Writer without waiting for the batch."""

    write_json(
        audit_dir / "03c_task_writers_records.json",
        {
            "checkpoint": "parallel_dispatch_partial",
            "dispatch_policy": dispatch_audit,
            "tasks": [
                records_by_index[index]
                for index in sorted(records_by_index)
            ],
        },
    )

def _record_is_valid_current_delivery(record: dict[str, Any]) -> bool:
    raw_sandbox = str(record.get("sandbox") or "").strip()
    if not raw_sandbox:
        return False
    sandbox = Path(raw_sandbox)
    if not sandbox.is_dir():
        return False
    if record.get("writer_completed") is not True:
        return False
    result = record.get("result_json")
    artifacts = record.get("artifacts")
    return bool(isinstance(result, dict) and result) or bool(
        isinstance(artifacts, dict)
        and artifacts.get("has_artifacts")
    )

def _record_has_terminal_task_verification(record: dict[str, Any]) -> bool:
    """Return whether the Reporter reached any normal scientific terminal outcome."""

    verification = record.get("task_verification")
    return (
        isinstance(verification, dict)
        and (verification.get("host_action") != "rerun_writer" or record.get("coordination_status") == "stopped")
    )



def _move_writer_generation_to_archive(source: Path, destination: Path) -> None:
    target = destination
    suffix = 2
    while target.exists():
        target = destination.with_name(f"{destination.name}__{suffix}")
        suffix += 1
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(target))







def _terminalize_rerun_request(
    *,
    record: dict[str, Any],
    verification: dict[str, Any] | None,
    stop_reason: str,
    uncertainty: str,
) -> dict[str, Any]:
    # Stop scheduling without rewriting the independent scientific request.
    terminal = verification if isinstance(verification, dict) else {}
    record["task_verification"] = terminal
    record["coordination_status"] = "stopped"
    record["coordination_reason"] = stop_reason
    record.setdefault("coordination_observations", []).append(uncertainty)
    record["scientific_stop_reason"] = stop_reason
    return terminal


def _complete_task_writer_runtime_refresh(
    *,
    record: dict[str, Any],
    marker: Path,
    required: bool,
) -> dict[str, Any]:
    if not required:
        return record
    record["runtime_refresh_required"] = True
    record["runtime_refresh_completed"] = True
    record["environment_refresh_required"] = True
    record["environment_refresh_completed"] = True
    try:
        marker.unlink(missing_ok=True)
    except OSError as exc:
        record.setdefault("coordination_observations", []).append(f"Refresh marker cleanup failed: {exc}")
    return record

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

def _archive_nonterminal_writer_delivery(
    *,
    sandbox: Path,
    output_subdir: str,
    round_no: int,
    session_status: dict[str, Any],
) -> None:
    progress_dir = sandbox / "writer_progress" / f"round_{round_no:03d}"
    progress_dir.mkdir(parents=True, exist_ok=True)
    output_dir = sandbox / "outputs" / output_subdir
    if output_dir.exists():
        _move_writer_generation_to_archive(output_dir, progress_dir / "outputs" / output_subdir)
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
            "reason": session_status.get("source") or session_status.get("error_kind") or "delivery_archived_for_continuation",
            "session_status": session_status,
        },
    )
