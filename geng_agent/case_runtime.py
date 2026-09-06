"""Host-owned case-runtime orchestration and compatibility facade."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
from typing import Any, Iterator, Mapping, Sequence

from packaging.utils import canonicalize_name

from .case_environment import (
    ArgvRunner,
    CommandResult,
    DEFAULT_TRUSTED_INDEXES,
    EnvironmentPolicyError,
    KNOWN_IMPORT_NAME_PROFILES,
    RequirementRequest,
    TrustedIndex,
    _environment_hash,
    _unprivileged_executable_path,
    normalize_requirement,
    resolve_case_environment,
    subprocess_argv_runner,
)
from .case_runtime_contracts import (
    CASE_RUNTIME_DIRNAME,
    HOST_SHARED_RUNTIME_MODE,
    WRITER_ENVIRONMENT_REQUEST_FILENAME,
    CaseRuntime,
    EnvironmentRequestRequired,
    EnvironmentResolutionError,
    _BASE_RUNTIME_MARKER,
    _BASE_RUNTIME_SELECTION_VERSION,
    _CAPABILITY_PROBE_PREFIX,
    _CASE_VENV_MARKER,
    _DISTRIBUTION_ALIASES,
    _FOUNDATION_BASELINE_REQUIREMENTS,
    _INSTALL_TAINT_SUFFIX,
    _LOCAL_RUNTIME_NAMES,
    _coerce_normalized_request,
    _dedupe_requests,
    _normalized_request,
    _request_for_runtime_name,
)
from .case_runtime_inventory import (
    _base_mirror_is_trusted,
    _case_base_interpreter,
    _case_identity,
    _case_python_path,
    _case_taint_path,
    _case_venv_dir,
    _case_venv_is_trusted as _case_venv_is_trusted_impl,
    _case_venv_provenance,
    _cleanup_failed_case_runtime as _cleanup_failed_case_runtime_impl,
    _cleanup_failed_host_shared_runtime,
    _clear_active_environment_evidence,
    _copy_runtime_inventory,
    _create_case_venv as _create_case_venv_impl,
    _harden_runtime_tree,
    _mark_case_environment_tainted,
    _retire_case_venv,
    _runtime_inventory_digest,
    _runtime_inventory_manifest,
    _runtime_manifest_digest,
    _runtime_regular_file_digest,
    _write_case_venv_marker,
)
from .case_runtime_locking import (
    _assert_secure_host_ancestor,
    _case_runtime_guard,
    _host_prefix_from_identity,
    _host_prefix_guess,
    _host_runtime_provenance,
    _host_shared_runtime_guard,
    _host_shared_runtime_lock_target,
    _host_shared_runtime_mutex_identity,
    _host_uid,
    _open_or_create_host_root,
    _runtime_file_guard,
    _trusted_host_path,
)
from .case_runtime_probe import (
    _CAPABILITY_PROBE_SCRIPT,
    _assert_regular_nofollow,
    _is_reparse_point,
    _probe_runtime_capabilities,
    _read_regular_file_nofollow,
    _run_checked,
    _trusted_runtime_roots,
)
from .case_runtime_requests import (
    _persisted_host_requests,
    environment_request_prompt,
    read_environment_request,
    requirements_from_scientific_architecture,
    requirements_missing_from_lock,
)
from .outputs import write_json
from .security import import_names_for_requirement


def ensure_case_runtime(
    *,
    output_dir: Path,
    audit_dir: Path,
    scientific_architecture: Mapping[str, Any] | None,
    extra_requirements: Sequence[RequirementRequest | str | Mapping[str, Any]] = (),
    base_interpreter: str | Path | None = None,
    resume: bool = True,
    run_argv: ArgvRunner = subprocess_argv_runner,
    trusted_indexes: Mapping[str, TrustedIndex] | None = None,
    index_identity: str = "pypi",
) -> CaseRuntime:
    """Resolve one case against the host-selected shared Python runtime.

    Package installation mutates the selected host interpreter, so every case
    using that interpreter shares one host-wide mutex. Case-local files remain
    dynamic evidence of the request and verified result; no case virtual
    environment is created, mirrored, retired, or otherwise managed here.
    """

    resolved_output = output_dir.resolve()
    resolved_audit = audit_dir.resolve()
    runtime_dir = resolved_audit / CASE_RUNTIME_DIRNAME
    runtime_dir.mkdir(parents=True, exist_ok=True)
    host_python = Path(base_interpreter or sys.executable).absolute()
    with _host_shared_runtime_guard(host_python):
        return _ensure_case_runtime_locked(
            output_dir=resolved_output,
            audit_dir=resolved_audit,
            scientific_architecture=scientific_architecture,
            extra_requirements=extra_requirements,
            base_interpreter=host_python,
            resume=resume,
            run_argv=run_argv,
            trusted_indexes=trusted_indexes,
            index_identity=index_identity,
        )


def _ensure_case_runtime_locked(
    *,
    output_dir: Path,
    audit_dir: Path,
    scientific_architecture: Mapping[str, Any] | None,
    extra_requirements: Sequence[RequirementRequest | str | Mapping[str, Any]] = (),
    base_interpreter: str | Path | None = None,
    resume: bool = True,
    run_argv: ArgvRunner = subprocess_argv_runner,
    trusted_indexes: Mapping[str, TrustedIndex] | None = None,
    index_identity: str = "pypi",
) -> CaseRuntime:
    """Install into and prove the selected host-shared runtime.

    The caller holds the interpreter-wide mutex for this complete transaction.
    Failure revokes only case-local active evidence because the host prefix may
    be shared by unrelated cases and services.
    """

    output_dir = output_dir.resolve()
    audit_dir = audit_dir.resolve()
    runtime_dir = audit_dir / CASE_RUNTIME_DIRNAME
    runtime_dir.mkdir(parents=True, exist_ok=True)
    persisted_request_path = output_dir / "03a_environment_request.json"
    persisted_requests = (
        _persisted_host_requests(persisted_request_path) if resume else ()
    )
    requests: list[RequirementRequest | str | Mapping[str, Any]] = list(
        requirements_from_scientific_architecture(scientific_architecture)
    )
    requests.extend(persisted_requests)
    requests.extend(extra_requirements)
    normalized_requests = _dedupe_requests(
        [_coerce_normalized_request(item) for item in requests]
    )
    persisted_signature = tuple(
        (item.requirement, tuple(sorted(item.import_names)))
        for item in _dedupe_requests(list(persisted_requests))
    )
    requested_signature = tuple(
        (item.requirement, tuple(sorted(item.import_names)))
        for item in normalized_requests
    )
    requirements_changed = bool(
        not resume
        or not persisted_request_path.is_file()
        or persisted_signature != requested_signature
    )
    host_python = Path(base_interpreter or sys.executable).absolute()
    if requirements_changed:
        _clear_active_environment_evidence(output_dir)

    try:
        resolution = resolve_case_environment(
            case_dir=output_dir,
            case_id=output_dir.name,
            target_interpreter=host_python,
            requirements=normalized_requests,
            index_identity=index_identity,
            trusted_indexes=(
                DEFAULT_TRUSTED_INDEXES if trusted_indexes is None else trusted_indexes
            ),
            force=not resume,
            run_argv=run_argv,
            verify_artifacts=True,
        )
    except Exception:
        _cleanup_failed_host_shared_runtime(output_dir=output_dir)
        raise
    if not resolution.ready or resolution.lock is None:
        category = str(resolution.report.get("status") or "resolution_failed")
        _cleanup_failed_host_shared_runtime(output_dir=output_dir)
        raise EnvironmentResolutionError(
            category,
            f"case environment is not ready: {category}",
            report=resolution.report,
        )

    lock = dict(resolution.lock)
    try:
        host_provenance = _host_runtime_provenance(
            host_python=host_python,
            interpreter_identity=lock.get("interpreter"),
        )
    except Exception:
        _cleanup_failed_host_shared_runtime(output_dir=output_dir)
        raise
    lock["runtime_mode"] = HOST_SHARED_RUNTIME_MODE
    lock["host_provenance"] = host_provenance
    # ``case_environment._environment_hash`` retains this legacy semantic slot;
    # populate it with host provenance so the active grant also authenticates
    # the selected shared interpreter without claiming that a venv was created.
    lock["venv_provenance"] = host_provenance
    report = dict(resolution.report)
    report["runtime_mode"] = HOST_SHARED_RUNTIME_MODE
    report["host_provenance"] = host_provenance
    try:
        pip_check = _run_checked(
            run_argv,
            (str(host_python), "-I", "-m", "pip", "check"),
            cwd=None,
            timeout=180.0,
        )
    except Exception:
        _cleanup_failed_host_shared_runtime(output_dir=output_dir)
        raise
    report["pip_check"] = {
        "returncode": pip_check.returncode,
        "stdout": pip_check.stdout[-8000:],
        "stderr": pip_check.stderr[-8000:],
    }
    if pip_check.returncode != 0:
        report["ready"] = False
        report["status"] = "abi_conflict"
        write_json(resolution.paths.report, report)
        _cleanup_failed_host_shared_runtime(output_dir=output_dir)
        raise EnvironmentResolutionError(
            "abi_conflict",
            "pip check found an incompatible host-shared dependency set",
            report=report,
        )

    try:
        capabilities = _probe_runtime_capabilities(
            case_python=host_python,
            architecture=scientific_architecture,
            output_dir=output_dir,
            run_argv=run_argv,
        )
        lock["capabilities"] = capabilities
        capabilities_ok = all(bool(item.get("ok")) for item in capabilities)
        lock["capabilities_ok"] = capabilities_ok
        lock["ready"] = bool(lock.get("ready")) and capabilities_ok
        lock["environment_hash"] = _environment_hash(lock)
        report["capabilities"] = capabilities
        report["ready"] = lock["ready"]
        report["status"] = "ready" if lock["ready"] else "capability_probe_failed"
        report.setdefault("probe", {})["environment_hash"] = lock["environment_hash"]
        write_json(resolution.paths.lock, lock)
        write_json(resolution.paths.report, report)
    except Exception:
        _cleanup_failed_host_shared_runtime(output_dir=output_dir)
        raise
    if not lock["ready"]:
        _cleanup_failed_host_shared_runtime(output_dir=output_dir)
        raise EnvironmentResolutionError(
            "capability_probe_failed",
            "the selected scientific runtime failed a real capability probe",
            report=report,
        )

    host_prefix = _host_prefix_from_identity(
        lock.get("interpreter"),
        fallback_python=host_python,
    )

    return CaseRuntime(
        venv_dir=host_prefix,
        python_executable=host_python,
        request_path=resolution.paths.request,
        lock_path=resolution.paths.lock,
        report_path=resolution.paths.report,
        environment_hash=str(lock["environment_hash"]),
        manifest=dict(resolution.manifest),
        lock=lock,
        report=report,
        trusted_read_roots=_trusted_runtime_roots(lock, host_python),
    )


def _case_venv_is_trusted(
    *,
    venv_dir: Path,
    case_python: Path,
    base_python: Path,
    output_dir: Path,
) -> bool:
    """Compatibility wrapper retaining facade-level trust hook patching."""

    return _case_venv_is_trusted_impl(
        venv_dir=venv_dir,
        case_python=case_python,
        base_python=base_python,
        output_dir=output_dir,
        trusted_host_path_fn=_trusted_host_path,
        unprivileged_executable_path_fn=_unprivileged_executable_path,
    )


def _cleanup_failed_case_runtime(
    *,
    output_dir: Path,
    venv_dir: Path,
) -> None:
    """Compatibility wrapper retaining facade-level retirement patching."""

    _cleanup_failed_case_runtime_impl(
        output_dir=output_dir,
        venv_dir=venv_dir,
        retire_case_venv_fn=_retire_case_venv,
    )


def _create_case_venv(
    *,
    base_python: Path,
    venv_dir: Path,
    allowed_parent: Path,
    output_dir: Path,
    working_dir: Path,
    run_argv: ArgvRunner,
) -> None:
    """Compatibility wrapper retaining facade-level host operation patching."""

    _create_case_venv_impl(
        base_python=base_python,
        venv_dir=venv_dir,
        allowed_parent=allowed_parent,
        output_dir=output_dir,
        working_dir=working_dir,
        run_argv=run_argv,
        open_or_create_host_root_fn=_open_or_create_host_root,
        retire_case_venv_fn=_retire_case_venv,
        run_checked_fn=_run_checked,
    )
