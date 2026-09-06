"""Interpreter probing and semantic environment-lock hashing."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import stat
from typing import Any, Mapping, Sequence

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from .environment_policy import (
    ArgvRunner,
    CommandResult,
    ENVIRONMENT_SCHEMA_VERSION,
    EnvironmentProbeError,
    _sha256_json,
)


_PROBE_OUTPUT_PREFIX = "GENG_CASE_ENVIRONMENT_JSON:"


def _probe_environment(
    manifest: Mapping[str, Any],
    *,
    run_argv: ArgvRunner,
    timeout: float,
    created_at: str,
    artifact_evidence: Mapping[str, Any] | None = None,
    cwd: Path | None = None,
) -> dict[str, Any]:
    interpreter = str(manifest["target_interpreter"])
    identity_result = _run(
        run_argv,
        (
            interpreter,
            "-I",
            "-c",
            _INTERPRETER_IDENTITY_SCRIPT,
            json.dumps({"mode": "identity"}, separators=(",", ":")),
        ),
        cwd=cwd,
        timeout=timeout,
    )
    identity = _parse_probe_result(identity_result, "target interpreter identity")
    marker_environment = identity.get("marker_environment")
    if not isinstance(marker_environment, dict):
        raise EnvironmentProbeError("target interpreter omitted marker_environment")

    requirement_items = manifest.get("requirements")
    if not isinstance(requirement_items, list):
        raise EnvironmentProbeError("environment manifest requirements are invalid")

    applicable: dict[str, bool] = {}
    grouped_imports: dict[str, dict[str, Any]] = {}
    for item in requirement_items:
        normalized = str(item.get("requirement") or "")
        try:
            parsed = Requirement(normalized)
            applies = parsed.marker is None or parsed.marker.evaluate(environment=marker_environment)
        except Exception as exc:
            raise EnvironmentProbeError(f"cannot evaluate requirement marker: {normalized}") from exc
        applicable[normalized] = applies
        if applies:
            distribution = str(item.get("distribution") or "")
            group = grouped_imports.setdefault(distribution, {"all": set(), "explicit": set()})
            names = {str(name) for name in item.get("import_names") or ()}
            group["all"].update(names)
            if item.get("import_names_explicit") is True:
                group["explicit"].update(names)

    package_payload = {
        "mode": "packages",
        "packages": [
            {
                "distribution": distribution,
                "import_names": sorted(values["explicit"] or values["all"]),
                "allow_discovery": not bool(values["explicit"]),
            }
            for distribution, values in sorted(grouped_imports.items())
        ],
    }
    packages_result = _run(
        run_argv,
        (
            interpreter,
            "-I",
            "-c",
            _PACKAGE_PROBE_SCRIPT,
            json.dumps(package_payload, ensure_ascii=False, separators=(",", ":")),
        ),
        cwd=cwd,
        timeout=timeout,
    )
    packages_payload = _parse_probe_result(packages_result, "target package import/version")
    raw_packages = packages_payload.get("packages")
    if not isinstance(raw_packages, list):
        raise EnvironmentProbeError("target package probe omitted packages")
    package_by_distribution = {
        canonicalize_name(str(item.get("distribution") or "")): item
        for item in raw_packages
        if isinstance(item, dict) and item.get("distribution")
    }

    locked_requirements: list[dict[str, Any]] = []
    for item in requirement_items:
        normalized = str(item["requirement"])
        distribution = canonicalize_name(str(item["distribution"]))
        applies = applicable[normalized]
        raw_probe = package_by_distribution.get(distribution, {})
        installed_version = raw_probe.get("installed_version")
        imports = raw_probe.get("imports") if isinstance(raw_probe.get("imports"), dict) else {}
        successful_imports = tuple(
            str(name) for name in raw_probe.get("successful_import_names") or () if str(name)
        )
        if not applies:
            version_satisfied = True
            imports_ok = True
            satisfied = True
            state = "not_applicable"
            expected_imports: tuple[str, ...] = ()
        else:
            version_satisfied = _version_satisfies(normalized, installed_version)
            declared_imports = tuple(str(name) for name in item.get("import_names") or ())
            expected_imports = (
                declared_imports
                if item.get("import_names_explicit") is True
                else successful_imports or declared_imports
            )
            imports_ok = bool(expected_imports) and all(
                isinstance(imports.get(name), dict) and imports[name].get("ok") is True
                for name in expected_imports
            )
            satisfied = installed_version is not None and version_satisfied and imports_ok
            state = "ready" if satisfied else "unresolved"
        locked_requirements.append(
            {
                "requirement": normalized,
                "distribution": distribution,
                "import_names": list(expected_imports if applies else item.get("import_names") or ()),
                "import_names_explicit": item.get("import_names_explicit") is True,
                "applicable": applies,
                "installed_version": installed_version,
                "version_satisfied": version_satisfied,
                "imports_ok": imports_ok,
                "imports": imports if applies else {},
                "satisfied": satisfied,
                "state": state,
            }
        )

    installed_distributions = _normalize_installed_distributions(
        packages_payload.get("installed_distributions")
    )
    ready = all(item["satisfied"] for item in locked_requirements)
    lock: dict[str, Any] = {
        "schema_version": ENVIRONMENT_SCHEMA_VERSION,
        "kind": "geng.case_environment.lock",
        "case_id": manifest["case_id"],
        "request_hash": manifest["request_hash"],
        "target_interpreter": manifest["target_interpreter"],
        "index": manifest["index"],
        "source_policy": {
            "trusted": True,
            "isolated_pip": True,
            "binary_wheels_only": True,
            "credential_environment_sanitized": True,
            "probe_isolation": (
                "unprivileged_no_new_privileges_plus_python_audit"
                if os.name != "nt"
                and getattr(os, "geteuid", lambda: -1)() == 0
                and shutil.which("setpriv")
                and _unprivileged_executable_path(Path(interpreter))
                else "sanitized_python_audit"
            ),
            "writer_supplied_urls_allowed": False,
            "writer_supplied_options_allowed": False,
            "artifact_report_verified": artifact_evidence is not None,
            "artifact_evidence": dict(artifact_evidence or {}),
        },
        "interpreter": identity,
        "requirements": locked_requirements,
        "installed_distributions": installed_distributions,
        "ready": ready,
        "created_at": created_at,
    }
    lock["resolution_hash"] = _resolution_hash(lock)
    lock["environment_hash"] = _environment_hash(lock)
    return lock


def _version_satisfies(requirement: str, installed_version: Any) -> bool:
    if not isinstance(installed_version, str) or not installed_version.strip():
        return False
    parsed = Requirement(requirement)
    if not parsed.specifier:
        return True
    try:
        return parsed.specifier.contains(Version(installed_version), prereleases=None)
    except InvalidVersion:
        return False


def _normalize_installed_distributions(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    normalized: dict[str, str] = {}
    for item in value:
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("distribution") or "").strip()
        version = str(item.get("version") or "").strip()
        if name and version:
            normalized[canonicalize_name(name)] = version
    return [
        {"distribution": distribution, "version": version}
        for distribution, version in sorted(normalized.items())
    ]


def _runtime_semantic_state(
    lock: Mapping[str, Any],
    *,
    include_capabilities: bool,
) -> dict[str, Any]:
    interpreter = lock.get("interpreter") if isinstance(lock.get("interpreter"), Mapping) else {}
    requirements = lock.get("requirements") if isinstance(lock.get("requirements"), list) else []
    source_policy = lock.get("source_policy") if isinstance(lock.get("source_policy"), Mapping) else {}
    evidence = (
        source_policy.get("artifact_evidence")
        if isinstance(source_policy.get("artifact_evidence"), Mapping)
        else {}
    )
    artifacts = evidence.get("artifacts") if isinstance(evidence.get("artifacts"), list) else []
    semantic: dict[str, Any] = {
        "schema_version": ENVIRONMENT_SCHEMA_VERSION,
        "request_hash": lock.get("request_hash"),
        "index_fingerprint": (
            lock.get("index", {}).get("fingerprint")
            if isinstance(lock.get("index"), Mapping)
            else None
        ),
        "interpreter": {
            "executable": interpreter.get("executable"),
            "python_full_version": interpreter.get("python_full_version"),
            "implementation": interpreter.get("implementation"),
            "marker_environment": interpreter.get("marker_environment"),
        },
        "requirements": [
            {
                "requirement": item.get("requirement"),
                "applicable": item.get("applicable"),
                "installed_version": item.get("installed_version"),
                "version_satisfied": item.get("version_satisfied"),
                "imports_ok": item.get("imports_ok"),
                "satisfied": item.get("satisfied"),
                "resolution_source": item.get("resolution_source"),
            }
            for item in requirements
            if isinstance(item, Mapping)
        ],
        "installed_distributions": lock.get("installed_distributions"),
        "artifacts": sorted(
            (
                {
                    "distribution": item.get("distribution"),
                    "version": item.get("version"),
                    "url": item.get("url"),
                    "sha256": item.get("sha256"),
                }
                for item in artifacts
                if isinstance(item, Mapping)
            ),
            key=lambda item: (
                str(item.get("distribution") or ""),
                str(item.get("version") or ""),
                str(item.get("url") or ""),
                str(item.get("sha256") or ""),
            ),
        ),
    }
    if include_capabilities:
        semantic["capabilities"] = lock.get("capabilities")
        semantic["venv_provenance"] = lock.get("venv_provenance")
    return semantic


def _resolution_hash(lock: Mapping[str, Any]) -> str:
    return _sha256_json(_runtime_semantic_state(lock, include_capabilities=False))


def _environment_hash(lock: Mapping[str, Any]) -> str:
    return _sha256_json(_runtime_semantic_state(lock, include_capabilities=True))


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


def _parse_probe_result(result: CommandResult, purpose: str) -> dict[str, Any]:
    if result.returncode != 0:
        detail = _bounded_output(result.stderr or result.stdout)
        raise EnvironmentProbeError(
            f"{purpose} probe exited with {result.returncode}: {detail or 'no output'}"
        )
    for line in reversed(result.stdout.splitlines()):
        if not line.startswith(_PROBE_OUTPUT_PREFIX):
            continue
        try:
            value = json.loads(line[len(_PROBE_OUTPUT_PREFIX) :])
        except json.JSONDecodeError as exc:
            raise EnvironmentProbeError(f"{purpose} probe returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise EnvironmentProbeError(f"{purpose} probe result must be an object")
        return value
    raise EnvironmentProbeError(f"{purpose} probe did not return its result marker")


def _unprivileged_executable_path(path: Path) -> bool:
    """Return true when uid 65534 can traverse and execute the selected launcher."""

    if os.name == "nt":
        return False
    try:
        absolute = path.absolute()
        resolved = absolute.resolve(strict=True)
        if not absolute.is_file() or not resolved.is_file():
            return False
        for candidate in {absolute, resolved}:
            if not (candidate.stat().st_mode & stat.S_IXOTH):
                return False
            current = candidate.parent
            while True:
                if not (current.stat().st_mode & stat.S_IXOTH):
                    return False
                if current == current.parent:
                    break
                current = current.parent
        return True
    except OSError:
        return False


def _bounded_output(value: Any, limit: int = 8000) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[:limit] + "\n...[truncated]"


_INTERPRETER_IDENTITY_SCRIPT = r"""
import json
import os
import platform
import sys

