"""Offline delivery checks for per-task Writer packages."""

import json
from pathlib import Path

import pytest

from geng_agent.agentic_report_editor import _project_delivery_materials
from geng_agent.outputs import validate_repro_project
from geng_agent.schemas import validate_stage
from geng_agent.task_writer_packaging import _package_task_directories
from geng_agent.project_portability import validate_repro_project_portability
from geng_agent.task_writer_support import _load_cached_task_writer_workflow


def _unit_inputs(root: Path):
    output = root / "case"
    audit = output / "audit"
    records = []
    entries = []
    units = []
    for index, value in enumerate((1, 2), start=1):
        task_id = f"t{index}"
        unit_id = f"unit_{index}"
        sandbox = audit / "03c_task_writer_sandboxes" / task_id
        (sandbox / "src" / "common").mkdir(parents=True)
        (sandbox / "src" / "common" / "shared.py").write_text(
            f"VALUE = {value}\n", encoding="utf-8")
        (sandbox / "tasks").mkdir()
        (sandbox / "tasks" / f"{task_id}.py").write_text(
            f"from src.common.shared import VALUE\nRESULT = VALUE\n", encoding="utf-8")
        (sandbox / "outputs" / task_id).mkdir(parents=True)
        (sandbox / "outputs" / task_id / "result.csv").write_text(
            f"value\n{value}\n", encoding="utf-8")
        (sandbox / "requirements.txt").write_text("numpy\n", encoding="utf-8")
        (sandbox / "config.json").write_text("{}\n", encoding="utf-8")
        (sandbox / "config_smoke.json").write_text('{"smoke": true}\n', encoding="utf-8")
        records.append({"task_id": task_id, "module": task_id,
                        "output_subdir": task_id, "execution_unit_id": unit_id,
                        "sandbox": str(sandbox), "writer_completed": True})
        entries.append({"task_id": task_id, "module": task_id,
                        "script": f"tasks/{task_id}.py", "output_subdir": task_id,
                        "config_full": f"configs/{task_id}_config.json",
                        "config_smoke": f"configs/{task_id}_config_smoke.json"})
        units.append({"unit_id": unit_id, "task_ids": [task_id], "mode": "singleton"})
    return {"repro_project_dir": output / "repro_project", "output_dir": output,
            "audit_dir": audit, "task_manifest": {"version": 1, "tasks": entries},
            "task_records": records, "execution_plan": {"execution_units": units},
            "foundation": None, "case_runtime": None,
            "analysis_snapshot_hash": "analysis", "foundation_snapshot_hash": "",
            "environment_hash": "", "require_lineage": False}


def test_independent_units_keep_conflicting_executed_source_and_outputs(tmp_path):
    inputs = _unit_inputs(tmp_path)
    paths, manifest, portability = _package_task_directories(**inputs)
    project = inputs["repro_project_dir"]

    assert portability["portable"] is True
    assert manifest["_meta"]["package_layout"] == "task_directories"
    assert not validate_stage("repro_project_manifest", manifest)
    assert "task_packages/t01_t1/src/common/shared.py" in paths
    assert "task_packages/t02_t2/src/common/shared.py" in paths
    assert not (project / "src" / "common" / "shared.py").exists()
    assert (project / "task_packages/t01_t1/src/common/shared.py").read_text() == "VALUE = 1\n"
    assert (project / "task_packages/t02_t2/src/common/shared.py").read_text() == "VALUE = 2\n"
    assert (project / "task_packages/t01_t1/outputs/t1/result.csv").read_text() == "value\n1\n"
    assert (project / "task_packages/t02_t2/outputs/t2/result.csv").read_text() == "value\n2\n"
    assert (project / "task_packages/t01_t1/run_experiment.py").is_file()
    assert (project / "task_packages/t02_t2/run_experiment.py").is_file()
    assert validate_repro_project_portability(project, run_smoke=False)["portable"] is True
    index = json.loads((project / "package_index.json").read_text(encoding="utf-8"))
    assert [task["directory"] for task in index["tasks"]] == ["task_packages/t01_t1", "task_packages/t02_t2"]
    materials = _project_delivery_materials(project)
    assert [task["task_id"] for task in materials["task_packages"]] == ["t1", "t2"]
    assert _project_delivery_materials(
        project, current_run_package_completed=False)["current_run_package_status"] == "failed"
    assert all(next(item for item in task["files"]
                    if item["path"].endswith("/run_experiment.py"))["status"] == "present"
               for task in materials["task_packages"])
    cached = _load_cached_task_writer_workflow(
        output_dir=inputs["output_dir"], repro_project_dir=project,
        run_repro=False, analysis_snapshot_hash="analysis")
    assert cached is not None
    assert len(cached["written_files"]) > len(manifest["files"])
    validation = validate_repro_project(project)
    assert validation["required_files_present"] is True, validation["missing_files"]


