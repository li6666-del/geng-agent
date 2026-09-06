"""Report Editor task packets and accepted asset handling."""

from __future__ import annotations

import hashlib
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
        writer_result = record.get("result_json") if isinstance(record.get("result_json"), dict) else {}
        terminal_outcome = _task_terminal_outcome(verification)
        packets.append(
            {
                "task_id": task_id,
                "task": task,
                "task_facts": facts_for_task(facts, task),
                "writer_summary": writer_result.get("summary"),
                "parameter_resolution": writer_result.get("parameter_resolution", []),
                "detail_comparison": writer_result.get("detail_comparison", {}),
                "writer_differences": writer_result.get("differences", []),
                "remaining_uncertainties": writer_result.get("remaining_uncertainties", []),
                "iteration_records": writer_result.get(
                    "iteration_records",
                    record.get("iteration_records", []),
                ),
                "execution_summary": writer_result.get("execution_summary", record.get("execution_summary", {})),
                "verification": verification,
                "terminal_outcome": terminal_outcome,
                "structured_evidence": {
                    "core_conclusions": verification.get("core_conclusions", []),
                    "key_numeric_comparisons": verification.get("key_numeric_comparisons", []),
                    "evidence_files": verification.get("evidence_files", []),
                    "writer_artifacts": writer_result.get("artifacts", []),
                },
                "local_assets": _editor_asset_paths(task_id, verification.get("local_assets")),
                "paper_assets": _editor_asset_paths(task_id, verification.get("paper_assets")),
            }
        )
    return packets


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
