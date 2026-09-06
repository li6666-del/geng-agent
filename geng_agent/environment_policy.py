"""Trusted dependency request and package-index policy.

This module contains the declarative, side-effect-free half of case environment
management.  It deliberately does not probe interpreters, launch installers, or
write locks; those transactional responsibilities remain in ``case_environment``.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import subprocess
from typing import Any, Mapping, Protocol, Sequence
from urllib.parse import urlsplit, urlunsplit

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name


ENVIRONMENT_SCHEMA_VERSION = 1
ENVIRONMENT_REQUEST_FILENAME = "03a_environment_request.json"
ENVIRONMENT_LOCK_FILENAME = "03a_environment.lock.json"
ENVIRONMENT_REPORT_FILENAME = "03a_environment_report.json"
ENVIRONMENT_PIP_PLAN_FILENAME = "03a_pip_resolution_report.json"
ENVIRONMENT_PIP_REPORT_FILENAME = "03a_pip_install_report.json"

_SAFE_INDEX_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_IMPORT_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")
_ARCHIVE_SUFFIXES = (".whl", ".tar.gz", ".tar.bz2", ".tar.xz", ".zip")
_VCS_PREFIXES = ("git+", "hg+", "svn+", "bzr+")

# Import-name hints only. Unknown distributions remain valid and use the
# conventional hyphen-to-underscore import name.
KNOWN_IMPORT_NAME_PROFILES: Mapping[str, tuple[str, ...]] = {
    "opencv-python": ("cv2",),
    "pillow": ("PIL",),
    "pyyaml": ("yaml",),
    "python-docx": ("docx",),
    "scikit-commpy": ("commpy",),
    "scikit-learn": ("sklearn",),
}


class EnvironmentPolicyError(ValueError):
    """A writer-controlled dependency request crossed the host policy boundary."""


class EnvironmentProbeError(RuntimeError):
    """The target interpreter did not produce a usable environment probe."""


@dataclass(frozen=True)
class TrustedIndex:
    """A host-owned package index identity."""

    identity: str
    url: str
    artifact_hosts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        identity = self.identity.strip().casefold()
        if not _SAFE_INDEX_ID_RE.fullmatch(identity):
            raise EnvironmentPolicyError(f"invalid trusted index identity: {self.identity!r}")

        normalized_url = _normalize_trusted_index_url(self.url)
        index_host = urlsplit(normalized_url).hostname
        normalized_hosts: list[str] = []
        for raw_host in self.artifact_hosts:
            host = str(raw_host).strip().casefold().rstrip(".")
            if not host or "/" in host or ":" in host or "@" in host:
                raise EnvironmentPolicyError(f"invalid trusted artifact host: {raw_host!r}")
            normalized_hosts.append(host)
        if index_host:
            normalized_hosts.append(index_host.casefold().rstrip("."))

        object.__setattr__(self, "identity", identity)
        object.__setattr__(self, "url", normalized_url)
        object.__setattr__(self, "artifact_hosts", tuple(sorted(set(normalized_hosts))))

    @property
    def fingerprint(self) -> str:
        return _sha256_json(
            {
                "identity": self.identity,
                "url": self.url,
                "artifact_hosts": list(self.artifact_hosts),
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity": self.identity,
            "url": self.url,
            "artifact_hosts": list(self.artifact_hosts),
            "fingerprint": self.fingerprint,
        }


@dataclass(frozen=True)
class RequirementRequest:
    """A dependency request plus non-executable scientific provenance."""

    requirement: str
    import_names: tuple[str, ...] = ()
    requested_by: str | None = None
    reason: str | None = None
    capability: str | None = None
    import_names_explicit: bool | None = None


@dataclass(frozen=True)
class NormalizedRequirement:
    requirement: str
    distribution: str
    extras: tuple[str, ...]
    specifier: str
    marker: str | None
    import_names: tuple[str, ...]
    requested_by: str | None
    reason: str | None
    capability: str | None
    import_names_explicit: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "requirement": self.requirement,
            "distribution": self.distribution,
            "extras": list(self.extras),
            "specifier": self.specifier,
            "marker": self.marker,
            "import_names": list(self.import_names),
            "requested_by": self.requested_by,
            "reason": self.reason,
            "capability": self.capability,
            "import_names_explicit": self.import_names_explicit,
        }


@dataclass(frozen=True)
class CaseEnvironmentPaths:
    root: Path
    request: Path
    lock: Path
    report: Path

    @classmethod
    def under(cls, case_dir: str | Path) -> "CaseEnvironmentPaths":
        root = Path(case_dir).resolve()
        return cls(
            root=root,
            request=root / ENVIRONMENT_REQUEST_FILENAME,
            lock=root / ENVIRONMENT_LOCK_FILENAME,
            report=root / ENVIRONMENT_REPORT_FILENAME,
        )


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""


class ArgvRunner(Protocol):
    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        timeout: float | None = None,
    ) -> CommandResult | subprocess.CompletedProcess[str]:
        """Run an argv vector without invoking a shell."""


@dataclass(frozen=True)
class EnvironmentResolution:
    paths: CaseEnvironmentPaths
    manifest: dict[str, Any]
    lock: dict[str, Any] | None
    report: dict[str, Any]

    @property
    def ready(self) -> bool:
        return bool(self.lock and self.lock.get("ready") and self.report.get("ready"))

    @property
    def cache_hit(self) -> bool:
        return bool(self.report.get("cache_hit"))


def normalize_requirement(
    value: str | RequirementRequest | Mapping[str, Any],
) -> NormalizedRequirement:
    """Normalize one ordinary PEP 508 distribution requirement."""

    request = _coerce_requirement_request(value)
    raw = request.requirement.strip()
    if not raw:
        raise EnvironmentPolicyError("empty Python requirement")
    if "\n" in raw or "\r" in raw:
        raise EnvironmentPolicyError("requirements must contain exactly one PEP 508 item")

    lowered = raw.casefold()
    if raw.startswith("-"):
        raise EnvironmentPolicyError(f"pip options are not allowed in requirements: {raw}")
    if any(prefix in lowered for prefix in _VCS_PREFIXES):
        raise EnvironmentPolicyError(f"VCS requirements are not allowed: {raw}")
    if _looks_like_path_or_archive(raw):
        raise EnvironmentPolicyError(f"path or archive requirements are not allowed: {raw}")

    try:
        parsed = Requirement(raw)
    except InvalidRequirement as exc:
        raise EnvironmentPolicyError(f"invalid PEP 508 requirement: {raw}") from exc
    if parsed.url is not None:
        raise EnvironmentPolicyError(f"URL requirements are not allowed: {raw}")

    distribution = canonicalize_name(parsed.name)
    extras = tuple(sorted(canonicalize_name(extra) for extra in parsed.extras))
    specifiers = tuple(sorted(str(specifier) for specifier in parsed.specifier))
    specifier = ",".join(specifiers)
    marker = str(parsed.marker) if parsed.marker is not None else None

    normalized = distribution
    if extras:
        normalized += f"[{','.join(extras)}]"
    normalized += specifier
    if marker:
        normalized += f"; {marker}"

    import_names_explicit = (
        bool(request.import_names)
        if request.import_names_explicit is None
        else bool(request.import_names_explicit)
    )
    import_names = request.import_names or _default_import_names(distribution)
    normalized_imports = tuple(sorted({_validate_import_name(name) for name in import_names}))
    return NormalizedRequirement(
        requirement=normalized,
        distribution=distribution,
        extras=extras,
        specifier=specifier,
        marker=marker,
        import_names=normalized_imports,
        requested_by=_optional_text(request.requested_by),
        reason=_optional_text(request.reason),
        capability=_optional_text(request.capability),
        import_names_explicit=import_names_explicit,
    )


def resolve_trusted_index(
    identity: str,
    trusted_indexes: Mapping[str, TrustedIndex] | None = None,
) -> TrustedIndex:
    catalog = DEFAULT_TRUSTED_INDEXES if trusted_indexes is None else trusted_indexes
    normalized_identity = str(identity).strip().casefold()
    index = catalog.get(normalized_identity)
    if index is None:
        known = ", ".join(sorted(catalog)) or "(none)"
        raise EnvironmentPolicyError(
            f"untrusted package index identity {identity!r}; host catalog contains: {known}"
        )
    if index.identity != normalized_identity:
        raise EnvironmentPolicyError(
            f"trusted index catalog key {normalized_identity!r} does not match index identity"
        )
    return index


def build_environment_manifest(
    *,
    case_id: str,
    target_interpreter: str | Path,
    requirements: Sequence[str | RequirementRequest | Mapping[str, Any]],
    index_identity: str = "pypi",
    trusted_indexes: Mapping[str, TrustedIndex] | None = None,
) -> dict[str, Any]:
    """Build a deterministic case environment request manifest."""

    normalized_case_id = str(case_id).strip()
    if not normalized_case_id:
        raise EnvironmentPolicyError("case_id must not be empty")
    interpreter = str(target_interpreter).strip()
    if not interpreter:
        raise EnvironmentPolicyError("target_interpreter must not be empty")

    index = resolve_trusted_index(index_identity, trusted_indexes)
    normalized = [normalize_requirement(item) for item in requirements]
    normalized.sort(
        key=lambda item: (
            item.requirement,
            item.import_names,
            item.requested_by or "",
            item.capability or "",
            item.reason or "",
        )
    )
    requirement_items = [item.to_dict() for item in normalized]
    semantic_request = {
        "schema_version": ENVIRONMENT_SCHEMA_VERSION,
        "target_interpreter": interpreter,
        "index_fingerprint": index.fingerprint,
        "requirements": [
            {
                "requirement": item["requirement"],
                "distribution": item["distribution"],
                "import_names": item["import_names"],
                "import_names_explicit": item["import_names_explicit"],
            }
            for item in requirement_items
        ],
    }
    return {
        "schema_version": ENVIRONMENT_SCHEMA_VERSION,
        "kind": "geng.case_environment.request",
        "case_id": normalized_case_id,
        "target_interpreter": interpreter,
        "index": index.to_dict(),
        "requirements": requirement_items,
        "request_hash": _sha256_json(semantic_request),
    }


def build_pip_install_argv(
    manifest: Mapping[str, Any],
    *,
    trusted_indexes: Mapping[str, TrustedIndex] | None = None,
    report_path: str | Path | None = None,
    dry_run: bool = False,
    cache_dir: str | Path | None = None,
) -> tuple[str, ...]:
    """Return the host-owned, shell-free pip installation argv."""

    interpreter = str(manifest.get("target_interpreter") or "").strip()
    index = manifest.get("index")
    requirements = manifest.get("requirements")
    if not interpreter or not isinstance(index, Mapping) or not isinstance(requirements, list):
        raise EnvironmentPolicyError("invalid case environment manifest")

    trusted = resolve_trusted_index(str(index.get("identity") or ""), trusted_indexes)
    if (
        trusted.url != index.get("url")
        or list(trusted.artifact_hosts) != index.get("artifact_hosts")
        or trusted.fingerprint != index.get("fingerprint")
    ):
        raise EnvironmentPolicyError("trusted index fingerprint mismatch")

    normalized_requirements: list[str] = []
    for item in requirements:
        if not isinstance(item, Mapping):
            raise EnvironmentPolicyError("manifest requirements must be objects")
        normalized = normalize_requirement(
            RequirementRequest(
                requirement=str(item.get("requirement") or ""),
                import_names=tuple(item.get("import_names") or ()),
            )
        )
        normalized_requirements.append(normalized.requirement)

    options: list[str] = []
    if dry_run:
        options.append("--dry-run")
    if report_path is not None:
        options.extend(("--report", str(Path(report_path).absolute())))
    if cache_dir is not None:
        options.extend(("--cache-dir", str(Path(cache_dir).absolute())))

    return (
        interpreter,
        "-I",
        "-m",
        "pip",
        "install",
        "--isolated",
        "--disable-pip-version-check",
        "--no-input",
        "--no-compile",
        "--only-binary",
        ":all:",
        "--index-url",
        trusted.url,
        *options,
        *normalized_requirements,
    )


def _coerce_requirement_request(
    value: str | RequirementRequest | Mapping[str, Any],
) -> RequirementRequest:
    if isinstance(value, RequirementRequest):
        return value
    if isinstance(value, str):
        return RequirementRequest(requirement=value)
    if not isinstance(value, Mapping):
        raise EnvironmentPolicyError("requirement request must be a string or object")
    has_import_names = "import_names" in value
    raw_imports = value.get("import_names") or ()
    if isinstance(raw_imports, str):
        import_names = (raw_imports,)
    elif isinstance(raw_imports, Sequence):
        import_names = tuple(str(item) for item in raw_imports)
    else:
        raise EnvironmentPolicyError("import_names must be a string or array of strings")
    return RequirementRequest(
        requirement=str(value.get("requirement") or ""),
        import_names=import_names,
        requested_by=value.get("requested_by"),
        reason=value.get("reason"),
        capability=value.get("capability"),
        import_names_explicit=(
            bool(value.get("import_names_explicit"))
            if "import_names_explicit" in value
            else bool(import_names) if has_import_names else False
        ),
    )


def _default_import_names(distribution: str) -> tuple[str, ...]:
    return KNOWN_IMPORT_NAME_PROFILES.get(distribution, (distribution.replace("-", "_"),))


def _validate_import_name(value: Any) -> str:
    name = str(value).strip()
    if not _IMPORT_NAME_RE.fullmatch(name):
        raise EnvironmentPolicyError(f"invalid Python import name: {value!r}")
    return name


def _looks_like_path_or_archive(value: str) -> bool:
    candidate = value.strip()
    lowered = candidate.casefold()
    if lowered.endswith(_ARCHIVE_SUFFIXES):
        return True
    if candidate.startswith((".", "/", "\\")):
        return True
    if re.match(r"^[A-Za-z]:[\\/]", candidate):
        return True
    return False


def _normalize_trusted_index_url(value: str) -> str:
    parsed = urlsplit(str(value).strip())
    if parsed.scheme.casefold() != "https" or not parsed.hostname:
        raise EnvironmentPolicyError("trusted package indexes must use an absolute HTTPS URL")
    if parsed.username is not None or parsed.password is not None:
        raise EnvironmentPolicyError("trusted package index URLs may not contain credentials")
    if parsed.query or parsed.fragment:
        raise EnvironmentPolicyError("trusted package index URLs may not contain query or fragment")
    host = parsed.hostname.casefold().rstrip(".")
    port = f":{parsed.port}" if parsed.port is not None else ""
    path = "/" + parsed.path.strip("/") if parsed.path.strip("/") else ""
    return urlunsplit(("https", f"{host}{port}", path, "", ""))


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _sha256_json(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


PYPI_INDEX = TrustedIndex(
    identity="pypi",
    url="https://pypi.org/simple",
    artifact_hosts=("files.pythonhosted.org",),
)
DEFAULT_TRUSTED_INDEXES: Mapping[str, TrustedIndex] = {PYPI_INDEX.identity: PYPI_INDEX}


__all__ = [
    "ArgvRunner",
    "CaseEnvironmentPaths",
    "CommandResult",
    "DEFAULT_TRUSTED_INDEXES",
    "ENVIRONMENT_LOCK_FILENAME",
    "ENVIRONMENT_PIP_PLAN_FILENAME",
    "ENVIRONMENT_PIP_REPORT_FILENAME",
    "ENVIRONMENT_REPORT_FILENAME",
    "ENVIRONMENT_REQUEST_FILENAME",
    "ENVIRONMENT_SCHEMA_VERSION",
    "EnvironmentPolicyError",
    "EnvironmentProbeError",
    "EnvironmentResolution",
    "KNOWN_IMPORT_NAME_PROFILES",
    "NormalizedRequirement",
    "PYPI_INDEX",
    "RequirementRequest",
    "TrustedIndex",
    "_sha256_json",
    "build_environment_manifest",
    "build_pip_install_argv",
    "normalize_requirement",
    "resolve_trusted_index",
]
