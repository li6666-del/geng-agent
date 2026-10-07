"""Real, model-free Windows permission tests against disposable processes."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import ctypes
from types import SimpleNamespace

import pytest

from geng_agent.windows_process_boundary import ProcessBoundaryUnavailable, WriterProcessDomain

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows AppContainer boundary")

_RIGHTS_PROBE = r'''
import ctypes, json, os, subprocess, sys
from ctypes import wintypes as wt
k=ctypes.WinDLL("kernel32",use_last_error=True)
k.OpenProcess.argtypes=[wt.DWORD,wt.BOOL,wt.DWORD]; k.OpenProcess.restype=wt.HANDLE
k.CloseHandle.argtypes=[wt.HANDLE]
k.TerminateProcess.argtypes=[wt.HANDLE,wt.UINT]
def probe(pid):
    result={}
    for name,mask in {"terminate":1,"write_dac":0x40000,"write_owner":0x80000,
                      "vm_write":0x20,"create_thread":2,"duplicate_handle":0x40}.items():
        handle=k.OpenProcess(mask,False,pid)
        result[name]=bool(handle)
        if handle:k.CloseHandle(handle)
    return result
targets=[int(value) for value in sys.argv[1:]]
own=subprocess.Popen([sys.executable,"-c","import time;time.sleep(90)"])
try:
    result={"outsiders":[probe(pid) for pid in targets],"own":probe(own.pid)}
    own.terminate();own.wait(timeout=10)
    result["own_stopped"]=own.returncode is not None
    child_code="import ctypes;from ctypes import wintypes as w;k=ctypes.WinDLL('kernel32',use_last_error=True);k.OpenProcess.argtypes=[w.DWORD,w.BOOL,w.DWORD];k.OpenProcess.restype=w.HANDLE;print(bool(k.OpenProcess(1,False,"+str(targets[0])+")))"
    child=subprocess.run([sys.executable,"-c",child_code],capture_output=True,text=True,check=True)
    result["grandchild_can_terminate_host"]=child.stdout.strip()=="True"
    print(json.dumps(result))
finally:
    if own.poll() is None:own.terminate();own.wait()
'''


@pytest.fixture(scope="module")
def domains(tmp_path_factory):
    first = tmp_path_factory.mktemp("app_a")
    second = tmp_path_factory.mktemp("app_b")
    with WriterProcessDomain(first) as a, WriterProcessDomain(second) as b:
        yield a, b


def test_native_process_rights_and_descendants(domains):
    a, b = domains
    with subprocess.Popen([sys.executable, "-c", "import time;time.sleep(90)"]) as host:
        peer = b.popen([sys.executable, "-c", "import time;time.sleep(90)"])
        try:
            result = a.run([sys.executable, "-c", _RIGHTS_PROBE, str(host.pid), str(peer.pid)],
                           capture_output=True, text=True, check=True, timeout=30)
            observed = json.loads(result.stdout)
            assert all(not permitted for rights in observed["outsiders"] for permitted in rights.values())
            assert all(observed["own"].values())
            assert observed["own_stopped"]
            assert not observed["grandchild_can_terminate_host"]
            assert host.poll() is None and peer.poll() is None
        finally:
            peer.kill_tree()
            peer.wait(timeout=10)
            host.terminate()
            host.wait(timeout=10)


def test_powershell_cannot_stop_host_and_stdio_works(domains):
    a, _ = domains
    powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    with subprocess.Popen([sys.executable, "-c", "import time;time.sleep(90)"]) as host:
        try:
            result = a.run([str(powershell), "-NoProfile", "-NonInteractive", "-Command",
                            f"Stop-Process -Id {host.pid} -Force -ErrorAction Stop"],
                           capture_output=True, text=True, timeout=30)
            assert result.returncode != 0
            assert host.poll() is None
        finally:
            host.terminate()
            host.wait(timeout=10)
    result = a.run([sys.executable, "-c", "import sys;print(sys.stdin.read().upper())"],
                   input="hello", capture_output=True, text=True, check=True, timeout=10)
    assert result.stdout.strip() == "HELLO"


def test_private_files_and_independent_run_stop(domains):
    a, b = domains
    marker = b.workspace / "private.txt"
    marker.write_text("peer-only", encoding="utf-8")
    b.allow_write(b.workspace)
    result = a.run([sys.executable, "-c", "import pathlib,sys;print(pathlib.Path(sys.argv[1]).read_text())", str(marker)],
                   capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    writer = a.popen([sys.executable, "-c", "import time;time.sleep(90)"])
    experiment = a.popen([sys.executable, "-c", "import time;time.sleep(90)"])
    try:
        experiment.kill_tree()
        experiment.wait(timeout=10)
        assert writer.poll() is None
        result = a.run([sys.executable, "-c", "from pathlib import Path;Path('own.txt').write_text('ok');print('ok')"],
                       capture_output=True, text=True, check=True, timeout=10)
        assert result.stdout.strip() == "ok"
    finally:
        writer.kill_tree()
        writer.wait(timeout=10)


def test_writer_can_read_only_granted_run_logs(domains, tmp_path):
    writer, peer = domains
    run_dir = tmp_path / "execution_runs" / "current"
    run_dir.mkdir(parents=True)
    stdout = run_dir / "stdout.log"
    stderr = run_dir / "stderr.log"
    unrelated = run_dir / "private.json"
    peer_log = peer.workspace / "peer.log"
    for path, content in ((stdout, "own stdout"), (stderr, "own stderr"),
                          (unrelated, "host metadata"), (peer_log, "peer run")):
        path.write_text(content, encoding="utf-8")
    writer.allow_read(stdout)
    writer.allow_read(stderr)
    probe = ("import json,sys;from pathlib import Path;"
             "read=lambda p: Path(p).read_text(encoding='utf-8');"
             "results=[];"
             "\nfor path in sys.argv[1:]:\n"
             " try: results.append(read(path))\n"
             " except OSError: results.append('denied')\n"
             "print(json.dumps(results))")
    result = writer.run([sys.executable, "-c", probe,
                         str(stdout), str(stderr), str(unrelated), str(peer_log)],
                        capture_output=True, text=True, check=True, timeout=20)
    assert json.loads(result.stdout) == ["own stdout", "own stderr", "denied", "denied"]


def test_reparse_path_cannot_expand_authorized_roots(domains, tmp_path):
    a, _ = domains
    outside = tmp_path / "outside"
    outside.mkdir()
    junction = a.workspace / "redirected"
    subprocess.run([os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", "mklink", "/J",
                    str(junction), str(outside)], capture_output=True, check=True)
    previous_roots = a._runtime_roots.copy()
    try:
        # Even a path under a cached runtime must pass the reparse check first.
        a._runtime_roots.add(a.workspace)
        with pytest.raises(ProcessBoundaryUnavailable, match="reparse"):
            a.allow_read(junction)
        with pytest.raises(ProcessBoundaryUnavailable, match="reparse"):
            a.allow_write(junction)
        with pytest.raises(ProcessBoundaryUnavailable, match="reparse"):
            a.allow_runtime_read(junction)
        assert outside not in a._grants
    finally:
        a._runtime_roots = previous_roots
        # rmdir removes this directory junction itself, never its target tree.
        os.rmdir(junction)


def test_runtime_reads_do_not_add_redundant_private_acls(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    package = runtime / "package.py"
    package.write_text("", encoding="utf-8")
    private = tmp_path / "input.json"
    private.write_text("{}", encoding="utf-8")
    domain = object.__new__(WriterProcessDomain)
    domain._runtime_roots = {runtime}
    domain._closed = False
    domain._lock = threading.RLock()
    grants = []
    monkeypatch.setattr(domain, "_grant", lambda path, mask: grants.append((path, mask)))
    domain.allow_read(runtime)
    domain.allow_read(package)
    assert grants == []
    domain.allow_read(private)
    assert grants == [(private, 0x1200A9)]


@pytest.mark.parametrize("fail_at_target", [True, False])
def test_authorization_error_identifies_exact_path_and_requested_access(tmp_path, monkeypatch, fail_at_target):
    from geng_agent import windows_process_boundary as boundary
    target = tmp_path / "catalog.json"
    failed_path = target if fail_at_target else target.parent
    opened = []
    closed = []

    def open_path(path, access, *_args):
        if Path(path) == failed_path:
            ctypes.set_last_error(5)
            return ctypes.c_void_p(-1).value
        handle = len(opened) + 1
        opened.append(handle)
        return handle

    monkeypatch.setattr(boundary, "_kernel", SimpleNamespace(CreateFileW=open_path,
        GetFileInformationByHandleEx=lambda *_args: True, CloseHandle=closed.append))
    with pytest.raises(ProcessBoundaryUnavailable) as error:
        with boundary._fixed_authorization_path(target):
            pytest.fail("denied authorization unexpectedly succeeded")
    assert str(failed_path) in str(error.value)
    assert f"access={0x60080 if fail_at_target else 0x20080:#x}" in str(error.value)
    assert "WinError 5" in str(error.value)
    assert closed == list(reversed(opened))
