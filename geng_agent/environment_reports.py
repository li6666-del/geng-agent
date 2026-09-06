"""Pip artifact verification and case environment report/lock helpers."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from .environment_policy import (
    ENVIRONMENT_SCHEMA_VERSION,
    EnvironmentPolicyError,
    TrustedIndex,
)


def validate_pip_report(path: str | Path, trusted_index: TrustedIndex) -> dict[str, Any]:
    """Verify that every downloaded artifact came from the host-owned source.

    Pip records the final download URL and archive hashes in ``--report`` output.
    Empty ``install`` arrays are valid when the case interpreter already satisfies
    every request.
    """

    report_path = Path(path)
    try:
        report_bytes = _read_regular_report_nofollow(report_path)
        report = json.loads(report_bytes.decode("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EnvironmentPolicyError(f"pip did not produce a valid report: {report_path}") from exc
    installs = report.get("install")
    if not isinstance(installs, list):
        raise EnvironmentPolicyError("pip report does not contain an install array")
    allowed_hosts = set(trusted_index.artifact_hosts)
    artifacts: list[dict[str, str]] = []
    for item in installs:
        if not isinstance(item, Mapping):
            raise EnvironmentPolicyError("pip report contains a malformed install record")
        download = item.get("download_info")
        if not isinstance(download, Mapping):
            raise EnvironmentPolicyError("pip install record omitted trusted download evidence")
        url = str(download.get("url") or "")
        parsed = urlsplit(url)
        host = (parsed.hostname or "").casefold().rstrip(".")
        if parsed.scheme.casefold() != "https" or host not in allowed_hosts:
            raise EnvironmentPolicyError(f"pip selected an untrusted artifact source: {url}")
        archive = download.get("archive_info")
        hashes = archive.get("hashes") if isinstance(archive, Mapping) else None
        sha256 = hashes.get("sha256") if isinstance(hashes, Mapping) else None
        if not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", sha256):
            raise EnvironmentPolicyError(f"pip report omitted a SHA-256 artifact hash: {url}")
        metadata = item.get("metadata") if isinstance(item.get("metadata"), Mapping) else {}
        distribution = canonicalize_name(str(metadata.get("name") or ""))
        version = str(metadata.get("version") or "").strip()
        if not distribution or not version:
            raise EnvironmentPolicyError(
                "pip install record omitted distribution name or version metadata"
            )
        artifacts.append(
            {
                "distribution": distribution,
                "version": version,
                "url": urlunsplit((parsed.scheme.casefold(), parsed.netloc.casefold(), parsed.path, "", "")),
                "sha256": sha256.casefold(),
            }
        )
    return {
        "report_sha256": hashlib.sha256(report_bytes).hexdigest(),
        "environment": {
            str(key): str(value)
            for key, value in (
                report.get("environment")
                if isinstance(report.get("environment"), Mapping)
                else {}
            ).items()
        },
        "artifacts": sorted(
            artifacts,
            key=lambda item: (
                item["distribution"], item["version"], item["url"], item["sha256"]
            ),
        ),
    }


def _read_regular_report_nofollow(path: Path, *, max_bytes: int = 64 * 1024 * 1024) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if os.name != "nt":
        flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise EnvironmentPolicyError(f"pip report could not be opened safely: {path}") from exc
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_size > max_bytes
        ):
            raise EnvironmentPolicyError(f"pip report has unsafe filesystem metadata: {path}")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            payload = handle.read(max_bytes + 1)
        if len(payload) > max_bytes:
            raise EnvironmentPolicyError(f"pip report is too large: {path}")
        return payload
    finally:
        os.close(descriptor)


def _manifest_has_applicable_requirements(
    manifest: Mapping[str, Any],
    marker_environment: Any,
) -> bool:
    requirements = manifest.get("requirements")
    if not isinstance(requirements, list) or not requirements:
        return False
    if not isinstance(marker_environment, Mapping):
        return True
    environment = {str(key): str(value) for key, value in marker_environment.items()}
    for item in requirements:
        if not isinstance(item, Mapping):
            return True
        try:
            parsed = Requirement(str(item.get("requirement") or ""))
            if parsed.marker is None or parsed.marker.evaluate(environment=environment):
                return True
        except Exception:
            return True
    return False


def _combine_artifact_evidence(
    plan: Mapping[str, Any],
    installed: Mapping[str, Any],
) -> dict[str, Any]:
    plan_artifacts = plan.get("artifacts")
    installed_artifacts = installed.get("artifacts")
    if plan_artifacts != installed_artifacts:
        raise EnvironmentPolicyError(
            "pip selected a different artifact set after the trusted resolution plan"
        )
    return {
        "plan_report_sha256": str(plan.get("report_sha256") or ""),
        "install_report_sha256": str(installed.get("report_sha256") or ""),
        "artifacts": list(plan_artifacts or []),
    }


def load_environment_lock(path: str | Path) -> dict[str, Any] | None:
    lock_path = Path(path)
    try:
        value = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    if value.get("kind") != "geng.case_environment.lock":
        return None
    return value


def locked_distributions(lock_or_path: Mapping[str, Any] | str | Path) -> frozenset[str]:
    """Return distributions proven usable by a case lock.

    A partially resolved lock is useful diagnostic evidence, but unresolved,
    non-applicable, version-mismatched, or import-failing items never become
    runtime permissions.
    """

    if isinstance(lock_or_path, Mapping):
        lock: Mapping[str, Any] | None = lock_or_path
    else:
        lock = load_environment_lock(lock_or_path)
    if lock is None:
        return frozenset()

    allowed: set[str] = set()
    requirements = lock.get("requirements")
    if not isinstance(requirements, list):
        return frozenset()
    for item in requirements:
        if not isinstance(item, Mapping):
            continue
        if (
            item.get("applicable") is True
            and item.get("satisfied") is True
            and item.get("version_satisfied") is True
            and item.get("imports_ok") is True
        ):
            distribution = str(item.get("distribution") or "").strip()
            if distribution:
                allowed.add(canonicalize_name(distribution))
    return frozenset(allowed)

def _base_report(manifest: Mapping[str, Any], *, started_at: str) -> dict[str, Any]:
    return {
        "schema_version": ENVIRONMENT_SCHEMA_VERSION,
        "kind": "geng.case_environment.report",
        "case_id": manifest["case_id"],
        "request_hash": manifest["request_hash"],
        "target_interpreter": manifest["target_interpreter"],
        "index": manifest["index"],
        "started_at": started_at,
    }


def _probe_report(lock: Mapping[str, Any], *, attempted: bool) -> dict[str, Any]:
    unresolved = [
        {
            "requirement": item.get("requirement"),
            "distribution": item.get("distribution"),
            "installed_version": item.get("installed_version"),
            "version_satisfied": item.get("version_satisfied"),
            "imports_ok": item.get("imports_ok"),
        }
        for item in lock.get("requirements", [])
        if isinstance(item, Mapping) and item.get("applicable") and not item.get("satisfied")
    ]
    return {
        "attempted": attempted,
        "ok": bool(lock.get("ready")),
        "unresolved": unresolved,
        "environment_hash": lock.get("environment_hash"),
    }


def _bounded_output(value: Any, limit: int = 8000) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[:limit] + "\n...[truncated]"


def _bounded_error(exc: BaseException) -> str:
    return _bounded_output(f"{type(exc).__name__}: {exc}")


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()
