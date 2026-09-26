"""Offline delivery checks for per-task Writer packages."""

import json
import subprocess
import sys
from pathlib import Path
from uuid import uuid4
from zipfile import ZipFile

import pytest

from geng_agent.agentic_report_editor import _project_delivery_materials
from geng_agent.outputs import validate_repro_project
from geng_agent.schemas import validate_stage
from geng_agent.task_writer_packaging import _package_task_directories
from geng_agent.project_portability import validate_repro_project_portability
from geng_agent.task_writer_support import _load_cached_task_writer_workflow
from geng_agent.web.delivery import REPORTS, build_delivery


def _assert_task_readme_lists_files(archive: ZipFile, prefix: str) -> None:
    readme = archive.read(prefix + "/readme.md").decode("utf-8")
    for info in archive.infolist():
        if (info.is_dir() or not info.filename.startswith(prefix + "/")
                or info.filename == prefix + "/readme.md"):
            continue
        relative = info.filename[len(prefix) + 1:]
        assert relative in readme, f"Unexplained delivery file: {relative}"


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
            "case_runtime": None,
            "analysis_snapshot_hash": "analysis",
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


def test_final_zip_preserves_each_tasks_result_bytes_and_locations(tmp_path):
    inputs = _unit_inputs(tmp_path)
    original_results = {}
    for record in inputs["task_records"]:
        task_id = record["task_id"]
        sandbox = Path(record["sandbox"])
        # These are opaque copy fixtures, not valid scientific PNG/NPZ data.
        # Identical names contain distinct bytes to detect cross-task mixing.
        result_files = {
            "outputs/results.csv": f"task,value\n{task_id},0.125\n".encode(),
            "outputs/figures/curve.png": b"\x89PNG\r\n" + task_id.encode(),
            f"outputs/{task_id}/metrics.json": json.dumps(
                {"task_id": task_id, "ber": 0.125}).encode(),
            f"outputs/{task_id}/arrays.npz": b"PK\x03\x04\x00\xff" + task_id.encode(),
            f"outputs/{task_id}/paper_target_comparison.csv": (
                f"task,reference,local\n{task_id},0.12,0.125\n".encode()),
            f"outputs/figures/{task_id}_only.svg": f"<svg>{task_id}</svg>".encode(),
            "results/parameter_sweep.csv": f"task,parameter\n{task_id},4\n".encode(),
        }
        for relative, contents in result_files.items():
            path = sandbox / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(contents)
        original_results[task_id] = result_files

    _package_task_directories(**inputs)
    project = inputs["repro_project_dir"]
    index = json.loads((project / "package_index.json").read_text(encoding="utf-8"))
    readme = (project / "README.md").read_text(encoding="utf-8")
    reports = {}
    for source, target in REPORTS.items():
        reports[target] = f"report fixture: {source}".encode()
        (inputs["output_dir"] / source).write_bytes(reports[target])

    delivery = build_delivery(inputs["output_dir"], str(uuid4()))
    with ZipFile(delivery) as archive:
        names = set(archive.namelist())
        for name, contents in reports.items():
            assert archive.read(name) == contents
        for entry in index["tasks"]:
            task_id = entry["task_id"]
            directory = entry["directory"]
            delivered_task = f"复现交付包/复现任务/{Path(directory).name}"
            output_directory = f"{directory}/outputs"
            assert entry["output_directory"] == output_directory
            assert (f"]({output_directory})" in readme
                    or f"]({output_directory}/)" in readme)
            for relative, original_bytes in original_results[task_id].items():
                assert (project / directory / relative).read_bytes() == original_bytes
                assert archive.read(f"{delivered_task}/复现结果/{relative}") == original_bytes
            for relative in ("src/common/shared.py", f"tasks/{task_id}.py", "config.json",
                             "config_smoke.json", "requirements.txt", "run_experiment.py"):
                assert archive.read(f"{delivered_task}/代码/{relative}") == (
                    project / directory / relative).read_bytes()
            _assert_task_readme_lists_files(archive, delivered_task)
            other_id = "t2" if task_id == "t1" else "t1"
            assert not any(name.startswith(f"{delivered_task}/复现结果/outputs/{other_id}/")
                           for name in names)
            assert f"{delivered_task}/复现结果/outputs/figures/{other_id}_only.svg" not in names
        assert all(name.startswith("复现交付包/") for name in names)
        assert not any(part in {"audit", "__pycache__", "execution_records", "task_notes",
                                "task_agent_result.json", "execution_evidence.json",
                                "source_inventory.json", "project_manifest.json"}
                       for name in names for part in name.split("/"))


