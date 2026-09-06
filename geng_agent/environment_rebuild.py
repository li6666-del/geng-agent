"""Verify final delivery in a clean venv; failures do not alter scientific verdicts."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from typing import Any
from urllib.parse import urlsplit

from packaging.requirements import Requirement
from .delivery_environment import _read
from .portability_inventory import build_source_inventory
from .portability_smoke import _run_relocated_smoke


@contextmanager
def _environment_lock(path: Path):
    """OS releases the lock even after interruption; independent envs do not wait."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt
            handle.write(b"0")
            handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def verify_clean_environment(project_root: str | Path, *, cache_dir: str | Path | None = None,
                             python_executable: str | Path | None = None,
                             timeout_s: float = 600, smoke_timeout_s: float = 120) -> dict[str, Any]:
    project = Path(project_root).resolve()
    started = time.monotonic()
    result: dict[str, Any] = {"schema_version": "1.0", "status": "inconclusive", "verified": False,
                              "environment_reused": False, "system_site_packages": False}
    base = str(python_executable or sys.executable)
    try:
        document = _read(project / "installation.json")
        if not document:
            raise ValueError("installation.json is missing; export the final installation contract first")
        if any("Unexportable" in str(warning) for warning in document.get("warnings", [])):
            raise ValueError("an unsafe or non-reconstructable requirement was not exported")
        for line in (project / "requirements.txt").read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if Requirement(line).url:
                raise ValueError("clean reconstruction accepts package requirements, not arbitrary direct URLs")
        for line in (project / "constraints.repro.txt").read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and Requirement(line).url:
                raise ValueError("constraints must not contain direct URLs")
        indexes = document.get("indexes", [])
        if not isinstance(indexes, list) or not indexes:
            raise ValueError("No recorded HTTPS package source is available")
        for source in indexes:
            parsed = urlsplit(str(source))
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("Package sources must be recorded unauthenticated HTTPS indexes")
        identity_probe = subprocess.run([base, "-I", "-c", "import sys,platform,json;print(json.dumps([sys.version,platform.platform(),platform.machine()]))"],
                                        text=True, capture_output=True, check=True, timeout=30)
        identity = {"interpreter": json.loads(identity_probe.stdout),
                    "files": {name: (project / name).read_text(encoding="utf-8-sig")
                              for name in ("requirements.txt", "requirements.repro.txt", "constraints.repro.txt", "installation.json")}}
        key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        result["environment_key"] = key
        # Default persistent cache is a host-owned sibling of case audit, outside
        # the delivered tree. Every project is still tested afresh after install.
        root = Path(cache_dir).resolve() if cache_dir else project.parent / "audit" / "clean_environments"
        if root == project or root.is_relative_to(project):
            raise ValueError("environment cache must be outside the delivered project")
        root.mkdir(parents=True, exist_ok=True)
        with _environment_lock(root / f"{key}.lock"):
            environment = root / key
            if environment.is_symlink():
                raise ValueError("environment cache must not be a symbolic link")
            python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            marker = _read(environment / ".delivery_environment.json")
            reused = bool(python.is_file() and marker.get("environment_key") == key)
            if not reused:
                subprocess.run([base, "-I", "-m", "venv", str(environment)], text=True,
                               capture_output=True, check=True, timeout=120)
                install_args = [str(python), "-I", "-m", "pip", "--isolated", "install",
                                "--disable-pip-version-check", "--only-binary=:all:", "--index-url", indexes[0]]
                for source in indexes[1:]:
                    install_args.extend(["--extra-index-url", source])
                install_args.extend(["-c", "constraints.repro.txt", "-r", "requirements.txt"])
                install = subprocess.run(install_args,
                                         cwd=project, capture_output=True, text=True, timeout=timeout_s, check=False)
                result["installation"] = {"returncode": install.returncode,
                                           "stdout_tail": install.stdout[-4000:], "stderr_tail": install.stderr[-4000:]}
                if install.returncode:
                    result["status"] = "installation_failed"
                    return result
            configuration = (environment / "pyvenv.cfg").read_text(encoding="utf-8").lower()
            if "include-system-site-packages = false" not in configuration:
                raise ValueError("Cached runtime is not an isolated virtual environment")
            check = subprocess.run([str(python), "-I", "-m", "pip", "--isolated", "check"],
                                   capture_output=True, text=True, timeout=60, check=False)
            if check.returncode:
                result.update(status="dependency_check_failed", detail=check.stdout[-4000:] + check.stderr[-4000:])
                return result
            inventory = subprocess.run([str(python), "-I", "-m", "pip", "--isolated", "list", "--format=json"],
                                       capture_output=True, text=True, timeout=60, check=True)
            versions = json.loads(inventory.stdout)
            if reused and marker.get("installed_distributions") != versions:
                result.update(status="cache_changed", detail="Cached dependency inventory changed; use a new cache directory to rebuild.")
                return result
            (environment / ".delivery_environment.json").write_text(json.dumps({"environment_key": key,
                "installed_distributions": versions}, sort_keys=True), encoding="utf-8")
            result["environment_reused"] = reused
            result["installed_distributions"] = versions
            smoke, issues, warnings = _run_relocated_smoke(project, inventory=build_source_inventory(project),
                python_executable=python, smoke_command=None, timeout_s=smoke_timeout_s)
            result.update(smoke=smoke, issues=issues, warnings=warnings,
                          verified=bool(smoke.get("verified")) and not issues,
                          status="passed" if smoke.get("verified") and not issues else "smoke_failed")
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        result.update(status="inconclusive", detail=f"{type(exc).__name__}: {exc}")
    finally:
        result["duration_s"] = round(time.monotonic() - started, 3)
        result["scientific_outcome_unchanged"] = True
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project")
    parser.add_argument("--cache-dir")
    parser.add_argument("--python")
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--output")
    args = parser.parse_args()
    result = verify_clean_environment(args.project, cache_dir=args.cache_dir,
                                      python_executable=args.python, timeout_s=args.timeout)
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        Path(args.output).write_text(rendered, encoding="utf-8")
    print(rendered)
    return 0 if result["verified"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
