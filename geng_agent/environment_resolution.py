"""Host-owned case environment runner and resolution transaction orchestration."""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import signal
import subprocess
from tempfile import TemporaryDirectory
from typing import Any, Callable, Mapping, Sequence

from packaging.utils import canonicalize_name

from .environment_policy import (
    ArgvRunner,
    CaseEnvironmentPaths,
    CommandResult,
    ENVIRONMENT_PIP_PLAN_FILENAME,
    ENVIRONMENT_PIP_REPORT_FILENAME,
    EnvironmentPolicyError,
    EnvironmentProbeError,
    EnvironmentResolution,
    RequirementRequest,
    TrustedIndex,
    build_environment_manifest,
    build_pip_install_argv,
    resolve_trusted_index,
)
from .environment_probe import (
    _environment_hash,
    _probe_environment,
    _resolution_hash,
    _unprivileged_executable_path,
)
from .environment_reports import (
    _base_report,
    _bounded_error,
    _bounded_output,
    _combine_artifact_evidence,
    _manifest_has_applicable_requirements,
    _probe_report,
    _timestamp,
    load_environment_lock,
    validate_pip_report,
)
from .outputs import write_json


def subprocess_argv_runner(
    argv: Sequence[str],
    *,
    cwd: Path | None = None,
    timeout: float | None = None,
) -> CommandResult:
    """Default host runner.  Tests and policy layers can inject a stricter one."""

    normalized = [str(part) for part in argv]
    pip_prefix = len(normalized) >= 4 and normalized[1:4] == ["-I", "-m", "pip"]
    legacy_pip_prefix = len(normalized) >= 3 and normalized[1:3] == ["-m", "pip"]
    is_pip = pip_prefix or legacy_pip_prefix
    is_python_probe = (
        len(normalized) >= 3
        and normalized[1] == "-I"
        and "-c" in normalized[2:4]
    )
    is_pip_check = (
        len(normalized) >= 5
        and normalized[1:5] == ["-I", "-m", "pip", "check"]
    ) or (
        len(normalized) >= 4
        and normalized[1:4] == ["-m", "pip", "check"]
    )
    with TemporaryDirectory(prefix="geng-case-runtime-") as runtime_home:
        env: dict[str, str] = {}
        for key in (
            "PATH", "Path", "SystemRoot", "WINDIR", "COMSPEC", "PATHEXT",
            "LANG", "LC_ALL", "LD_LIBRARY_PATH", "CUDA_HOME", "CUDA_PATH",
            "CUDA_VISIBLE_DEVICES", "NVIDIA_VISIBLE_DEVICES", "SSL_CERT_FILE",
            "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
        ):
            value = os.environ.get(key)
            if value:
                env[key] = value
        if is_pip:
            for key in (
                "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
                "http_proxy", "https_proxy", "all_proxy", "no_proxy",
            ):
                value = os.environ.get(key)
                if value:
                    env[key] = value
        env.update(
            {
                "HOME": runtime_home,
                "USERPROFILE": runtime_home,
                "USER": "geng-case-runtime",
                "LOGNAME": "geng-case-runtime",
                "USERNAME": "geng-case-runtime",
                "XDG_CACHE_HOME": str(Path(runtime_home) / ".cache"),
                "TMP": runtime_home,
                "TEMP": runtime_home,
                "TMPDIR": runtime_home,
                "PYTHONNOUSERSITE": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PIP_CONFIG_FILE": os.devnull,
                "PIP_DISABLE_PIP_VERSION_CHECK": "1",
                "PIP_NO_INPUT": "1",
            }
        )
        command = normalized
        process_cwd = runtime_home if is_pip else (str(cwd) if cwd is not None else None)
        setpriv = shutil.which("setpriv")
        if (
            os.name != "nt"
            and getattr(os, "geteuid", lambda: -1)() == 0
            and setpriv
            and (is_python_probe or is_pip_check)
            and _unprivileged_executable_path(Path(normalized[0]))
        ):
            os.chown(runtime_home, 65534, 65534)
            os.chmod(runtime_home, 0o700)
            env["USER"] = "nobody"
            env["LOGNAME"] = "nobody"
            command = [
                setpriv,
                "--bounding-set=-all",
                "--reuid=65534",
                "--regid=65534",
                "--clear-groups",
                "--no-new-privs",
                "--pdeathsig=KILL",
                "--",
                *normalized,
            ]
            process_cwd = runtime_home
        process = subprocess.Popen(
            command,
            cwd=process_cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=False,
            close_fds=True,
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            # The probe owns a fresh process group.  Killing the whole group
            # prevents a native extension from leaving forked descendants
            # behind after the host timeout fires.
            if os.name != "nt":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                process.kill()
            final_stdout, final_stderr = process.communicate()
            raise subprocess.TimeoutExpired(
                exc.cmd,
                exc.timeout,
                output=final_stdout or exc.output,
                stderr=final_stderr or exc.stderr,
            ) from exc
    return CommandResult(
        argv=tuple(normalized),
        returncode=int(process.returncode),
        stdout=stdout or "",
        stderr=stderr or "",
    )

_RESOLUTION_SOURCES = frozenset({"host_runtime", "trusted_index", "not_applicable"})


def _requirement_record_key(item: Mapping[str, Any]) -> tuple[str, str]:
    return (
        str(item.get("requirement") or ""),
        canonicalize_name(str(item.get("distribution") or "")),
    )


def _resolution_sources_from_lock(
    lock: Mapping[str, Any],
) -> dict[tuple[str, str], str]:
    source_policy = lock.get("source_policy")
    evidence = (
        source_policy.get("artifact_evidence")
        if isinstance(source_policy, Mapping)
        else None
    )
    artifacts = evidence.get("artifacts") if isinstance(evidence, Mapping) else []
    artifact_versions = {
        (
            canonicalize_name(str(item.get("distribution") or "")),
            str(item.get("version") or ""),
        )
        for item in artifacts or ()
        if isinstance(item, Mapping)
    }
    sources: dict[tuple[str, str], str] = {}
    for item in lock.get("requirements", ()):
        if not isinstance(item, Mapping):
            continue
        key = _requirement_record_key(item)
        source = str(item.get("resolution_source") or "")
        if source not in _RESOLUTION_SOURCES:
            if item.get("applicable") is False:
                source = "not_applicable"
            elif (
                key[1],
                str(item.get("installed_version") or ""),
            ) in artifact_versions:
                source = "trusted_index"
            else:
                source = "host_runtime"
        sources[key] = source
    return sources


def _apply_resolution_sources(
    lock: dict[str, Any],
    sources: Mapping[tuple[str, str], str],
) -> dict[str, Any]:
    host_reused = False
    index_installed = False
    for item in lock.get("requirements", ()):
        if not isinstance(item, dict):
            continue
        if item.get("applicable") is False:
            source = "not_applicable"
        else:
            source = str(sources.get(_requirement_record_key(item)) or "")
            if source not in {"host_runtime", "trusted_index"}:
                raise EnvironmentProbeError(
                    "resolved dependency omitted its host-controlled resolution source"
                )
        item["resolution_source"] = source
        host_reused = host_reused or source == "host_runtime"
        index_installed = index_installed or source == "trusted_index"

    source_policy = lock.get("source_policy")
    if not isinstance(source_policy, dict):
        source_policy = {}
        lock["source_policy"] = source_policy
    source_policy["host_runtime_reused"] = host_reused
    source_policy["host_runtime_verified"] = bool(host_reused and lock.get("ready") is True)
    source_policy["trusted_index_installed"] = index_installed
    lock["resolution_hash"] = _resolution_hash(lock)
    lock["environment_hash"] = _environment_hash(lock)
    return lock


def _unresolved_manifest(
    manifest: Mapping[str, Any],
    probe_lock: Mapping[str, Any],
) -> tuple[dict[str, Any], set[tuple[str, str]], dict[tuple[str, str], str]]:
    manifest_items = [
        item for item in manifest.get("requirements", ()) if isinstance(item, Mapping)
    ]
    manifest_keys = {_requirement_record_key(item) for item in manifest_items}
    probed_items = [
        item for item in probe_lock.get("requirements", ()) if isinstance(item, Mapping)
    ]
    probed_keys = {_requirement_record_key(item) for item in probed_items}
    if probed_keys != manifest_keys:
        raise EnvironmentProbeError(
            "target package probe did not cover the complete environment request"
        )

    unresolved: set[tuple[str, str]] = set()
    sources: dict[tuple[str, str], str] = {}
    for item in probed_items:
        key = _requirement_record_key(item)
        if item.get("applicable") is False:
            sources[key] = "not_applicable"
        elif item.get("satisfied") is True:
            sources[key] = "host_runtime"
        else:
            unresolved.add(key)
            sources[key] = "trusted_index"

    filtered = dict(manifest)
    filtered["requirements"] = [
        dict(item) for item in manifest_items if _requirement_record_key(item) in unresolved
    ]
    return filtered, unresolved, sources

def resolve_case_environment(
    *,
    case_dir: str | Path,
    case_id: str,
    target_interpreter: str | Path,
    requirements: Sequence[str | RequirementRequest | Mapping[str, Any]],
    index_identity: str = "pypi",
    trusted_indexes: Mapping[str, TrustedIndex] | None = None,
    dry_run: bool = False,
    force: bool = False,
    run_argv: ArgvRunner = subprocess_argv_runner,
    install_timeout: float = 1800.0,
    probe_timeout: float = 180.0,
    verify_artifacts: bool = False,
    now: Callable[[], datetime] | None = None,
) -> EnvironmentResolution:
    """Resolve, install, probe, and atomically lock one case environment.

    ``dry_run`` writes a request and plan report without invoking *any* command.
    A normal call first validates a matching ready lock and then probes the full
    request in the host-selected interpreter. Only requirements that fail that
    import/version probe are sent to the trusted installer.
    """

    clock = now or (lambda: datetime.now(timezone.utc))
    paths = CaseEnvironmentPaths.under(case_dir)
    paths.root.mkdir(parents=True, exist_ok=True)
    manifest = build_environment_manifest(
        case_id=case_id,
        target_interpreter=target_interpreter,
        requirements=requirements,
        index_identity=index_identity,
        trusted_indexes=trusted_indexes,
    )
    write_json(paths.request, manifest)
    trusted_index = resolve_trusted_index(index_identity, trusted_indexes)
    pip_plan_path = paths.root / ENVIRONMENT_PIP_PLAN_FILENAME
    pip_report_path = paths.root / ENVIRONMENT_PIP_REPORT_FILENAME
    started_at = _timestamp(clock())
    artifact_evidence: dict[str, Any] | None = None

    if dry_run:
        install_argv = build_pip_install_argv(
            manifest,
            trusted_indexes=trusted_indexes,
            report_path=pip_report_path if verify_artifacts else None,
        )
        report = _base_report(manifest, started_at=started_at)
        report.update(
            {
                "status": "planned",
                "ready": False,
                "dry_run": True,
                "cache_hit": False,
                "install": {
                    "attempted": False,
                    "argv": list(install_argv),
                    "returncode": None,
                    "stdout": "",
                    "stderr": "",
                },
                "probe": {"attempted": False, "ok": None, "unresolved": []},
                "lock_path": None,
                "finished_at": _timestamp(clock()),
            }
        )
        write_json(paths.report, report)
        return EnvironmentResolution(paths=paths, manifest=manifest, lock=None, report=report)

    existing_lock = None if force else load_environment_lock(paths.lock)
    if (
        existing_lock is not None
        and existing_lock.get("request_hash") == manifest["request_hash"]
        and existing_lock.get("ready") is True
        and existing_lock.get("resolution_hash") == _resolution_hash(existing_lock)
        and existing_lock.get("environment_hash") == _environment_hash(existing_lock)
    ):
        existing_sources = _resolution_sources_from_lock(existing_lock)
        needs_artifact_evidence = "trusted_index" in existing_sources.values()
        cache_evidence: dict[str, Any] | None = None
        try:
            if verify_artifacts and needs_artifact_evidence:
                cache_evidence = _combine_artifact_evidence(
                    validate_pip_report(pip_plan_path, trusted_index),
                    validate_pip_report(pip_report_path, trusted_index),
                )
                source_policy = existing_lock.get("source_policy")
                if (
                    not isinstance(source_policy, Mapping)
                    or source_policy.get("artifact_evidence") != cache_evidence
                ):
                    raise EnvironmentPolicyError(
                        "cached artifact evidence does not match the active case lock"
                    )
            current_lock = _probe_environment(
                manifest,
                run_argv=run_argv,
                timeout=probe_timeout,
                created_at=str(existing_lock.get("created_at") or started_at),
                artifact_evidence=cache_evidence,
                cwd=paths.root,
            )
            _apply_resolution_sources(current_lock, existing_sources)
        except (EnvironmentProbeError, EnvironmentPolicyError):
            current_lock = None
        if (
            current_lock is not None
            and current_lock.get("ready") is True
            and current_lock.get("resolution_hash")
            == (
                existing_lock.get("resolution_hash")
                or _resolution_hash(existing_lock)
            )
        ):
            report = _base_report(manifest, started_at=started_at)
            report.update(
                {
                    "status": "cached",
                    "ready": True,
                    "dry_run": False,
                    "cache_hit": True,
                    "install": {
                        "attempted": False,
                        "argv": [],
                        "returncode": None,
                        "stdout": "",
                        "stderr": "",
                    },
                    "probe": _probe_report(current_lock, attempted=True),
                    "artifact_evidence": cache_evidence,
                    "lock_path": str(paths.lock),
                    "finished_at": _timestamp(clock()),
                }
            )
            write_json(paths.lock, current_lock)
            write_json(paths.report, report)
            return EnvironmentResolution(
                paths=paths,
                manifest=manifest,
                lock=current_lock,
                report=report,
            )

    try:
        # Revoke stale case evidence before examining the selected interpreter;
        # an interrupted or failed resolution must never leave a ready grant.
        paths.lock.unlink(missing_ok=True)
        pip_plan_path.unlink(missing_ok=True)
        pip_report_path.unlink(missing_ok=True)
        preinstall_lock = _probe_environment(
            manifest,
            run_argv=run_argv,
            timeout=probe_timeout,
            created_at=started_at,
            artifact_evidence=None,
            cwd=paths.root,
        )
        install_manifest, unresolved_keys, resolution_sources = _unresolved_manifest(
            manifest,
            preinstall_lock,
        )
    except Exception as exc:
        report = _base_report(manifest, started_at=started_at)
        report.update(
            {
                "status": "probe_failed",
                "ready": False,
                "dry_run": False,
                "cache_hit": False,
                "install": {
                    "attempted": False,
                    "argv": [],
                    "returncode": None,
                    "stdout": "",
                    "stderr": "",
                },
                "probe": {
                    "attempted": True,
                    "ok": False,
                    "unresolved": [],
                    "error": _bounded_error(exc),
                },
                "lock_path": None,
                "finished_at": _timestamp(clock()),
            }
        )
        write_json(paths.report, report)
        return EnvironmentResolution(paths=paths, manifest=manifest, lock=None, report=report)

    if not unresolved_keys:
        lock = _apply_resolution_sources(preinstall_lock, resolution_sources)
        write_json(paths.lock, lock)
        report = _base_report(manifest, started_at=started_at)
        report.update(
            {
                "status": "ready",
                "ready": True,
                "dry_run": False,
                "cache_hit": False,
                "resolution_mode": "host_runtime_reuse",
                "install": {
                    "attempted": False,
                    "argv": [],
                    "returncode": None,
                    "stdout": "",
                    "stderr": "",
                },
                "probe": _probe_report(lock, attempted=True),
                "artifact_evidence": None,
                "lock_path": str(paths.lock),
                "finished_at": _timestamp(clock()),
            }
        )
        write_json(paths.report, report)
        return EnvironmentResolution(
            paths=paths,
            manifest=manifest,
            lock=lock,
            report=report,
        )

    install_result: CommandResult
    install_argv: tuple[str, ...] = ()
    try:
        with TemporaryDirectory(prefix=".03a-pip-cache-", dir=paths.root) as cache_dir:
            install_argv = build_pip_install_argv(
                install_manifest,
                trusted_indexes=trusted_indexes,
                report_path=pip_report_path if verify_artifacts else None,
                cache_dir=cache_dir,
            )
            if verify_artifacts:
                plan_argv = build_pip_install_argv(
                    install_manifest,
                    trusted_indexes=trusted_indexes,
                    report_path=pip_plan_path,
                    dry_run=True,
                    cache_dir=cache_dir,
                )
                plan_result = _run(
                    run_argv,
                    plan_argv,
                    cwd=None,
                    timeout=install_timeout,
                )
                if plan_result.returncode != 0:
                    raise EnvironmentPolicyError(
                        "trusted dependency resolution failed before installation: "
                        + _bounded_output(plan_result.stderr or plan_result.stdout)
                    )
                plan_evidence = validate_pip_report(pip_plan_path, trusted_index)
                if (
                    not plan_evidence.get("artifacts")
                    and _manifest_has_applicable_requirements(
                        install_manifest,
                        plan_evidence.get("environment"),
                    )
                ):
                    raise EnvironmentPolicyError(
                        "a new case environment cannot establish package provenance "
                        "from an empty pip resolution report"
                    )
            install_result = _run(
                run_argv,
                install_argv,
                cwd=None,
                timeout=install_timeout,
            )
            if verify_artifacts and install_result.returncode == 0:
                artifact_evidence = _combine_artifact_evidence(
                    plan_evidence,
                    validate_pip_report(pip_report_path, trusted_index),
                )
    except Exception as exc:
        report = _base_report(manifest, started_at=started_at)
        report.update(
            {
                "status": "install_failed",
                "ready": False,
                "dry_run": False,
                "cache_hit": False,
                "install": {
                    "attempted": True,
                    "argv": list(install_argv),
                    "returncode": None,
                    "stdout": "",
                    "stderr": _bounded_error(exc),
                },
                "probe": _probe_report(preinstall_lock, attempted=True),
                "lock_path": None,
                "finished_at": _timestamp(clock()),
            }
        )
        write_json(paths.report, report)
        return EnvironmentResolution(paths=paths, manifest=manifest, lock=None, report=report)

    if install_result.returncode != 0:
        report = _base_report(manifest, started_at=started_at)
        report.update(
            {
                "status": "install_failed",
                "ready": False,
                "dry_run": False,
                "cache_hit": False,
                "install": {
                    "attempted": True,
                    "argv": list(install_result.argv),
                    "returncode": install_result.returncode,
                    "stdout": _bounded_output(install_result.stdout),
                    "stderr": _bounded_output(install_result.stderr),
                },
                "probe": _probe_report(preinstall_lock, attempted=True),
                "artifact_evidence": artifact_evidence,
                "lock_path": None,
                "finished_at": _timestamp(clock()),
            }
        )
        write_json(paths.report, report)
        return EnvironmentResolution(paths=paths, manifest=manifest, lock=None, report=report)

    try:
        lock = _probe_environment(
            manifest,
            run_argv=run_argv,
            timeout=probe_timeout,
            created_at=_timestamp(clock()),
            artifact_evidence=artifact_evidence,
            cwd=paths.root,
        )
        _apply_resolution_sources(lock, resolution_sources)
        write_json(paths.lock, lock)
        probe_report = _probe_report(lock, attempted=True)
    except EnvironmentProbeError as exc:
        lock = None
        probe_report = {
            "attempted": True,
            "ok": False,
            "unresolved": [],
            "error": _bounded_error(exc),
        }

    probe_ok = bool(lock and lock.get("ready"))
    status = "ready" if probe_ok else "probe_failed"
    report = _base_report(manifest, started_at=started_at)
    report.update(
        {
            "status": status,
            "ready": status == "ready",
            "dry_run": False,
            "cache_hit": False,
            "install": {
                "attempted": True,
                "argv": list(install_result.argv),
                "returncode": install_result.returncode,
                "stdout": _bounded_output(install_result.stdout),
                "stderr": _bounded_output(install_result.stderr),
            },
            "probe": probe_report,
            "artifact_evidence": artifact_evidence,
            "lock_path": str(paths.lock) if lock is not None else None,
            "finished_at": _timestamp(clock()),
        }
    )
    write_json(paths.report, report)
    return EnvironmentResolution(paths=paths, manifest=manifest, lock=lock, report=report)

def _run(
    runner: ArgvRunner,
    argv: Sequence[str],
    *,
    cwd: Path | None,
    timeout: float | None,
) -> CommandResult:
    result = runner(tuple(str(part) for part in argv), cwd=cwd, timeout=timeout)
    if isinstance(result, CommandResult):
        return result
    result_argv = (
        result.args
        if isinstance(result.args, Sequence) and not isinstance(result.args, str)
        else argv
    )
    return CommandResult(
        argv=tuple(str(part) for part in result_argv),
        returncode=int(result.returncode),
        stdout=result.stdout or "",
        stderr=result.stderr or "",
    )
