"""Shared contracts and low-level issue helpers for portability validation."""

from __future__ import annotations

from pathlib import PurePosixPath, PureWindowsPath
import re
from typing import Any, Iterable, Mapping

_PARENT_SEGMENT = re.compile(r"(?:(?<![A-Za-z0-9_.])|[\\/])\.\.(?:[\\/]|$)")
_URL = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")

class ProjectPortabilityError(RuntimeError):
    """Raised when substantive portability blockers are found.

    ``issues`` is a stable list of machine-readable issue dictionaries and
    ``result`` contains the complete validation report, including the inventory.
    """

    def __init__(self, result: Mapping[str, Any]):
        self.result = dict(result)
        raw_issues = self.result.get("issues")
        self.issues = list(raw_issues) if isinstance(raw_issues, list) else []
        summary = "; ".join(
            f"{item.get('code', 'portability_error')}: {item.get('message', '')}"
            for item in self.issues[:5]
            if isinstance(item, Mapping)
        )
        if len(self.issues) > 5:
            summary += f"; and {len(self.issues) - 5} more"
        super().__init__(summary or "reproduction project is not portable")

def _result(
    *,
    inventory: Mapping[str, Any] | None,
    issues: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
    smoke: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "portable": not issues,
        "inventory": dict(inventory) if inventory is not None else None,
        "issues": issues,
        "warnings": warnings,
        "smoke": dict(smoke),
    }

def _as_warning(
    issue: Mapping[str, Any],
    *,
    code: str,
    message: str,
) -> dict[str, Any]:
    warning = dict(issue)
    warning["code"] = code
    warning["message"] = message
    warning["severity"] = "warning"
    return warning

def _is_absolute_cross_platform(value: str) -> bool:
    return PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute()

def _issue(code: str, file: str, message: str, **details: Any) -> dict[str, Any]:
    issue: dict[str, Any] = {"code": code, "file": file, "message": message}
    issue.update({key: value for key, value in details.items() if value is not None})
    return issue

def _warning(code: str, file: str, message: str, **details: Any) -> dict[str, Any]:
    warning = _issue(code, file, message, **details)
    warning["severity"] = "warning"
    return warning

def _dedupe_issues(issues: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for issue in issues:
        key = (
            str(issue.get("code") or ""),
            str(issue.get("file") or ""),
            str(issue.get("location") or issue.get("line") or ""),
            str(issue.get("reference") or issue.get("message") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(issue)
    return result
