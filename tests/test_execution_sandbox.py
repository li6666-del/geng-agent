from __future__ import annotations
import json
from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory, gettempdir
import shutil
import uuid
import unittest
from unittest.mock import patch

from geng_agent.execution_sandbox import ScientificSandboxUnavailable, scientific_sandbox_launch


@contextmanager
def native_sandbox_temporary_directory():
    # Python's Windows mkdtemp uses owner-only ACLs (mode 0700). Restricted
    # tokens cannot write those directories even under the built-in workspace
    # profile. Real cases use ordinary mkdir and inherit standard parent ACLs.
    path = Path(gettempdir()) / ("geng_native_sandbox_" + uuid.uuid4().hex)
    path.mkdir()
    try:
        yield str(path)
    finally:
        path.resolve().relative_to(Path(gettempdir()).resolve())
        shutil.rmtree(path)


class ExecutionSandboxTests(unittest.TestCase):
    def test_missing_cli_fails_before_science_execution(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "output"
            output.mkdir()
            with self.assertRaises(ScientificSandboxUnavailable):
                scientific_sandbox_launch([sys.executable, "-c", "print(1)"], work_dir=root,
                    write_roots=[output], env={}, codex_executable=root / "missing-codex")

    def test_real_os_sandbox_blocks_python_and_native_writes_outside_outputs(self):
        with native_sandbox_temporary_directory() as temporary:
            base = Path(temporary)
            root = base / "project"
            output = root / "outputs"
            output.mkdir(parents=True)
            runtime = root / ".geng_runtime"
            runtime.mkdir()
            frozen = root / "science.py"
            frozen.write_text("original source")
            audit = base / "audit"
            audit.mkdir()
            (audit / "receipt.json").write_text("original receipt")
            script = """import ctypes,json,os,pathlib,sys
root=pathlib.Path.cwd()
result={}
print('cwd='+str(root))
assert os.environ['HOME'] == str(root/'.geng_runtime')
assert os.environ['USERPROFILE'] == str(root/'.geng_runtime')
os.makedirs(root/'.geng_runtime'/'torchinductor'/'nested', exist_ok=True)
(root/'.geng_runtime'/'torchinductor'/'nested'/'cache.txt').write_text('cache')
for name,path in [('output',root/'outputs'/'result.txt'),('source',root/'science.py'),('audit',root.parent/'audit'/'receipt.json')]:
    try:
        path.write_text('python mutation')
        result['python_'+name]=True
    except OSError as exc:
        result['python_'+name]=False
        print(str(exc))
    if os.name=='nt':
        api=ctypes.WinDLL('kernel32',use_last_error=True)
        api.CreateFileW.restype=ctypes.c_void_p
        handle=api.CreateFileW(str(path),0x40000000,0,None,2,0,None)
        result['native_'+name]=handle not in (None,ctypes.c_void_p(-1).value)
        if result['native_'+name]: api.CloseHandle(ctypes.c_void_p(handle))
    else:
        api=ctypes.CDLL(None)
        descriptor=api.open(os.fsencode(path),os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
        result['native_'+name]=descriptor>=0
        if descriptor>=0: api.close(descriptor)
print(json.dumps(result))
"""
            env = {"HOME": str(runtime), "USERPROFILE": str(runtime), "TEMP": str(runtime), "TMP": str(runtime)}
            launch = scientific_sandbox_launch([sys.executable, "-I", "-c", script], work_dir=root,
                write_roots=[output, runtime], env=env)
            result = subprocess.run(launch["command"], cwd=root, env=launch["env"], capture_output=True,
                                    text=True, timeout=40)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            observed = json.loads(result.stdout.strip().splitlines()[-1])
            self.assertEqual(observed, {"python_output": True, "native_output": True,
                "python_source": False, "native_source": False, "python_audit": False, "native_audit": False}, result.stdout + result.stderr)
            self.assertEqual(frozen.read_text(), "original source")
            self.assertEqual((audit / "receipt.json").read_text(), "original receipt")
            self.assertFalse(launch["policy"]["native_read_isolation"])


if __name__ == "__main__":
    unittest.main()
