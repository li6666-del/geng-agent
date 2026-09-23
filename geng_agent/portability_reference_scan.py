"""Inspect actual filesystem entries; source-text policy heuristics are retired."""
from __future__ import annotations
import os
import stat
from pathlib import Path, PurePosixPath
from typing import Any
from .foundation_snapshot import path_is_foundation_link
from .portability_contracts import _issue, _is_absolute_cross_platform, _URL, _PARENT_SEGMENT
from .portability_inventory import _is_ignored_directory, _is_ignored_file

def _filesystem_issues(root: Path) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    casefolded: dict[str, str] = {}
    for directory, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        directory_path = Path(directory)
        kept: list[str] = []
        for name in sorted(dirnames):
            path = directory_path / name
            relative = path.relative_to(root).as_posix()
            if path_is_foundation_link(path):
                issues.append(_issue("filesystem_link", relative, "directory link is not a self-contained project entry"))
                continue
            if not _is_ignored_directory(name):
                kept.append(name)
        dirnames[:] = kept
        for name in sorted(filenames):
            path = directory_path / name
            relative = path.relative_to(root).as_posix()
            if path_is_foundation_link(path):
                issues.append(_issue("filesystem_link", relative, "file link is not a self-contained project entry"))
                continue
            if not stat.S_ISREG(path.lstat().st_mode):
                issues.append(_issue("filesystem_special", relative, "non-regular project entry"))
                continue
            if _is_ignored_file(path):
                continue
            folded = relative.casefold()
            previous = casefolded.get(folded)
            if previous is not None and previous != relative:
                issues.append(
                    _issue(
                        "case_colliding_paths",
                        relative,
                        f"path collides case-insensitively with {previous}",
                    )
                )
            else:
                casefolded[folded] = relative
    return issues


def _looks_path_like(value: str) -> bool:
    raw = value.strip().strip("\"'")
    if not raw or "\n" in raw or _URL.match(raw):
        return False
    if _is_absolute_cross_platform(raw) or _PARENT_SEGMENT.search(raw):
        return True
    path = PurePosixPath(raw.replace("\\", "/"))
    return "/" in raw.replace("\\", "/") or bool(path.suffix)
