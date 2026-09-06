from __future__ import annotations

import json
import shutil
import stat
from pathlib import Path
from typing import Any

from .outputs import write_json
from .paper_evidence import safe_label
from .security import redact_text
from .task_reporter_snapshot import (
    REPORT_ASSETS_DIR,
    _copy_regular_file_without_links,
    _path_is_link_like,
    _sha256_file,
)


REPORT_ASSET_SUFFIXES = frozenset({".png", ".jpg", ".jpeg"})
REPORT_ASSET_MAX_BYTES = 20_000_000


def _task_record_run_valid_hint(
    task_record: dict[str, Any],
) -> bool | None:
    """Prefer host-owned execution evidence; self-report formatting is advisory."""

    host_execution = task_record.get("host_execution")
    if isinstance(host_execution, dict):
        passed = host_execution.get("passed")
        if isinstance(passed, bool):
            return passed
        returncode = host_execution.get("returncode")
        if isinstance(returncode, int) and not isinstance(returncode, bool):
            return returncode == 0
    host_returncode = task_record.get("host_run_returncode")
    if isinstance(host_returncode, int) and not isinstance(host_returncode, bool):
        return host_returncode == 0

    execution = (
        task_record.get("execution_summary")
        if isinstance(task_record.get("execution_summary"), dict)
        else {}
    )
    try:
        full_run_count = int(execution.get("full_run_count"))
    except (TypeError, ValueError):
        return None
    last_returncode = execution.get("last_returncode")
    if (
        full_run_count >= 1
        and isinstance(last_returncode, int)
        and not isinstance(last_returncode, bool)
    ):
        return last_returncode == 0
    return None


def _evidence_path_issues(
    verification: dict[str, Any],
    workspace: Path,
) -> list[str]:
    issues: list[str] = []
    containers = [("task", verification)]
    conclusions = verification.get("core_conclusions")
    containers.extend(
        (str(item.get("claim_id") or "unnamed claim"), item)
        for item in (conclusions if isinstance(conclusions, list) else [])
        if isinstance(item, dict)
    )
    for label, container in containers:
        values = container.get("evidence_files")
        for raw_path in values if isinstance(values, list) else []:
            if _verified_reporter_evidence_path(raw_path, workspace) is None:
                issues.append(
                    "evidence file is missing or outside task reporter workspace "
                    f"({label}): {raw_path}"
                )
    return issues


def _verified_reporter_evidence_path(raw_path: Any, workspace: Path) -> Path | None:
    relative = Path(str(raw_path or ""))
    if not str(raw_path or "").strip() or ".." in relative.parts:
        return None
    try:
        root = workspace.resolve(strict=True)
        candidate = root / relative
        if _path_has_link_component(candidate, root):
            return None
        resolved = candidate.resolve(strict=True)
        if resolved.is_relative_to(root) and resolved.is_file():
            return resolved
    except (OSError, ValueError):
        pass
    return None


def normalize_reporter_observation_evidence(
    raw: dict[str, Any], workspace: Path, *, host_execution: dict[str, Any] | None = None
) -> tuple[dict[str, Any], list[str]]:
    """Keep findings, but do not certify success from missing local evidence.

    This is a reportability check, not a format gate or a Writer retry trigger.
    A copied source file can establish a method failure without output artifacts.
    """

    document = json.loads(json.dumps(raw, ensure_ascii=False))
    warnings = _evidence_path_issues(document, workspace)
    local_root = (workspace / "inputs" / "writer_output").resolve()
    unobserved_paths: set[Path] = set()
    if isinstance(host_execution, dict):
        receipt = host_execution.get("receipt") or {}
        output_prefix = Path("outputs") / str(receipt.get("output_subdir") or receipt.get("task_id") or "")
        for relative in host_execution.get("unobserved_artifacts", []):
            try:
                copied = local_root / "outputs" / Path(relative).relative_to(output_prefix)
                unobserved_paths.add(copied.resolve())
            except ValueError:
                continue
        if unobserved_paths:
            warnings.append("Post-execution artifacts are presentation only; scientific claims require observed outputs or source evidence.")

    def local_evidence(container: dict[str, Any]) -> list[Path]:
        values = container.get("evidence_files")
        verified = [
            path for value in values if (path := _verified_reporter_evidence_path(value, workspace)) is not None
        ] if isinstance(values, list) else []
        return [
            path for path in verified
            if (path.is_relative_to(local_root / "outputs")
                or path.is_relative_to(local_root / "source"))
            and path not in unobserved_paths
        ]

    shared_local_evidence = local_evidence(document)
    missing_local_ids: set[str] = set()
    conclusions = document.get("core_conclusions")
    for item in conclusions if isinstance(conclusions, list) else []:
        if not isinstance(item, dict):
            continue
        if local_evidence(item) or shared_local_evidence:
            continue
        claim_id = str(item.get("claim_id") or "unnamed claim")
        missing_local_ids.add(claim_id)
        status = str(item.get("status") or "").strip()
        if status not in {"supported", "unsupported", "unassessable_missing_information"}:
            status = (
                "supported" if item.get("supported") is True
                else "unsupported" if item.get("supported") is False
                else "unassessable_missing_information"
            )
        if status == "supported":
            item["status"] = "unassessable_missing_information"
            warnings.append(
                f"claim {claim_id} has no verifiable copied local output or source evidence; "
                "support was retained as unassessable rather than certified"
            )
        elif status == "unsupported":
            warnings.append(
                f"claim {claim_id} reports a failure without verifiable copied local evidence; "
                "the finding is retained, but cannot authorize a scientific rerun"
            )
    rerun = document.get("rerun_evidence")
    if isinstance(rerun, dict) and rerun.get("rerun_reason") == "core_conclusion_failed":
        affected = rerun.get("contract_item_ids")
        if isinstance(affected, list) and missing_local_ids.intersection(map(str, affected)):
            document["rerun_evidence"] = None
    if warnings:
        uncertainties = document.get("remaining_uncertainties")
        if not isinstance(uncertainties, list):
            uncertainties = []
        document["remaining_uncertainties"] = list(dict.fromkeys([
            *map(str, uncertainties), *warnings,
        ]))
    return document, warnings


