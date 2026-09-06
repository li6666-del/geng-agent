from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
from pathlib import Path
from typing import Any

from .task_writer_support import (
    PAPER_EVIDENCE_DIR,
    TRUSTED_PROJECT_FILES,
)


REPORT_ASSETS_DIR = "report_assets"
WRITER_SOURCE_DIR = "source"
_WRITER_SOURCE_MAX_BYTES = 10_000_000
_WRITER_OUTPUT_MAX_FILE_BYTES = 256 * 1024 * 1024
_WRITER_OUTPUT_MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
_WRITER_SOURCE_EXCLUDED_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "__pycache__",
        "audit",
        "env",
        "node_modules",
        "outputs",
        PAPER_EVIDENCE_DIR,
        "repair_logs",
        REPORT_ASSETS_DIR,
        "venv",
    }
)
_WRITER_SOURCE_EXCLUDED_FILES = frozenset(
    {"task_agent_result.json", "task_agent_result.md"}
)


def _manifest_declared_source_paths(source_sandbox: Path) -> set[str]:
    declared = set(TRUSTED_PROJECT_FILES)
    manifest_path = source_sandbox / "tasks_manifest.json"
    if (
        _path_is_link_like(source_sandbox)
        or not manifest_path.is_file()
        or _path_is_link_like(manifest_path)
    ):
        return declared
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return declared
    tasks = manifest.get("tasks") if isinstance(manifest, dict) else None
    for item in tasks if isinstance(tasks, list) else []:
        if not isinstance(item, dict):
            continue
        values: list[Any] = [
            item.get("script"),
            item.get("entrypoint"),
            item.get("config_full"),
            item.get("config_smoke"),
        ]
        if isinstance(item.get("source_files"), list):
            values.extend(item["source_files"])
        for value in values:
            raw = str(value or "").replace("\\", "/").strip().lstrip("./")
            candidate = Path(raw)
            if raw and not candidate.is_absolute() and ".." not in candidate.parts:
                declared.add(candidate.as_posix())
    return declared


def _path_is_link_like(path: Path) -> bool:
    """Detect links and Windows reparse points without following them."""

    try:
        metadata = path.lstat()
    except OSError:
        return False
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse_flag)


def _safe_writer_tree_files(
    *,
    root: Path,
    source_root: Path,
) -> tuple[list[tuple[Path, Path]], list[str]]:
    """Enumerate regular files without following links or escaping the sandbox."""

    warnings: list[str] = []
    if _path_is_link_like(source_root):
        return [], ["assigned writer sandbox is a symbolic link or reparse point"]
    try:
        relative_root = root.relative_to(source_root)
        source_root_resolved = source_root.resolve(strict=True)
    except (OSError, ValueError):
        return [], ["assigned writer input root is missing or outside its sandbox"]
    current = source_root
    for part in relative_root.parts:
        current = current / part
        if _path_is_link_like(current):
            return [], [
                "writer input root rejected symbolic link: "
                f"{relative_root.as_posix()}"
            ]
    try:
        root_resolved = root.resolve(strict=True)
        root_resolved.relative_to(source_root_resolved)
    except (OSError, ValueError):
        return [], ["assigned writer input root resolves outside its sandbox"]
    if not root.is_dir():
        return [], []

    pending = [root]
    files: list[tuple[Path, Path]] = []
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name)
        except OSError as exc:
            relative = directory.relative_to(root).as_posix() or "."
            warnings.append(
                f"writer input directory skipped {relative}: {type(exc).__name__}"
            )
            continue
        for entry in entries:
            path = Path(entry.path)
            relative = path.relative_to(root)
            if _path_is_link_like(path):
                warnings.append(
                    f"writer input skipped symbolic link: {relative.as_posix()}"
                )
                continue
            try:
                resolved = path.resolve(strict=True)
                resolved.relative_to(root_resolved)
                resolved.relative_to(source_root_resolved)
            except (OSError, ValueError):
                warnings.append(
                    "writer input skipped path outside its sandbox: "
                    f"{relative.as_posix()}"
                )
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    pending.append(path)
                elif entry.is_file(follow_symlinks=False):
                    files.append((relative, path))
                else:
                    warnings.append(
                        "writer input skipped non-regular file: "
                        f"{relative.as_posix()}"
                    )
            except OSError as exc:
                warnings.append(
                    f"writer input skipped {relative.as_posix()}: "
                    f"{type(exc).__name__}"
                )
    return sorted(files, key=lambda item: item[0].as_posix()), warnings


