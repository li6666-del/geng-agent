"""Dependency request, runtime-lock, and requirements policy."""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name


RuntimeDocument = Mapping[str, Any] | Path | None
KNOWN_PACKAGE_PROFILES: dict[str, dict[str, tuple[str, ...]]] = {
    "numpy": {"import_names": ("numpy",), "aliases": ()},
    "scipy": {"import_names": ("scipy",), "aliases": ()},
    "matplotlib": {"import_names": ("matplotlib",), "aliases": ()},
    "scikit-learn": {"import_names": ("sklearn",), "aliases": ("sklearn",)},
    "reedsolo": {"import_names": ("reedsolo",), "aliases": ()},
    "pillow": {"import_names": ("PIL",), "aliases": ()},
    "pandas": {"import_names": ("pandas",), "aliases": ()},
    "sympy": {"import_names": ("sympy",), "aliases": ()},
    "numba": {"import_names": ("numba",), "aliases": ()},
    "torch": {"import_names": ("torch",), "aliases": ("pytorch",)},
    "brotli": {"import_names": ("brotli",), "aliases": ()},
    "pesq": {"import_names": ("pesq",), "aliases": ()},
    "scikit-commpy": {"import_names": ("commpy",), "aliases": ("commpy",)},
    "galois": {"import_names": ("galois",), "aliases": ()},
    "networkx": {"import_names": ("networkx",), "aliases": ()},
    "h5py": {"import_names": ("h5py",), "aliases": ()},
    "tqdm": {"import_names": ("tqdm",), "aliases": ()},
    "tensorflow": {"import_names": ("tensorflow",), "aliases": ()},
    "jax": {"import_names": ("jax",), "aliases": ()},
    "flax": {"import_names": ("flax",), "aliases": ()},
    "optax": {"import_names": ("optax",), "aliases": ()},
    "pyyaml": {"import_names": ("yaml",), "aliases": ("yaml",)},
    "opencv-python": {"import_names": ("cv2",), "aliases": ()},
}
DEFAULT_REPRO_PACKAGE_PROFILES = frozenset({
    "numpy", "scipy", "matplotlib", "scikit-learn", "reedsolo", "pillow",
    "pandas", "sympy", "numba", "torch", "brotli", "pesq",
    "scikit-commpy", "galois", "networkx", "h5py", "tqdm",
})
_DISTRIBUTION_ALIASES = {
    canonicalize_name(alias): package
    for package, profile in KNOWN_PACKAGE_PROFILES.items()
    for alias in profile.get("aliases", ())
}
REQUIREMENT_IMPORT_NAMES = {
    package: set(profile["import_names"])
    for package, profile in KNOWN_PACKAGE_PROFILES.items()
}
IMPORT_REQUIREMENT_NAMES = {
    import_name: package
    for package, profile in KNOWN_PACKAGE_PROFILES.items()
    for import_name in profile["import_names"]
}
IMPORT_REQUIREMENT_NAMES["mpl_toolkits"] = "matplotlib"
_UNDECLARED_IMPORT_RE = re.compile(
    r"third-party import is not declared in requirements\.txt: .+ \(expected package ([^)]+)\)"
)


def _canonical_distribution_name(value: Any) -> str:
    name = canonicalize_name(str(value or "").strip())
    return _DISTRIBUTION_ALIASES.get(name, name)


