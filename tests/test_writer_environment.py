from __future__ import annotations

import os
import json
import subprocess
import sys
from pathlib import Path

from geng_agent.case_runtime_contracts import CaseRuntime
from geng_agent.writer_environment import (
    cleanup_completed_writer_environments,
    ensure_writer_environment,
    snapshot_writer_environment,
)
from geng_agent.execution_receipts import ExecutionBroker, probe_execution_environment
from tests.test_execution_sandbox import native_sandbox_temporary_directory


def _python(root: Path) -> Path:
    return root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _site(root: Path) -> Path:
    return root / "Lib/site-packages" if os.name == "nt" else next((root / "lib").glob("python*/site-packages"))


def _value(python: Path) -> str:
    result = subprocess.run(
        [str(python), "-I", "-c", "import shared_science_probe; print(shared_science_probe.VALUE)"],
        check=True, capture_output=True, text=True,
    )
    return result.stdout.strip()


def test_writers_reuse_base_packages_but_keep_private_install_paths() -> None:
    with native_sandbox_temporary_directory() as temporary:
        _check_writer_environments(Path(temporary))


def _check_writer_environments(tmp_path: Path) -> None:
    base = tmp_path / "base"
    subprocess.run([sys.executable, "-m", "venv", str(base)], check=True)
    (_site(base) / "shared_science_probe.py").write_text("VALUE = 'base'\n", encoding="utf-8")
    base_python = _python(base)
    runtime = CaseRuntime(
        venv_dir=base, python_executable=base_python,
        request_path=tmp_path / "request.json", lock_path=tmp_path / "lock.json",
        report_path=tmp_path / "report.json", environment_hash="base-v1",
        manifest={}, lock={}, report={}, trusted_read_roots=(base,),
    )
    first = tmp_path / "writer_1"
    second = tmp_path / "writer_2"
    first.mkdir()
    second.mkdir()
    first_python = ensure_writer_environment(first, runtime)
    second_python = ensure_writer_environment(second, runtime)
    assert _value(first_python) == "base"
    assert _value(second_python) == "base"
    (_site(first_python.parent.parent) / "shared_science_probe.py").write_text(
        "VALUE = 'writer_1'\n", encoding="utf-8",
    )
    assert _value(first_python) == "writer_1"
    assert _value(second_python) == "base"
    assert _value(base_python) == "base"
    subprocess.run([str(first_python), "-m", "pip", "--version"], check=True, capture_output=True)

    tasks = first / "tasks"
    tasks.mkdir()
    (tasks / "__init__.py").write_text("", encoding="utf-8")
    (tasks / "sample.py").write_text(
        "from pathlib import Path\nimport shared_science_probe\n"
        "def main(config):\n"
        "    Path('outputs/sample/result.csv').write_text(shared_science_probe.VALUE)\n",
        encoding="utf-8",
    )
    (first / "config.json").write_text('{"run_profile":"full"}', encoding="utf-8")
    (first / "tasks_manifest.json").write_text(json.dumps({
        "tasks": [{"task_id": "sample", "module": "sample", "output_subdir": "sample",
                   "config_full": "config.json"}],
    }), encoding="utf-8")
    inventory = probe_execution_environment(base_python)["inventory"]["packages"]
    broker = ExecutionBroker(
        first, tmp_path / "audit", first_python, shared_runtime_python=base_python,
        expected_installed_distributions=[{"distribution": name, "version": version}
                                          for name, version in inventory],
    )
    receipt = broker.execute({"task_id": "sample", "mode": "full"})
    assert receipt["returncode"] == 0
    assert (first / "outputs/sample/result.csv").read_text(encoding="utf-8") == "writer_1"
    snapshot_writer_environment(first, runtime)
    snapshot_writer_environment(second, runtime)
    cleanup = cleanup_completed_writer_environments(
        output_dir=tmp_path, task_records=[{"sandbox": str(first)}, {"sandbox": str(second)}],
    )
    assert len(cleanup["removed"]) == 2, cleanup
    assert not first_python.exists()
    assert _value(ensure_writer_environment(first, runtime)) == "base"
    cleanup_completed_writer_environments(
        output_dir=tmp_path, task_records=[{"sandbox": str(first)}],
    )