def _copy_regular_file_without_links(
    *,
    source: Path,
    target: Path,
    source_root: Path,
) -> None:
    if _path_is_link_like(source_root) or _path_is_link_like(source):
        raise ValueError("source is a symbolic link or reparse point")
    source_root_resolved = source_root.resolve(strict=True)
    resolved = source.resolve(strict=True)
    resolved.relative_to(source_root_resolved)
    flags = (
        os.O_RDONLY
        | int(getattr(os, "O_BINARY", 0))
        | int(getattr(os, "O_NOFOLLOW", 0))
    )
    descriptor = os.open(source, flags)
    try:
        opened = os.fstat(descriptor)
        current = source.lstat()
        if not stat.S_ISREG(opened.st_mode) or (
            opened.st_dev,
            opened.st_ino,
        ) != (current.st_dev, current.st_ino):
            raise ValueError("source changed or is not a regular file")
        target.parent.mkdir(parents=True, exist_ok=True)
        source_handle = os.fdopen(descriptor, "rb")
        descriptor = -1
        try:
            with source_handle, target.open("wb") as target_handle:
                shutil.copyfileobj(
                    source_handle,
                    target_handle,
                    length=1024 * 1024,
                )
        except Exception:
            target.unlink(missing_ok=True)
            raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _copy_writer_output_snapshot(
    *,
    source_sandbox: Path,
    source_output: Path,
    target_root: Path,
    max_file_bytes: int = _WRITER_OUTPUT_MAX_FILE_BYTES,
    max_total_bytes: int = _WRITER_OUTPUT_MAX_TOTAL_BYTES,
) -> tuple[list[str], list[str]]:
    files, warnings = _safe_writer_tree_files(
        root=source_output,
        source_root=source_sandbox,
    )
    copied: list[str] = []
    total_bytes = 0
    for relative, source in files:
        if relative.name.lower().startswith("paper_target") or REPORT_ASSETS_DIR in {
            part.lower() for part in relative.parts
        }:
            continue
        try:
            size = source.stat().st_size
        except OSError as exc:
            warnings.append(
                f"writer output skipped {relative.as_posix()}: {type(exc).__name__}"
            )
            continue
        if size > max_file_bytes:
            warnings.append(
                f"writer output skipped {relative.as_posix()}: {size} bytes "
                "exceeds the per-file resource limit"
            )
            continue
        if total_bytes + size > max_total_bytes:
            warnings.append(
                f"writer output skipped {relative.as_posix()}: cumulative input "
                "exceeds the total resource limit"
            )
            continue
        try:
            _copy_regular_file_without_links(
                source=source,
                target=target_root / relative,
                source_root=source_sandbox,
            )
        except (OSError, ValueError) as exc:
            warnings.append(
                f"writer output skipped {relative.as_posix()}: {type(exc).__name__}"
            )
            continue
        total_bytes += size
        copied.append(relative.as_posix())
    return copied, warnings


def _looks_like_text_source(path: Path) -> bool:
    try:
        size = path.stat().st_size
        if size <= 0 or size > _WRITER_SOURCE_MAX_BYTES:
            return False
        sample = path.read_bytes()[:8192]
    except OSError:
        return False
    if b"\x00" in sample:
        return False
    try:
        sample.decode("utf-8-sig")
    except UnicodeDecodeError:
        return False
    return True


