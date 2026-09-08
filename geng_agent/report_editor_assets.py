"""Report Editor task packets and accepted asset handling."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
from typing import Any

from .paper_evidence import facts_for_task, safe_label
from .report_editor_workspace import REPORT_ASSETS_DIR


def _build_task_packets(
    *,
    facts: dict[str, Any],
    tasks: dict[str, Any],
    task_records: list[dict[str, Any]],
    task_verifications: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    task_by_id = {
        str(task.get("task_id") or ""): task
        for task in tasks.get("repro_tasks", [])
        if isinstance(task, dict)
    }
    record_by_id = {
        str(record.get("task_id") or ""): record
        for record in task_records
        if isinstance(record, dict)
    }
    verification_by_id = {
        str(item.get("task_id") or ""): item
        for item in task_verifications
        if isinstance(item, dict)
    }
    packets: list[dict[str, Any]] = []
    for task_id, task in task_by_id.items():
        record = record_by_id.get(task_id, {})
        verification = verification_by_id.get(task_id, {})
        terminal_outcome = _task_terminal_outcome(verification)
        packets.append(
            {
                "task_id": task_id,
                "task": {key: task[key] for key in ("task_id", "title", "target", "figure_or_claim", "metric") if key in task},
                "remaining_uncertainties": verification.get("remaining_uncertainties", []),
                "execution_summary": _host_report_execution(record, verification),
                "verification": {key: verification[key] for key in (
                    "task_id", "outcome", "host_action", "run_valid",
                    "decision_reason", "decision_authority", "engineering_status", "engineering_issues", "asset_notes", "core_conclusions", "key_numeric_comparisons",
                    "comparison_summary", "differences", "non_material_differences", "evidence_files", "confidence",
                    "verified_facts", "provenance_base") if key in verification},
                "terminal_outcome": terminal_outcome,
                "local_assets": _editor_asset_paths(task_id, verification.get("local_assets")),
                "paper_assets": _editor_asset_paths(task_id, verification.get("paper_assets")),
            }
        )
    return packets


def _host_report_execution(record: dict[str, Any], verification: dict[str, Any]) -> dict[str, Any]:
    """Separate observed attempts and valid execution from scientific support.

    Old Writer summaries are never a substitute for host receipts. Earlier
    receipts prove process completion, not support for the paper's conclusions.
    """
    host = record.get("host_execution") if isinstance(record.get("host_execution"), dict) else {}
    latest = host.get("receipt") if isinstance(host.get("receipt"), dict) else {}
    receipts: dict[str, dict[str, Any]] = {}
    writer_status = record.get("writer_status") or {}
    audit = writer_status.get("execution_audit_dir") if isinstance(writer_status, dict) else None
    if audit:
        for path in (Path(audit) / "execution_runs").glob("*/execution_receipt.json"):
            try:
                receipt = json.loads(path.read_text(encoding="utf-8"))
                if (receipt.get("observer") == "orchestration_host" and receipt.get("task_id") == record.get("task_id")
                        and receipt.get("mode") == "full" and receipt.get("run_id")):
                    receipts[str(receipt["run_id"])] = receipt
            except (OSError, ValueError, TypeError):
                continue
    if latest.get("observer") == "orchestration_host" and latest.get("mode") == "full" and latest.get("run_id"):
        receipts[str(latest["run_id"])] = latest
    latest_observed = (latest.get("run_id") in receipts and latest.get("observer") == "orchestration_host"
                       and latest.get("mode") == "full")
    valid_latest = None
    if latest_observed and (host.get("passed") is False or verification.get("run_valid") is False):
        valid_latest = 0
    elif latest_observed and host.get("passed") is True and verification.get("run_valid") is True:
        valid_latest = 1
    return {
        "source": "host_execution_receipts" if receipts else "unavailable",
        "observed_full_attempt_count": len(receipts) if receipts else None,
        "observed_process_completed_count": sum(r.get("returncode") == 0 and not r.get("cancelled") for r in receipts.values()) if receipts else None,
        "latest_valid_execution_count": valid_latest,
        "valid_count_scope": "latest execution and artifact validity only (0 or 1; unknown is null); scientific support is given only by the task outcome",
        "latest_run_id": latest.get("run_id") if receipts else None,
        "latest_returncode": latest.get("returncode") if receipts else None,
        "unavailable_reason": "" if receipts else "No host-observed execution receipt supplied; Writer counts are unverified",
    }


def _task_terminal_outcome(verification: dict[str, Any]) -> str:
    for key in ("terminal_outcome", "outcome", "scientific_outcome"):
        value = str(verification.get(key) or "").strip()
        if value:
            return value
    return "unclassified_terminal_result"


def _editor_asset_paths(task_id: str, values: Any) -> list[str]:
    paths: list[str] = []
    for raw_path in values if isinstance(values, list) else []:
        name = Path(str(raw_path)).name
        if name:
            paths.append(f"{REPORT_ASSETS_DIR}/{safe_label(task_id)}/{name}")
    return paths


def _sanitize_task_packet_assets(task_packets: list[dict[str, Any]], root: Path) -> list[str]:
    warnings: list[str] = []
    for packet in task_packets:
        task_id = safe_label(str(packet.get("task_id") or "task"))
        for key in ("local_assets", "paper_assets"):
            retained: list[str] = []
            values = packet.get(key) if isinstance(packet.get(key), list) else []
            for raw_path in values:
                resolved = _resolve_report_asset(root, task_id=task_id, raw_path=raw_path)
                if resolved is None:
                    warnings.append(f"{task_id}: ignored unavailable or unsafe {key[:-1]}: {raw_path}")
                    continue
                relative, _ = resolved
                retained.append(f"{REPORT_ASSETS_DIR}/{relative.as_posix()}")
            packet[key] = retained
    return warnings


def _resolve_report_asset(root: Path, *, task_id: str, raw_path: Any) -> tuple[Path, Path] | None:
    try:
        relative = Path(str(raw_path))
        if relative.is_absolute():
            return None
        relative = relative.relative_to(REPORT_ASSETS_DIR)
        if len(relative.parts) != 2 or relative.parts[0] != task_id:
            return None
        source_root = root.resolve()
        candidate = source_root / relative
        if candidate.is_symlink():
            return None
        asset = candidate.resolve()
        if not asset.is_relative_to(source_root):
            return None
        if not asset.is_file() or asset.suffix.lower() not in {".png", ".jpg", ".jpeg"}:
            return None
        if asset.stat().st_size > 20_000_000:
            return None
    except (OSError, ValueError):
        return None
    return relative, asset

def _accepted_asset_inventory(root: Path, task_packets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    inventory: list[dict[str, Any]] = []
    for relative, source in _accepted_asset_sources(root, task_packets):
        stat = source.stat()
        inventory.append(
            {
                "path": relative.as_posix(),
                "size": stat.st_size,
                "sha256": _sha256_file(source),
            }
        )
    return inventory


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

def _copy_assets_for_editor(source: Path, target: Path, task_packets: list[dict[str, Any]]) -> list[str]:
    warnings: list[str] = []
    target.mkdir(parents=True, exist_ok=True)
    copied_paths: set[str] = set()
    for relative, asset in _accepted_asset_sources(source, task_packets):
        destination = target / relative
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(asset, destination)
            copied_paths.add(f"{REPORT_ASSETS_DIR}/{relative.as_posix()}")
        except OSError as exc:
            warnings.append(f"could not copy optional report asset {relative.as_posix()}: {type(exc).__name__}")
    for packet in task_packets:
        for key in ("local_assets", "paper_assets"):
            values = packet.get(key) if isinstance(packet.get(key), list) else []
            packet[key] = [value for value in values if str(value) in copied_paths]
    return warnings


def _accepted_asset_sources(
    source: Path,
    task_packets: list[dict[str, Any]],
) -> list[tuple[Path, Path]]:
    selected: list[tuple[Path, Path]] = []
    seen: set[Path] = set()
    for packet in task_packets:
        task_id = safe_label(str(packet.get("task_id") or "task"))
        for key in ("local_assets", "paper_assets"):
            values = packet.get(key) if isinstance(packet.get(key), list) else []
            for raw_path in values:
                resolved = _resolve_report_asset(source, task_id=task_id, raw_path=raw_path)
                if resolved is None:
                    continue
                relative, asset = resolved
                if relative not in seen:
                    seen.add(relative)
                    selected.append((relative, asset))
    return sorted(selected, key=lambda item: item[0].as_posix())
