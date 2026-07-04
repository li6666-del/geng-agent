"""Environment preflight for a fresh deployment.

Goal: BEFORE a user feeds in a PDF, make the local requirements explicit and
checkable on whatever machine is running geng-agent — which Python version and
which libraries must be installed. A misconfigured machine (no numpy, wrong
Python) silently turns every paper into a template fallback, so this module is
the up-front gate that surfaces it instead.

Two distinct dependency classes, both required on a new machine:
  1. orchestrator deps  -> needed to even start geng-agent (parse PDF, render
     pages, validate JSON, write Word). Declared in pyproject [project].
  2. repro whitelist     -> the third-party libs the *generated* reproduction
     code is allowed to import. Single source of truth is
     security.ALLOWED_REQUIREMENTS; if these are absent the generator is told
     "don't use numpy" and the runner blocks execution -> fallback.

This module only inspects (importlib.util.find_spec / importlib.metadata); it
never imports the heavy packages, so `geng-agent doctor` still runs on a machine
that is missing them.
"""

from __future__ import annotations

import importlib.metadata as importlib_metadata
import importlib.util
import os
import shlex
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from .config import get_config_value
from .security import ALLOWED_REQUIREMENTS, import_names_for_requirement

# Minimum interpreter. Keep in sync with pyproject `requires-python`.
REQUIRED_PYTHON: tuple[int, int] = (3, 11)

# (pip/PyPI name, import name, human purpose). Mirrors pyproject [project.dependencies].
ORCHESTRATOR_DEPENDENCIES: tuple[tuple[str, str, str], ...] = (
    ("pypdf", "pypdf", "解析 PDF 文本"),
    ("pymupdf", "fitz", "把论文页面渲染成 PNG 供多模态审查"),
    ("pydantic", "pydantic", "JSON 结构校验"),
    ("python-docx", "docx", "生成 Word 报告"),
    ("pillow", "PIL", "图像处理（也供复现代码使用）"),
)

# Repro libs whose absence almost certainly forces a fallback.
CRITICAL_REPRO_PACKAGES: frozenset[str] = frozenset({"numpy", "scipy", "matplotlib"})

# pillow is already covered by the orchestrator list; "sklearn" is just the import
# alias of "scikit-learn" (install the latter). Skip both to avoid double-listing.
_REPRO_SKIP_REQUIREMENTS: frozenset[str] = frozenset({"pillow", "sklearn"})


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
    configured: bool
    available: bool
    command: str | None
    purpose: str
    note: str | None = None


@dataclass(frozen=True)
class EnvironmentReport:
    interpreter: str
    python_version: str
    python_required: str
    python_ok: bool
    orchestrator: list[PackageStatus]
    repro: list[PackageStatus]
    pdffigures2: ExternalToolStatus | None = None

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
        lib (generation poisoned + runner blocks -> guaranteed fallback)."""
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
            purpose="复现代码可用库（关键）" if package in CRITICAL_REPRO_PACKAGES else "复现代码可用库（可选，缺则降级近似）",
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
        pdffigures2=_check_pdffigures2(),
    )


def _check_pdffigures2() -> ExternalToolStatus:
    command = get_config_value("GENG_PDFFIGURES2_CMD")
    purpose = "抽取论文 Figure/Table 边界，用于 result_review 原论文图裁剪"
    if not command:
        return ExternalToolStatus(
            name="PDFFigures2",
            configured=False,
            available=False,
            command=None,
            purpose=purpose,
            note="未设置 GENG_PDFFIGURES2_CMD；报告会回退到整页论文图。",
        )
    available, note = _pdffigures2_command_available(command)
    return ExternalToolStatus(
        name="PDFFigures2",
        configured=True,
        available=bool(available),
        command=command,
        purpose=purpose,
        note=note,
    )


def _pdffigures2_command_available(command: str) -> tuple[bool, str | None]:
    parts = _split_command_tokens(command)
    if not parts:
        return False, "命令为空；可设置为 jar 路径或包含 {pdf}/{json_dir} 等占位符的启动命令。"
    if len(parts) == 1 and parts[0].lower().endswith(".jar"):
        jar_ok = Path(parts[0]).exists()
        java_cmd = get_config_value("GENG_PDFFIGURES2_JAVA_CMD") or "java"
        java_parts = _split_command_tokens(java_cmd)
        java_ok = bool(java_parts and _command_entry_exists(java_parts[0]))
        if jar_ok and java_ok:
            return True, None
        missing = []
        if not jar_ok:
            missing.append("jar 文件不存在")
        if not java_ok:
            missing.append("java 入口未找到")
        return False, "；".join(missing)

    entry_ok = _command_entry_exists(parts[0])
    jar_ok = True
    if len(parts) >= 3 and parts[1].lower() == "-jar":
        jar_ok = Path(parts[2]).exists()
    if entry_ok and jar_ok:
        return True, None
    missing = []
    if not entry_ok:
        missing.append("命令入口未找到")
    if not jar_ok:
        missing.append("jar 文件不存在")
    return False, f"{'；'.join(missing)}；可设置为 jar 路径或完整启动命令模板。"


def _command_entry_exists(entry: str) -> bool:
    return bool(shutil.which(entry) or Path(entry).exists())


def _split_command_tokens(command: str) -> list[str]:
    try:
        parts = shlex.split(command, posix=os.name != "nt")
    except ValueError:
        parts = []
    if not parts:
        raw = command.strip().strip('"')
        return [raw] if raw else []
    return [_strip_outer_quotes(part) for part in parts if _strip_outer_quotes(part)]


def _strip_outer_quotes(value: str) -> str:
    text = str(value).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        return text[1:-1]
    return text


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
    lines.append("二、复现代码使用的白名单库（缺关键项几乎必然吃兜底）:")
    for item in report.repro:
        version = f" {item.version}" if item.version else ""
        tag = "关键" if item.critical else "可选"
        lines.append(f"  [{_mark(item.installed)}] {item.package}{version}  ({tag})")
    lines.append("")
    if report.pdffigures2 is not None:
        tool = report.pdffigures2
        state = "ok " if tool.available else "MISS"
        command = f"  命令: {tool.command}" if tool.command else "  命令: 未配置"
        lines.append("三、可选论文原图抽取工具:")
        lines.append(f"  [{state}] {tool.name}  - {tool.purpose}")
        lines.append(command)
        if tool.note:
            lines.append(f"  说明: {tool.note}")
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
    header = "[警告] 环境自检未通过（继续运行，但很可能吃兜底）:" if report.fatal else "[提示] 复现环境不完整:"
    command = remedy_command(report)
    tail = f"\n  修复: {command}\n  详情: geng-agent doctor" if command else "\n  详情: geng-agent doctor"
    return f"{header}\n  " + "; ".join(problems) + tail
