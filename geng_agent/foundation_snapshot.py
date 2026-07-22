from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any


FOUNDATION_SCHEMA_VERSION = "1.0"
FOUNDATION_WORKFLOW_VERSION = "2"
FOUNDATION_CONTRACT_VERSION = "1"

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TOP_LEVEL_FILES = {"requirements.txt", "README.foundation.md"}
_CONFIG_SUFFIXES = {".json", ".yaml", ".yml"}


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def path_is_foundation_link(path: Path) -> bool:
    """Detect symlinks and Windows reparse points, including dangling links."""

    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse_flag)


def scan_foundation_tree(root: Path) -> tuple[list[Path], list[Path], list[Path], list[Path]]:
    """Walk a tree without traversing any symlink, junction, or reparse point."""

    files: list[Path] = []
    directories: list[Path] = []
    links: list[Path] = []
    special: list[Path] = []
    pending = [root]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as iterator:
            entries = sorted(iterator, key=lambda entry: entry.name)
        for entry in entries:
            path = Path(entry.path)
            if path_is_foundation_link(path):
                links.append(path)
            elif entry.is_dir(follow_symlinks=False):
                directories.append(path)
                pending.append(path)
            elif entry.is_file(follow_symlinks=False):
                files.append(path)
            else:
                special.append(path)
    return sorted(files), sorted(directories), sorted(links), sorted(special)


def foundation_snapshot_hash(files: list[dict[str, Any]]) -> str:
    payload = [
        {"path": item.get("path"), "sha256": item.get("sha256"), "bytes": item.get("bytes")}
        for item in files
    ]
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def is_foundation_frozen_path(relative: str) -> bool:
    return relative.startswith(("src/", "tests/", "configs/foundation")) or relative == "README.foundation.md"


def validate_foundation_relpath(raw: Any) -> str:
    """Return one canonical POSIX path or fail before touching the filesystem."""

    if not isinstance(raw, str) or not raw or raw != raw.strip():
        raise ValueError("path must be a non-empty canonical string")
    if "\x00" in raw or "\\" in raw or "//" in raw:
        raise ValueError("path contains a forbidden separator or NUL")
    parts = raw.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("path contains an empty, dot, or parent segment")
    posix = PurePosixPath(raw)
    windows = PureWindowsPath(raw)
    if posix.is_absolute() or windows.is_absolute() or windows.drive or windows.root:
        raise ValueError("path must be relative")
    if posix.as_posix() != raw or any(":" in part for part in parts):
        raise ValueError("path is not canonical or contains a Windows stream/drive marker")
    if any(PureWindowsPath(part).is_reserved() for part in parts):
        raise ValueError("path contains a reserved Windows name")

    allowed = False
    if raw in _TOP_LEVEL_FILES:
        allowed = True
    elif len(parts) >= 2 and parts[0] == "src":
        allowed = posix.suffix == ".py" and posix.name not in {"_io.py", "_backend.py"}
    elif len(parts) >= 2 and parts[0] == "tests":
        allowed = posix.suffix == ".py"
    elif len(parts) == 2 and parts[0] == "configs":
        allowed = parts[1].startswith("foundation") and posix.suffix.lower() in _CONFIG_SUFFIXES
    if not allowed:
        raise ValueError("path is outside Foundation ownership")
    return raw


def resolve_foundation_path(
    root: Path,
    relative: Any,
    *,
    require_file: bool = False,
) -> Path:
    """Resolve a validated path without following a symlink below the root."""

    normalized = validate_foundation_relpath(relative)
    root = root.expanduser()
    if path_is_foundation_link(root):
        raise ValueError("Foundation root must not be a symlink")
    root_resolved = root.resolve(strict=False)
    candidate = root.joinpath(*PurePosixPath(normalized).parts)

    current = root
    for part in PurePosixPath(normalized).parts:
        current = current / part
        if path_is_foundation_link(current):
            raise ValueError(f"Foundation path contains a symlink: {normalized}")
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise ValueError("Foundation path escapes its root") from exc
    if require_file and (path_is_foundation_link(candidate) or not candidate.is_file()):
        raise ValueError("Foundation path is not a regular file")
    return candidate


