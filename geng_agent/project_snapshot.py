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


_RESTORE_PRUNE_SKIP = ("outputs", "repair_logs", "__pycache__")


def _restore_project(snapshot: Path, dst: Path, *, prune: bool = False) -> None:
    """Copy a snapshot's files back over the project dir, restoring the snapshotted state.

    With ``prune=True`` it ALSO deletes files in ``dst`` that the snapshot does not contain,
    making the restore a true mirror. The science-repair gate needs this: a rejected round's
    newly-created files must not survive the revert, or "reversible repair" leaks junk into
    later rounds. Outputs/logs/caches are never pruned -- the snapshot deliberately excludes
    them (see :func:`_snapshot_project`), so deleting them would wipe live results."""
    snapshot_files: set[str] = set()
    for path in snapshot.rglob("*"):
        if path.is_file():
            rel = path.relative_to(snapshot)
            snapshot_files.add(rel.as_posix())
            target = dst / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
    if not prune:
        return
    for path in sorted(dst.rglob("*"), reverse=True):
        if not path.is_file():
            continue
        parts = path.relative_to(dst).parts
        if any(skip in parts for skip in _RESTORE_PRUNE_SKIP):
            continue
        if path.relative_to(dst).as_posix() not in snapshot_files:
            try:
                path.unlink()
            except OSError:
                pass