def implementation_version():
    version = sys.implementation.version
    result = ".".join(str(part) for part in version[:3])
    if version.releaselevel != "final":
        result += version.releaselevel[0] + str(version.serial)
    return result

payload = {
    "executable": sys.executable,
    "prefix": sys.prefix,
    "base_prefix": sys.base_prefix,
    "sys_path": [path for path in sys.path if path],
    "python_full_version": platform.python_version(),
    "python_version": ".".join(platform.python_version_tuple()[:2]),
    "implementation": platform.python_implementation(),
    "marker_environment": {
        "implementation_name": sys.implementation.name,
        "implementation_version": implementation_version(),
        "os_name": os.name,
        "platform_machine": platform.machine(),
        "platform_release": platform.release(),
        "platform_system": platform.system(),
        "platform_version": platform.version(),
        "python_full_version": platform.python_version(),
        "platform_python_implementation": platform.python_implementation(),
        "python_version": ".".join(platform.python_version_tuple()[:2]),
        "sys_platform": sys.platform,
        "extra": "",
    },
}
print("GENG_CASE_ENVIRONMENT_JSON:" + json.dumps(payload, sort_keys=True))
"""


_PACKAGE_PROBE_SCRIPT = r"""
import importlib
import importlib.metadata
import json
import os
import re
import sys

