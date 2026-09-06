"""Report Editor repair workspace and structural output handling."""

from __future__ import annotations

import hashlib
from pathlib import Path
import re
import shutil
from typing import Any


REPORT_MARKDOWN_FILES = ("review.md", "reproduction_report.md", "result_review.md")
REPORT_ASSETS_DIR = "report_assets"
REPORT_FILE_ALIASES = {
    "review.md": ("main_report.md", "final_review.md", "主报告.md", "审查报告.md"),
    "reproduction_report.md": ("repro_report.md", "local_reproduction_report.md", "本地复现报告.md"),
    "result_review.md": ("comparison_report.md", "result_comparison.md", "结果对比报告.md", "论文对比报告.md"),
}
REPORT_MARKDOWN_MAX_BYTES = 16 * 1024 * 1024

def _repair_targets(context: dict[str, Any] | None) -> list[str]:
    if not isinstance(context, dict):
        return []
    values = context.get("missing_outputs") if isinstance(context.get("missing_outputs"), list) else []
    targets = [name for name in REPORT_MARKDOWN_FILES if name in values]
    return targets or list(REPORT_MARKDOWN_FILES)


def _repair_issues(context: dict[str, Any] | None) -> list[str]:
    if not isinstance(context, dict):
        return []
    issues: list[str] = []
    for name in context.get("missing_outputs", []) if isinstance(context.get("missing_outputs"), list) else []:
        issues.append(f"missing or unreadable report: {name}")
    reason = context.get("result_review_result")
    reason = reason.get("reason") if isinstance(reason, dict) else None
    if reason and str(reason) not in issues:
        issues.append(str(reason))
    return issues[:12]


def _seed_repair_drafts(
    *,
    prior_workspace: Path,
    workspace: Path,
    repair_targets: list[str],
    max_bytes: int = REPORT_MARKDOWN_MAX_BYTES,
) -> tuple[list[str], dict[str, bytes]]:
    if not prior_workspace.is_dir() or prior_workspace.is_symlink():
        return [], {}
    preserved: list[str] = []
    snapshots: dict[str, bytes] = {}
    for name in REPORT_MARKDOWN_FILES:
        if name in repair_targets:
            continue
        source = prior_workspace / name
        if not _nonempty_file(source, max_bytes=max_bytes):
            continue
        payload = source.read_bytes()
        (workspace / name).write_bytes(payload)
        preserved.append(name)
        snapshots[name] = payload
    return preserved, snapshots


def _restore_protected_reports(
    *,
    workspace: Path,
    protected_reports: dict[str, bytes],
    max_bytes: int = REPORT_MARKDOWN_MAX_BYTES,
) -> list[str]:
    restored: list[str] = []
    for name, payload in protected_reports.items():
        path = workspace / name
        try:
            current = path.read_bytes() if _nonempty_file(path, max_bytes=max_bytes) else None
        except OSError:
            current = None
        if current == payload:
            continue
        if path.is_symlink() or path.is_file():
            path.unlink(missing_ok=True)
        elif path.is_dir():
            shutil.rmtree(path)
        path.write_bytes(payload)
        restored.append(name)
    return restored


def _normalize_report_editor_outputs(
    workspace: Path,
    *,
    max_bytes: int = REPORT_MARKDOWN_MAX_BYTES,
) -> list[str]:
    actions: list[str] = []
    for target_name, aliases in REPORT_FILE_ALIASES.items():
        target = workspace / target_name
        if (
            _nonempty_file(target, max_bytes=max_bytes)
            or target.is_symlink()
            or (target.exists() and not target.is_file())
        ):
            continue
        candidates = [workspace / alias for alias in aliases]
        candidates = [
            path for path in candidates if _nonempty_file(path, max_bytes=max_bytes)
        ]
        if len(candidates) == 1:
            target.unlink(missing_ok=True)
            candidates[0].replace(target)
            actions.append(f"renamed {candidates[0].name} to {target_name}")

    outer_fence = re.compile(r"\A\s*```(?:markdown|md)?\s*\n(?P<body>.*)\n```\s*\Z", re.IGNORECASE | re.DOTALL)
    image_link = re.compile(r"(!\[[^\]\n]*\]\()([^\)\n]+)(\))")
    for name in REPORT_MARKDOWN_FILES:
        path = workspace / name
        if not path.is_file() or path.is_symlink():
            continue
        try:
            if path.stat().st_size > max_bytes:
                continue
            raw = path.read_bytes()
        except OSError:
            continue
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("utf-8", errors="replace")
            actions.append(f"replaced invalid UTF-8 bytes in {name}")
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        match = outer_fence.fullmatch(normalized)
        if match:
            normalized = match.group("body")
            actions.append(f"removed outer Markdown fence from {name}")
        normalized, replacements = image_link.subn(
            lambda item: item.group(1) + item.group(2).replace("\\", "/") + item.group(3),
            normalized,
        )
        if replacements and "\\" in text:
            actions.append(f"normalized image paths in {name}")
        normalized = normalized.strip()
        if normalized:
            normalized += "\n"
        encoded = normalized.encode("utf-8")
        if encoded != raw:
            path.write_bytes(encoded)
            if not any(name in action for action in actions):
                actions.append(f"normalized encoding or line endings in {name}")
    return actions


