"""Whole-project snapshot/restore for reversible repro-project mutations.

Used by the Phase-D science-repair loop: snapshot before a repair round, restore when the
gate rejects it. These primitives are load-bearing for the repair gate.
"""

from __future__ import annotations

import shutil
from pathlib import Path


def _snapshot_project(src: Path, dst: Path) -> Path:
    """Copy the current repro project (source files only) to dst, overwriting any prior
    snapshot. Outputs/logs/caches are excluded -- the snapshot exists to restore CODE state."""
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("outputs", "repair_logs", "__pycache__", "*.pyc"))
    return dst


def _restore_project(snapshot: Path, dst: Path) -> None:
    """Copy a snapshot's files back over the project dir, restoring the snapshotted state."""
    for path in snapshot.rglob("*"):
        if path.is_file():
            target = dst / path.relative_to(snapshot)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