def real_path(value):
    try:
        return os.path.realpath(os.fspath(value))
    except (OSError, TypeError, ValueError):
        return ""

allowed_roots = {
    real_path(path)
    for path in [sys.prefix, sys.base_prefix, os.getcwd(), os.environ.get("HOME"), *sys.path]
    if path
}
writable_roots = {
    real_path(path)
    for path in (os.environ.get("HOME"), os.environ.get("TMPDIR"), os.environ.get("TEMP"))
    if path
}
allowed_roots.update(
    path for path in (
        "/usr/lib", "/usr/lib64", "/lib", "/lib64", "/usr/share",
        "/etc/ssl/certs", "/dev", "/proc/cpuinfo", "/proc/meminfo",
        "/proc/self", "/proc/driver/nvidia", "/sys/bus/pci/devices",
    )
    if os.path.exists(path)
)

def inside_allowed(path):
    resolved = real_path(path)
    if not resolved:
        return False
    return any(resolved == root or resolved.startswith(root + os.sep) for root in allowed_roots)

def inside_writable(path):
    resolved = real_path(path)
    if not resolved:
        return False
    return any(resolved == root or resolved.startswith(root + os.sep) for root in writable_roots)

def open_is_write(mode, flags):
    if isinstance(mode, str) and any(marker in mode for marker in ("w", "a", "x", "+")):
        return True
    if isinstance(flags, int):
        write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
        return bool(flags & write_flags)
    return False

