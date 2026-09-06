"""Case-runtime data contracts, errors, constants, and request normalization."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from packaging.utils import canonicalize_name

from .case_environment import (
    KNOWN_IMPORT_NAME_PROFILES,
    RequirementRequest,
    normalize_requirement,
)


CASE_RUNTIME_DIRNAME = "03a_case_environment"
WRITER_ENVIRONMENT_REQUEST_FILENAME = "environment_request.json"
HOST_SHARED_RUNTIME_MODE = "host_shared"
_FOUNDATION_BASELINE_REQUIREMENTS = ("numpy", "matplotlib")
_LOCAL_RUNTIME_NAMES = {
    "builtin",
    "builtins",
    "project-local",
    "project_local",
    "python",
    "python-standard-library",
    "standard-library",
    "standard_library",
    "stdlib",
}
_DISTRIBUTION_ALIASES = {
    "pytorch": "torch",
    "sklearn": "scikit-learn",
    "yaml": "pyyaml",
    "opencv": "opencv-python",
}
_CAPABILITY_PROBE_PREFIX = "GENG_CASE_CAPABILITY_JSON:"
_CASE_VENV_MARKER = ".geng_host_venv.json"
_BASE_RUNTIME_MARKER = ".geng_host_runtime.json"
_BASE_RUNTIME_SELECTION_VERSION = 2
_INSTALL_TAINT_SUFFIX = ".install-tainted"

class EnvironmentResolutionError(RuntimeError):
    """A case environment could not be proven usable."""

    def __init__(self, category: str, message: str, *, report: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.category = category
        self.report = dict(report or {})


class EnvironmentRequestRequired(RuntimeError):
    """A writer requested a safe dependency that only the host may install."""

    def __init__(self, requests: Sequence[RequirementRequest], *, source: str):
        normalized = tuple(_normalized_request(item, requested_by=source) for item in requests)
        super().__init__(f"{source} requested {len(normalized)} case-environment extension(s)")
        self.requests = normalized
        self.source = source


@dataclass(frozen=True)
class CaseRuntime:
    venv_dir: Path
    python_executable: Path
    request_path: Path
    lock_path: Path
    report_path: Path
    environment_hash: str
    manifest: dict[str, Any]
    lock: dict[str, Any]
    report: dict[str, Any]
    trusted_read_roots: tuple[Path, ...]

    @property
    def fingerprint(self) -> str:
        return self.environment_hash

def _request_for_runtime_name(
    value: str,
    *,
    requested_by: str,
    reason: str,
    capability: str | None = None,
) -> RequirementRequest | None:
    raw = value.strip()
    key = canonicalize_name(raw)
    if not raw or key in _LOCAL_RUNTIME_NAMES:
        return None
    distribution = _DISTRIBUTION_ALIASES.get(key, key)
    known_imports = KNOWN_IMPORT_NAME_PROFILES.get(distribution, ())
    return RequirementRequest(
        requirement=distribution,
        import_names=tuple(sorted(known_imports)),
        requested_by=requested_by,
        reason=reason,
        capability=capability,
        import_names_explicit=bool(known_imports),
    )


def _coerce_normalized_request(
    value: RequirementRequest | str | Mapping[str, Any],
) -> RequirementRequest:
    normalized = normalize_requirement(value)
    return RequirementRequest(
        requirement=normalized.requirement,
        import_names=normalized.import_names,
        requested_by=normalized.requested_by,
        reason=normalized.reason,
        capability=normalized.capability,
        import_names_explicit=normalized.import_names_explicit,
    )


def _normalized_request(request: RequirementRequest, *, requested_by: str) -> RequirementRequest:
    normalized = normalize_requirement(request)
    return RequirementRequest(
        requirement=normalized.requirement,
        import_names=normalized.import_names,
        requested_by=requested_by,
        reason=normalized.reason,
        capability=normalized.capability,
        import_names_explicit=normalized.import_names_explicit,
    )


def _dedupe_requests(requests: Sequence[RequirementRequest]) -> tuple[RequirementRequest, ...]:
    unique: dict[tuple[str, tuple[str, ...]], RequirementRequest] = {}
    for request in requests:
        normalized = _coerce_normalized_request(request)
        key = (normalized.requirement, tuple(sorted(normalized.import_names)))
        previous = unique.get(key)
        if previous is None or (
            normalized.import_names_explicit is True
            and previous.import_names_explicit is not True
        ):
            unique[key] = normalized
    return tuple(unique[key] for key in sorted(unique))
