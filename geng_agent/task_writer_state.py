"""Task-writer resume, checkpoint, refresh, and archival state."""

from __future__ import annotations

import hashlib
import ast
import json
import shutil
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Callable

from .case_environment import EnvironmentPolicyError, RequirementRequest
from .foundation_snapshot import path_is_foundation_link
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
    declined_foundation_revision_ids: set[str] | None = None,
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
            return observed.get("passed") is True
        except (OSError, ValueError, TypeError):
            return False
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
        revision = record.get("foundation_revision_request")
        pending_revision = isinstance(revision, dict) and bool(revision.get("request_id"))
        expected_sandbox = Path(layout["sandbox"])
        sandbox = Path(str(record.get("sandbox") or ""))
        if (
            not _task_writer_resume_sandbox_is_safe(
                audit_dir=audit_dir,
                sandbox=expected_sandbox,
            )
            or not sandbox.exists()
            or path_is_foundation_link(sandbox)
            or sandbox.resolve() != expected_sandbox.resolve()
            or (_task_writer_runtime_refresh_pending(expected_sandbox) and not pending_revision)
        ):
            continue
        if str(record.get("analysis_snapshot_hash") or "") != expected_hash(layout):
            continue
        if pending_revision and str(revision["request_id"]) in (declined_foundation_revision_ids or set()):
            record["scientific_stop_reason"] = "foundation_revision_unresolved"
        # A stopped scientific repair is preserved as a blocker, never as a
        # verified full result. Requiring execution here would relaunch the
        # same impossible repair before the host can report it honestly.
        if not pending_revision and not receipt_is_current(record, expected_sandbox):
            invalid_receipt_indexes.add(index)
            continue
        record.setdefault("index", index)
        record.setdefault("execution_unit_id", str(layout["execution_unit_id"]))
        records[index] = record

    # A compound execution unit is atomic for reuse. Never combine a partial
    # checkpoint from one logical member with a fresh shared run for the rest.
    for layout in {str(item["execution_unit_id"]): item for item in layouts.values()}.values():
        indexes = list(layout["member_indexes"])
        present = [index for index in indexes if index in records]
        if present and len(present) != len(indexes):
            for index in present:
                records.pop(index, None)

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
            or path_is_foundation_link(evidence_index)
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
    if path_is_foundation_link(sandbox):
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
            if path_is_foundation_link(cursor):
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
            path_is_foundation_link(path)
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
        return marker.is_file() and not path_is_foundation_link(marker)
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
    status = str(record.get("task_writer_status") or "")
    if status not in {WRITER_REVIEW_STATUS, FINAL_MATCHED_STATUS}:
        return False
    if status == FINAL_MATCHED_STATUS:
        verification = record.get("verification_result")
        if record.get("verification_verified") is not True or not isinstance(verification, dict):
            return False
        if verification.get("outcome") not in {"reproduced", "reproduced_with_assumptions"}:
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
    task_id = str(record.get("task_id") or "")
    return (
        isinstance(verification, dict)
        and verification.get("host_action") == "complete"
        and verification.get("outcome") in TERMINAL_SCIENTIFIC_OUTCOMES
        and not task_verification_issues(verification, task_id)
    )

def _archive_execution_unit_delivery(
    *,
    sandbox: Path,
    members: list[tuple[int, dict[str, Any], dict[str, Any]]],
    execution_unit_id: str,
    round_no: int,
    session_status: dict[str, Any],
) -> None:
    progress_dir = sandbox / "writer_progress" / f"round_{round_no:03d}"
    progress_dir.mkdir(parents=True, exist_ok=True)
    archived_paths: list[str] = []
    task_ids = [
        str(task.get("task_id") or entry.get("task_id") or f"task_{index}")
        for index, task, entry in members
    ]
    active_outputs = sandbox / "outputs"
    if active_outputs.exists():
        _move_writer_generation_to_archive(active_outputs, progress_dir / "outputs")
        archived_paths.append("outputs")

    unit_result_path = sandbox / "execution_unit_result.json"
    unit_result = _read_optional_json_object(unit_result_path)
    unit_asset_relative = Path("execution_units") / safe_label(execution_unit_id)
    unit_asset_root = sandbox / unit_asset_relative
    # Preserve expensive shared checkpoints. Reuse is decided from producer
    # receipts and current dependency hashes, never from mere file existence.
    unit_asset_root.mkdir(parents=True, exist_ok=True)

    raw_lineage = unit_result.get("artifact_lineage")
    for raw_entry in raw_lineage if isinstance(raw_lineage, list) else []:
        if not isinstance(raw_entry, dict):
            continue
        artifact = _active_writer_artifact_path(
            sandbox=sandbox,
            raw_path=raw_entry.get("path"),
        )
        if artifact is None or not artifact.exists():
            continue
        try:
            relative = artifact.relative_to(sandbox)
        except ValueError:
            continue
        # Namespace artifacts were moved as one atomic tree above. Outputs are
        # also already archived per logical task.
        if relative.parts[: len(unit_asset_relative.parts)] == unit_asset_relative.parts:
            continue
        if relative.parts and relative.parts[0] == "outputs":
            continue
        destination = progress_dir / "shared_artifacts" / relative
        _move_writer_generation_to_archive(artifact, destination)
        archived_paths.append(relative.as_posix())

    if unit_result_path.is_file():
        _move_writer_generation_to_archive(
            unit_result_path,
            progress_dir / "execution_unit_result.json",
        )
        archived_paths.append("execution_unit_result.json")
    write_json(
        progress_dir / "session_status.json",
        {
            "terminal": False,
            "reason": "execution_unit_generation_archived_before_continuation",
            "task_ids": task_ids,
            "session_status": session_status,
            "archived_active_paths": sorted(set(archived_paths)),
            "preserved_shared_artifact_root": unit_asset_relative.as_posix(),
        },
    )