def _runtime_document(value: RuntimeDocument) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Path):
        try:
            loaded = json.loads(value.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return loaded if isinstance(loaded, dict) else {}
    return dict(value)


def _record_distribution(record: Any, fallback: str = "") -> str:
    if isinstance(record, str):
        raw_requirement = record
        raw_distribution = ""
    elif isinstance(record, Mapping):
        raw_requirement = str(record.get("requirement") or "")
        raw_distribution = str(
            record.get("distribution") or record.get("package") or record.get("name") or ""
        )
    else:
        return _canonical_distribution_name(fallback)
    if not raw_distribution and raw_requirement:
        try:
            raw_distribution = Requirement(raw_requirement).name
        except InvalidRequirement:
            raw_distribution = raw_requirement
    return _canonical_distribution_name(raw_distribution or fallback)


def _runtime_requirement_records(value: RuntimeDocument) -> dict[str, dict[str, Any]]:
    document = _runtime_document(value)
    records: dict[str, dict[str, Any]] = {}
    raw_requirements = document.get("requirements", [])
    if isinstance(raw_requirements, Mapping):
        iterable = raw_requirements.items()
    elif isinstance(raw_requirements, list):
        iterable = (("", item) for item in raw_requirements)
    else:
        iterable = ()
    for fallback, raw_record in iterable:
        package = _record_distribution(raw_record, str(fallback))
        if not package:
            continue
        if isinstance(raw_record, Mapping):
            records[package] = dict(raw_record)
        else:
            records[package] = {"requirement": str(raw_record), "distribution": package}
    raw_profiles = document.get("package_profiles", {})
    if isinstance(raw_profiles, Mapping):
        for fallback, raw_record in raw_profiles.items():
            package = _record_distribution(raw_record, str(fallback))
            if package and package not in records:
                records[package] = dict(raw_record) if isinstance(raw_record, Mapping) else {}
    return records


def _runtime_import_names(
    package: str,
    *,
    runtime_policy: RuntimeDocument = None,
    runtime_lock: RuntimeDocument = None,
) -> set[str]:
    canonical = _canonical_distribution_name(package)
    names = set(REQUIREMENT_IMPORT_NAMES.get(canonical, ()))
    for document in (runtime_policy, runtime_lock):
        record = _runtime_requirement_records(document).get(canonical, {})
        raw_names = record.get("import_names")
        if isinstance(raw_names, list):
            names.update(str(item).strip() for item in raw_names if str(item).strip())
    return names or {canonical.replace("-", "_")}


def _runtime_dependency_state(
    package: str,
    *,
    runtime_policy: RuntimeDocument = None,
    runtime_lock: RuntimeDocument = None,
    for_import: bool = False,
) -> tuple[str, bool]:
    canonical = _canonical_distribution_name(package)
    if runtime_lock is not None:
        if not _runtime_lock_is_trusted(runtime_lock):
            return "runtime_lock_untrusted_or_malformed", False
        record = _runtime_requirement_records(runtime_lock).get(canonical)
        if record is None:
            return "dependency_lock_unresolved", False
        if record.get("applicable") is False:
            return ("dependency_not_applicable", not for_import)
        if record.get("version_satisfied") is False:
            return "dependency_version_mismatch", False
        if record.get("imports_ok") is False:
            return "dependency_import_probe_failed", False
        if "installed_version" in record and not record.get("installed_version"):
            return "dependency_not_installed", False
        if record.get("satisfied") is True:
            return "ready", True
        return "dependency_lock_unresolved", False
    available = _requirement_imports_available(
        canonical, runtime_policy=runtime_policy, runtime_lock=None
    )
    return ("ready", True) if available else ("dependency_not_installed", False)


def _runtime_lock_is_trusted(runtime_lock: RuntimeDocument) -> bool:
    lock = _runtime_document(runtime_lock)
    source_policy = lock.get("source_policy")
    index = lock.get("index")
    host_provenance = lock.get("host_provenance")
    host_provenance_ok = bool(
        isinstance(host_provenance, Mapping)
        and host_provenance.get("kind") == "geng.host_shared_runtime"
        and host_provenance.get("runtime_mode") == "host_shared"
        and str(host_provenance.get("selected_launcher") or "")
        and str(host_provenance.get("resolved_executable") or "")
        and str(host_provenance.get("prefix") or "")
        and re.fullmatch(
            r"[0-9a-f]{64}",
            str(host_provenance.get("mutex_identity_sha256") or ""),
        )
    )
    evidence = (
        source_policy.get("artifact_evidence")
        if isinstance(source_policy, Mapping)
        else None
    )
    artifacts = evidence.get("artifacts") if isinstance(evidence, Mapping) else None
    artifact_versions = {
        (
            _canonical_distribution_name(item.get("distribution")),
            str(item.get("version") or ""),
        )
        for item in artifacts or []
        if isinstance(item, Mapping)
    }
    requirement_records = lock.get("requirements")
    trusted_index_required = False
    requirements_covered = isinstance(requirement_records, list)
    for item in requirement_records if isinstance(requirement_records, list) else ():
        if not isinstance(item, Mapping):
            requirements_covered = False
            break
        source = str(item.get("resolution_source") or "")
        if item.get("applicable") is False:
            if source != "not_applicable":
                requirements_covered = False
                break
            continue
        if not (
            item.get("satisfied") is True
            and item.get("version_satisfied") is True
            and item.get("imports_ok") is True
            and str(item.get("installed_version") or "")
        ):
            requirements_covered = False
            break
        if source == "host_runtime":
            if not (
                isinstance(source_policy, Mapping)
                and source_policy.get("host_runtime_verified") is True
            ):
                requirements_covered = False
                break
            continue
        if source == "trusted_index":
            trusted_index_required = True
            if (
                _canonical_distribution_name(item.get("distribution")),
                str(item.get("installed_version") or ""),
            ) not in artifact_versions:
                requirements_covered = False
                break
            continue
        requirements_covered = False
        break
    allowed_artifact_hosts = {
        str(host).casefold().rstrip(".")
        for host in (index.get("artifact_hosts") if isinstance(index, Mapping) else []) or []
        if str(host)
    }
    digests_ok = bool(
        isinstance(evidence, Mapping)
        and re.fullmatch(r"[0-9a-f]{64}", str(evidence.get("plan_report_sha256") or ""))
        and re.fullmatch(r"[0-9a-f]{64}", str(evidence.get("install_report_sha256") or ""))
        and isinstance(artifacts, list)
        and all(
            isinstance(item, Mapping)
            and urlsplit(str(item.get("url") or "")).scheme.casefold() == "https"
            and (urlsplit(str(item.get("url") or "")).hostname or "").casefold().rstrip(".")
            in allowed_artifact_hosts
            and re.fullmatch(r"[0-9a-f]{64}", str(item.get("sha256") or ""))
            for item in artifacts
        )
    )
    return bool(
        lock.get("kind") == "geng.case_environment.lock"
        and lock.get("ready") is True
        and lock.get("runtime_mode") == "host_shared"
        and host_provenance_ok
        and isinstance(source_policy, Mapping)
        and source_policy.get("trusted") is True
        and source_policy.get("binary_wheels_only") is True
        and (
            not trusted_index_required
            or (
                source_policy.get("artifact_report_verified") is True
                and digests_ok
            )
        )
        and requirements_covered
        and isinstance(index, Mapping)
        and str(index.get("fingerprint") or "")
    )


def _lock_requirement_matches(parsed: Requirement, record: Mapping[str, Any]) -> bool:
    try:
        locked = Requirement(str(record.get("requirement") or ""))
    except InvalidRequirement:
        return False
    return bool(
        _canonical_distribution_name(parsed.name) == _canonical_distribution_name(locked.name)
        and set(parsed.extras) == set(locked.extras)
        and {str(item) for item in parsed.specifier} == {str(item) for item in locked.specifier}
        and str(parsed.marker or "") == str(locked.marker or "")
    )


def _matching_lock_record(
    parsed: Requirement,
    runtime_lock: RuntimeDocument,
) -> dict[str, Any] | None:
    document = _runtime_document(runtime_lock)
    records = document.get("requirements")
    for raw_record in records if isinstance(records, list) else []:
        if isinstance(raw_record, Mapping) and _lock_requirement_matches(parsed, raw_record):
            return dict(raw_record)
    return None


def _runtime_lock_binding_issues(
    runtime_policy: RuntimeDocument,
    runtime_lock: RuntimeDocument,
) -> list[dict[str, str]]:
    if runtime_lock is None:
        return []
    if not _runtime_lock_is_trusted(runtime_lock):
        return [{
            "file": "03a_environment.lock.json",
            "category": "runtime_lock_untrusted_or_malformed",
            "message": "case runtime lock is missing trusted-source or ready-state evidence",
        }]
    if runtime_policy is None:
        return []
    policy = _runtime_document(runtime_policy)
    lock = _runtime_document(runtime_lock)
    policy_hash = str(policy.get("request_hash") or "")
    lock_hash = str(lock.get("request_hash") or "")
    policy_index = policy.get("index") if isinstance(policy.get("index"), Mapping) else {}
    lock_index = lock.get("index") if isinstance(lock.get("index"), Mapping) else {}
    policy_fingerprint = str(policy_index.get("fingerprint") or "")
    lock_fingerprint = str(lock_index.get("fingerprint") or "")
    if (
        (policy_hash and policy_hash != lock_hash)
        or (policy_fingerprint and policy_fingerprint != lock_fingerprint)
    ):
        return [{
            "file": "03a_environment.lock.json",
            "category": "runtime_lock_binding_mismatch",
            "message": "case runtime lock does not match the active environment request/source",
        }]
    return []


def _parse_safe_requirement(line: str) -> tuple[Requirement | None, str | None]:
    if line.startswith("-") or "://" in line or "/" in line or "\\" in line:
        return None, "unsafe_requirement_syntax"
    try:
        parsed = Requirement(line)
    except InvalidRequirement:
        return None, "invalid_requirement"
    if parsed.url:
        return None, "unsafe_requirement_syntax"
    return parsed, None


def split_requirement_issues(
    issues: list[dict[str, Any]],
    *,
    runtime_policy: RuntimeDocument = None,
    runtime_lock: RuntimeDocument = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split dependency findings into runner-blocking issues and softer warnings.

    In the task-writer workflow, a writer may already have produced trusted full-run
    artifacts. A missing declaration for a dependency proven available by the active
    case lock is reproducibility metadata debt, not evidence that the run failed.
    Missing installations, unresolved locks, unsafe syntax, and broad import fallbacks
    remain blocking.
    """
    blocking: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    for issue in issues:
        target = warnings if is_nonblocking_requirement_issue(
            issue, runtime_policy=runtime_policy, runtime_lock=runtime_lock
        ) else blocking
        item = dict(issue)
        item.setdefault("severity", "warning" if target is warnings else "error")
        target.append(item)
    return blocking, warnings


def is_nonblocking_requirement_issue(
    issue: dict[str, Any],
    *,
    runtime_policy: RuntimeDocument = None,
    runtime_lock: RuntimeDocument = None,
) -> bool:
    message = str(issue.get("message") or "")
    match = _UNDECLARED_IMPORT_RE.fullmatch(message)
    package = _canonical_distribution_name(issue.get("package") or (match.group(1) if match else ""))
    if issue.get("category") == "dependency_declaration_missing" or match:
        if not package:
            return False
        _, available = _runtime_dependency_state(
            package,
            runtime_policy=runtime_policy,
            runtime_lock=runtime_lock,
            for_import=True,
        )
        return available
    if message == "trusted torch backend is used but requirements.txt does not declare torch":
        _, available = _runtime_dependency_state(
            "torch", runtime_policy=runtime_policy, runtime_lock=runtime_lock, for_import=True
        )
        return available
    return False


def _requirement_imports_available(
    package: str,
    *,
    runtime_policy: RuntimeDocument = None,
    runtime_lock: RuntimeDocument = None,
) -> bool:
    try:
        return all(
            importlib.util.find_spec(name) is not None
            for name in import_names_for_requirement(
                package, runtime_policy=runtime_policy, runtime_lock=runtime_lock
            )
        )
    except (ImportError, ValueError):
        return False


def dependency_policy_prompt_text(
    *,
    runtime_policy: RuntimeDocument = None,
    runtime_lock: RuntimeDocument = None,
) -> str:
    available = []
    unavailable = []
    packages = (
        sorted(_runtime_requirement_records(runtime_lock))
        if runtime_lock is not None
        else sorted(KNOWN_PACKAGE_PROFILES)
    )
    for package in packages:
        import_names = sorted(import_names_for_requirement(
            package, runtime_policy=runtime_policy, runtime_lock=runtime_lock
        ))
        item = f"{package} (import: {', '.join(import_names)})"
        _, usable = _runtime_dependency_state(
            package, runtime_policy=runtime_policy, runtime_lock=runtime_lock
        )
        if usable:
            available.append(item)
        else:
            unavailable.append(item)

    lines = [
        "依赖与 import 规则：",
        "1. Python 标准库和本项目本地模块不需要写入 requirements.txt。",
        "2. 按论文的真实科学需求选择第三方库；下面的 known profiles 只是名称/导入提示，不是包白名单。未知包应提交 environment request，绝不能因此换成更弱的标准库或 NumPy 近似。",
        "3. 只要 Python 代码里出现第三方 import，就必须在 requirements.txt 里写对应包名，一行一个包名。",
        "4. requirements.txt 可写包名和普通 PEP 508 版本约束；禁止 URL、VCS、本地路径、自定义 index 和安装参数。安装只能由宿主 resolver 从可信来源执行。",
        "5. 不要用 broad try/except 包住第三方 import 来静默降级；缺库时生成 environment request，等待 case lock 证明已安装并通过 import probe。",
        "6. 标准通信原语优先调用成熟库，但不得用标准实现替换论文真正的自定义算法。",
        "当前 case lock 已验证可用（未提供 lock 时仅为宿主兼容探测）：",
    ]
    lines.extend(f"- {item}" for item in available or ["（无）"])
    if unavailable:
        lines.append("尚未由当前 case lock 验证；应请求 resolver，不得静默降级：")
        lines.extend(f"- {item}" for item in unavailable)
    lines.append(
        "Architecture execution contract precedence: if scientific_architecture/1.1 "
        "requires a framework that is unresolved or unavailable, request the case "
        "environment and report an explicit capability gap. Never silently replace it with a "
        "standard-library placeholder or a scientifically weaker approximation."
    )
    return "\n".join(lines)


def import_names_for_requirement(
    package: str,
    *,
    runtime_policy: RuntimeDocument = None,
    runtime_lock: RuntimeDocument = None,
) -> set[str]:
    return _runtime_import_names(
        package, runtime_policy=runtime_policy, runtime_lock=runtime_lock
    )


def requirement_name_for_import(
    import_root: str,
    *,
    runtime_policy: RuntimeDocument = None,
    runtime_lock: RuntimeDocument = None,
) -> str:
    for document in (runtime_lock, runtime_policy):
        for package, record in _runtime_requirement_records(document).items():
            raw_names = record.get("import_names")
            if isinstance(raw_names, list) and import_root in {
                str(item).split(".", 1)[0] for item in raw_names
            }:
                return package
    known = IMPORT_REQUIREMENT_NAMES.get(import_root)
    if known:
        return known
    return _canonical_distribution_name(import_root)
