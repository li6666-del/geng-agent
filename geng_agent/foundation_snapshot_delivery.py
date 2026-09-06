"""Foundation snapshot validation, installation, freezing, and restoration."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any

from .foundation_execution_policy import TRUSTED_PROJECT_FILES as _TRUSTED_PROJECT_FILES
from .foundation_snapshot import (
    file_sha256,
    foundation_snapshot_hash,
    is_foundation_frozen_path,
    path_is_foundation_link,
    resolve_foundation_path,
    scan_foundation_tree,
    validate_foundation_relpath,
    validate_foundation_snapshot,
)
from .io_runtime import BACKEND_RUNTIME_PY, IO_RUNTIME_PY
from .json_utils import pretty_json
from .task_writer_support import PAPER_EVIDENCE_DIR


_FOUNDATION_WRITER_DELIVERY_RECEIPT = ".foundation-writer-delivery.json"
_FOUNDATION_WRITER_DELIVERY_SCHEMA_VERSION = "1"
_PYTHON_RUNTIME_CACHE_SUFFIXES = frozenset({".pyc", ".pyo"})


def _publish_directory_with_rollback(*, staging: Path, target: Path) -> None:
    """Publish a sibling directory and restore the previous target on failure."""

    target.parent.mkdir(parents=True, exist_ok=True)
    backup = target.with_name(f".{target.name}.previous")
    if path_is_foundation_link(staging) or not staging.is_dir():
        raise RuntimeError("Foundation directory publication staging path is unsafe")
    for path, label in ((target, "target"), (backup, "backup")):
        if path_is_foundation_link(path):
            raise RuntimeError(
                f"Foundation directory publication {label} is a link or reparse point"
            )
        if path.exists() and not path.is_dir():
            raise RuntimeError(
                f"Foundation directory publication {label} is not a directory"
            )
    if not target.exists() and backup.exists():
        os.replace(backup, target)
    elif target.exists() and backup.exists():
        shutil.rmtree(backup)
    moved_previous = False
    if target.exists():
        os.replace(target, backup)
        moved_previous = True
    try:
        os.replace(staging, target)
    except Exception:
        if moved_previous and backup.exists() and not target.exists():
            os.replace(backup, target)
        raise
    else:
        if backup.exists():
            shutil.rmtree(backup, ignore_errors=True)


def validate_foundation_bundle(
    foundation: Any,
    *,
    expected_input_hash: str | None = None,
    expected_required_modules: set[str] | None = None,
) -> list[dict[str, str]]:
    """Validate both the outer hand-off record and its immutable snapshot."""

    if not isinstance(foundation, dict):
        return [{"path": "$", "message": "Foundation hand-off must be an object"}]
    manifest = foundation.get("manifest") if isinstance(foundation.get("manifest"), dict) else {}
    issues: list[dict[str, str]] = []
    if foundation.get("snapshot_hash") != manifest.get("snapshot_hash"):
        issues.append(
            {
                "path": "$.snapshot_hash",
                "message": "outer Foundation snapshot hash does not match its manifest",
            }
        )
    raw_snapshot_dir = foundation.get("snapshot_dir")
    if not isinstance(raw_snapshot_dir, str) or not raw_snapshot_dir.strip():
        issues.append({"path": "$.snapshot_dir", "message": "must be a non-empty path string"})
        return issues
    issues.extend(
        validate_foundation_snapshot(
            manifest,
            Path(raw_snapshot_dir),
            expected_input_hash=expected_input_hash,
            expected_required_modules=expected_required_modules,
        )
    )
    return issues


def install_foundation_snapshot(target: Path, foundation: dict[str, Any]) -> set[str]:
    """Install a validated snapshot without following manifest-controlled links."""

    issues = validate_foundation_bundle(foundation)
    if issues:
        raise RuntimeError(f"foundation snapshot validation failed: {issues[:5]}")
    manifest = foundation["manifest"]
    snapshot_dir = Path(foundation["snapshot_dir"])

    target.mkdir(parents=True, exist_ok=True)
    installed: set[str] = set()
    for item in manifest["files"]:
        relative = validate_foundation_relpath(item["path"])
        source = resolve_foundation_path(snapshot_dir, relative, require_file=True)
        destination = resolve_foundation_path(target, relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination = resolve_foundation_path(target, relative)
        if path_is_foundation_link(destination) or (destination.exists() and not destination.is_file()):
            raise RuntimeError(f"unsafe Foundation destination: {relative}")

        descriptor, temp_name = tempfile.mkstemp(
            prefix=f".{destination.name}.foundation-",
            dir=destination.parent,
        )
        os.close(descriptor)
        temp_path = Path(temp_name)
        try:
            shutil.copy2(source, temp_path)
            if temp_path.stat().st_size != item["bytes"] or file_sha256(temp_path) != item["sha256"]:
                raise RuntimeError(f"Foundation file changed during installation: {relative}")
            if path_is_foundation_link(destination) or (destination.exists() and not destination.is_file()):
                raise RuntimeError(f"unsafe Foundation destination: {relative}")
            os.replace(temp_path, destination)
        finally:
            temp_path.unlink(missing_ok=True)
        installed.add(relative)
    ownership_path = target / "foundation_manifest.json"
    if path_is_foundation_link(ownership_path) or (
        ownership_path.exists() and not ownership_path.is_file()
    ):
        raise RuntimeError("unsafe installed Foundation ownership manifest")
    # This is navigation for Writers, never the authority used by the host's
    # integrity checks (which retain the validated external bundle).
    _write_foundation_manifest(ownership_path, manifest)
    return installed



def _is_restricted_project_path(relative: str) -> bool:
    parts = relative.split("/")
    if relative in {"src", "tests"} or relative.startswith(("src/", "tests/")):
        return True
    return len(parts) >= 2 and parts[0] == "configs" and parts[1].startswith("foundation")


def _is_python_runtime_cache_file(relative: str) -> bool:
    """Return whether a restricted-tree file is disposable Python bytecode.

    ``__pycache__`` directories themselves are not integrity entries.  Only
    bytecode files are ignored so a source file, link, or other unexpected
    payload placed below such a directory remains visible to the integrity
    scan.
    """

    parts = relative.split("/")
    return (
        bool(parts)
        and parts[0] in {"src", "tests"}
        and Path(parts[-1]).suffix.casefold() in _PYTHON_RUNTIME_CACHE_SUFFIXES
    )


def _scan_restricted_project(
    project_dir: Path,
) -> tuple[dict[str, Path], list[Path], list[dict[str, str]]]:
    actual_files: dict[str, Path] = {}
    directories: list[Path] = []
    issues: list[dict[str, str]] = []
    try:
        if path_is_foundation_link(project_dir):
            return {}, [], [{"file": ".", "message": "project root is a link or reparse point"}]
        if project_dir.exists() and not project_dir.is_dir():
            return {}, [], [{"file": ".", "message": "project root is not a directory"}]
    except OSError as exc:
        return {}, [], [{"file": ".", "message": f"cannot inspect project root: {exc}"}]

    for root_name in ("src", "tests", "configs"):
        root = project_dir / root_name
        try:
            if path_is_foundation_link(root):
                issues.append({"file": root_name, "message": "Foundation-owned directory is a link or reparse point"})
                continue
            if not root.exists():
                continue
            if not root.is_dir():
                issues.append({"file": root_name, "message": "Foundation-owned path is not a directory"})
                continue
            files, found_directories, links, special = scan_foundation_tree(root)
        except OSError as exc:
            issues.append({"file": root_name, "message": f"cannot scan Foundation-owned directory: {exc}"})
            continue

        for path in found_directories:
            relative = path.relative_to(project_dir).as_posix()
            if _is_restricted_project_path(relative):
                directories.append(path)
        for path in links:
            relative = path.relative_to(project_dir).as_posix()
            if _is_restricted_project_path(relative):
                issues.append({"file": relative, "message": "frozen Foundation tree contains a link or reparse point"})
        for path in special:
            relative = path.relative_to(project_dir).as_posix()
            if _is_restricted_project_path(relative):
                issues.append({"file": relative, "message": "frozen Foundation tree contains a non-regular entry"})
        for path in files:
            relative = path.relative_to(project_dir).as_posix()
            if _is_restricted_project_path(relative):
                actual_files[relative] = path
    return actual_files, directories, issues


def _is_private_source_path(
    relative: str,
    *,
    frozen_paths: set[str],
    manifest: dict[str, Any],
) -> bool:
    """Allow private modules without allowing import shadowing of frozen code."""

    if not isinstance(manifest.get("scope"), dict):
        return False
    if not relative.startswith("src/") or not relative.endswith(".py"):
        return False
    if relative in frozen_paths or relative in _TRUSTED_PROJECT_FILES:
        return False
    key = relative[:-12] if relative.endswith("/__init__.py") else relative[:-3]
    for frozen in frozen_paths | _TRUSTED_PROJECT_FILES:
        if not frozen.startswith("src/") or not frozen.endswith(".py"):
            continue
        frozen_key = frozen[:-12] if frozen.endswith("/__init__.py") else frozen[:-3]
        if key == frozen_key or frozen_key.startswith(key + "/"):
            return False
    return True


def foundation_private_source_paths(project_dir: Path, foundation: dict[str, Any]) -> list[str]:
    """Enumerate safe private source files for the final project merger."""

    manifest = foundation.get("manifest", {})
    frozen = {str(item["path"]) for item in manifest.get("frozen_files", [])}
    actual_files, _, issues = _scan_restricted_project(project_dir)
    if issues:
        raise RuntimeError(f"unsafe private source tree: {issues[:5]}")
    return sorted(
        relative for relative in actual_files
        if _is_private_source_path(relative, frozen_paths=frozen, manifest=manifest)
    )


def foundation_violations(project_dir: Path, foundation: dict[str, Any]) -> list[dict[str, str]]:
    bundle_issues = validate_foundation_bundle(foundation)
    if bundle_issues:
        return [
            {"file": str(item.get("path") or "foundation_manifest.json"), "message": str(item.get("message") or "invalid manifest")}
            for item in bundle_issues
        ]

    manifest = foundation["manifest"]
    frozen = {str(item["path"]): str(item["sha256"]) for item in manifest["frozen_files"]}
    actual_files, _, scan_issues = _scan_restricted_project(project_dir)
    issues = list(scan_issues)
    for relative, expected in sorted(frozen.items()):
        try:
            path = resolve_foundation_path(project_dir, relative)
            if not path.is_file():
                issues.append({"file": relative, "message": "frozen foundation file was deleted"})
            elif file_sha256(path) != expected:
                issues.append({"file": relative, "message": "frozen foundation file was modified"})
        except (OSError, ValueError) as exc:
            issues.append({"file": relative, "message": str(exc)})

    allowed_files = set(frozen) | _TRUSTED_PROJECT_FILES
    for relative in sorted(set(actual_files) - allowed_files):
        if _is_python_runtime_cache_file(relative):
            continue
        if _is_private_source_path(relative, frozen_paths=set(frozen), manifest=manifest):
            continue
        issues.append({"file": relative, "message": "task writer created a file inside frozen Foundation ownership"})
    return issues


def restore_foundation_snapshot(project_dir: Path, foundation: dict[str, Any]) -> None:
    bundle_issues = validate_foundation_bundle(foundation)
    if bundle_issues:
        raise RuntimeError(f"cannot restore invalid Foundation snapshot: {bundle_issues[:5]}")

    manifest = foundation["manifest"]
    frozen_paths = {str(item["path"]) for item in manifest["frozen_files"]}
    actual_files, directories, scan_issues = _scan_restricted_project(project_dir)
    if scan_issues:
        raise RuntimeError(f"cannot restore Foundation through unsafe project paths: {scan_issues[:5]}")

    root_resolved = project_dir.resolve(strict=False)
    unexpected = sorted(set(actual_files) - frozen_paths - _TRUSTED_PROJECT_FILES)
    for relative in unexpected:
        if _is_private_source_path(relative, frozen_paths=frozen_paths, manifest=manifest):
            continue
        path = actual_files[relative]
        try:
            if path_is_foundation_link(path):
                raise ValueError("restricted file became a link during restore")
            path.resolve(strict=False).relative_to(root_resolved)
            path.unlink()
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"cannot safely remove unexpected Foundation file {relative}: {exc}") from exc

    for directory in sorted(directories, key=lambda path: len(path.parts), reverse=True):
        try:
            if path_is_foundation_link(directory):
                raise ValueError("restricted directory became a link during restore")
            directory.resolve(strict=False).relative_to(root_resolved)
            directory.rmdir()
        except FileNotFoundError:
            continue
        except OSError:
            # Expected parent directories and non-empty task-owned config trees remain.
            continue
        except ValueError as exc:
            raise RuntimeError(f"cannot safely clean Foundation directory {directory}: {exc}") from exc

    install_foundation_snapshot(project_dir, foundation)

def _copy_foundation_snapshot(*, sandbox: Path, snapshot_dir: Path) -> list[dict[str, Any]]:
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    files: list[dict[str, Any]] = []
    for path in _foundation_project_files(sandbox):
        relative = validate_foundation_relpath(path.relative_to(sandbox).as_posix())
        target = resolve_foundation_path(snapshot_dir, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target = resolve_foundation_path(snapshot_dir, relative)
        shutil.copy2(path, target)
        files.append({"path": relative, "sha256": file_sha256(target), "bytes": target.stat().st_size})
    return files


def _publish_foundation_snapshot(*, sandbox: Path, snapshot_dir: Path) -> list[dict[str, Any]]:
    """Build a complete snapshot off to the side before publishing its path."""

    snapshot_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{snapshot_dir.name}.staging-",
            dir=snapshot_dir.parent,
        )
    )
    try:
        files = _copy_foundation_snapshot(sandbox=sandbox, snapshot_dir=staging)
        _publish_directory_with_rollback(staging=staging, target=snapshot_dir)
        return files
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def _writer_delivery_relpath(value: Any) -> str:
    if value == "foundation_result.json":
        return value
    return validate_foundation_relpath(value)


def _writer_delivery_files(sandbox: Path) -> list[Path]:
    files = list(_foundation_project_files(sandbox))
    result_path = sandbox / "foundation_result.json"
    if path_is_foundation_link(result_path):
        raise RuntimeError("Foundation Writer result is a link or reparse point")
    if result_path.is_file():
        files.append(result_path)
    return sorted(files, key=lambda path: path.relative_to(sandbox).as_posix())


def persist_foundation_writer_delivery(
    *,
    sandbox: Path,
    delivery_dir: Path,
    input_hash: str,
    analysis_hash: str,
    environment_hash: str,
    required_modules: set[str],
    trusted_changed: list[str],
) -> dict[str, Any]:
    """Persist the pristine completed Writer hand-off before host tests can mutate it."""

    _assert_foundation_sandbox_layout_safe(sandbox)
    delivery_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{delivery_dir.name}.staging-",
            dir=delivery_dir.parent,
        )
    )
    try:
        files: list[dict[str, Any]] = []
        for source in _writer_delivery_files(sandbox):
            relative = _writer_delivery_relpath(source.relative_to(sandbox).as_posix())
            destination = staging.joinpath(*relative.split("/"))
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            files.append(
                {
                    "path": relative,
                    "sha256": file_sha256(destination),
                    "bytes": destination.stat().st_size,
                }
            )
        receipt = {
            "schema_version": _FOUNDATION_WRITER_DELIVERY_SCHEMA_VERSION,
            "input_hash": input_hash,
            "analysis_snapshot_hash": analysis_hash,
            "environment_lock_hash": environment_hash,
            "snapshot_hash": foundation_snapshot_hash(files),
            "files": files,
            "required_modules": sorted(required_modules),
            "trusted_changed": sorted(set(trusted_changed)),
        }
        (staging / _FOUNDATION_WRITER_DELIVERY_RECEIPT).write_text(
            pretty_json(receipt),
            encoding="utf-8",
        )
        _assert_foundation_sandbox_layout_safe(staging)
        _publish_directory_with_rollback(staging=staging, target=delivery_dir)
        return receipt
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def load_foundation_writer_delivery(
    *,
    delivery_dir: Path,
    expected_input_hash: str,
    expected_required_modules: set[str],
) -> dict[str, Any] | None:
    """Load only a complete, content-verified Writer delivery receipt."""

    try:
        _assert_foundation_sandbox_layout_safe(delivery_dir)
        receipt_path = delivery_dir / _FOUNDATION_WRITER_DELIVERY_RECEIPT
        if path_is_foundation_link(receipt_path) or not receipt_path.is_file():
            return None
        receipt = json.loads(receipt_path.read_text(encoding="utf-8-sig"))
        if (
            not isinstance(receipt, dict)
            or receipt.get("schema_version") != _FOUNDATION_WRITER_DELIVERY_SCHEMA_VERSION
            or receipt.get("input_hash") != expected_input_hash
            or receipt.get("required_modules") != sorted(expected_required_modules)
        ):
            return None
        raw_files = receipt.get("files")
        if not isinstance(raw_files, list) or not raw_files:
            return None
        files: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in raw_files:
            if not isinstance(raw, dict):
                return None
            relative = _writer_delivery_relpath(raw.get("path"))
            if relative.casefold() in seen:
                return None
            seen.add(relative.casefold())
            source = delivery_dir.joinpath(*relative.split("/"))
            if path_is_foundation_link(source) or not source.is_file():
                return None
            size = raw.get("bytes")
            digest = raw.get("sha256")
            if (
                isinstance(size, bool)
                or not isinstance(size, int)
                or size < 0
                or source.stat().st_size != size
                or not isinstance(digest, str)
                or file_sha256(source) != digest
            ):
                return None
            files.append({"path": relative, "sha256": digest, "bytes": size})
        if files != sorted(files, key=lambda item: str(item["path"])):
            return None
        if receipt.get("snapshot_hash") != foundation_snapshot_hash(files):
            return None
        if "foundation_result.json" not in seen:
            return None
        trusted_changed = receipt.get("trusted_changed")
        if not isinstance(trusted_changed, list) or any(
            not isinstance(item, str) for item in trusted_changed
        ):
            return None
        return receipt
    except (OSError, RuntimeError, ValueError, UnicodeError, json.JSONDecodeError):
        return None


def restore_foundation_writer_delivery(
    *,
    delivery_dir: Path,
    receipt: dict[str, Any],
    sandbox: Path,
) -> None:
    """Restore a fresh validation sandbox from the immutable Writer receipt."""

    sandbox.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{sandbox.name}.restore-",
            dir=sandbox.parent,
        )
    )
    try:
        for item in receipt["files"]:
            relative = _writer_delivery_relpath(item["path"])
            source = delivery_dir.joinpath(*relative.split("/"))
            destination = staging.joinpath(*relative.split("/"))
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            if (
                destination.stat().st_size != item["bytes"]
                or file_sha256(destination) != item["sha256"]
            ):
                raise RuntimeError(f"Foundation Writer delivery changed during restore: {relative}")
        _restore_trusted_runtime_atomically(staging)
        _assert_foundation_sandbox_layout_safe(staging)
        _publish_directory_with_rollback(staging=staging, target=sandbox)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def _foundation_project_files(sandbox: Path) -> list[Path]:
    _assert_foundation_sandbox_layout_safe(sandbox)
    candidates: set[Path] = set()
    for root_name in ("src", "tests", "configs"):
        root = sandbox / root_name
        if path_is_foundation_link(root):
            raise RuntimeError(f"Foundation-owned directory is a link or reparse point: {root_name}")
        if not root.is_dir():
            continue
        files, _, links, special = scan_foundation_tree(root)
        if links:
            relative = links[0].relative_to(sandbox).as_posix()
            raise RuntimeError(f"Foundation output contains a link or reparse point: {relative}")
        if special:
            relative = special[0].relative_to(sandbox).as_posix()
            raise RuntimeError(f"Foundation output contains a non-regular entry: {relative}")
        for path in files:
            if "__pycache__" in path.parts:
                continue
            relative = path.relative_to(sandbox).as_posix()
            if relative == "tests/runtime_artifacts" or relative.startswith(
                "tests/runtime_artifacts/"
            ):
                continue
            if relative in _TRUSTED_PROJECT_FILES:
                continue
            validate_foundation_relpath(relative)
            candidates.add(path)
    for name in ("requirements.txt", "README.foundation.md"):
        path = sandbox / name
        if path_is_foundation_link(path):
            raise RuntimeError(f"Foundation output contains a link or reparse point: {name}")
        if path.is_file():
            validate_foundation_relpath(name)
            candidates.add(path)
    return sorted(
        candidates,
        key=lambda path: path.relative_to(sandbox).as_posix(),
    )


def _assert_foundation_sandbox_layout_safe(sandbox: Path) -> None:
    """Reject links, special files, and agent-owned hardlinks without traversal."""

    if path_is_foundation_link(sandbox):
        raise RuntimeError("Foundation sandbox root is a link or reparse point")
    if not sandbox.is_dir():
        raise RuntimeError("Foundation sandbox root is not a directory")
    files, _, links, special = scan_foundation_tree(sandbox)
    if links:
        relative = links[0].relative_to(sandbox).as_posix()
        raise RuntimeError(f"Foundation output contains a link or reparse point: {relative}")
    if special:
        relative = special[0].relative_to(sandbox).as_posix()
        raise RuntimeError(f"Foundation output contains a non-regular entry: {relative}")
    for path in files:
        relative_path = path.relative_to(sandbox)
        if relative_path.parts and relative_path.parts[0] == PAPER_EVIDENCE_DIR:
            continue
        if path.lstat().st_nlink > 1:
            relative = relative_path.as_posix()
            raise RuntimeError(f"Foundation output contains a hard-linked regular file: {relative}")


def _restore_trusted_runtime_atomically(sandbox: Path) -> None:
    """Restore host-owned runtime files without ever opening their targets."""

    if path_is_foundation_link(sandbox):
        raise RuntimeError("Foundation sandbox root is a link or reparse point")
    src_dir = sandbox / "src"
    if path_is_foundation_link(src_dir):
        raise RuntimeError("Foundation-owned directory is a link or reparse point: src")
    if not src_dir.is_dir():
        raise RuntimeError("Foundation-owned path is not a directory: src")

    for name, content in (
        ("_io.py", IO_RUNTIME_PY),
        ("_backend.py", BACKEND_RUNTIME_PY),
    ):
        target = src_dir / name
        if path_is_foundation_link(target):
            raise RuntimeError(f"Foundation output contains a link or reparse point: src/{name}")
        if target.exists() and not target.is_file():
            raise RuntimeError(f"Foundation output contains a non-regular entry: src/{name}")
        if target.exists() and target.lstat().st_nlink > 1:
            raise RuntimeError(f"Foundation output contains a hard-linked regular file: src/{name}")
        if path_is_foundation_link(src_dir) or not src_dir.is_dir():
            raise RuntimeError("Foundation-owned directory changed into an unsafe path: src")

        descriptor, temp_name = tempfile.mkstemp(prefix=f".{name}.trusted-", dir=src_dir)
        temp_path = Path(temp_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            if path_is_foundation_link(src_dir):
                raise RuntimeError("Foundation-owned directory changed into a link or reparse point: src")
            if path_is_foundation_link(target):
                raise RuntimeError(f"Foundation output changed into a link or reparse point: src/{name}")
            os.replace(temp_path, target)
        except Exception:
            try:
                os.close(descriptor)
            except OSError:
                pass
            raise
        finally:
            temp_path.unlink(missing_ok=True)


def _is_frozen_path(relative: str) -> bool:
    return is_foundation_frozen_path(relative)


def _write_foundation_manifest(path: Path, manifest: dict[str, Any]) -> None:
    """Replace the manifest atomically so an existing link cannot write through."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if path_is_foundation_link(path.parent):
        raise RuntimeError("Foundation manifest parent directory is a link or reparse point")
    descriptor, temp_name = tempfile.mkstemp(prefix=".foundation-manifest-", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(pretty_json(manifest))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            try:
                os.close(descriptor)
            except OSError:
                pass
            raise
        if path_is_foundation_link(path.parent):
            raise RuntimeError("Foundation manifest parent changed into a link or reparse point")
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)

def _snapshot_hash(files: list[dict[str, Any]]) -> str:
    return foundation_snapshot_hash(files)


def _trusted_hashes(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for relative in ("src/_io.py", "src/_backend.py"):
        path = root / Path(relative)
        if path.is_file():
            result[relative] = _sha256(path)
    return result


def _sha256(path: Path) -> str:
    return file_sha256(path)