def _active_writer_artifact_path(*, sandbox: Path, raw_path: Any) -> Path | None:
    normalized = str(raw_path or "").strip().replace("\\", "/")
    if not normalized:
        return None
    portable = PurePosixPath(normalized)
    if (
        portable.is_absolute()
        or PureWindowsPath(normalized).is_absolute()
        or PureWindowsPath(normalized).drive
        or ".." in portable.parts
    ):
        return None
    candidate = sandbox / Path(*portable.parts)
    try:
        candidate.resolve().relative_to(sandbox.resolve())
    except (OSError, ValueError):
        return None
    protected_roots = {
        "configs",
        PAPER_EVIDENCE_DIR,
        "src",
        "tasks",
        "writer_progress",
    }
    if portable.parts and portable.parts[0] in protected_roots:
        return None
    protected_root_files = {
        "config.json",
        "config_smoke.json",
        "execution_unit_result.json",
        "execution_plan.json",
        "README.md",
        "requirements.txt",
        "run_experiment.py",
        "tasks_manifest.json",
    }
    if len(portable.parts) == 1 and portable.name in protected_root_files:
        return None
    if candidate.is_symlink():
        return None
    return candidate

def _move_writer_generation_to_archive(source: Path, destination: Path) -> None:
    target = destination
    suffix = 2
    while target.exists():
        target = destination.with_name(f"{destination.name}__{suffix}")
        suffix += 1
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(target))

def _complete_execution_unit_runtime_refresh(
    *,
    records: list[dict[str, Any]],
    marker: Path,
    required: bool,
    writer_status: dict[str, Any],
) -> bool:
    if not required:
        return False
    fresh_delivery_usable = (
        writer_status.get("ok") is True
        and not writer_status.get("error_kind")
        and bool(records)
        and all(
            record.get("writer_completed") is True
            and record.get("task_writer_status") == TASK_WRITER_TERMINAL_STATUS
            for record in records
        )
    )
    if fresh_delivery_usable:
        try:
            marker.unlink(missing_ok=True)
        except OSError:
            fresh_delivery_usable = False
    for record in records:
        record["runtime_refresh_required"] = True
        record["runtime_refresh_completed"] = bool(fresh_delivery_usable)
        record["environment_refresh_required"] = True
        record["environment_refresh_completed"] = bool(fresh_delivery_usable)
    return bool(fresh_delivery_usable)

def _task_environment_requests(
    records: list[dict[str, Any]],
) -> tuple[RequirementRequest, ...]:
    requests: list[RequirementRequest] = []
    for record in records:
        if record.get("writer_error_kind") == "environment_request_invalid":
            status = record.get("writer_status")
            reason = status.get("blocked_reason") if isinstance(status, dict) else None
            raise EnvironmentPolicyError(
                str(reason or "task writer produced an invalid environment request")
            )
        raw_requests = record.get("environment_requests")
        for item in raw_requests if isinstance(raw_requests, list) else []:
            if not isinstance(item, dict):
                raise EnvironmentPolicyError("task writer environment request is malformed")
            requests.append(
                RequirementRequest(
                    requirement=str(item.get("requirement") or ""),
                    import_names=tuple(str(name) for name in item.get("import_names") or ()),
                    requested_by=str(item.get("requested_by") or record.get("task_id") or "task_writer"),
                    reason=str(item.get("reason") or "") or None,
                    capability=str(item.get("capability") or "") or None,
                    import_names_explicit=bool(item.get("import_names_explicit")),
                )
            )
    return tuple(requests)

