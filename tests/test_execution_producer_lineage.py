"""Persistent scientific state must retain a current, complete producer chain."""
from __future__ import annotations

from pathlib import Path
import sys

import pytest

from geng_agent.execution_receipts import (
    ExecutionBroker, _observed_runtime_distributions, artifact_hashes, file_hash, find_host_execution, validate_receipt,
)
from geng_agent.outputs import write_json
from tests.test_execution_sandbox import native_sandbox_temporary_directory


@pytest.fixture
def producer_workspace():
    with native_sandbox_temporary_directory() as temporary:
        yield Path(temporary)


def _project(root: Path) -> tuple[Path, ExecutionBroker]:
    project = root / "project"
    (project / "tasks").mkdir(parents=True)
    (project / "tasks/__init__.py").write_text("", encoding="utf-8")
    (project / "tasks/train.py").write_text(
        "from pathlib import Path\nVALUE = 1\ndef main(config):\n"
        "    Path('execution_units/model.txt').write_text(str(VALUE))\n"
        "    Path('outputs/train/loss.csv').write_text('loss\\n0.1\\n')\n", encoding="utf-8")
    (project / "tasks/features.py").write_text(
        "from pathlib import Path\ndef main(config):\n"
        "    weight = int(Path('execution_units/model.txt').read_text())\n"
        "    Path('execution_units/features.txt').write_text(str(weight * 3))\n"
        "    Path('outputs/features/summary.csv').write_text(str(weight * 3))\n", encoding="utf-8")
    (project / "tasks/evaluate.py").write_text(
        "from pathlib import Path\ndef main(config):\n"
        "    value = Path('execution_units/features.txt').read_text()\n"
        "    Path('outputs/evaluate/result.csv').write_text(value)\n", encoding="utf-8")
    (project / "tasks/independent.py").write_text(
        "from pathlib import Path\ndef main(config):\n"
        "    Path('outputs/independent/result.csv').write_text('unrelated result')\n", encoding="utf-8")
    write_json(project / "config.json", {"run_profile": "full"})
    write_json(project / "tasks_manifest.json", {"tasks": [
        {"task_id": task, "module": task, "output_subdir": task, "config_full": "config.json"}
        for task in ("train", "features", "evaluate", "independent")
    ]})
    return project, ExecutionBroker(project, root / "audit", Path(sys.executable))


def test_cached_features_cannot_outlive_their_upstream_training_recipe(producer_workspace: Path) -> None:
    project, broker = _project(producer_workspace)
    for task, consumed in (("train", []), ("features", ["execution_units/model.txt"]),
                           ("evaluate", ["execution_units/features.txt"]), ("independent", [])):
        receipt = broker.execute({"task_id": task, "mode": "full", "inputs": consumed})
        assert validate_receipt(project, receipt, task_id=task)["passed"], receipt
    assert find_host_execution(project, broker.audit_dir, "evaluate")["passed"]
    features = project / "execution_units/features.txt"
    original_features = features.read_bytes()
    trainer = project / "tasks/train.py"
    trainer.write_text(trainer.read_text().replace("VALUE = 1", "VALUE = 2"), encoding="utf-8")
    # All immediate feature inputs are byte-identical; only the grandparent
    # training recipe changed. Reusing this derived state must still fail.
    cached_evaluation = find_host_execution(project, broker.audit_dir, "evaluate")
    assert cached_evaluation["passed"] is False
    assert any("no current producer receipt" in issue for issue in cached_evaluation["issues"])
    assert find_host_execution(project, broker.audit_dir, "independent")["passed"] is True
    with pytest.raises(ValueError, match="no current producer receipt"):
        broker.execute({"task_id": "evaluate", "mode": "full", "inputs": ["execution_units/features.txt"]})
    assert features.read_bytes() == original_features


def test_full_consumer_cannot_use_a_smoke_producer_checkpoint(producer_workspace: Path) -> None:
    _project_dir, broker = _project(producer_workspace)
    smoke = broker.execute({"task_id": "train", "mode": "smoke"})
    assert smoke["returncode"] == 0
    with pytest.raises(ValueError, match="no current producer receipt"):
        broker.execute({"task_id": "features", "mode": "full", "inputs": ["execution_units/model.txt"]})
    for task, consumed in (("features", "execution_units/model.txt"), ("evaluate", "execution_units/features.txt")):
        downstream_smoke = broker.execute({"task_id": task, "mode": "smoke", "inputs": [consumed]})
        assert downstream_smoke["returncode"] == 0, downstream_smoke.get("stderr_tail")
        assert downstream_smoke["dependency_issues"] == []
    with pytest.raises(ValueError, match="no current producer receipt"):
        broker.execute({"task_id": "evaluate", "mode": "full", "inputs": ["execution_units/features.txt"]})