def test_failed_unit_packaging_preserves_previous_project(tmp_path):
    inputs = _unit_inputs(tmp_path)
    project = inputs["repro_project_dir"]
    project.mkdir(parents=True)
    (project / "README.md").write_text("previous delivery", encoding="utf-8")
    inputs["task_records"][1]["sandbox"] = str(tmp_path / "absent")

    with pytest.raises(ValueError, match="sandbox unavailable"):
        _package_task_directories(**inputs)

    assert (project / "README.md").read_text(encoding="utf-8") == "previous delivery"


def test_successful_replacement_archives_previous_project(tmp_path):
    inputs = _unit_inputs(tmp_path)
    project = inputs["repro_project_dir"]
    project.mkdir(parents=True)
    (project / "README.md").write_text("previous delivery", encoding="utf-8")

    _package_task_directories(**inputs)

    assert "按任务交付" in (project / "README.md").read_text(encoding="utf-8")
    audit = json.loads((inputs["audit_dir"] / "03c_task_package_delivery.json").read_text(
        encoding="utf-8"))
    previous = Path(audit["previous_project"])
    assert (previous / "README.md").read_text(encoding="utf-8") == "previous delivery"


def test_strongly_coupled_tasks_each_receive_the_complete_executed_unit(tmp_path):
    inputs = _unit_inputs(tmp_path)
    joint = inputs["task_records"][0]["sandbox"]
    inputs["task_records"][1]["sandbox"] = joint
    for record in inputs["task_records"]:
        record["execution_unit_id"] = "joint"
    inputs["execution_plan"]["execution_units"] = [
        {"unit_id": "joint", "task_ids": ["t1", "t2"], "mode": "joint"}]
    sandbox = Path(joint)
    (sandbox / "tasks/t2.py").write_text("RESULT = 2\n", encoding="utf-8")
    (sandbox / "outputs/t2").mkdir()
    (sandbox / "outputs/t2/result.csv").write_text("value\n2\n", encoding="utf-8")
    (sandbox / "configs").mkdir()
    (sandbox / "configs/t2_config.json").write_text("{}\n", encoding="utf-8")
    (sandbox / "configs/t2_config_smoke.json").write_text(
        '{"smoke": true}\n', encoding="utf-8")

    _package_task_directories(**inputs)
    project = inputs["repro_project_dir"]
    for directory in ("task_packages/t01_t1", "task_packages/t02_t2"):
        assert (project / directory / "tasks/t1.py").is_file()
        assert (project / directory / "tasks/t2.py").is_file()
        assert (project / directory / "outputs/t1/result.csv").is_file()
        assert (project / directory / "outputs/t2/result.csv").is_file()
        assert validate_repro_project(project / directory)["required_files_present"] is True
    index = json.loads((project / "package_index.json").read_text(encoding="utf-8"))
    assert [task["execution_unit_id"] for task in index["tasks"]] == ["joint", "joint"]


def test_foundation_source_is_copied_into_each_task_folder(tmp_path, monkeypatch):
    inputs = _unit_inputs(tmp_path)
    inputs["foundation"] = {"snapshot_dir": "fixture"}
    inputs["foundation_snapshot_hash"] = "frozen-definition"

    def install(target, _foundation):
        source = target / "src/common/shared.py"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("VALUE = 7\n", encoding="utf-8")
        (target / "foundation_manifest.json").write_text(
            '{"frozen_files": [{"path": "src/common/shared.py"}]}', encoding="utf-8")
        return {"src/common/shared.py", "foundation_manifest.json"}

    monkeypatch.setattr("geng_agent.task_writer_packaging.install_foundation_snapshot", install)
    _package_task_directories(**inputs)
    project = inputs["repro_project_dir"]
    for directory in ("task_packages/t01_t1", "task_packages/t02_t2"):
        assert (project / directory / "src/common/shared.py").read_text() == "VALUE = 7\n"
        assert (project / directory / "foundation_manifest.json").is_file()