def validate_foundation_manifest(
    manifest: Any,
    *,
    expected_input_hash: str | None = None,
    expected_required_modules: set[str] | None = None,
) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    if not isinstance(manifest, dict):
        return [{"path": "$", "message": "foundation manifest must be a JSON object"}]

    expected_fields = {
        "schema_version": FOUNDATION_SCHEMA_VERSION,
        "workflow_version": FOUNDATION_WORKFLOW_VERSION,
        "contract_version": FOUNDATION_CONTRACT_VERSION,
    }
    for field, expected in expected_fields.items():
        if manifest.get(field) != expected:
            issues.append({"path": f"$.{field}", "message": f"must equal {expected}"})
    for field in ("input_hash", "analysis_snapshot_hash", "snapshot_hash"):
        value = manifest.get(field)
        if not isinstance(value, str) or not _SHA256.fullmatch(value):
            issues.append({"path": f"$.{field}", "message": "must be a lowercase SHA-256"})
    if expected_input_hash is not None and manifest.get("input_hash") != expected_input_hash:
        issues.append({"path": "$.input_hash", "message": "does not match current Foundation inputs"})

    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        issues.append({"path": "$.files", "message": "must be a non-empty list"})
        return issues

    canonical_files: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(files):
        base = f"$.files[{index}]"
        if not isinstance(item, dict):
            issues.append({"path": base, "message": "must be an object"})
            continue
        try:
            relative = validate_foundation_relpath(item.get("path"))
        except ValueError as exc:
            issues.append({"path": f"{base}.path", "message": str(exc)})
            continue
        folded = relative.casefold()
        if folded in seen:
            issues.append({"path": f"{base}.path", "message": "duplicate or case-colliding path"})
        seen.add(folded)
        sha256 = item.get("sha256")
        size = item.get("bytes")
        if not isinstance(sha256, str) or not _SHA256.fullmatch(sha256):
            issues.append({"path": f"{base}.sha256", "message": "must be a lowercase SHA-256"})
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            issues.append({"path": f"{base}.bytes", "message": "must be a non-negative integer"})
        canonical_files.append({"path": relative, "sha256": sha256, "bytes": size})

    if len(canonical_files) == len(files):
        canonical_paths = [str(item["path"]) for item in canonical_files]
        if canonical_paths != sorted(canonical_paths):
            issues.append({"path": "$.files", "message": "must be sorted by canonical path"})

        actual_snapshot_hash = foundation_snapshot_hash(canonical_files)
        if manifest.get("snapshot_hash") != actual_snapshot_hash:
            issues.append({"path": "$.snapshot_hash", "message": "does not match the canonical file manifest"})
        expected_frozen = [item for item in canonical_files if is_foundation_frozen_path(str(item["path"]))]
        if manifest.get("frozen_files") != expected_frozen:
            issues.append({"path": "$.frozen_files", "message": "must equal the frozen subset derived from files"})

        file_paths = {str(item["path"]) for item in canonical_files}
        required_modules = manifest.get("required_modules")
        if (
            not isinstance(required_modules, list)
            or not required_modules
            or any(not isinstance(item, str) for item in required_modules)
            or required_modules != sorted(set(required_modules))
        ):
            issues.append({"path": "$.required_modules", "message": "must be a sorted non-empty unique string list"})
        else:
            for index, relative in enumerate(required_modules):
                try:
                    normalized = validate_foundation_relpath(relative)
                except ValueError as exc:
                    issues.append({"path": f"$.required_modules[{index}]", "message": str(exc)})
                    continue
                if not normalized.startswith("src/") or PurePosixPath(normalized).suffix != ".py":
                    issues.append(
                        {
                            "path": f"$.required_modules[{index}]",
                            "message": "required module must be a Foundation Python module under src/",
                        }
                    )
                if normalized not in file_paths:
                    issues.append({"path": f"$.required_modules[{index}]", "message": "required module is absent from files"})
            if expected_required_modules is not None and required_modules != sorted(expected_required_modules):
                issues.append(
                    {"path": "$.required_modules", "message": "does not match the current scientific architecture"}
                )

    validation = manifest.get("validation")
    if not isinstance(validation, dict):
        issues.append({"path": "$.validation", "message": "must be an object"})
    else:
        if validation.get("tests_passed") is not True:
            issues.append({"path": "$.validation.tests_passed", "message": "must be true"})
        if validation.get("local_imports_resolve") is not True:
            issues.append({"path": "$.validation.local_imports_resolve", "message": "must be true"})
    return _dedupe(issues)


