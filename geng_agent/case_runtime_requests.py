"""Architecture and writer dependency requests for case runtimes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from .case_environment import (
    EnvironmentPolicyError,
    RequirementRequest,
    normalize_requirement,
)
from .case_runtime_contracts import (
    CaseRuntime,
    WRITER_ENVIRONMENT_REQUEST_FILENAME,
    _FOUNDATION_BASELINE_REQUIREMENTS,
    _coerce_normalized_request,
    _dedupe_requests,
    _normalized_request,
    _request_for_runtime_name,
)
from .case_runtime_probe import _read_regular_file_nofollow
from .security import import_names_for_requirement


def requirements_from_scientific_architecture(
    architecture: Mapping[str, Any] | None,
) -> tuple[RequirementRequest, ...]:
    """Translate execution choices into resolver requests without choosing the stack."""

    requests: list[RequirementRequest] = [
        RequirementRequest(
            requirement=name,
            import_names=tuple(sorted(import_names_for_requirement(name))),
            requested_by="foundation_baseline",
            reason="shared numerical and figure runtime used by generated projects",
        )
        for name in _FOUNDATION_BASELINE_REQUIREMENTS
    ]
    components = architecture.get("components") if isinstance(architecture, Mapping) else []
    for index, component in enumerate(components if isinstance(components, list) else []):
        if not isinstance(component, Mapping):
            continue
        component_id = str(component.get("id") or f"component_{index}")
        execution = component.get("execution")
        if not isinstance(execution, Mapping):
            continue
        device_policy = str(execution.get("device_policy") or "cpu")
        primary = str(execution.get("primary_framework") or "").strip()
        if primary and device_policy != "external_runtime":
            request = _request_for_runtime_name(
                primary,
                requested_by=f"architecture:{component_id}",
                reason="scientific architecture primary framework",
                capability=device_policy,
            )
            if request is not None:
                requests.append(request)
        supporting = execution.get("supporting_libraries")
        for raw_library in supporting if isinstance(supporting, list) else []:
            request = _request_for_runtime_name(
                str(raw_library),
                requested_by=f"architecture:{component_id}",
                reason="scientific architecture supporting library",
            )
            if request is not None:
                requests.append(request)
    return _dedupe_requests(requests)

def read_environment_request(
    *,
    sandbox: Path,
    source: str,
) -> tuple[RequirementRequest, ...]:
    """Read a writer request without following links or accepting install controls."""

    path = sandbox / WRITER_ENVIRONMENT_REQUEST_FILENAME
    raw = _read_regular_file_nofollow(path, max_bytes=64 * 1024, allow_missing=True)
    if raw is None:
        return ()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EnvironmentPolicyError("environment request must be valid JSON") from exc
    raw_requirements = payload.get("requirements") if isinstance(payload, Mapping) else None
    if not isinstance(raw_requirements, list) or not raw_requirements:
        raise EnvironmentPolicyError("environment request must contain a non-empty requirements array")
    requests = [
        _normalized_request(_coerce_normalized_request(item), requested_by=source)
        for item in raw_requirements
    ]
    return _dedupe_requests(requests)


def requirements_missing_from_lock(
    requirements_path: Path,
    lock: Mapping[str, Any],
    *,
    source: str,
) -> tuple[RequirementRequest, ...]:
    """Return declarations whose constraints are not satisfied by the active lock."""

    raw = _read_regular_file_nofollow(
        requirements_path,
        max_bytes=1024 * 1024,
        allow_missing=True,
    )
    if raw is None:
        return ()
    requests: list[RequirementRequest] = []
    for line_no, raw_line in enumerate(
        raw.decode("utf-8", errors="replace").splitlines(),
        start=1,
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            normalized = normalize_requirement(line)
        except EnvironmentPolicyError as exc:
            raise EnvironmentPolicyError(
                f"requirements.txt contains an invalid or unsafe PEP 508 entry at line {line_no}"
            ) from None
        if _lock_satisfies_requirement(normalized, lock):
            continue
        requests.append(
            RequirementRequest(
                requirement=normalized.requirement,
                import_names=normalized.import_names,
                import_names_explicit=False,
                requested_by=source,
                reason="generated code declared a dependency absent from the active case lock",
            )
        )
    return _dedupe_requests(requests)


def _lock_satisfies_requirement(
    normalized: Any,
    lock: Mapping[str, Any],
) -> bool:
    """Use verified installed state, not declaration-string equality."""

    parsed = Requirement(normalized.requirement)
    marker_environment = lock.get("interpreter", {}).get("marker_environment", {})
    if parsed.marker is not None:
        try:
            if not parsed.marker.evaluate(
                environment=(marker_environment if isinstance(marker_environment, Mapping) else None)
            ):
                return True
        except (KeyError, ValueError):
            pass

    requested_extras = set(normalized.extras)
    for item in lock.get("requirements", []):
        if not isinstance(item, Mapping) or not (
            item.get("applicable") is True
            and item.get("satisfied") is True
            and item.get("version_satisfied") is True
            and item.get("imports_ok") is True
        ):
            continue
        distribution = canonicalize_name(str(item.get("distribution") or ""))
        if distribution != normalized.distribution:
            continue
        installed_version = str(item.get("installed_version") or "").strip()
        if not installed_version:
            continue
        raw_locked_requirement = str(item.get("requirement") or distribution)
        try:
            locked_extras = set(normalize_requirement(raw_locked_requirement).extras)
        except EnvironmentPolicyError:
            locked_extras = set()
        if not requested_extras.issubset(locked_extras):
            continue
        if parsed.specifier and (
            not parsed.specifier.contains(installed_version, prereleases=True)
        ):
            continue
        return True
    return False


def environment_request_prompt(runtime: CaseRuntime) -> str:
    return f"""The host has selected the case Python interpreter `{runtime.python_executable}`.
Do not run pip, conda, apt, curl, or any installer. Use only dependencies proven by
`{runtime.lock_path.name}`. If faithful implementation needs another Python package,
write `{WRITER_ENVIRONMENT_REQUEST_FILENAME}` as JSON with a `requirements` array.
Each item may contain only `requirement`, `import_names`, and `reason`; use an ordinary
PEP 508 name/version constraint. URLs, VCS/path references, index options, and shell
commands are forbidden. Stop after writing the request. The host will resolve it from
a trusted source, update the dynamic case lock, verify real capability, and restart
this writer in a clean sandbox. Never downgrade the paper's framework or algorithm
merely because the current lock lacks a package."""

def _persisted_host_requests(path: Path) -> tuple[RequirementRequest, ...]:
    try:
        raw = _read_regular_file_nofollow(path, max_bytes=1024 * 1024, allow_missing=True)
        if raw is None:
            return ()
        payload = json.loads(raw.decode("utf-8"))
    except (EnvironmentPolicyError, UnicodeDecodeError, json.JSONDecodeError):
        return ()
    items = payload.get("requirements") if isinstance(payload, Mapping) else None
    if not isinstance(items, list):
        return ()
    result: list[RequirementRequest] = []
    for item in items:
        try:
            result.append(_coerce_normalized_request(item))
        except EnvironmentPolicyError:
            continue
    return tuple(result)
