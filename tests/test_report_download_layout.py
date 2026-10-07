"""Offline checks that report navigation describes the actual download ZIP.

These fixtures contain tiny opaque report bytes and fixed text files. They do
not run an agent, install dependencies, or rerun any scientific experiment.
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from uuid import uuid4
from zipfile import ZipFile

import pytest

from geng_agent.agentic_report_editor import (
    _editor_input_hash,
    _project_delivery_materials,
)
from geng_agent.final_delivery import (
    DELIVERY_ROOT,
    REPORTS,
    describe_download_layout,
    task_delivery_directories,
)
from geng_agent.web.delivery import build_delivery


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _reports(case: Path) -> None:
    for source in REPORTS:
        (case / source).write_bytes(b"opaque offline Word fixture")


def _project(case: Path, *, directories=("t01", "t02"), results=True):
    project = case / "repro_project"
    entries = []
    for number, directory in enumerate(directories, start=1):
        task_id = f"T{number}_ScientificClaim"
        task = project / "task_packages" / directory
        (task / "src").mkdir(parents=True)
        (task / "src" / "channel.py").write_text(
            f"CHANNEL_VALUE = {number}\n", encoding="utf-8")
        (task / "run_experiment.py").write_text(
            "# Offline fixture; never executed by this suite.\n", encoding="utf-8")
        (task / "config.json").write_text("{}\n", encoding="utf-8")
        if results:
            (task / "outputs" / task_id).mkdir(parents=True)
            (task / "outputs" / task_id / "ber.csv").write_text(
                f"task,ber\n{task_id},0.125\n", encoding="utf-8")
        entries.append({
            "task_id": task_id,
            "directory": f"task_packages/{directory}",
            "full_command": ["python", "run_experiment.py", "config.json"],
            "smoke_command": ["python", "run_experiment.py", "config_smoke.json"],
        })
    manifest = {"layout": "task_directories", "task_packages": entries}
    _write_json(project / "package_index.json", {"tasks": entries})
    _write_json(project / "reproducibility_manifest.json", manifest)
    (project / "README.md").write_text(
        "# Internal project\nUse task_packages and internal execution records.\n",
        encoding="utf-8")
    return project, manifest


def _assert_zip_paths(case: Path, material: dict) -> None:
    _reports(case)
    with ZipFile(build_delivery(case, str(uuid4()))) as archive:
        names = set(archive.namelist())
        assert all(name.startswith(material["root"] + "/") for name in names)
        assert {Path(value).name for value in REPORTS.values()} == set(material["reports"])
        for entry in material["tasks"]:
            assert entry["readme_path"] in names
            assert entry["code_directory"] + "/" in names
            assert entry["results_directory"] + "/" in names
            assert entry["download_directory"] + "/readme.md" == entry["readme_path"]
            assert any(name.startswith(entry["code_directory"] + "/")
                       and not name.endswith("/") for name in names)


def test_actual_task_directories_and_report_material_share_zip_navigation(tmp_path):
    case = tmp_path / "case"
    project, manifest = _project(case)
    layout = describe_download_layout(project, package_manifest=manifest)
    material = _project_delivery_materials(project)["website_download"]

    assert material == layout
    assert material["root"] == DELIVERY_ROOT
    assert [entry["task_id"] for entry in material["tasks"]] == [
        "T1_ScientificClaim", "T2_ScientificClaim"]
    assert [entry["internal_directory"] for entry in material["tasks"]] == [
        "task_packages/t01", "task_packages/t02"]
    for directory, entry in zip(("t01", "t02"), material["tasks"]):
        assert entry["download_directory"] == f"{DELIVERY_ROOT}/复现任务/{directory}"
        assert entry["code_directory"] == f"{DELIVERY_ROOT}/复现任务/{directory}/代码"
        assert entry["results_directory"] == f"{DELIVERY_ROOT}/复现任务/{directory}/复现结果"
    _assert_zip_paths(case, material)


def test_nonstandard_task_directory_keeps_its_actual_name(tmp_path):
    case = tmp_path / "case"
    project, _ = _project(case, directories=("t07_OriginalTask",))
    material = _project_delivery_materials(project)["website_download"]
    entry = material["tasks"][0]

    assert entry["task_id"] == "T1_ScientificClaim"
    assert entry["internal_directory"] == "task_packages/t07_OriginalTask"
    assert entry["code_directory"] == f"{DELIVERY_ROOT}/复现任务/t07_OriginalTask/代码"
    _assert_zip_paths(case, material)


@pytest.mark.parametrize("index_content", [None, "{not-json", "[]", '{"tasks": []}'])
def test_optional_index_does_not_hide_existing_task_directories(tmp_path, index_content):
    case = tmp_path / "case"
    project, _ = _project(case, directories=("t07_OriginalTask", "custom_experiment"))
    index = project / "package_index.json"
    if index_content is None:
        index.unlink()
    else:
        index.write_text(index_content, encoding="utf-8")

    actual = task_delivery_directories(project)
    layout = _project_delivery_materials(project)["website_download"]
    assert [directory for directory, _, _ in actual] == ["custom_experiment", "t07_OriginalTask"]
    assert [entry["internal_directory"] for entry in layout["tasks"]] == [
        "task_packages/custom_experiment", "task_packages/t07_OriginalTask"]
    _assert_zip_paths(case, layout)


def test_legacy_single_project_retains_original_command_and_download_fallback(tmp_path):
    case = tmp_path / "case"
    project = case / "repro_project"
    project.mkdir(parents=True)
    (project / "run_experiment.py").write_text("# legacy fixture\n", encoding="utf-8")
    command = ["python", "run_experiment.py", "--mode", "full", "configs/custom.json"]
    _write_json(project / "reproducibility_manifest.json", {"full_command": command})

    material = _project_delivery_materials(project)["website_download"]
    assert len(material["tasks"]) == 1
    entry = material["tasks"][0]
    assert entry["internal_directory"] == "."
    assert entry["download_directory"] == f"{DELIVERY_ROOT}/复现任务/t01_task"
    assert entry["recorded_commands"]["full_command"] == command
    _assert_zip_paths(case, material)


def test_missing_project_does_not_advertise_a_downloadable_task(tmp_path):
    project = tmp_path / "case" / "repro_project"
    material = _project_delivery_materials(project)

    assert not material["project_present"]
    assert material["website_download"]["tasks"] == []
    assert describe_download_layout(project)["tasks"] == []


def test_failed_current_packaging_keeps_old_paths_without_claiming_current_success(tmp_path):
    case = tmp_path / "case"
    project, _ = _project(case, directories=("old_saved_task",))
    previous = (project / "src_inventory_marker.txt")
    previous.write_bytes(b"previous case retained")

    material = _project_delivery_materials(project, current_run_package_completed=False)
    assert material["current_run_package_status"] == "failed"
    assert material["project_present"]
    assert material["website_download"]["tasks"][0]["internal_directory"] == (
        "task_packages/old_saved_task")
    assert previous.read_bytes() == b"previous case retained"


def test_directory_rename_changes_editor_identity_even_with_stale_manifest(tmp_path):
    case = tmp_path / "case"
    project, _ = _project(case, directories=("t01",))

    def identity():
        return _editor_input_hash(output_dir=case, paper={}, task_packets=[],
                                  runtime_result={}, report_materials={
                                      "project_delivery": _project_delivery_materials(project)})

    before = identity()
    assert identity() == before
    (project / "task_packages" / "t01").rename(project / "task_packages" / "renamed_task")
    after = identity()
    assert after != before
    assert identity() == after
    layout = _project_delivery_materials(project)["website_download"]
    assert layout["tasks"][0]["internal_directory"] == "task_packages/renamed_task"
    _assert_zip_paths(case, layout)


def test_recorded_commands_are_not_rewritten_or_guessed(tmp_path):
    case = tmp_path / "case"
    project, manifest = _project(case, directories=("t07_OriginalTask",))
    full = ["python", "run_custom.py", "--label", "quoted label", "--offset", "-2"]
    smoke = "python run_custom.py --profile quick"
    manifest["task_packages"][0].update(full_command=full, smoke_command=smoke)
    original = deepcopy(manifest)

    entry = describe_download_layout(project, package_manifest=manifest)["tasks"][0]
    assert entry["recorded_commands"]["full_command"] == full
    assert entry["recorded_commands"]["smoke_command"] == smoke
    assert manifest == original
    # Existing source filenames do not authorize invented run commands.
    empty = describe_download_layout(project, package_manifest={})["tasks"][0]
    assert not empty["recorded_commands"].get("full_command")
    assert not empty["recorded_commands"].get("smoke_command")


def test_task_without_results_still_has_code_and_empty_result_directory(tmp_path):
    case = tmp_path / "case"
    project, _ = _project(case, directories=("t01",), results=False)
    layout = _project_delivery_materials(project)["website_download"]
    _assert_zip_paths(case, layout)
    entry = layout["tasks"][0]
    _reports(case)
    with ZipFile(build_delivery(case, str(uuid4()))) as archive:
        assert entry["results_directory"] + "/" in archive.namelist()
        assert not any(name.startswith(entry["results_directory"] + "/")
                       and not name.endswith("/") for name in archive.namelist())


def test_unsafe_task_path_is_material_observation_not_an_editor_exception(tmp_path, monkeypatch):
    case = tmp_path / "case"
    project, _ = _project(case, directories=("t01",))
    from geng_agent import final_delivery

    actual = final_delivery.path_is_link
    unsafe = project / "task_packages" / "t01"
    monkeypatch.setattr(final_delivery, "path_is_link", lambda path: Path(path) == unsafe or actual(path))

    # The report receives the engineering problem and can explain it. The
    # existing archive path boundary is not weakened by the navigation helper.
    material = _project_delivery_materials(project)
    layout = material["website_download"]
    assert layout["tasks"] == []
    assert layout.get("observation")
    with pytest.raises(ValueError):
        task_delivery_directories(project)