def _accepted_asset_issues(
    verification: dict[str, Any],
    workspace: Path,
    task_id: str,
    *,
    crop_result: dict[str, Any],
    require_verified_pdf_crop: bool = False,
) -> list[str]:
    """Validate only supplied assets; missing visual packaging is non-blocking."""

    del crop_result, require_verified_pdf_crop
    issues: list[str] = []
    asset_root = (
        workspace / REPORT_ASSETS_DIR / safe_label(task_id)
    ).resolve()
    for key in ("local_assets", "paper_assets"):
        values = verification.get(key)
        if not isinstance(values, list):
            continue
        for raw_path in values:
            path = workspace / str(raw_path)
            is_symlink = path.is_symlink()
            try:
                resolved = path.resolve(strict=True)
                owned = resolved.parent == asset_root
            except (OSError, ValueError):
                resolved = path
                owned = False
            if not owned:
                issues.append(
                    f"ignored {key} outside the assigned asset directory: {raw_path}"
                )
            elif (
                not _ordinary_report_asset(resolved)
                or is_symlink
            ):
                issues.append(f"ignored missing or unsupported {key}: {raw_path}")
            elif resolved.stat().st_size > REPORT_ASSET_MAX_BYTES:
                issues.append(f"ignored oversized {key}: {raw_path}")
    return issues


def _materialize_task_assets(
    *,
    asset_candidates: dict[str, list[str]],
    workspace: Path,
    task_id: str,
) -> tuple[dict[str, list[str]], list[str]]:
    """Copy declared images from bounded reporter inputs into one sanitized tree."""

    published = {"local_assets": [], "paper_assets": []}
    warnings: list[str] = []
    task_label = safe_label(task_id)
    asset_root = workspace / REPORT_ASSETS_DIR / task_label
    staging_parent = workspace / ".host_report_asset_staging"
    staging_root = staging_parent / task_label
    if _path_is_link_like(workspace):
        return published, ["reporter workspace is a symbolic link or reparse point"]
    try:
        _remove_generated_path(staging_parent, workspace=workspace)
        staging_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return published, [
            "host could not prepare sanitized reporter assets: "
            f"{type(exc).__name__}"
        ]
    used_names: set[str] = set()
    seen: set[tuple[str, Path]] = set()

    for key in ("local_assets", "paper_assets"):
        allowed_roots = [asset_root]
        if key == "local_assets":
            allowed_roots.append(workspace / "inputs" / "writer_output" / "outputs")
        else:
            allowed_roots.append(workspace / "paper_evidence")
        for raw_path in asset_candidates.get(key, []):
            source, issue = _resolve_materialization_source(
                workspace=workspace,
                raw_path=raw_path,
                allowed_roots=allowed_roots,
            )
            if source is None:
                warnings.append(f"ignored {key[:-1]}: {raw_path} ({issue})")
                continue
            identity = (key, source)
            if identity in seen:
                continue
            seen.add(identity)
            name = _unique_asset_name(source, key=key, used_names=used_names)
            destination = staging_root / name
            try:
                _copy_regular_file_without_links(
                    source=source,
                    target=destination,
                    source_root=workspace,
                )
            except (OSError, ValueError) as exc:
                warnings.append(
                    f"ignored {key[:-1]}: {raw_path} "
                    f"({type(exc).__name__} during host materialization)"
                )
                continue
            used_names.add(name.casefold())
            published[key].append(
                f"{REPORT_ASSETS_DIR}/{task_label}/{name}"
            )

    if any(published.values()):
        try:
            asset_parent = asset_root.parent
            if _path_is_link_like(asset_parent) or asset_parent.is_file():
                _remove_generated_path(asset_parent, workspace=workspace)
            _remove_generated_path(asset_root, workspace=workspace)
            asset_parent.mkdir(parents=True, exist_ok=True)
            staging_root.replace(asset_root)
        except OSError as exc:
            warnings.append(
                "host could not publish sanitized reporter assets: "
                f"{type(exc).__name__}"
            )
            published = {"local_assets": [], "paper_assets": []}
    try:
        _remove_generated_path(staging_parent, workspace=workspace)
    except OSError as exc:
        warnings.append(
            "host could not clean reporter asset staging: "
            f"{type(exc).__name__}"
        )
    return published, warnings