def _rerun_evidence_fingerprint(evidence: Any, progress: str = "") -> str:
    """Return an order-stable scientific rerun identity for loop detection."""

    value = evidence if isinstance(evidence, dict) else {}

    def _normalized_list(key: str) -> list[str]:
        raw = value.get(key)
        if not isinstance(raw, list):
            return []
        return sorted({str(item).strip().casefold() for item in raw if str(item).strip()})

    payload = {
        "rerun_reason": str(value.get("rerun_reason") or "none").strip().casefold(),
        "contract_item_ids": _normalized_list("contract_item_ids"),
        "change_targets": _normalized_list("change_targets"),
        "progress": progress,
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _writer_progress_fingerprint(sandbox: Path) -> str:
    """Compare actual numerical progress, falling back to source before a run.

    Rewording a rerun request or changing source comments is not progress. A
    changed numerical result may justify another scientific inspection even
    when the same method or trend still needs work.
    """
    paths = [p for p in (sandbox / "outputs").rglob("*") if p.is_file()
             and p.suffix.lower() in {".csv", ".npy", ".npz", ".json"}
             and p.name not in {"execution_receipt.json", "task_agent_result.json"}]
    digest = hashlib.sha256()
    # A real method correction can leave the headline metric unchanged. Keep
    # executable changes in the state, but ignore comments and formatting so
    # cosmetic edits cannot manufacture progress.
    for path in sorted([p for name in ("tasks", "src") for p in (sandbox / name).rglob("*.py") if p.is_file() and not p.is_symlink()]):
        digest.update(path.relative_to(sandbox).as_posix().encode())
        try:
            digest.update(ast.dump(ast.parse(path.read_text(encoding="utf-8-sig")), include_attributes=False).encode())
        except (ValueError, OSError, SyntaxError):
            digest.update(b"<invalid-source>")
    paths += [p for p in (sandbox / "configs").rglob("*.json") if p.is_file()]
    paths += [sandbox / name for name in ("config.json", "config_smoke.json") if (sandbox / name).is_file()]
    for path in sorted(paths):
        digest.update(path.relative_to(sandbox).as_posix().encode())
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    return digest.hexdigest()

def _writer_source_config_fingerprint(sandbox: Path) -> str:
    """Hash task-owned source and run configuration, excluding outputs."""

    digest = hashlib.sha256()
    paths = list(_task_owned_files(sandbox))
    src_dir = sandbox / "src"
    if src_dir.is_dir():
        paths.extend(
            path
            for path in src_dir.rglob("*.py")
            if path.is_file() and not path.is_symlink()
        )
    configs_dir = sandbox / "configs"
    if configs_dir.is_dir():
        paths.extend(
            path
            for path in configs_dir.rglob("*")
            if path.is_file() and not path.is_symlink()
        )
    for name in ("config.json", "config_smoke.json", "requirements.txt"):
        path = sandbox / name
        if path.is_file() and not path.is_symlink():
            paths.append(path)
    for path in sorted(set(paths), key=lambda item: item.relative_to(sandbox).as_posix()):
        relative = path.relative_to(sandbox).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        try:
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError:
            digest.update(b"<unreadable>")
        digest.update(b"\0")
    return digest.hexdigest()

def _record_source_config_fingerprint(record: dict[str, Any], fallback: Path) -> str:
    raw_sandbox = str(record.get("sandbox") or "").strip()
    sandbox = Path(raw_sandbox) if raw_sandbox else fallback
    return _writer_source_config_fingerprint(sandbox)

def _terminalize_rerun_request(
    *,
    record: dict[str, Any],
    verification: dict[str, Any] | None,
    stop_reason: str,
    uncertainty: str,
) -> dict[str, Any]:
    terminal = dict(verification or {})
    terminal["host_action"] = "complete"
    terminal["rerun_reason"] = "none"
    terminal["outcome"] = (
        "execution_failed" if terminal.get("run_valid") is False else "not_reproduced"
    )
    uncertainties = terminal.get("remaining_uncertainties")
    if not isinstance(uncertainties, list):
        uncertainties = []
        terminal["remaining_uncertainties"] = uncertainties
    uncertainties.append(uncertainty)
    record["task_verification"] = terminal
    if isinstance(record.get("task_reporter"), dict):
        record["task_reporter"]["task_verification"] = terminal
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
    marker.unlink(missing_ok=True)
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
            "reason": "writer session ended without ready_for_review",
            "session_status": session_status,
        },
    )
