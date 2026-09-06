"""Runtime inventory, mirror, venv trust, creation, retirement, and cleanup."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
from typing import Any, Mapping, Sequence

from .case_environment import ArgvRunner, _unprivileged_executable_path
from .case_runtime_contracts import (
    EnvironmentResolutionError,
    _BASE_RUNTIME_MARKER,
    _BASE_RUNTIME_SELECTION_VERSION,
    _CASE_VENV_MARKER,
    _INSTALL_TAINT_SUFFIX,
)
from .case_runtime_locking import (
    _case_runtime_guard,
    _host_uid,
    _open_or_create_host_root,
    _trusted_host_path,
)
from .case_runtime_probe import _is_reparse_point, _run_checked
from .outputs import write_json


def _case_python_path(venv_dir: Path) -> Path:
    return venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

def _case_venv_dir(*, output_dir: Path, runtime_dir: Path) -> Path:
    """Keep root-run probe interpreters in a path traversable by an unprivileged user."""

    if os.name != "nt" and getattr(os, "geteuid", lambda: -1)() == 0 and shutil.which("setpriv"):
        configured = os.environ.get("GENG_CASE_VENV_ROOT")
        root = Path(configured).expanduser() if configured else Path(tempfile.gettempdir()) / "geng-agent-case-envs"
        if not root.is_absolute() or root == Path(root.anchor):
            raise EnvironmentResolutionError("unsafe_venv_root", str(root))
        root = _open_or_create_host_root(root)
        identity = hashlib.sha256(str(output_dir).encode("utf-8")).hexdigest()[:24]
        return root / identity
    return runtime_dir / "venv"


def _runtime_regular_file_digest(path: Path) -> str:
    """Hash one runtime file without following a last-moment link swap."""

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise EnvironmentResolutionError(
            "base_runtime_unavailable",
            "host Python runtime changed while it was inventoried",
        ) from exc
    digest = hashlib.sha256()
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise EnvironmentResolutionError(
                "base_runtime_unavailable",
                "selected host Python runtime path is not a regular file",
            )
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(descriptor)
        if (
            before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
        ):
            raise EnvironmentResolutionError(
                "base_runtime_unavailable",
                "host Python runtime changed while it was inventoried",
            )
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _runtime_inventory_manifest(
    *,
    source_prefix: Path,
    resolved_python: Path,
    stdlib: Path,
) -> tuple[dict[str, Any], ...]:
    """Select and inventory only the files required to run the host Python.

    This is deliberately a positive selection.  In particular, arbitrary
    prefix-level ``etc``, ``share``, package entry points, credentials, and
    site-packages never enter the unprivileged mirror.
    """

    try:
        prefix = source_prefix.resolve(strict=True)
        python_path = resolved_python.resolve(strict=True)
        stdlib_path = stdlib.resolve(strict=True)
        python_path.relative_to(prefix)
        stdlib_path.relative_to(prefix)
    except (OSError, ValueError) as exc:
        raise EnvironmentResolutionError(
            "base_runtime_unavailable",
            "host Python runtime paths do not share one trusted prefix",
        ) from exc

    selected: dict[str, Path] = {}

    def add(path: Path) -> None:
        try:
            relative = path.absolute().relative_to(prefix).as_posix()
        except ValueError as exc:
            raise EnvironmentResolutionError(
                "unsafe_base_runtime",
                "selected host Python runtime path escaped its prefix",
            ) from exc
        if relative and relative != ".":
            selected[relative] = path.absolute()

    def add_with_parents(path: Path) -> None:
        add(path)
        current = path.absolute().parent
        while current != prefix:
            add(current)
            current = current.parent

    add_with_parents(python_path)
    excluded_directories = {"site-packages", "dist-packages", "__pycache__"}
    for current_root, directories, files in os.walk(
        stdlib_path,
        topdown=True,
        followlinks=False,
    ):
        root_path = Path(current_root)
        add_with_parents(root_path)
        retained_directories: list[str] = []
        for name in sorted(directories):
            if name in excluded_directories:
                continue
            child = root_path / name
            add(child)
            if not child.is_symlink():
                retained_directories.append(name)
        directories[:] = retained_directories
        for name in sorted(files):
            if name.endswith(".pyc"):
                continue
            add(root_path / name)

    # A self-contained/Conda-style launcher commonly needs prefix-local
    # shared libraries.  Only top-level dynamic libraries are admitted; build
    # metadata, static archives, configuration trees, and arbitrary data are
    # intentionally excluded.
    for library_root_name in ("lib", "lib64"):
        library_root = prefix / library_root_name
        if not library_root.exists() and not library_root.is_symlink():
            continue
        if library_root.is_symlink():
            add(library_root)
            continue
        if not library_root.is_dir():
            raise EnvironmentResolutionError(
                "unsafe_base_runtime",
                "host Python shared-library root is not a directory",
            )
        for child in sorted(library_root.iterdir(), key=lambda item: item.name):
            name = child.name
            if ".so" in name or name.endswith(".dylib"):
                add_with_parents(child)

    # Every copied link must resolve inside the prefix and land on another
    # positively selected path.  The copy step rewrites it as an internal
    # relative link so the mirror never points back into /root.
    for relative, path in list(selected.items()):
        try:
            info = path.lstat()
        except OSError as exc:
            raise EnvironmentResolutionError(
                "base_runtime_unavailable",
                "host Python runtime changed while it was inventoried",
            ) from exc
        if not stat.S_ISLNK(info.st_mode):
            continue
        try:
            target = path.resolve(strict=True)
            target_relative = target.relative_to(prefix).as_posix()
        except (OSError, ValueError) as exc:
            raise EnvironmentResolutionError(
                "unsafe_base_runtime",
                "selected host Python runtime link escapes its prefix",
            ) from exc
        if target_relative not in selected:
            raise EnvironmentResolutionError(
                "unsafe_base_runtime",
                f"selected host Python runtime link has an unselected target: {relative}",
            )

    entries: list[dict[str, Any]] = []
    for relative in sorted(selected, key=lambda value: (value.count("/"), value)):
        path = selected[relative]
        try:
            info = path.lstat()
        except OSError as exc:
            raise EnvironmentResolutionError(
                "base_runtime_unavailable",
                "host Python runtime changed while it was inventoried",
            ) from exc
        if os.name != "nt" and _host_uid() == 0:
            # POSIX reports symlinks as 0777 even though their mode bits are
            # not mutation permissions.  Bind links by root ownership and the
            # internal selected target checks above; apply write-bit policy to
            # concrete files and directories only.
            writable_concrete_path = (
                not stat.S_ISLNK(info.st_mode) and bool(info.st_mode & 0o022)
            )
            if info.st_uid != 0 or writable_concrete_path:
                raise EnvironmentResolutionError(
                    "unsafe_base_runtime",
                    "selected host Python runtime path is not host-owned and immutable: "
                    f"{relative}",
                )
        if stat.S_ISDIR(info.st_mode):
            entries.append(
                {
                    "path": relative,
                    "kind": "directory",
                    "mode": stat.S_IMODE(info.st_mode) & ~0o022,
                }
            )
        elif stat.S_ISREG(info.st_mode):
            entries.append(
                {
                    "path": relative,
                    "kind": "file",
                    "mode": stat.S_IMODE(info.st_mode) & ~0o022,
                    "sha256": _runtime_regular_file_digest(path),
                }
            )
        elif stat.S_ISLNK(info.st_mode):
            target = path.resolve(strict=True)
            entries.append(
                {
                    "path": relative,
                    "kind": "symlink",
                    "target": target.relative_to(prefix).as_posix(),
                }
            )
        else:
            raise EnvironmentResolutionError(
                "unsafe_base_runtime",
                "selected host Python runtime contains an unsupported filesystem type",
            )
    return tuple(entries)


def _runtime_inventory_digest(
    *,
    source_prefix: Path,
    resolved_python: Path,
    stdlib: Path,
) -> str:
    """Hash the same positive runtime inventory that will be copied."""

    manifest = _runtime_inventory_manifest(
        source_prefix=source_prefix,
        resolved_python=resolved_python,
        stdlib=stdlib,
    )
    return _runtime_manifest_digest(manifest)


def _runtime_manifest_digest(manifest: Sequence[Mapping[str, Any]]) -> str:
    payload = {
        "selection_version": _BASE_RUNTIME_SELECTION_VERSION,
        "entries": manifest,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _copy_runtime_inventory(
    *,
    source_prefix: Path,
    destination_prefix: Path,
    manifest: Sequence[Mapping[str, Any]],
) -> None:
    """Copy an already inventoried runtime without broad prefix traversal."""

    destination_prefix.mkdir(mode=0o700)
    directory_entries = [item for item in manifest if item.get("kind") == "directory"]
    file_entries = [item for item in manifest if item.get("kind") == "file"]
    link_entries = [item for item in manifest if item.get("kind") == "symlink"]

    for entry in directory_entries:
        destination = destination_prefix / str(entry["path"])
        destination.mkdir(mode=int(entry["mode"]), parents=True, exist_ok=True)
        destination.chmod(int(entry["mode"]))

    for entry in file_entries:
        relative = str(entry["path"])
        source = source_prefix / relative
        destination = destination_prefix / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        source_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        source_flags |= getattr(os, "O_NOFOLLOW", 0)
        destination_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        destination_flags |= getattr(os, "O_CLOEXEC", 0)
        source_descriptor = os.open(source, source_flags)
        try:
            source_info = os.fstat(source_descriptor)
            if not stat.S_ISREG(source_info.st_mode):
                raise EnvironmentResolutionError(
                    "base_runtime_changed",
                    f"selected runtime file changed type during copy: {relative}",
                )
            destination_descriptor = os.open(destination, destination_flags, 0o600)
            digest = hashlib.sha256()
            try:
                while True:
                    chunk = os.read(source_descriptor, 1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                    view = memoryview(chunk)
                    while view:
                        written = os.write(destination_descriptor, view)
                        view = view[written:]
                if hasattr(os, "fchmod"):
                    os.fchmod(destination_descriptor, int(entry["mode"]))
                else:
                    # Windows has no fchmod; this is the exclusively created,
                    # inventory-validated destination, never an arbitrary path.
                    destination.chmod(int(entry["mode"]))
            finally:
                os.close(destination_descriptor)
        finally:
            os.close(source_descriptor)
        if digest.hexdigest() != entry.get("sha256"):
            raise EnvironmentResolutionError(
                "base_runtime_changed",
                f"selected runtime file changed during copy: {relative}",
            )

    for entry in link_entries:
        relative = str(entry["path"])
        target_relative = str(entry["target"])
        destination = destination_prefix / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        target = destination_prefix / target_relative
        link_value = os.path.relpath(target, start=destination.parent)
        destination.symlink_to(
            link_value,
            target_is_directory=target.is_dir(),
        )


def _harden_runtime_tree(root: Path) -> None:
    """Make a copied host runtime immutable to group/other users."""

    root_resolved = root.resolve(strict=True)
    for current_root, directories, files in os.walk(root, followlinks=False):
        current = Path(current_root)
        for name in [*directories, *files]:
            path = current / name
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                try:
                    target = path.resolve(strict=True)
                    target.relative_to(root_resolved)
                except (OSError, ValueError) as exc:
                    raise EnvironmentResolutionError(
                        "unsafe_base_runtime_mirror",
                        "mirrored host Python contains a link outside the runtime mirror",
                    ) from exc
                if os.name != "nt" and _host_uid() == 0 and info.st_uid != 0:
                    raise EnvironmentResolutionError(
                        "unsafe_base_runtime_mirror",
                        "mirrored host Python contains a non-host-owned link",
                    )
                continue
            if os.name != "nt" and _host_uid() == 0 and info.st_uid != 0:
                raise EnvironmentResolutionError(
                    "unsafe_base_runtime_mirror",
                    "mirrored host Python contains a non-host-owned path",
                )
            if stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode):
                path.chmod(stat.S_IMODE(info.st_mode) & ~0o022)
    root.chmod(0o755)


def _base_mirror_is_trusted(
    *,
    mirror: Path,
    mirror_python: Path,
    mirror_stdlib: Path,
    source_prefix: Path,
    inventory_digest: str,
) -> bool:
    marker_path = mirror / _BASE_RUNTIME_MARKER
    if not _trusted_host_path(mirror, directory=True):
        return False
    if not _trusted_host_path(marker_path, directory=False):
        return False
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        resolved_python = mirror_python.resolve(strict=True)
        resolved_python.relative_to(mirror.resolve(strict=True))
        current_digest = _runtime_inventory_digest(
            source_prefix=mirror,
            resolved_python=mirror_python,
            stdlib=mirror_stdlib,
        )
    except (
        OSError,
        ValueError,
        json.JSONDecodeError,
        EnvironmentResolutionError,
    ):
        return False
    return bool(
        isinstance(marker, Mapping)
        and marker.get("schema_version") == 2
        and marker.get("kind") == "geng.host_python_runtime"
        and marker.get("selection_version") == _BASE_RUNTIME_SELECTION_VERSION
        and marker.get("source_prefix") == str(source_prefix)
        and marker.get("inventory_sha256") == inventory_digest
        and current_digest == inventory_digest
        and _trusted_host_path(resolved_python, directory=False)
        and _unprivileged_executable_path(mirror_python)
    )


def _case_base_interpreter(base_python: Path) -> Path:
    """Return an interpreter whose complete runtime is traversable by uid 65534.

    Some remote images ship a self-contained Python under ``/root`` rather than
    a system Python. A venv created directly from it keeps an absolute launcher
    back to that private prefix, so dropping privileges would fail before Python
    starts. Mirror only the host runtime (never site-packages or credentials) to
    a root-owned, read-only path under ``/tmp`` and create case venvs from there.
    """

    if os.name == "nt" or _host_uid() != 0 or _unprivileged_executable_path(base_python):
        return base_python
    if shutil.which("setpriv") is None:
        raise EnvironmentResolutionError(
            "probe_isolation_unavailable",
            "root-run case environments require setpriv for unprivileged probes",
        )

    try:
        resolved_python = base_python.resolve(strict=True)
        source_prefix = resolved_python.parent.parent.resolve(strict=True)
    except OSError as exc:
        raise EnvironmentResolutionError(
            "base_runtime_unavailable",
            "host Python runtime cannot be resolved for case isolation",
        ) from exc
    if source_prefix == Path(source_prefix.anchor):
        raise EnvironmentResolutionError(
            "unsafe_base_runtime",
            "host Python runtime prefix is too broad to mirror safely",
        )
    stdlib_candidates = sorted(
        path
        for path in (source_prefix / "lib").glob("python3.*")
        if not path.is_symlink() and path.is_dir() and (path / "encodings").is_dir()
    )
    if len(stdlib_candidates) != 1:
        raise EnvironmentResolutionError(
            "base_runtime_incomplete",
            "host Python runtime does not expose one unambiguous standard library",
        )
    stdlib = stdlib_candidates[0]

    configured = os.environ.get("GENG_CASE_PYTHON_BASE_ROOT")
    mirror_root = (
        Path(configured).expanduser()
        if configured
        else Path(tempfile.gettempdir()) / "geng-agent-python-bases"
    )
    if not mirror_root.is_absolute() or mirror_root == Path(mirror_root.anchor):
        raise EnvironmentResolutionError(
            "unsafe_base_runtime_root",
            "case Python base mirror root must be an absolute non-root directory",
        )
    mirror_root = _open_or_create_host_root(mirror_root)
    source_manifest = _runtime_inventory_manifest(
        source_prefix=source_prefix,
        resolved_python=resolved_python,
        stdlib=stdlib,
    )
    inventory_digest = _runtime_manifest_digest(source_manifest)
    mirror_id = inventory_digest[:24]
    python_relative = resolved_python.relative_to(source_prefix)
    stdlib_relative = stdlib.relative_to(source_prefix)
    mirror = mirror_root / mirror_id
    mirror_python = mirror / python_relative
    mirror_stdlib = mirror / stdlib_relative

    # Multiple cases may begin together. Serialize the one-time base copy in
    # the same no-follow, host-owned lock domain used for case venvs.
    with _case_runtime_guard(mirror):
        if _base_mirror_is_trusted(
            mirror=mirror,
            mirror_python=mirror_python,
            mirror_stdlib=mirror_stdlib,
            source_prefix=source_prefix,
            inventory_digest=inventory_digest,
        ):
            return mirror_python
        if mirror.exists():
            if not _trusted_host_path(mirror, directory=True):
                raise EnvironmentResolutionError(
                    "unsafe_base_runtime_mirror",
                    "case Python base mirror has an unsafe filesystem type",
                )
            if mirror.resolve().parent != mirror_root.resolve():
                raise EnvironmentResolutionError(
                    "unsafe_base_runtime_mirror",
                    "case Python base mirror escaped its host-owned root",
                )
            shutil.rmtree(mirror)
        staging = mirror_root / f".{mirror_id}.{os.getpid()}.building"
        if staging.exists():
            if staging.is_symlink() or staging.resolve().parent != mirror_root.resolve():
                raise EnvironmentResolutionError(
                    "unsafe_base_runtime_mirror",
                    "case Python base staging path is unsafe",
                )
            shutil.rmtree(staging)
        try:
            _copy_runtime_inventory(
                source_prefix=source_prefix,
                destination_prefix=staging,
                manifest=source_manifest,
            )
            staged_manifest = _runtime_inventory_manifest(
                source_prefix=staging,
                resolved_python=staging / python_relative,
                stdlib=staging / stdlib_relative,
            )
            if (
                staged_manifest != source_manifest
                or _runtime_manifest_digest(staged_manifest) != inventory_digest
            ):
                raise EnvironmentResolutionError(
                    "base_runtime_changed",
                    "host Python runtime changed while its selected inventory was copied",
                )
            _harden_runtime_tree(staging)
            hardened_digest = _runtime_inventory_digest(
                source_prefix=staging,
                resolved_python=staging / python_relative,
                stdlib=staging / stdlib_relative,
            )
            if hardened_digest != inventory_digest:
                raise EnvironmentResolutionError(
                    "base_runtime_mirror_failed",
                    "host Python runtime mirror no longer matches its selected inventory",
                )
            staged_python = staging / python_relative
            if not staged_python.is_file() or not _unprivileged_executable_path(staged_python):
                raise EnvironmentResolutionError(
                    "base_runtime_mirror_failed",
                    "mirrored host Python is not usable by the case probe user",
                )
            write_json(
                staging / _BASE_RUNTIME_MARKER,
                {
                    "schema_version": 2,
                    "kind": "geng.host_python_runtime",
                    "selection_version": _BASE_RUNTIME_SELECTION_VERSION,
                    "source_prefix": str(source_prefix),
                    "inventory_sha256": inventory_digest,
                    "stdlib": stdlib.relative_to(source_prefix).as_posix(),
                    "python": python_relative.as_posix(),
                },
            )
            (staging / _BASE_RUNTIME_MARKER).chmod(0o600)
            staging.rename(mirror)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    return mirror_python


def _case_identity(output_dir: Path) -> str:
    return hashlib.sha256(str(output_dir.resolve()).encode("utf-8")).hexdigest()


def _case_venv_is_trusted(
    *,
    venv_dir: Path,
    case_python: Path,
    base_python: Path,
    output_dir: Path,
    trusted_host_path_fn=_trusted_host_path,
    unprivileged_executable_path_fn=_unprivileged_executable_path,
) -> bool:
    marker_path = venv_dir / _CASE_VENV_MARKER
    config_path = venv_dir / "pyvenv.cfg"
    if not trusted_host_path_fn(venv_dir, directory=True):
        return False
    if not trusted_host_path_fn(marker_path, directory=False):
        return False
    if not trusted_host_path_fn(config_path, directory=False):
        return False
    try:
        config_bytes = config_path.read_bytes()
        config: dict[str, str] = {}
        for raw_line in config_bytes.decode("utf-8", errors="strict").splitlines():
            if "=" not in raw_line:
                continue
            key, value = raw_line.split("=", 1)
            config[key.strip().casefold()] = value.strip()
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        home = Path(config.get("home") or "").resolve(strict=True)
        expected_home = base_python.resolve(strict=True).parent
        resolved_case_python = case_python.resolve(strict=True)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        return False
    if os.name != "nt" and resolved_case_python != base_python.resolve(strict=True):
        return False
    if os.name == "nt":
        site_packages_candidates = [venv_dir / "Lib" / "site-packages"]
    else:
        site_packages_candidates = sorted((venv_dir / "lib").glob("python3.*/site-packages"))
    if len(site_packages_candidates) != 1:
        return False
    site_packages = site_packages_candidates[0]
    return bool(
        isinstance(marker, Mapping)
        and marker.get("schema_version") == 1
        and marker.get("kind") == "geng.case_venv"
        and marker.get("case_identity") == _case_identity(output_dir)
        and marker.get("base_interpreter") == str(base_python.resolve(strict=True))
        and marker.get("pyvenv_cfg_sha256") == hashlib.sha256(config_bytes).hexdigest()
        and config.get("include-system-site-packages", "").casefold() == "false"
        and home == expected_home
        and trusted_host_path_fn(resolved_case_python, directory=False)
        and trusted_host_path_fn(site_packages, directory=True)
        and (os.name == "nt" or unprivileged_executable_path_fn(case_python))
    )


def _write_case_venv_marker(
    *,
    venv_dir: Path,
    base_python: Path,
    output_dir: Path,
) -> None:
    config_path = venv_dir / "pyvenv.cfg"
    try:
        config_bytes = config_path.read_bytes()
    except OSError as exc:
        raise EnvironmentResolutionError(
            "venv_create_failed",
            "case venv omitted pyvenv.cfg",
        ) from exc
    marker_path = venv_dir / _CASE_VENV_MARKER
    write_json(
        marker_path,
        {
            "schema_version": 1,
            "kind": "geng.case_venv",
            "case_identity": _case_identity(output_dir),
            "base_interpreter": str(base_python.resolve(strict=True)),
            "pyvenv_cfg_sha256": hashlib.sha256(config_bytes).hexdigest(),
        },
    )
    if os.name != "nt":
        marker_path.chmod(0o600)


def _case_venv_provenance(*, venv_dir: Path, case_python: Path) -> dict[str, Any]:
    marker_path = venv_dir / _CASE_VENV_MARKER
    config_path = venv_dir / "pyvenv.cfg"
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    return {
        "schema_version": marker.get("schema_version"),
        "case_identity": marker.get("case_identity"),
        "base_interpreter": marker.get("base_interpreter"),
        "pyvenv_cfg_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
        "include_system_site_packages": False,
        "case_interpreter": str(case_python.absolute()),
    }


def _case_taint_path(venv_dir: Path) -> Path:
    return venv_dir.parent / f".{venv_dir.name}{_INSTALL_TAINT_SUFFIX}"


def _mark_case_environment_tainted(venv_dir: Path, output_dir: Path) -> Path:
    taint_path = _case_taint_path(venv_dir)
    if taint_path.exists() and not _trusted_host_path(taint_path, directory=False):
        raise EnvironmentResolutionError(
            "unsafe_environment_taint",
            "case environment taint marker is not host-owned",
        )
    write_json(
        taint_path,
        {
            "schema_version": 1,
            "kind": "geng.case_environment.install_taint",
            "case_identity": _case_identity(output_dir),
        },
    )
    if os.name != "nt":
        taint_path.chmod(0o600)
    return taint_path


def _retire_case_venv(venv_dir: Path, *, allowed_parent: Path) -> None:
    """Remove only the deterministic venv below its already trusted parent."""

    if not venv_dir.exists() and not venv_dir.is_symlink():
        return
    if venv_dir.parent.resolve(strict=True) != allowed_parent.resolve(strict=True):
        raise EnvironmentResolutionError("unsafe_venv_path", str(venv_dir))
    if venv_dir.is_symlink() or _is_reparse_point(venv_dir):
        venv_dir.unlink()
        return
    if os.name != "nt" and _host_uid() == 0 and not _trusted_host_path(venv_dir, directory=True):
        raise EnvironmentResolutionError(
            "unsafe_venv_path",
            "case venv is not host-owned",
        )
    shutil.rmtree(venv_dir)


def _clear_active_environment_evidence(output_dir: Path) -> None:
    for name in (
        "03a_environment.lock.json",
        "03a_pip_resolution_report.json",
        "03a_pip_install_report.json",
    ):
        path = output_dir / name
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            raise EnvironmentResolutionError(
                "environment_evidence_cleanup_failed",
                f"host could not retire stale environment evidence: {name}",
            ) from exc


def _cleanup_failed_host_shared_runtime(*, output_dir: Path) -> None:
    """Revoke only the case grant; the selected host prefix is never retired."""

    try:
        (output_dir / "03a_environment.lock.json").unlink(missing_ok=True)
    except OSError as exc:
        raise EnvironmentResolutionError(
            "environment_evidence_cleanup_failed",
            "host could not revoke the failed case environment lock",
        ) from exc


def _cleanup_failed_case_runtime(
    *,
    output_dir: Path,
    venv_dir: Path,
    retire_case_venv_fn=_retire_case_venv,
) -> None:
    """Revoke the active grant first; taint remains until a clean rebuild."""

    try:
        (output_dir / "03a_environment.lock.json").unlink(missing_ok=True)
    finally:
        try:
            retire_case_venv_fn(venv_dir, allowed_parent=venv_dir.parent)
        except (OSError, EnvironmentResolutionError):
            pass


def _create_case_venv(
    *,
    base_python: Path,
    venv_dir: Path,
    allowed_parent: Path,
    output_dir: Path,
    working_dir: Path,
    run_argv: ArgvRunner,
    open_or_create_host_root_fn=_open_or_create_host_root,
    retire_case_venv_fn=_retire_case_venv,
    run_checked_fn=_run_checked,
) -> None:
    if os.name != "nt" and _host_uid() == 0:
        allowed_parent = open_or_create_host_root_fn(allowed_parent)
    else:
        allowed_parent.mkdir(parents=True, exist_ok=True)
    retire_case_venv_fn(venv_dir, allowed_parent=allowed_parent)
    staging = allowed_parent / f".{venv_dir.name}.{os.getpid()}.building"
    retire_case_venv_fn(staging, allowed_parent=allowed_parent)
    result = run_checked_fn(
        run_argv,
        (str(base_python), "-m", "venv", str(staging)),
        cwd=working_dir,
        timeout=300.0,
    )
    if result.returncode != 0:
        retire_case_venv_fn(staging, allowed_parent=allowed_parent)
        raise EnvironmentResolutionError(
            "venv_create_failed",
            (result.stderr or result.stdout or "case venv creation failed")[-4000:],
        )
    try:
        staging.chmod(0o755)
    except OSError:
        pass
    staging.rename(venv_dir)
    _write_case_venv_marker(
        venv_dir=venv_dir,
        base_python=base_python,
        output_dir=output_dir,
    )