def audit(event, args):
    if event.startswith("socket."):
        raise PermissionError("network is disabled during dependency probes")
    if (
        event == "subprocess.Popen" or event == "os.system"
        or event.startswith("os.spawn") or event.startswith("os.posix_spawn")
        or event.startswith("os.exec") or event in {"os.fork", "os.forkpty"}
    ):
        raise PermissionError("process creation is disabled during dependency probes")
    if event == "open" and args and not isinstance(args[0], int):
        mode = args[1] if len(args) > 1 else None
        flags = args[2] if len(args) > 2 else None
        if open_is_write(mode, flags):
            if not inside_writable(args[0]):
                raise PermissionError("writes outside the dependency probe scratch space are disabled")
        elif not inside_allowed(args[0]):
            raise PermissionError("file access outside the dependency probe sandbox is disabled")
    if event in {"os.chdir", "os.listdir", "os.scandir"} and args and not inside_allowed(args[0]):
        raise PermissionError("filesystem traversal outside the dependency probe sandbox is disabled")
    if event in {
        "os.remove", "os.rmdir", "os.mkdir", "os.rename", "os.replace",
        "os.link", "os.symlink", "os.chmod", "os.chown", "os.truncate",
    }:
        for value in args[:2]:
            if isinstance(value, (str, bytes, os.PathLike)) and not inside_writable(value):
                raise PermissionError("filesystem mutation outside dependency probe scratch space is disabled")

sys.addaudithook(audit)

def valid_import_name(value):
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*", value))

def discovered_top_levels(metadata):
    candidates = set()
    try:
        top_level = metadata.read_text("top_level.txt") or ""
    except BaseException:
        top_level = ""
    for line in top_level.splitlines():
        name = line.strip()
        if valid_import_name(name):
            candidates.add(name)
    if candidates:
        return sorted(candidates)
    try:
        files = metadata.files or ()
    except BaseException:
        files = ()
    for entry in files:
        text = str(entry).replace("\\", "/")
        first = text.split("/", 1)[0]
        lowered = first.casefold()
        if lowered.endswith((".dist-info", ".egg-info", ".data")):
            continue
        if first.endswith(".py"):
            first = first[:-3]
        if valid_import_name(first):
            candidates.add(first)
    return sorted(candidates)

def try_import(name, imports):
    try:
        importlib.import_module(name)
        imports[name] = {"ok": True, "error": None}
        return True
    except BaseException as exc:
        imports[name] = {"ok": False, "error": type(exc).__name__ + ": " + str(exc)}
        return False

request = json.loads(sys.argv[1])
packages = []
for item in request.get("packages", []):
    distribution = item["distribution"]
    try:
        metadata = importlib.metadata.distribution(distribution)
        installed_version = metadata.version
        version_error = None
    except BaseException as exc:
        metadata = None
        installed_version = None
        version_error = type(exc).__name__ + ": " + str(exc)
    imports = {}
    successful = []
    for import_name in item.get("import_names", []):
        if try_import(import_name, imports):
            successful.append(import_name)
    if item.get("allow_discovery") and not successful and metadata is not None:
        for import_name in discovered_top_levels(metadata):
            if import_name in imports:
                continue
            if try_import(import_name, imports):
                successful.append(import_name)
                break
    packages.append(
        {
            "distribution": distribution,
            "installed_version": installed_version,
            "version_error": version_error,
            "imports": imports,
            "successful_import_names": successful,
        }
    )

installed = {}
for distribution in importlib.metadata.distributions():
    name = distribution.metadata.get("Name")
    if not name:
        continue
    canonical = re.sub(r"[-_.]+", "-", name).lower()
    installed[canonical] = distribution.version

payload = {
    "packages": packages,
    "installed_distributions": [
        {"distribution": name, "version": version}
        for name, version in sorted(installed.items())
    ],
}
print("GENG_CASE_ENVIRONMENT_JSON:" + json.dumps(payload, sort_keys=True))
"""


__all__ = [
    "_environment_hash",
    "_probe_environment",
    "_resolution_hash",
    "_unprivileged_executable_path",
]
