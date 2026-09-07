"""Small current-state handoffs and local references for isolated Writers."""
from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path, PurePosixPath
from typing import Any

from .execution_receipts import file_hash, _inside
from .foundation_snapshot import path_is_foundation_link


def archive_satisfied_environment_request(sandbox: Path, runtime: Any) -> str | None:
    """Consume an old request only when the new lock proves its packages and imports."""
    from .case_runtime_requests import read_environment_request, _lock_satisfies_requirement
    from .case_environment import normalize_requirement
    from packaging.requirements import Requirement
    source = _regular_file(sandbox, "environment_request.json")
    if source is None:
        return None
    requests = read_environment_request(sandbox=sandbox, source="writer_resume")
    not_applicable = []
    for request in requests:
        normalized = normalize_requirement(request)
        if not _lock_satisfies_requirement(normalized, runtime.lock):
            return None
        parsed = Requirement(normalized.requirement)
        marker_environment = runtime.lock.get("interpreter", {}).get("marker_environment")
        if parsed.marker is not None and isinstance(marker_environment, dict):
            try:
                if not parsed.marker.evaluate(environment=marker_environment):
                    not_applicable.append(normalized.requirement)
                    continue
            except (KeyError, ValueError):
                pass
        proven_imports = set()
        for record in runtime.lock.get("requirements", []):
            if (isinstance(record, dict) and record.get("distribution") == normalized.distribution
                    and record.get("satisfied") is True and record.get("imports_ok") is True):
                proven_imports.update(name for name, probe in record.get("imports", {}).items()
                                     if isinstance(probe, dict) and probe.get("ok") is True)
        if not set(normalized.import_names).issubset(proven_imports):
            return None
    relative = f"writer_progress/environment_requests/{file_hash(source)}.resolved.json"
    target = _inside(sandbox, relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"request": json.loads(source.read_text(encoding="utf-8")),
        "resolved_environment_hash": runtime.environment_hash,
        **({"not_applicable_requirements": not_applicable} if not_applicable else {})}, ensure_ascii=False, indent=2), encoding="utf-8")
    source.unlink()
    return relative


def _regular_file(root: Path, relative: str) -> Path | None:
    parts = PurePosixPath(relative.replace("\\", "/")).parts
    if not parts or any(p in {"..", "."} or ":" in p for p in parts) or relative.startswith("/"):
        return None
    cursor = root
    try:
        if path_is_foundation_link(cursor):
            return None
        for part in parts:
            cursor = cursor / part
            if path_is_foundation_link(cursor):
                return None
        cursor.resolve().relative_to(root.resolve())
        return cursor if cursor.is_file() else None
    except (ValueError, OSError):
        return None


def localize_writer_feedback(feedback: dict[str, Any], *, reporter_root: Path,
                             sandbox: Path, output_subdir: str) -> dict[str, Any]:
    """Resolve only reviewed paper/source/output files; never expose other role state."""
    result = copy.deepcopy(feedback)
    mappings: dict[str, dict[str, str]] = {}
    unresolved: list[str] = []

    def localize(raw: Any) -> Any:
        if not isinstance(raw, str):
            return raw
        relative = raw.replace("\\", "/")
        # Older Reporter notes may use a case-relative prefix.
        for prefix in ("paper_evidence/", "inputs/writer_output/"):
            position = relative.find(prefix)
            if position >= 0:
                relative = relative[position:]
                break
        if not relative.startswith(("paper_evidence/", "inputs/writer_output/")):
            unresolved.append(raw)
            return raw
        reviewed = _regular_file(reporter_root, relative)
        if reviewed is None:
            unresolved.append(raw)
            return raw
        digest = file_hash(reviewed)
        target_relative = relative
        if relative.startswith("inputs/writer_output/source/"):
            target_relative = relative.removeprefix("inputs/writer_output/source/")
        elif relative.startswith("inputs/writer_output/outputs/"):
            target_relative = f"outputs/{output_subdir}/" + relative.removeprefix("inputs/writer_output/outputs/")
        current = _regular_file(sandbox, target_relative)
        if current is None or file_hash(current) != digest:
            # Preserve the reviewed bytes when current source/output has moved or changed.
            target_relative = f"writer_progress/review_evidence/{digest}/{reviewed.name}"
            target = sandbox / target_relative
            cursor = sandbox
            for part in PurePosixPath(target_relative).parts[:-1]:
                cursor = cursor / part
                if path_is_foundation_link(cursor):
                    raise ValueError("unsafe Writer feedback destination")
                cursor.mkdir(exist_ok=True)
            if path_is_foundation_link(target):
                raise ValueError("unsafe Writer feedback destination")
            shutil.copyfile(reviewed, target)
        mappings[raw] = {"path": target_relative, "sha256": digest}
        return target_relative

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"evidence_files", "paper_evidence_files"} and isinstance(child, list):
                    value[key] = [localize(item) for item in child]
                else:
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(result)
    result["writer_evidence_references"] = mappings
    if unresolved:
        result["unresolved_review_references"] = sorted(set(unresolved))
    return result


def writer_recovery_context(sandbox: Path, *, reason: str, task_ids: list[str]) -> dict[str, Any]:
    """Point at current facts without embedding old transcripts or scientific verdicts."""
    archives = sorted((sandbox / "writer_progress").glob("round_*/session_status.json"))
    latest = archives[-1] if archives else None
    previous: dict[str, Any] = {}
    if latest is not None and _regular_file(sandbox, latest.relative_to(sandbox).as_posix()):
        try:
            previous = json.loads(latest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    if not isinstance(previous, dict):
        previous = {}
    session = previous.get("session_status") or {}
    if not isinstance(session, dict):
        session = {}
    receipt_paths = list((sandbox / "outputs").glob("*/execution_receipt.json"))
    if latest is not None:
        receipt_paths.extend(latest.parent.glob("outputs/*/execution_receipt.json"))
    return {
        "reason": reason, "task_ids": task_ids,
        "latest_archive": latest.parent.relative_to(sandbox).as_posix() if latest else None,
        "previous_session": {k: session[k] for k in ("source", "error_kind", "blocked_reason", "error") if k in session},
        "current_sources": ["tasks/", "src/", "configs/", "config.json"],
        "preserved_assets": "execution_units/",
        "existing_execution_receipts": [p.relative_to(sandbox).as_posix() for p in receipt_paths
            if _regular_file(sandbox, p.relative_to(sandbox).as_posix())],
        "latest_unit_lineage": (latest.parent / "execution_unit_result.json").relative_to(sandbox).as_posix()
            if latest and _regular_file(sandbox, (latest.parent / "execution_unit_result.json").relative_to(sandbox).as_posix()) else None,
        "execution_status_command": "python run_task.py --task TASK_ID --status",
        "policy": "Inspect existing implementation, latest archived outputs and producer receipts before rerunning. File existence alone does not establish a reusable checkpoint.",
    }