def _resolve_materialization_source(
    *,
    workspace: Path,
    raw_path: Any,
    allowed_roots: list[Path],
) -> tuple[Path | None, str | None]:
    raw = str(raw_path or "").strip().replace("\\", "/")
    relative = Path(raw)
    if not raw or relative.is_absolute() or ".." in relative.parts:
        return None, "path is not a safe workspace-relative path"
    try:
        workspace_root = workspace.resolve(strict=True)
        candidate = workspace / relative
        if _path_has_link_component(candidate, workspace):
            return None, "symbolic links and reparse points are not publishable"
        source = candidate.resolve(strict=True)
        source.relative_to(workspace_root)
    except (OSError, ValueError):
        return None, "file is missing or outside the reporter workspace"
    if not _ordinary_report_asset(source):
        return None, "only ordinary PNG/JPG/JPEG files are publishable"
    try:
        size = source.stat().st_size
    except OSError:
        return None, "file metadata is unavailable"
    if size > REPORT_ASSET_MAX_BYTES:
        return None, f"file exceeds {REPORT_ASSET_MAX_BYTES} bytes"
    allowed = False
    for root in allowed_roots:
        try:
            source.relative_to(root.resolve(strict=True))
            allowed = True
            break
        except (OSError, ValueError):
            continue
    if not allowed:
        return None, "path is outside the allowed asset source roots"
    return source, None


def _ordinary_report_asset(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISREG(metadata.st_mode)
        and not _path_is_link_like(path)
        and path.suffix.lower() in REPORT_ASSET_SUFFIXES
    )


def _path_has_link_component(path: Path, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return True
    current = root
    if _path_is_link_like(current):
        return True
    for part in relative.parts:
        current = current / part
        if _path_is_link_like(current):
            return True
    return False


def _unique_asset_name(source: Path, *, key: str, used_names: set[str]) -> str:
    stem = safe_label(source.stem)
    suffix = source.suffix.lower()
    candidate = f"{stem}{suffix}"
    if candidate.casefold() not in used_names:
        return candidate
    prefix = "local" if key == "local_assets" else "paper"
    index = 1
    while True:
        candidate = f"{prefix}_{stem}_{index:02d}{suffix}"
        if candidate.casefold() not in used_names:
            return candidate
        index += 1


def _remove_generated_path(path: Path, *, workspace: Path) -> None:
    try:
        resolved_parent = path.parent.resolve(strict=True)
        resolved_parent.relative_to(workspace.resolve(strict=True))
    except (OSError, ValueError):
        return
    if _path_is_link_like(path) or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path)


def _normalize_verification_paths(
    *,
    verification: dict[str, Any],
    workspace: Path,
    output_dir: Path,
    published_assets: dict[str, list[str]],
) -> dict[str, Any]:
    normalized = json.loads(json.dumps(verification, ensure_ascii=False))
    stable_evidence: list[str] = []
    values = normalized.get("evidence_files")
    for raw_path in values if isinstance(values, list) else []:
        source = (workspace / str(raw_path)).resolve()
        try:
            stable_evidence.append(
                source.relative_to(output_dir.resolve()).as_posix()
            )
        except ValueError:
            stable_evidence.append(str(source))
    normalized["evidence_files"] = stable_evidence
    for key in ("local_assets", "paper_assets"):
        normalized[key] = list(published_assets.get(key, []))
    return normalized


def _copy_task_assets(*, source: Path, target: Path) -> list[str]:
    if not source.is_dir() or _path_is_link_like(source):
        raise ValueError("task reporter did not create an asset directory")
    if _path_is_link_like(target.parent):
        raise ValueError("task asset publication root must not be a link")
    if _path_is_link_like(target) or target.is_file():
        target.unlink(missing_ok=True)
    elif target.is_dir():
        shutil.rmtree(target)
    copied: list[str] = []
    try:
        paths = sorted(source.iterdir(), key=lambda item: item.name.casefold())
    except OSError as exc:
        raise ValueError("task reporter asset directory is unreadable") from exc
    for path in paths:
        if not _ordinary_report_asset(path):
            raise ValueError(f"task reporter asset must not be a symlink: {path}")
        if path.stat().st_size > REPORT_ASSET_MAX_BYTES:
            raise ValueError(f"unsupported task reporter asset: {path.name}")
        destination = target / path.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        _copy_regular_file_without_links(
            source=path,
            target=destination,
            source_root=source,
        )
        copied.append(str(destination))
    if not copied:
        raise ValueError("task reporter asset directory is empty")
    return copied