def test_missing_task_results_do_not_block_code_delivery(tmp_path):
    inputs = _unit_inputs(tmp_path)
    sandbox = Path(inputs["task_records"][0]["sandbox"])
    remaining_result = (Path(inputs["task_records"][1]["sandbox"])
                        / "outputs/t2/result.csv").read_bytes()
    (sandbox / "outputs/t1/result.csv").unlink()
    (sandbox / "outputs/t1").rmdir()
    (sandbox / "outputs").rmdir()

    _package_task_directories(**inputs)
    project = inputs["repro_project_dir"]
    for source in REPORTS:
        (inputs["output_dir"] / source).write_bytes(b"report fixture")
    delivery = build_delivery(inputs["output_dir"], str(uuid4()))

    with ZipFile(delivery) as archive:
        names = set(archive.namelist())
        prefix = "复现交付包/复现任务"
        assert f"{prefix}/t01_t1/代码/tasks/t1.py" in names
        assert f"{prefix}/t01_t1/复现结果/outputs/t1/result.csv" not in names
        assert archive.read(f"{prefix}/t02_t2/复现结果/outputs/t2/result.csv") == remaining_result
        _assert_task_readme_lists_files(archive, prefix + "/t01_t1")
    assert (project / "task_packages/t01_t1/tasks/t1.py").is_file()


def test_writer_authored_readme_survives_internal_packaging_and_final_zip(tmp_path):
    inputs = _unit_inputs(tmp_path)
    guides = {}
    for record in inputs["task_records"]:
        task_id = record["task_id"]
        guide = (
            f"# {task_id} 文件导读\r\n\r\n"
            f"`代码/tasks/{task_id}.py`：读取公共模块的固定值，作为本测试任务的输出。\r\n\r\n"
            f"`复现结果/outputs/{task_id}/result.csv`：保存该任务输出的数值，便于核对代码传出的结果。\r\n"
        ).encode("utf-8")
        (Path(record["sandbox"]) / "delivery_readme.md").write_bytes(guide)
        guides[task_id] = guide
    _package_task_directories(**inputs)
    for name in REPORTS:
        (inputs["output_dir"] / name).write_bytes(b"report fixture")
    with ZipFile(build_delivery(inputs["output_dir"], str(uuid4()))) as archive:
        assert not any(name.endswith("delivery_readme.md") for name in archive.namelist())
        for index, task_id in enumerate(guides, start=1):
            assert archive.read(f"复现交付包/复现任务/t{index:02d}_{task_id}/readme.md") == guides[task_id]


def test_extracted_task_runs_with_original_input_paths_without_internal_archives(tmp_path):
    case = tmp_path / "case"
    project = case / "repro_project"
    task = project / "task_packages" / "t07_OriginalTask"
    original_result = b'{"value": 6}\n'
    files = {
        "run.py": (
            "import json\nfrom pathlib import Path\nfrom src.compute import total\n"
            "config = json.loads(Path('configs/full.json').read_text())\n"
            "value = total(Path(config['input']).read_text())\n"
            "destination = Path(config['output'])\n"
            "destination.parent.mkdir(parents=True, exist_ok=True)\n"
            "destination.write_text(json.dumps({'value': value}) + '\\n')\n"
        ).encode(),
        "src/compute.py": b"def total(text):\n    return sum(map(int, text.split(',')))\n",
        "configs/full.json": b'{"input": "upstream/samples.csv", "output": "outputs/calculated.json"}',
        "requirements.txt": b"# Standard library only\n",
        "execution_records/T7/input_snapshot/samples.csv": b"1,2,3",
        "outputs/calculated.json": original_result,
        "outputs/T7/task_agent_result.json": json.dumps({
            "task_id": "T7", "delivery_files": [{"path": "outputs/calculated.json",
                "role": "result", "description": "Three input values summed by the task."}],
        }).encode(),
        "execution_evidence.json": json.dumps({"tasks": [{"task_id": "T7", "files": [{
            "kind": "input_hashes", "original_path": "upstream/samples.csv",
            "packaged_path": "execution_records/T7/input_snapshot/samples.csv",
        }]}]}).encode(),
    }
    for relative, content in files.items():
        path = task / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    (project / "package_index.json").write_text(json.dumps({"tasks": [{
        "task_id": "T7", "directory": "task_packages/t07_OriginalTask",
    }]}), encoding="utf-8")
    for source in REPORTS:
        (case / source).write_bytes(b"offline report fixture")

    destination = tmp_path / "relocated"
    prefix = "复现交付包/复现任务/t07_OriginalTask"
    with ZipFile(build_delivery(case, str(uuid4()))) as archive:
        names = set(archive.namelist())
        assert archive.read(prefix + "/代码/upstream/samples.csv") == b"1,2,3"
        assert archive.read(prefix + "/复现结果/outputs/calculated.json") == original_result
        assert not any("execution_records" in name or "execution_evidence" in name
                       or "task_agent_result" in name for name in names)
        _assert_task_readme_lists_files(archive, prefix)
        assert "Three input values summed by the task." in archive.read(
            prefix + "/readme.md").decode("utf-8")
        archive.extractall(destination)

    # This is a three-number standard-library fixture, never a model or experiment.
    code = destination / prefix / "代码"
    completed = subprocess.run([sys.executable, "-B", "run.py"], cwd=code,
                               capture_output=True, text=True, timeout=10)
    assert completed.returncode == 0, completed.stderr
    assert json.loads((code / "outputs/calculated.json").read_text()) == {"value": 6}
    assert (destination / prefix / "复现结果/outputs/calculated.json").read_bytes() == original_result
    assert (task / "outputs/calculated.json").read_bytes() == original_result
