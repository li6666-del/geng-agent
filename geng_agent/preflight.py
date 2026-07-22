"""Environment preflight for a fresh deployment.

Goal: BEFORE a user feeds in a PDF, make the local requirements explicit and
checkable on whatever machine is running geng-agent — which Python version and
which libraries must be installed. A misconfigured machine (no numpy, wrong
Python) weakens or blocks task-writer reproduction, so this module surfaces it
before a paper run starts.

Two distinct dependency classes, both required on a new machine:
  1. orchestrator deps  -> needed to even start geng-agent (parse PDF, render
     pages, validate JSON, write Word). Declared in pyproject [project].
  2. repro whitelist     -> the third-party libs the *generated* reproduction
     code is allowed to import. Single source of truth is
     security.ALLOWED_REQUIREMENTS; if these are absent the generator is told
     "don't use numpy" and the task writer cannot deliver a valid full run.

This module never imports heavy packages, so `geng-agent doctor` still runs on
a machine that is missing them. The normal environment report uses
``find_spec`` / package metadata only; the architecture-planning inventory may
also issue one bounded, read-only ``nvidia-smi`` query to distinguish package
availability from accelerator hardware presence.
"""

from __future__ import annotations

import importlib.metadata as importlib_metadata
import importlib.util
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Any

from .codex_runner import split_command
from .config import get_config_value
from .security import ALLOWED_REQUIREMENTS, import_names_for_requirement

# Minimum interpreter. Keep in sync with pyproject `requires-python`.
REQUIRED_PYTHON: tuple[int, int] = (3, 11)

# (pip/PyPI name, import name, human purpose). Mirrors pyproject [project.dependencies].
ORCHESTRATOR_DEPENDENCIES: tuple[tuple[str, str, str], ...] = (
    ("pypdf", "pypdf", "解析 PDF 文本"),
    ("pymupdf", "fitz", "把论文页面渲染成 PNG 供 Codex 事实抽取和 task writer 审查"),
    ("pydantic", "pydantic", "JSON 结构校验"),
    ("python-docx", "docx", "生成 Word 报告"),
    ("pillow", "PIL", "图像处理（也供复现代码使用）"),
)

# Repro libs whose absence almost certainly forces a fallback.
CRITICAL_REPRO_PACKAGES: frozenset[str] = frozenset({"numpy", "scipy", "matplotlib"})

# pillow is already covered by the orchestrator list; "sklearn" is just the import
# alias of "scikit-learn" (install the latter). Skip both to avoid double-listing.
_REPRO_SKIP_REQUIREMENTS: frozenset[str] = frozenset({"pillow", "sklearn"})

# Framework choice is an architecture decision; policy and installation are host
# facts. Keep unsupported but common runtimes visible so the designer reports a
# capability gap instead of silently selecting a weaker stack.
ARCHITECTURE_RUNTIME_PACKAGES: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    ("numpy", "numpy", "numpy", ("numpy",)),
    ("scipy", "scipy", "scipy", ("scipy",)),
    ("pytorch", "torch", "torch", ("torch", "pytorch")),
    ("tensorflow", "tensorflow", "tensorflow", ("tensorflow", "keras")),
    ("jax", "jax", "jax", ("jax", "flax", "optax")),
)
ARCHITECTURE_EXTERNAL_RUNTIMES: tuple[tuple[str, str], ...] = (
    ("julia", "julia"),
    ("matlab", "matlab"),
)


@dataclass(frozen=True)
class PackageStatus:
    package: str
    import_name: str
    installed: bool
    version: str | None
    purpose: str
    critical: bool


@dataclass(frozen=True)
class ExternalToolStatus:
    name: str
    command: str
    available: bool
    resolved_executable: str | None
    purpose: str