def _writer_source_paths(source_sandbox: Path) -> list[Path]:
    files, _ = _safe_writer_tree_files(
        root=source_sandbox,
        source_root=source_sandbox,
    )
    declared = _manifest_declared_source_paths(source_sandbox)
    paths: list[Path] = []
    for relative, path in files:
        relative_name = relative.as_posix()
        if relative.name.lower() in _WRITER_SOURCE_EXCLUDED_FILES:
            continue
        if any(
            part.lower() in _WRITER_SOURCE_EXCLUDED_DIRS
            for part in relative.parts[:-1]
        ):
            continue
        if relative_name not in declared and not _looks_like_text_source(path):
            continue
        try:
            if path.stat().st_size > _WRITER_SOURCE_MAX_BYTES:
                continue
        except OSError:
            continue
        paths.append(path)
    return paths


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _writer_source_inventory(source_sandbox: Path) -> list[dict[str, Any]]:
    inventory: list[dict[str, Any]] = []
    declared = _manifest_declared_source_paths(source_sandbox)
    for path in _writer_source_paths(source_sandbox):
        try:
            metadata = path.stat()
            relative = path.relative_to(source_sandbox).as_posix()
            inventory.append(
                {
                    "sandbox_relative_path": relative,
                    "size": metadata.st_size,
                    "declared_by_manifest": relative in declared,
                    "sha256": _sha256_file(path),
                    "ownership": (
                        "host_trusted"
                        if relative in TRUSTED_PROJECT_FILES
                        else "writer_owned"
                    ),
                }
            )
        except OSError:
            continue
    return inventory


def _copy_writer_source_snapshot(
    *,
    source_sandbox: Path,
    target_root: Path,
) -> tuple[list[dict[str, Any]], list[str]]:
    copied: list[dict[str, Any]] = []
    warnings: list[str] = []
    for item in _writer_source_inventory(source_sandbox):
        relative = str(item["sandbox_relative_path"])
        source = source_sandbox / Path(relative)
        target = target_root / Path(relative)
        try:
            _copy_regular_file_without_links(
                source=source,
                target=target,
                source_root=source_sandbox,
            )
        except (OSError, ValueError) as exc:
            warnings.append(
                f"writer source snapshot skipped {relative}: {type(exc).__name__}"
            )
            continue
        copied_item = dict(item)
        copied_item["path"] = (
            f"inputs/writer_output/{WRITER_SOURCE_DIR}/{Path(relative).as_posix()}"
        )
        copied.append(copied_item)
    return copied, warnings


def _file_inventory(
    root: Path,
    *,
    source_root: Path,
    max_file_bytes: int = _WRITER_OUTPUT_MAX_FILE_BYTES,
    max_total_bytes: int = _WRITER_OUTPUT_MAX_TOTAL_BYTES,
) -> list[dict[str, Any]]:
    files, warnings = _safe_writer_tree_files(root=root, source_root=source_root)
    inventory: list[dict[str, Any]] = [
        {"warning": warning} for warning in warnings
    ]
    total_bytes = 0
    for relative, path in files:
        if relative.name.lower().startswith("paper_target") or REPORT_ASSETS_DIR in {
            part.lower() for part in relative.parts
        }:
            continue
        try:
            file_stat = path.stat()
            if file_stat.st_size > max_file_bytes:
                inventory.append(
                    {
                        "path": relative.as_posix(),
                        "size": file_stat.st_size,
                        "skipped": "per_file_resource_limit",
                    }
                )
                continue
            if total_bytes + file_stat.st_size > max_total_bytes:
                inventory.append(
                    {
                        "path": relative.as_posix(),
                        "size": file_stat.st_size,
                        "skipped": "total_resource_limit",
                    }
                )
                continue
            inventory.append(
                {
                    "path": relative.as_posix(),
                    "size": file_stat.st_size,
                    "sha256": _sha256_file(path),
                }
            )
            total_bytes += file_stat.st_size
        except OSError:
            continue
    return inventory