def validate_foundation_snapshot(
    manifest: Any,
    snapshot_dir: Path,
    *,
    expected_input_hash: str | None = None,
    expected_required_modules: set[str] | None = None,
) -> list[dict[str, str]]:
    issues = validate_foundation_manifest(
        manifest,
        expected_input_hash=expected_input_hash,
        expected_required_modules=expected_required_modules,
    )
    if issues:
        return issues
    assert isinstance(manifest, dict)
    files = manifest["files"]

    try:
        unsafe_root = path_is_foundation_link(snapshot_dir)
        is_directory = snapshot_dir.is_dir()
    except OSError as exc:
        return [{"path": str(snapshot_dir), "message": f"cannot inspect Foundation snapshot directory: {exc}"}]
    if unsafe_root or not is_directory:
        return [{"path": str(snapshot_dir), "message": "Foundation snapshot directory is missing or is a link"}]

    expected_paths = {str(item["path"]) for item in files}
    try:
        disk_files, _, links, special = scan_foundation_tree(snapshot_dir)
    except OSError as exc:
        return [{"path": str(snapshot_dir), "message": f"cannot scan Foundation snapshot: {exc}"}]
    actual_paths = {path.relative_to(snapshot_dir).as_posix() for path in disk_files}
    for path in links:
        issues.append(
            {
                "path": path.relative_to(snapshot_dir).as_posix(),
                "message": "Foundation snapshot contains a link or reparse point",
            }
        )
    for path in special:
        issues.append(
            {
                "path": path.relative_to(snapshot_dir).as_posix(),
                "message": "Foundation snapshot contains a non-regular filesystem entry",
            }
        )

    for index, item in enumerate(files):
        relative = str(item["path"])
        try:
            path = resolve_foundation_path(snapshot_dir, relative, require_file=True)
            actual_size = path.stat().st_size
            actual_hash = file_sha256(path) if actual_size == item["bytes"] else None
        except (OSError, ValueError) as exc:
            issues.append(
                {
                    "path": f"$.files[{index}].path",
                    "message": f"cannot inspect snapshot file: {exc}",
                }
            )
            continue
        if actual_size != item["bytes"]:
            issues.append({"path": relative, "message": "snapshot file size does not match manifest"})
        elif actual_hash != item["sha256"]:
            issues.append({"path": relative, "message": "snapshot file hash does not match manifest"})

    for relative in sorted(actual_paths - expected_paths):
        issues.append({"path": relative, "message": "unexpected file exists in Foundation snapshot"})
    for relative in sorted(expected_paths - actual_paths):
        issues.append({"path": relative, "message": "Foundation snapshot file is missing"})
    return _dedupe(issues)


def _dedupe(issues: list[dict[str, str]]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for issue in issues:
        key = (str(issue.get("path") or ""), str(issue.get("message") or ""))
        if key not in seen:
            seen.add(key)
            result.append({"path": key[0], "message": key[1]})
    return result
