"""Safe host paths and cross-process locking for case runtimes."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from typing import Any, Iterator, Mapping

from .case_runtime_contracts import EnvironmentResolutionError, HOST_SHARED_RUNTIME_MODE


def _host_uid() -> int | None:
    return getattr(os, "geteuid", lambda: None)()


def _assert_secure_host_ancestor(path: Path) -> None:
    """Reject replaceable ancestors for a root-owned runtime directory."""

    if os.name == "nt" or _host_uid() != 0:
        return
    current = path.absolute()
    while True:
        try:
            info = current.lstat()
        except OSError as exc:
            raise EnvironmentResolutionError(
                "unsafe_runtime_root",
                "case runtime root has an unavailable ancestor",
            ) from exc
        if current.is_symlink() or not stat.S_ISDIR(info.st_mode) or info.st_uid != 0:
            raise EnvironmentResolutionError(
                "unsafe_runtime_root",
                "case runtime root ancestors must be root-owned directories without links",
            )
        writable = bool(info.st_mode & 0o022)
        sticky_root = bool(info.st_mode & stat.S_ISVTX) and info.st_uid == 0
        if writable and not sticky_root:
            raise EnvironmentResolutionError(
                "unsafe_runtime_root",
                "case runtime root has a replaceable group/world-writable ancestor",
            )
        if current == current.parent:
            break
        current = current.parent


def _open_or_create_host_root(path: Path) -> Path:
    """Atomically establish a non-link, root-owned, non-writable runtime root."""

    root = path.absolute()
    if not root.is_absolute() or root == Path(root.anchor):
        raise EnvironmentResolutionError(
            "unsafe_runtime_root",
            "case runtime root must be an absolute non-root directory",
        )
    if os.name == "nt" or _host_uid() != 0:
        root.mkdir(parents=True, exist_ok=True)
        return root

    _assert_secure_host_ancestor(root.parent)
    try:
        os.mkdir(root, 0o755)
    except FileExistsError:
        pass
    except OSError as exc:
        raise EnvironmentResolutionError(
            "unsafe_runtime_root",
            "case runtime root could not be created",
        ) from exc

    try:
        info = root.lstat()
    except OSError as exc:
        raise EnvironmentResolutionError(
            "unsafe_runtime_root",
            "case runtime root could not be inspected",
        ) from exc
    if (
        root.is_symlink()
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != 0
        or bool(info.st_mode & 0o022)
    ):
        raise EnvironmentResolutionError(
            "unsafe_runtime_root",
            "case runtime root must be root-owned and not group/world writable",
        )

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(root, flags)
    except OSError as exc:
        raise EnvironmentResolutionError(
            "unsafe_runtime_root",
            "case runtime root failed no-follow verification",
        ) from exc
    try:
        confirmed = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(confirmed.st_mode)
            or confirmed.st_uid != 0
            or bool(confirmed.st_mode & 0o022)
        ):
            raise EnvironmentResolutionError(
                "unsafe_runtime_root",
                "case runtime root changed during verification",
            )
        # mkdir(0o755) is still filtered by the caller's umask.  Only repair
        # traversal permissions after the same no-follow descriptor has proved
        # that this is the intended root-owned directory.
        os.fchmod(descriptor, 0o755)
        confirmed = os.fstat(descriptor)
        if stat.S_IMODE(confirmed.st_mode) != 0o755:
            raise EnvironmentResolutionError(
                "unsafe_runtime_root",
                "case runtime root permissions could not be established",
            )
    finally:
        os.close(descriptor)
    return root


def _trusted_host_path(path: Path, *, directory: bool) -> bool:
    """Check root ownership and immutable-to-unprivileged metadata without following links."""

    try:
        info = path.lstat()
    except OSError:
        return False
    if path.is_symlink() or (os.name != "nt" and bool(info.st_mode & 0o022)):
        return False
    if directory:
        type_ok = stat.S_ISDIR(info.st_mode)
    else:
        type_ok = stat.S_ISREG(info.st_mode) and info.st_nlink == 1
    if not type_ok:
        return False
    return os.name == "nt" or _host_uid() != 0 or info.st_uid == 0


def _host_prefix_guess(host_python: Path) -> Path:
    """Return a stable prefix guess without executing the selected launcher."""

    try:
        if host_python.samefile(Path(sys.executable)):
            return Path(sys.prefix).absolute()
    except OSError:
        pass
    if host_python.parent.name.casefold() in {"bin", "scripts"}:
        return host_python.parent.parent.absolute()
    return host_python.parent.absolute()


def _host_prefix_from_identity(
    interpreter_identity: Any,
    *,
    fallback_python: Path,
) -> Path:
    identity = interpreter_identity if isinstance(interpreter_identity, Mapping) else {}
    value = str(identity.get("prefix") or "").strip()
    return Path(value).absolute() if value else _host_prefix_guess(fallback_python)


def _host_shared_runtime_mutex_identity(host_python: Path) -> str:
    """Hash the real executable plus its selected host prefix.

    The case directory is deliberately absent. Equivalent launcher aliases in
    one prefix therefore serialize on the same host-wide package mutation lock.
    Replacing the underlying interpreter produces a new identity on platforms
    that expose file identity metadata.
    """

    launcher = host_python.absolute()
    try:
        resolved = launcher.resolve(strict=True)
        info = resolved.stat()
    except OSError as exc:
        raise EnvironmentResolutionError(
            "host_interpreter_unavailable",
            "the selected host Python launcher is unavailable",
        ) from exc
    device = getattr(info, "st_dev", None)
    inode = getattr(info, "st_ino", None)
    file_identity = (
        {"device": device, "inode": inode}
        if inode not in (None, 0)
        else {"resolved_executable": str(resolved)}
    )
    payload = {
        "schema_version": 1,
        "os_name": os.name,
        "prefix": str(_host_prefix_guess(launcher).resolve(strict=False)),
        "file_identity": file_identity,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _host_shared_runtime_lock_target(host_python: Path) -> Path:
    """Return the global lock file for the selected real host interpreter."""

    configured = os.environ.get("GENG_HOST_RUNTIME_LOCK_ROOT")
    root = (
        Path(configured).expanduser()
        if configured
        else Path(tempfile.gettempdir()) / "geng-agent-host-runtime-locks"
    )
    if not root.is_absolute() or root == Path(root.anchor):
        raise EnvironmentResolutionError("unsafe_runtime_root", str(root))
    identity = _host_shared_runtime_mutex_identity(host_python)
    return root / f"host-shared-{identity}.lock"


def _host_runtime_provenance(
    *,
    host_python: Path,
    interpreter_identity: Any,
) -> dict[str, Any]:
    identity = interpreter_identity if isinstance(interpreter_identity, Mapping) else {}
    try:
        resolved = host_python.resolve(strict=True)
    except OSError as exc:
        raise EnvironmentResolutionError(
            "host_interpreter_unavailable",
            "the selected host Python launcher became unavailable",
        ) from exc
    return {
        "schema_version": 1,
        "kind": "geng.host_shared_runtime",
        "runtime_mode": HOST_SHARED_RUNTIME_MODE,
        "selected_launcher": str(host_python.absolute()),
        "resolved_executable": str(resolved),
        "prefix": str(_host_prefix_from_identity(identity, fallback_python=host_python)),
        "base_prefix": identity.get("base_prefix"),
        "python_full_version": identity.get("python_full_version"),
        "implementation": identity.get("implementation"),
        "mutex_identity_sha256": _host_shared_runtime_mutex_identity(host_python),
    }


@contextmanager
def _host_shared_runtime_guard(host_python: Path) -> Iterator[None]:
    """Serialize the complete shared-host environment transaction."""

    with _runtime_file_guard(_host_shared_runtime_lock_target(host_python)):
        yield


@contextmanager
def _case_runtime_guard(venv_dir: Path) -> Iterator[None]:
    """Serialize venv, pip, report, probe, and final lock work for one case."""

    with _runtime_file_guard(venv_dir.parent / f".{venv_dir.name}.lock"):
        yield


@contextmanager
def _runtime_file_guard(lock_path: Path) -> Iterator[None]:
    """Hold one host-owned cross-process runtime lock file."""

    parent = lock_path.parent
    if os.name != "nt" and _host_uid() == 0:
        parent = _open_or_create_host_root(parent)
    else:
        parent.mkdir(parents=True, exist_ok=True)
    lock_path = parent / lock_path.name
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise EnvironmentResolutionError(
            "unsafe_runtime_lock",
            "case runtime lock could not be opened safely",
        ) from exc
    handle = os.fdopen(descriptor, "r+b", closefd=True)
    try:
        info = os.fstat(handle.fileno())
        insecure_posix_metadata = os.name != "nt" and (
            bool(info.st_mode & 0o022)
            or (_host_uid() == 0 and info.st_uid != 0)
        )
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or insecure_posix_metadata
        ):
            raise EnvironmentResolutionError(
                "unsafe_runtime_lock",
                "case runtime lock metadata is not host-owned",
            )
        if os.name == "nt":
            import msvcrt

            if info.st_size == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()