def test_cyclic_producer_claims_have_no_grounded_origin(tmp_path: Path) -> None:
    project, broker = _project(tmp_path)
    (project / "execution_units").mkdir()
    for name in ("a", "b"):
        (project / f"execution_units/{name}.txt").write_text(name, encoding="utf-8")
    hashes = {f"execution_units/{name}.txt": file_hash(project / f"execution_units/{name}.txt") for name in ("a", "b")}
    for index, (own, upstream) in enumerate((("a", "b"), ("b", "a")), 1):
        write_json(broker.audit_dir / "execution_runs" / own / "execution_receipt.json", {
            "observer": "orchestration_host", "mode": "full", "returncode": 0,
            "inputs_stable": True, "finished_at": index,
            "produced_artifacts": {f"execution_units/{own}.txt": hashes[f"execution_units/{own}.txt"]},
            "input_hashes": {f"execution_units/{upstream}.txt": hashes[f"execution_units/{upstream}.txt"]},
            "source_hashes": {},
            "environment_observation": {"before": {"inventory": {"packages": []}}},
            "observed_import_roots": [],
            "observed_distributions": {},
        })
    assert broker._check_producers({"execution_units/a.txt": hashes["execution_units/a.txt"]})


@pytest.mark.parametrize("python_location", ["python.exe", "Scripts/python.exe", "bin/python"])
def test_producer_environment_change_invalidates_only_consumed_packages_and_chains(tmp_path: Path, python_location: str) -> None:
    project, broker = _project(tmp_path)
    prefix = tmp_path / "selected_runtime"
    broker.python = prefix / python_location

    def distribution(name: str, version: str, imported: str) -> None:
        directory = prefix / "Lib/site-packages" / (name.replace("-", "_") + ".dist-info")
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n", encoding="utf-8")
        (directory / "top_level.txt").write_text(imported + "\n", encoding="utf-8")

    distribution("scikit-commpy", "1.0", "commpy")
    distribution("unrelated-package", "1.0", "unrelated")
    state = project / "execution_units/model.txt"
    state.parent.mkdir()
    state.write_text("trained state", encoding="utf-8")
    common = {"observer": "orchestration_host", "mode": "full", "returncode": 0,
              "inputs_stable": True, "python_executable": str(broker.python),
              "environment_observation": {"before": {"inventory": {"packages": [
                  ["scikit-commpy", "1.0"], ["unrelated-package", "1.0"]]}}}}
    state_hash = file_hash(state)
    observed = _observed_runtime_distributions(broker.python, ["commpy"], common["environment_observation"]["before"]["inventory"])
    assert observed == {"scikit-commpy": "1.0"}
    producer = {**common, "task_id": "train", "finished_at": 1, "observed_import_roots": ["commpy"],
                "observed_distributions": observed,
                "source_hashes": {"tasks/train.py": file_hash(project / "tasks/train.py")}, "input_hashes": {},
                "produced_artifacts": {"execution_units/model.txt": state_hash}}
    write_json(broker.audit_dir / "execution_runs/train/execution_receipt.json", producer)
    for task, inputs in (("evaluate", {"execution_units/model.txt": state_hash}), ("independent", {})):
        output = project / "outputs" / task / "result.csv"
        output.parent.mkdir(parents=True)
        output.write_text("metric\n1\n", encoding="utf-8")
        write_json(broker.audit_dir / "execution_runs" / task / "execution_receipt.json", {
            **common, "task_id": task, "finished_at": 2, "observed_import_roots": [],
            "source_hashes": {f"tasks/{task}.py": file_hash(project / f"tasks/{task}.py")},
            "input_hashes": inputs, "output_hashes": artifact_hashes(project, task),
        })
    consumed = {"execution_units/model.txt": state_hash}
    assert broker._check_producers(consumed) == []
    assert find_host_execution(project, broker.audit_dir, "evaluate")["passed"]
    distribution("unrelated-package", "2.0", "unrelated")
    assert broker._check_producers(consumed) == []
    assert find_host_execution(project, broker.audit_dir, "evaluate")["passed"]
    # Reading metadata anew on each call catches an upgrade without a new
    # execution or lock rewrite; the unrelated task remains reusable.
    distribution("scikit-commpy", "2.0", "commpy")
    assert broker._check_producers(consumed)
    assert find_host_execution(project, broker.audit_dir, "evaluate")["passed"] is False
    assert find_host_execution(project, broker.audit_dir, "independent")["passed"] is True
    distribution("scikit-commpy", "1.0", "commpy")
    assert find_host_execution(project, broker.audit_dir, "evaluate")["passed"] is True
    alias_metadata = prefix / "Lib/site-packages/scikit_commpy.dist-info"
    (alias_metadata / "METADATA").unlink()
    (alias_metadata / "top_level.txt").unlink()
    assert broker._check_producers(consumed)
    assert find_host_execution(project, broker.audit_dir, "evaluate")["passed"] is False
    assert find_host_execution(project, broker.audit_dir, "independent")["passed"] is True
