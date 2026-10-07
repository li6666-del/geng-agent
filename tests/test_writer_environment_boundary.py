"""Exercise private Writer environments with real, offline AppContainer processes."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest

from geng_agent.case_runtime_contracts import CaseRuntime
from geng_agent.windows_process_boundary import WriterProcessDomain
from geng_agent import writer_environment


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows AppContainer boundary")


class OfflineWriterDomain(WriterProcessDomain):
    """Keep production process launch behavior, with network disabled for this test."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.commands = []

    def run(self, command, **kwargs):
        self.commands.append(tuple(str(part) for part in command))
        kwargs["allow_network"] = False
        try:
            return super().run(command, **kwargs)
        except subprocess.CalledProcessError as exc:
            print("Isolated command stdout:", exc.stdout)
            print("Isolated command stderr:", exc.stderr)
            raise


@pytest.fixture
def environment(tmp_path, monkeypatch):
    # Use the already installed test runtime as the shared base. No base venv,
    # installed package, case output, or real Writer environment is modified.
    monkeypatch.setattr(writer_environment, "get_config_value", lambda _key: None)
    monkeypatch.setenv("PIP_NO_INDEX", "1")
    monkeypatch.setenv("PIP_DISABLE_PIP_VERSION_CHECK", "1")
    sandbox = tmp_path / "writer"
    sandbox.mkdir()
    runtime = CaseRuntime(
        venv_dir=Path(sys.prefix), python_executable=Path(sys.executable),
        request_path=tmp_path / "request.json", lock_path=tmp_path / "lock.json",
        report_path=tmp_path / "report.json", environment_hash="test-existing-runtime",
        manifest={}, lock={}, report={}, trusted_read_roots=(Path(sys.prefix),),
    )
    root = writer_environment.writer_environment_root(sandbox, runtime)
    with OfflineWriterDomain(sandbox, extra_write_roots=(root,)) as domain:
        domain.allow_runtime_read(runtime.venv_dir)
        yield sandbox, runtime, root, domain


def test_production_private_venv_build_and_pip_run_in_writer_domain(environment):
    sandbox, runtime, root, domain = environment
    try:
        python = writer_environment.ensure_writer_environment(
            sandbox, runtime, process_boundary=domain,
        )
    except subprocess.CalledProcessError:
        # venv discards its bundled ensurepip child's output. Keep that inner
        # failure visible without trying an unrestricted host installation.
        partial_python = root / "Scripts" / "python.exe"
        if partial_python.is_file():
            diagnostic = domain.run(
                [str(partial_python), "-I", "-m", "ensurepip", "--upgrade", "--default-pip", "-v"],
                cwd=sandbox, capture_output=True, text=True, timeout=30,
            )
            print("Isolated ensurepip diagnostic stdout:", diagnostic.stdout)
            print("Isolated ensurepip diagnostic stderr:", diagnostic.stderr)
        raise
    assert python == root / "Scripts" / "python.exe"
    assert any(command[1:4] == ("-I", "-m", "venv") for command in domain.commands)
    assert any(command[1:4] == ("-I", "-S", "-c") for command in domain.commands)

    result = domain.run(
        [str(python), "-I", "-c",
         "import hashlib,json,math,sys;"
         "print(json.dumps({'prefix':sys.prefix,'answer':math.isqrt(144),"
         "'digest':hashlib.sha256(b'writer').hexdigest(),'path':sys.path}))"],
        cwd=sandbox, capture_output=True, text=True, check=True, timeout=30,
    )
    observed = json.loads(result.stdout)
    assert Path(observed["prefix"]) == root
    assert observed["answer"] == 12
    assert len(observed["digest"]) == 64
    shared_site = runtime.venv_dir / "Lib" / "site-packages"
    assert shared_site in [Path(path) for path in observed["path"]]
    assert (root / "Lib" / "site-packages" / "geng_shared_science.pth").read_text(
        encoding="utf-8",
    ).strip() == str(shared_site.resolve())

    pip = domain.run(
        [str(python), "-I", "-m", "pip", "--version"],
        cwd=sandbox, capture_output=True, text=True, check=True, timeout=30,
    )
    assert pip.stdout.startswith("pip ")
    # Pip may itself come from the shared read-only site-packages. Its install
    # destination must still be this private environment.
    dist = "geng_boundary_probe-0.0.1.dist-info"
    files = {
        "geng_boundary_probe.py": "VALUE = 731\n",
        dist + "/METADATA": "Metadata-Version: 2.1\nName: geng-boundary-probe\nVersion: 0.0.1\n",
        dist + "/WHEEL": "Wheel-Version: 1.0\nGenerator: offline-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    files[dist + "/RECORD"] = "".join(name + ",,\n" for name in [*files, dist + "/RECORD"])
    wheel = sandbox / "geng_boundary_probe-0.0.1-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    installed = domain.run(
        [str(python), "-I", "-m", "pip", "install", "--no-index", "--no-deps",
         "--no-cache-dir", "--disable-pip-version-check", str(wheel)],
        cwd=sandbox, capture_output=True, text=True, check=True, timeout=30,
    )
    assert "Successfully installed" in installed.stdout
    checked = domain.run(
        [str(python), "-I", "-c",
         "import tempfile,json;from pathlib import Path;import geng_boundary_probe as p;"
         "d=tempfile.TemporaryDirectory();target=Path(d.name)/'value';target.write_text('private');"
         "assert target.read_text()=='private';d.cleanup();"
         "print(json.dumps({'file':p.__file__,'value':p.VALUE}))"],
        cwd=sandbox, capture_output=True, text=True, check=True, timeout=30,
    )
    package = json.loads(checked.stdout)
    assert package["value"] == 731
    assert Path(package["file"]).is_relative_to(root)
    assert not (shared_site / "geng_boundary_probe.py").exists()


def test_restored_site_packages_junction_cannot_borrow_host_write_access(environment, tmp_path):
    sandbox, runtime, root, domain = environment
    # Restore a genuine Python environment without bootstrapping pip: this
    # regression concerns unsafe host writes, independently of ensurepip health.
    domain.run(
        [str(runtime.python_executable), "-I", "-m", "venv", "--without-pip", str(root)],
        cwd=sandbox, capture_output=True, text=True, check=True, timeout=30,
    )
    writer_environment.ensure_writer_environment(sandbox, runtime, process_boundary=domain)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "geng_shared_science.pth"
    original = b"outside host-owned sentinel; do not replace\r\n"
    sentinel.write_bytes(original)
    marker = root / ".geng_writer_environment.json"
    original_marker = marker.read_bytes()
    local_site = root / "Lib" / "site-packages"
    saved_site = root / "Lib" / "site-packages-original"

    # These are verified disposable test paths. A rename retains every venv
    # file; os.rmdir below removes only the junction, never its target contents.
    for target in (local_site, saved_site, outside):
        assert target.resolve().is_relative_to(tmp_path.resolve())
    local_site.rename(saved_site)
    junction_created = False
    try:
        subprocess.run(
            [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", "mklink", "/J",
             str(local_site), str(outside)],
            check=True, capture_output=True, text=True, timeout=10,
        )
        junction_created = True
        with pytest.raises(subprocess.CalledProcessError) as stopped:
            writer_environment.ensure_writer_environment(
                sandbox, runtime, process_boundary=domain,
            )
        assert "PermissionError" in (stopped.value.stderr or "")
        assert sentinel.read_bytes() == original
        assert list(outside.iterdir()) == [sentinel]
        assert marker.read_bytes() == original_marker
    finally:
        if junction_created:
            os.rmdir(local_site)
        saved_site.rename(local_site)