@dataclass(frozen=True)
class EnvironmentReport:
    interpreter: str
    python_version: str
    python_required: str
    python_ok: bool
    orchestrator: list[PackageStatus]
    repro: list[PackageStatus]
    mineru: ExternalToolStatus | None = None

    @property
    def missing_orchestrator(self) -> list[PackageStatus]:
        return [item for item in self.orchestrator if not item.installed]

    @property
    def missing_repro_critical(self) -> list[PackageStatus]:
        return [item for item in self.repro if not item.installed and item.critical]

    @property
    def missing_repro_optional(self) -> list[PackageStatus]:
        return [item for item in self.repro if not item.installed and not item.critical]

    @property
    def fatal(self) -> bool:
        """True when the machine cannot do a real reproduction: wrong Python, a
        missing orchestrator dep (CLI won't start), or a missing critical repro
        lib (task writers cannot complete a faithful full run)."""
        return (
            (not self.python_ok)
            or bool(self.missing_orchestrator)
            or bool(self.missing_repro_critical)
        )

    @property
    def ok(self) -> bool:
        return not self.fatal and not self.missing_repro_optional


def _is_installed(import_name: str) -> bool:
    try:
        return importlib.util.find_spec(import_name) is not None
    except (ImportError, ValueError):
        return False


def _distribution_version(package: str) -> str | None:
    try:
        return importlib_metadata.version(package)
    except Exception:
        return None


def _repro_requirements() -> list[tuple[str, str]]:
    """Canonical (package, import_name) pairs for the repro whitelist, sorted,
    with pillow/sklearn aliases dropped."""
    items: dict[str, str] = {}
    for package in ALLOWED_REQUIREMENTS:
        if package in _REPRO_SKIP_REQUIREMENTS:
            continue
        import_name = sorted(import_names_for_requirement(package))[0]
        items[package] = import_name
    return sorted(items.items())


def check_environment() -> EnvironmentReport:
    orchestrator = [
        PackageStatus(
            package=package,
            import_name=import_name,
            installed=_is_installed(import_name),
            version=_distribution_version(package),
            purpose=purpose,
            critical=True,
        )
        for package, import_name, purpose in ORCHESTRATOR_DEPENDENCIES
    ]
    repro = [
        PackageStatus(
            package=package,
            import_name=import_name,
            installed=_is_installed(import_name),
            version=_distribution_version(package),
            purpose="复现代码可用库（关键）" if package in CRITICAL_REPRO_PACKAGES else "复现代码可用库（可选，缺失时报告能力缺口）",
            critical=package in CRITICAL_REPRO_PACKAGES,
        )
        for package, import_name in _repro_requirements()
    ]
    return EnvironmentReport(
        interpreter=sys.executable,
        python_version=".".join(str(part) for part in sys.version_info[:3]),
        python_required=f"{REQUIRED_PYTHON[0]}.{REQUIRED_PYTHON[1]}+",
        python_ok=sys.version_info[:2] >= REQUIRED_PYTHON,
        orchestrator=orchestrator,
        repro=repro,
        mineru=_check_mineru_command(),
    )


