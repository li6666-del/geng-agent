"""File observations without following filesystem links."""
import hashlib
import os
import stat
from pathlib import Path

_REGENERABLE_CACHE_DIRS = frozenset({".pytest_cache", "__pycache__"})

def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

def path_is_link(path: Path) -> bool:
    """Detect symlinks and Windows reparse points, including dangling links."""

    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse_flag)

def scan_tree(root: Path, *, skip_regenerable_caches: bool = False) -> tuple[list[Path], list[Path], list[Path], list[Path]]:
    """Walk a tree without traversing any symlink, junction, or reparse point."""

    files: list[Path] = []
    directories: list[Path] = []
    links: list[Path] = []
    special: list[Path] = []
    pending = [root]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as iterator:
            entries = sorted(iterator, key=lambda entry: entry.name)
        for entry in entries:
            path = Path(entry.path)
            if path_is_link(path):
                links.append(path)
            elif entry.is_dir(follow_symlinks=False):
                directories.append(path)
                # Inspect the entry type first. A cache-named junction/link is
                # still rejected above; a regular unused cache is never read.
                if not skip_regenerable_caches or entry.name.casefold() not in _REGENERABLE_CACHE_DIRS:
                    pending.append(path)
            elif entry.is_file(follow_symlinks=False):
                files.append(path)
            else:
                special.append(path)
    return sorted(files), sorted(directories), sorted(links), sorted(special)