def _task_assets_exist(
    output_dir: Path,
    task_id: str,
    verification: dict[str, Any],
    *,
    asset_manifest: Any = None,
) -> bool:
    root = output_dir / REPORT_ASSETS_DIR / safe_label(task_id)
    declared: list[tuple[str, str]] = []
    for key in ("local_assets", "paper_assets"):
        values = verification.get(key)
        for raw_path in values if isinstance(values, list) else []:
            relative = str(raw_path).replace("\\", "/")
            expected = (
                f"{REPORT_ASSETS_DIR}/{safe_label(task_id)}/"
                f"{Path(relative).name}"
            )
            if relative != expected:
                return False
            declared.append((key, expected))
    if not declared:
        return True
    if not isinstance(asset_manifest, list):
        return False
    manifest = {
        (str(item.get("kind") or ""), str(item.get("path") or "")): item
        for item in asset_manifest
        if isinstance(item, dict)
    }
    if set(manifest) != set(declared):
        return False
    try:
        resolved_root = root.resolve(strict=True)
    except OSError:
        return False
    if _path_is_link_like(root):
        return False
    for key, relative in declared:
        path = output_dir / Path(relative)
        try:
            resolved = path.resolve(strict=True)
            if resolved.parent != resolved_root or not _ordinary_report_asset(resolved):
                return False
            size = resolved.stat().st_size
            item = manifest[(key, relative)]
            if (
                size > REPORT_ASSET_MAX_BYTES
                or item.get("size") != size
                or item.get("sha256") != _sha256_file(resolved)
            ):
                return False
        except (OSError, ValueError):
            return False
    return True


def _task_asset_manifest(
    output_dir: Path,
    task_id: str,
    published_assets: dict[str, list[str]],
) -> list[dict[str, Any]]:
    manifest: list[dict[str, Any]] = []
    root = output_dir / REPORT_ASSETS_DIR / safe_label(task_id)
    try:
        resolved_root = root.resolve(strict=True)
    except OSError:
        return []
    if _path_is_link_like(root):
        return []
    for key in ("local_assets", "paper_assets"):
        for relative in published_assets.get(key, []):
            path = output_dir / Path(relative)
            try:
                resolved = path.resolve(strict=True)
                if (
                    resolved.parent != resolved_root
                    or not _ordinary_report_asset(resolved)
                    or resolved.stat().st_size > REPORT_ASSET_MAX_BYTES
                ):
                    return []
                manifest.append(
                    {
                        "kind": key,
                        "path": str(relative).replace("\\", "/"),
                        "size": resolved.stat().st_size,
                        "sha256": _sha256_file(resolved),
                    }
                )
            except (OSError, ValueError):
                return []
    return manifest


def _task_reporter_failure(
    *,
    task_id: str,
    task_audit_dir: Path,
    status_path: Path,
    input_hash: str,
    workspace: Path,
    error: Exception,
    error_kind: str,
) -> dict[str, Any]:
    del task_audit_dir
    message = redact_text(f"{type(error).__name__}: {error}")[:1500]
    status = {
        "ok": False,
        "backend": "codex",
        "mode": "isolated_task_reporter",
        "task_id": task_id,
        "input_hash": input_hash,
        "cached": False,
        "workspace": str(workspace),
        "codex_status": {
            "ok": False,
            "error_kind": error_kind,
            "error": message,
        },
        "task_verification": {},
        "validation_issues": [message],
        "asset_issues": [],
        "asset_paths": [],
        "asset_manifest": [],
        "scientific_successful": False,
        "crop_status": "unresolved",
        "crop_result": {"status": "unresolved", "issues": [message]},
        "terminal": False,
        "error": message,
    }
    write_json(status_path, status)
    return status


def _task_reporter_reason(
    codex_status: dict[str, Any],
    validation_issues: list[str],
    asset_issues: list[str],
) -> str:
    if not codex_status.get("ok"):
        return str(
            codex_status.get("blocked_reason")
            or codex_status.get("error")
            or "task reporter failed"
        )
    if validation_issues:
        return (
            "task reporter verification was invalid: "
            + "; ".join(validation_issues[:8])
        )
    if asset_issues:
        return (
            "task reporter assets were invalid: "
            + "; ".join(asset_issues[:8])
        )
    return "task reporter delivery was incomplete"