def _probe_nvidia_devices() -> dict[str, Any]:
    """Return a small, read-only CUDA hardware hint for architecture planning.

    This intentionally uses ``nvidia-smi`` instead of importing a deep-learning
    framework. Importability and hardware presence are separate facts; the
    generated Foundation must still verify that its selected framework can
    actually use the requested device at runtime.
    """

    executable = shutil.which("nvidia-smi")
    if executable is None:
        return {
            "nvidia_smi_available": False,
            "devices": [],
            "probe_status": "tool_unavailable",
        }

    try:
        completed = subprocess.run(
            [
                executable,
                "--query-gpu=name,memory.total",
                "--format=csv,noheader,nounits",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "nvidia_smi_available": True,
            "devices": [],
            "probe_status": "probe_failed",
            "probe_error": f"{type(exc).__name__}: {exc}",
        }

    if completed.returncode != 0:
        error = (completed.stderr or completed.stdout or "unknown nvidia-smi error").strip()
        return {
            "nvidia_smi_available": True,
            "devices": [],
            "probe_status": "probe_failed",
            "probe_error": error[:500],
        }

    devices: list[dict[str, Any]] = []
    for index, raw_line in enumerate(completed.stdout.splitlines()):
        line = raw_line.strip()
        if not line:
            continue
        name, separator, memory_text = line.rpartition(",")
        if not separator:
            devices.append({"index": index, "name": line, "memory_total_mib": None})
            continue
        try:
            memory_total_mib: int | None = int(memory_text.strip())
        except ValueError:
            memory_total_mib = None
        devices.append(
            {
                "index": index,
                "name": name.strip(),
                "memory_total_mib": memory_total_mib,
            }
        )
    return {
        "nvidia_smi_available": True,
        "devices": devices,
        "probe_status": "ok",
    }


def _architecture_runtime_inventory(report: EnvironmentReport) -> list[dict[str, Any]]:
    reported = {item.package: item for item in report.repro}
    runtimes: list[dict[str, Any]] = []
    for runtime, package, import_name, aliases in ARCHITECTURE_RUNTIME_PACKAGES:
        package_status = reported.get(package)
        installed = package_status.installed if package_status is not None else _is_installed(import_name)
        version = package_status.version if package_status is not None else _distribution_version(package)
        policy_allowed = package in ALLOWED_REQUIREMENTS
        runtimes.append(
            {
                "runtime": runtime,
                "aliases": list(aliases),
                "package": package,
                "import_name": import_name,
                "policy_allowed": policy_allowed,
                "installed": installed,
                "version": version,
                "usable_now": policy_allowed and installed,
                "status": (
                    "ready"
                    if policy_allowed and installed
                    else "package_missing"
                    if policy_allowed
                    else "environment_extension_required"
                ),
            }
        )
    known_packages = {str(item["package"]) for item in runtimes}
    for item in report.repro:
        if item.package in known_packages:
            continue
        aliases = sorted({item.package, item.import_name})
        runtimes.append(
            {
                "runtime": item.package,
                "aliases": aliases,
                "package": item.package,
                "import_name": item.import_name,
                "policy_allowed": True,
                "installed": item.installed,
                "version": item.version,
                "usable_now": item.installed,
                "status": "ready" if item.installed else "package_missing",
            }
        )
    return runtimes


def _runtime_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def architecture_execution_capability_gaps(
    architecture: dict[str, Any],
    inventory: dict[str, Any],
) -> list[dict[str, str]]:
    """Report material host gaps without changing the selected science stack."""

    local_runtime_keys = {
        "builtin",
        "builtins",
        "projectlocal",
        "pythonstandardlibrary",
        "standardlibrary",
        "stdlib",
    }
    alias_entries: dict[str, dict[str, Any]] = {}
    for entry in inventory.get("python_runtime_registry", []):
        if not isinstance(entry, dict):
            continue
        values = [entry.get("runtime"), entry.get("package"), entry.get("import_name")]
        aliases = entry.get("aliases")
        if isinstance(aliases, list):
            values.extend(aliases)
        for value in values:
            key = _runtime_key(value)
            if key:
                alias_entries[key] = entry
    external_entries = {
        _runtime_key(item.get("runtime")): item
        for item in inventory.get("external_runtime_registry", [])
        if isinstance(item, dict) and _runtime_key(item.get("runtime"))
    }
    accelerators = inventory.get("accelerators")
    devices = accelerators.get("devices") if isinstance(accelerators, dict) else []
    visible_accelerator = any(isinstance(item, dict) for item in devices or [])

    gaps: list[dict[str, str]] = []
    components = architecture.get("components")
    for index, component in enumerate(components if isinstance(components, list) else []):
        if not isinstance(component, dict):
            continue
        execution = component.get("execution")
        if not isinstance(execution, dict):
            continue
        component_id = str(component.get("id") or f"component_{index}")
        framework = str(execution.get("primary_framework") or "")
        framework_key = _runtime_key(framework)
        device_policy = str(execution.get("device_policy") or "")
        supporting_libraries = execution.get("supporting_libraries")
        seen_supporting_keys: set[str] = set()
        for library in supporting_libraries if isinstance(supporting_libraries, list) else []:
            library_name = str(library or "")
            library_key = _runtime_key(library_name)
            if (
                not library_key
                or library_key == framework_key
                or library_key in local_runtime_keys
                or library_key in seen_supporting_keys
            ):
                continue
            seen_supporting_keys.add(library_key)
            entry = alias_entries.get(library_key)
            if entry is not None:
                if entry.get("policy_allowed") is not True:
                    gap_kind = "environment_extension_required"
                    gap_message = (
                        "selected supporting library requires an explicit environment "
                        f"policy extension: {library_name}"
                    )
                elif entry.get("installed") is not True:
                    gap_kind = "runtime_package_missing"
                    gap_message = (
                        f"selected supporting library is not installed on this host: {library_name}"
                    )
                else:
                    gap_kind = ""
                    gap_message = ""
            else:
                external_entry = external_entries.get(library_key)
                if external_entry is not None:
                    gap_kind = (
                        "" if external_entry.get("available") is True else "external_runtime_unavailable"
                    )
                    gap_message = (
                        ""
                        if not gap_kind
                        else f"required supporting runtime is not visible on this host: {library_name}"
                    )
                else:
                    gap_kind = "runtime_unregistered"
                    gap_message = (
                        "selected supporting library has no trusted host registry entry: "
                        f"{library_name}"
                    )
            if gap_kind:
                gaps.append(
                    {
                        "component_id": component_id,
                        "kind": gap_kind,
                        "runtime": library_name,
                        "role": "supporting_library",
                        "message": gap_message,
                    }
                )
        if device_policy == "external_runtime":
            entry = external_entries.get(framework_key)
            if entry is None or entry.get("available") is not True:
                gaps.append(
                    {
                        "component_id": component_id,
                        "kind": "external_runtime_unavailable",
                        "runtime": framework,
                        "message": f"required external runtime is not visible on this host: {framework}",
                    }
                )
            continue
        if framework_key not in local_runtime_keys:
            entry = alias_entries.get(framework_key)
            if entry is None:
                gap_kind = "runtime_unregistered"
                gap_message = f"selected runtime has no trusted host registry entry: {framework}"
            elif entry.get("policy_allowed") is not True:
                gap_kind = "environment_extension_required"
                gap_message = f"selected runtime requires an explicit environment policy extension: {framework}"
            elif entry.get("installed") is not True:
                gap_kind = "runtime_package_missing"
                gap_message = f"selected runtime package is not installed on this host: {framework}"
            else:
                gap_kind = ""
                gap_message = ""
            if gap_kind:
                gaps.append(
                    {
                        "component_id": component_id,
                        "kind": gap_kind,
                        "runtime": framework,
                        "message": gap_message,
                    }
                )
        if device_policy == "accelerator_required" and not visible_accelerator:
            gaps.append(
                {
                    "component_id": component_id,
                    "kind": "accelerator_unavailable",
                    "runtime": framework,
                    "message": "architecture requires an accelerator but the trusted host probe found none",
                }
            )
    return gaps


def architecture_capability_inventory(
    report: EnvironmentReport | None = None,
) -> dict[str, Any]:
    """Build trusted host context for the scientific architecture designer.

    The inventory is feasibility evidence, not scientific evidence. In
    particular, a missing package must never make the designer silently replace
    a trainable/autograd component with a NumPy approximation. The architecture
    should declare the scientifically appropriate execution contract and let the
    Foundation report an explicit dependency or runtime-capability gap.
    """

    current = report or check_environment()
    installed = [
        {
            "package": item.package,
            "import_name": item.import_name,
            "version": item.version,
        }
        for item in current.repro
        if item.installed
    ]
    unavailable = [item.package for item in current.repro if not item.installed]
    return {
        "evidence_class": "host_capability_only_not_paper_evidence",
        "python": {
            "version": current.python_version,
            "minimum_supported": current.python_required,
        },
        "installed_reproduction_packages": installed,
        "unavailable_allowed_reproduction_packages": unavailable,
        "accelerators": _probe_nvidia_devices(),
        "python_runtime_registry": _architecture_runtime_inventory(current),
        "external_runtime_registry": [
            {
                "runtime": runtime,
                "executable": executable,
                "available": shutil.which(executable) is not None,
                "status": "host_tool_visible" if shutil.which(executable) is not None else "tool_missing",
            }
            for runtime, executable in ARCHITECTURE_EXTERNAL_RUNTIMES
        ],
        "interpretation": {
            "package_importable_does_not_prove_device_usable": True,
            "foundation_must_runtime_verify_selected_backend": True,
            "missing_package_must_not_trigger_silent_scientific_downgrade": True,
        },
    }


def _check_mineru_command() -> ExternalToolStatus:
    command = get_config_value("GENG_MINERU_CMD") or "mineru"
    argv = split_command(command)
    resolved = shutil.which(argv[0]) if argv else None
    return ExternalToolStatus(
        name="MinerU",
        command=command,
        available=resolved is not None,
        resolved_executable=resolved,
        purpose="为 Task Reporter 提供论文整图候选；缺失时自动回退到页面图定位",
    )


def remedy_command(report: EnvironmentReport) -> str | None:
    """One pip command that installs everything needed (orchestrator + repro)."""
    if report.ok:
        return None
    return f'"{report.interpreter}" -m pip install -e ".[repro]"'


def _mark(installed: bool) -> str:
    # ASCII only: the Windows console codepage (GBK) can't render check/cross glyphs.
    return "ok " if installed else "MISS"


def format_report(report: EnvironmentReport) -> str:
    lines: list[str] = []
    lines.append("耿同学agent 环境自检（建议在输入 PDF 前运行）")
    lines.append(f"  解释器: {report.interpreter}")
    py_state = "ok" if report.python_ok else "TOO OLD"
    lines.append(f"  [{py_state}] Python {report.python_version}（要求 {report.python_required}）")
    lines.append("")
    lines.append("一、运行 geng-agent 必需的库（缺这些连 CLI 都起不来）:")
    for item in report.orchestrator:
        version = f" {item.version}" if item.version else ""
        lines.append(f"  [{_mark(item.installed)}] {item.package}{version}  - {item.purpose}")
    lines.append("")
    lines.append("二、复现代码使用的白名单库（缺关键项会阻碍 task writer 完成 full 运行）:")
    for item in report.repro:
        version = f" {item.version}" if item.version else ""
        tag = "关键" if item.critical else "可选"
        lines.append(f"  [{_mark(item.installed)}] {item.package}{version}  ({tag})")
    if report.mineru is not None:
        lines.append("")
        lines.append("三、可选论文版面工具:")
        location = f" -> {report.mineru.resolved_executable}" if report.mineru.resolved_executable else ""
        lines.append(
            f"  [{_mark(report.mineru.available)}] {report.mineru.name}: "
            f"{report.mineru.command}{location}  - {report.mineru.purpose}"
        )
    lines.append("")
    if report.ok:
        lines.append("结论: 环境就绪，可以输入 PDF 开始审查。")
        return "\n".join(lines)

    lines.append("结论: 环境不完整。建议先补齐再输入 PDF:")
    command = remedy_command(report)
    if command:
        lines.append(f"  {command}")
    if not report.python_ok:
        lines.append(f"  注意: 当前 Python {report.python_version} 低于要求 {report.python_required}，需先升级解释器。")
    if not report.fatal and report.missing_repro_optional:
        lines.append("  （当前只缺可选库：不影响启动，但相关论文会退到近似实现。）")
    return "\n".join(lines)


def environment_warning(report: EnvironmentReport) -> str | None:
    """Compact one-block warning for the start of `review`. Returns
    None when the environment is fully ready. Printed to stderr so it never
    pollutes the stdout result lines."""
    if report.ok:
        return None
    problems: list[str] = []
    if not report.python_ok:
        problems.append(f"Python {report.python_version} 低于要求 {report.python_required}")
    if report.missing_orchestrator:
        problems.append("缺运行依赖: " + ", ".join(item.package for item in report.missing_orchestrator))
    if report.missing_repro_critical:
        problems.append("缺关键复现库: " + ", ".join(item.package for item in report.missing_repro_critical))
    if report.missing_repro_optional:
        problems.append("缺可选复现库: " + ", ".join(item.package for item in report.missing_repro_optional))
    header = "[警告] 环境自检未通过（继续运行，但 task writer 很可能无法完成 full）:" if report.fatal else "[提示] 复现环境不完整:"
    command = remedy_command(report)
    tail = f"\n  修复: {command}\n  详情: geng-agent doctor" if command else "\n  详情: geng-agent doctor"
    return f"{header}\n  " + "; ".join(problems) + tail