def _recover_unsafe_report_outputs(
    workspace: Path,
    *,
    max_bytes: int = REPORT_MARKDOWN_MAX_BYTES,
) -> tuple[list[str], list[str], list[str]]:
    """Quarantine packaging-shaped outputs so deterministic reports can replace them."""

    recovered: list[str] = []
    actions: list[str] = []
    failures: list[str] = []
    quarantine_root = workspace / "discarded_report_outputs"
    for name in REPORT_MARKDOWN_FILES:
        path = workspace / name
        unsafe_reason = ""
        try:
            if path.is_symlink():
                unsafe_reason = "symbolic link"
            elif path.exists() and not path.is_file():
                unsafe_reason = "non-file output"
            elif path.is_file():
                size = path.stat().st_size
                if size > max_bytes:
                    unsafe_reason = f"resource limit exceeded ({size} bytes)"
                else:
                    with path.open("rb") as handle:
                        handle.read(1)
        except OSError as exc:
            unsafe_reason = f"unreadable output: {type(exc).__name__}"
        if not unsafe_reason:
            continue
        try:
            quarantine_root.mkdir(parents=True, exist_ok=True)
            target = quarantine_root / name
            if target.exists() or target.is_symlink():
                target = quarantine_root / f"{path.stem}_recovered{path.suffix}"
            path.replace(target)
        except OSError as exc:
            failures.append(f"{name} could not be quarantined: {type(exc).__name__}")
            continue
        recovered.append(name)
        actions.append(f"quarantined unsafe {name}: {unsafe_reason}")
    return recovered, actions, failures



def _inspect_report_editor_outputs(
    workspace: Path,
    *,
    max_bytes: int = REPORT_MARKDOWN_MAX_BYTES,
) -> dict[str, list[str]]:
    missing: list[str] = []
    hard_issues: list[str] = []
    for name in REPORT_MARKDOWN_FILES:
        path = workspace / name
        try:
            if path.is_symlink():
                hard_issues.append(f"{name} must not be a symbolic link")
            elif not path.exists():
                missing.append(name)
            elif not path.is_file():
                hard_issues.append(f"{name} must be a regular file")
            elif path.stat().st_size > max_bytes:
                hard_issues.append(f"{name} exceeds the report resource limit")
            elif not path.read_text(encoding="utf-8").strip():
                missing.append(name)
        except (OSError, UnicodeError) as exc:
            hard_issues.append(f"{name} could not be read safely: {type(exc).__name__}")
    return {"missing": missing, "hard_issues": hard_issues}

def _clear_editor_outputs(output_dir: Path) -> None:
    for name in (*REPORT_MARKDOWN_FILES, "review.docx", "reproduction_report.docx", "result_review.docx", "report_editor_error.json"):
        path = output_dir / name
        if path.is_file() or path.is_symlink():
            path.unlink(missing_ok=True)
        elif path.is_dir():
            shutil.rmtree(path)


def _report_outputs_fingerprint(
    output_dir: Path,
    *,
    max_bytes: int = REPORT_MARKDOWN_MAX_BYTES,
) -> str | None:
    paths = [output_dir / name for name in REPORT_MARKDOWN_FILES]
    if not all(_nonempty_file(path, max_bytes=max_bytes) for path in paths):
        return None
    digest = hashlib.sha256()
    try:
        for path in paths:
            digest.update(path.name.encode("utf-8"))
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def _nonempty_file(
    path: Path,
    *,
    max_bytes: int = REPORT_MARKDOWN_MAX_BYTES,
) -> bool:
    try:
        size = path.stat().st_size
        return path.is_file() and not path.is_symlink() and 0 < size <= max_bytes
    except OSError:
        return False
