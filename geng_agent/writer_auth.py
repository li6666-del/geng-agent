"""Short-lived Codex authentication homes for an isolated Writer invocation.

Only the login file, and inherited-mode configuration, cross this boundary.
Personal MCP files, sandbox account secrets and caches receive no new access.
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import shutil
import stat
import tempfile


def _env_value(env, name):
    return next((value for key, value in env.items() if key.upper() == name), None)


def _remove_private_home(path: Path, parent: Path) -> None:
    # Check the fixed lexical name, not a resolved target controlled by a child.
    if not path.is_absolute() or path.parent != parent or not path.name.startswith("geng-writer-auth-"):
        raise RuntimeError("Refusing to remove an unexpected Writer authentication directory")
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    is_reparse = bool(getattr(metadata, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    if path.is_symlink():
        path.unlink()
    elif is_reparse:
        # A directory junction must be removed as a link, never traversed.
        path.rmdir()
    elif stat.S_ISDIR(metadata.st_mode):
        # Python >=3.8 on Windows does not traverse child directory junctions.
        shutil.rmtree(path)
    else:
        path.unlink()


@contextmanager
def writer_auth_home(domain, model_config, env):
    """Yield an environment using a disposable, explicitly authorized CODEX_HOME.

    TEMP/TMP is selected before the Domain changes child environment roots. The
    caller can place it in the configured development-artifact temporary root.
    No caller environment or original authentication/configuration file changes.
    """
    temporary_parent = Path(_env_value(env, "TEMP") or _env_value(env, "TMP") or tempfile.gettempdir()).resolve()
    temporary_parent.mkdir(parents=True, exist_ok=True)
    home = Path(tempfile.mkdtemp(prefix="geng-writer-auth-", dir=temporary_parent))
    original_home = _env_value(env, "CODEX_HOME")
    original_home = Path(original_home).expanduser() if original_home else Path.home() / ".codex"
    native_account = (model_config.provider == "openai" and not model_config.base_url
                      and not model_config.env_key and not _env_value(env, "OPENAI_API_KEY"))
    try:
        # Copy before granting the Writer access, so it cannot substitute targets.
        names = []
        if not model_config.managed or native_account:
            names.append("auth.json")
        if not model_config.managed:
            names.append("config.toml")
        for name in names:
            source = original_home / name
            if source.is_file():
                shutil.copyfile(source, home / name)
        domain.allow_write(home)
        isolated_env = {key: value for key, value in env.items() if key.upper() != "CODEX_HOME"}
        isolated_env["CODEX_HOME"] = str(home)
        yield isolated_env
    finally:
        _remove_private_home(home, temporary_parent)
