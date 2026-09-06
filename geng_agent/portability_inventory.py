"""Deterministic source inventory construction and verification."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Sequence

from .portability_contracts import (
    _PARENT_SEGMENT,
    _is_absolute_cross_platform,
    _issue,
)

_IGNORED_DIRECTORIES = frozenset({
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "node_modules",
    "venv",
})
_IGNORED_DIRECTORY_NAMES = frozenset(name.casefold() for name in _IGNORED_DIRECTORIES)
_IGNORED_SUFFIXES = frozenset({".pyc", ".pyo"})
_SOURCE_INVENTORY_NAME = "source_inventory.json"

def build_source_inventory(project_root: str | Path) -> dict[str, Any]:
    """Build a deterministic, content-addressed inventory of project files.

    Every path is POSIX-style and relative to ``project_root``.  Interpreter
    caches, virtual environments, VCS metadata, and Python bytecode are omitted.
    The root ``source_inventory.json`` is also omitted so that the inventory is
    not self-referential.  Symlinks are not followed; the validator reports them
    as portability blockers.
    """

    root = Path(project_root)
    if not root.is_dir():
        raise ValueError(f"project root is not a directory: {root}")

    files: list[dict[str, Any]] = []
    for path in _iter_regular_files(root):
        relative = path.relative_to(root).as_posix()
        if relative == _SOURCE_INVENTORY_NAME:
            continue
        files.append(
            {
                "path": relative,
                "sha256": _sha256_file(path),
                "size": path.stat().st_size,
            }
        )
    files.sort(key=lambda item: str(item["path"]))
    return {
        "schema_version": "1.0",
        "files": files,
        "inventory_sha256": _inventory_digest(files),
    }

def _iter_regular_files(root: Path) -> Iterable[Path]:
    for directory, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        directory_path = Path(directory)
        dirnames[:] = sorted(
            name
            for name in dirnames
            if not _is_ignored_directory(name) and not (directory_path / name).is_symlink()
        )
        for filename in sorted(filenames):
            path = directory_path / filename
            if _is_ignored_file(path) or path.is_symlink() or not path.is_file():
                continue
            yield path

def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

def _inventory_digest(files: Sequence[Mapping[str, Any]]) -> str:
    encoded = json.dumps(
        list(files),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()

def _source_inventory_issues(
    root: Path,
    actual_inventory: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Validate the committed inventory against all package files except itself."""

    path = root / _SOURCE_INVENTORY_NAME
    if not path.is_file() or path.is_symlink():
        return [
            _issue(
                "source_inventory_missing",
                _SOURCE_INVENTORY_NAME,
                "portable package must include a regular source_inventory.json",
            )
        ]
    try:
        document = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return [
            _issue(
                "source_inventory_invalid",
                _SOURCE_INVENTORY_NAME,
                f"source inventory cannot be read as JSON: {exc}",
            )
        ]
    if not isinstance(document, Mapping):
        return [
            _issue(
                "source_inventory_invalid",
                _SOURCE_INVENTORY_NAME,
                "source inventory must be a JSON object",
            )
        ]

    issues: list[dict[str, Any]] = []
    declared_files = document.get("files")
    if document.get("schema_version") != "1.0" or not isinstance(declared_files, list):
        issues.append(
            _issue(
                "source_inventory_invalid",
                _SOURCE_INVENTORY_NAME,
                "source inventory must use schema_version 1.0 and contain a files list",
            )
        )
        return issues

    normalized_files: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for index, raw_item in enumerate(declared_files):
        location = f"$.files[{index}]"
        if not isinstance(raw_item, Mapping):
            issues.append(
                _issue(
                    "source_inventory_invalid",
                    _SOURCE_INVENTORY_NAME,
                    "inventory file entry must be an object",
                    location=location,
                )
            )
            continue
        relative = raw_item.get("path")
        sha256 = raw_item.get("sha256")
        size = raw_item.get("size")
        if (
            not isinstance(relative, str)
            or not relative
            or relative == _SOURCE_INVENTORY_NAME
            or _is_absolute_cross_platform(relative)
            or _PARENT_SEGMENT.search(relative.replace("\\", "/"))
            or "\\" in relative
            or relative in seen_paths
            or not isinstance(sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", sha256) is None
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
        ):
            issues.append(
                _issue(
                    "source_inventory_invalid",
                    _SOURCE_INVENTORY_NAME,
                    "inventory entries require a unique POSIX relative path, SHA-256, and non-negative size",
                    location=location,
                )
            )
            continue
        seen_paths.add(relative)
        normalized_files.append({"path": relative, "sha256": sha256, "size": size})

    if issues:
        return issues
    if normalized_files != sorted(normalized_files, key=lambda item: item["path"]):
        issues.append(
            _issue(
                "source_inventory_invalid",
                _SOURCE_INVENTORY_NAME,
                "inventory files must be sorted by path",
            )
        )
    declared_digest = document.get("inventory_sha256")
    calculated_declared_digest = _inventory_digest(normalized_files)
    if declared_digest != calculated_declared_digest:
        issues.append(
            _issue(
                "source_inventory_digest_mismatch",
                _SOURCE_INVENTORY_NAME,
                "inventory_sha256 does not match the declared file entries",
            )
        )

    actual_files = actual_inventory.get("files")
    actual_digest = actual_inventory.get("inventory_sha256")
    if normalized_files != actual_files or declared_digest != actual_digest:
        issues.append(
            _issue(
                "source_inventory_content_mismatch",
                _SOURCE_INVENTORY_NAME,
                "source inventory does not match current package files (excluding itself)",
            )
        )
    return issues

def _is_ignored_directory(name: str) -> bool:
    return name.casefold() in _IGNORED_DIRECTORY_NAMES

def _is_ignored_file(path: Path) -> bool:
    return path.suffix.lower() in _IGNORED_SUFFIXES
