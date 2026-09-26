"""Offline projection checks for files shipped to the reader, without scientific runs."""

import json
from pathlib import Path
from urllib.parse import quote

import pytest

from geng_agent.final_delivery import collect_task_delivery, render_task_readme


def _write(root: Path, relative: str, content: bytes | str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
    return path


def _metadata(root: Path, entries) -> None:
    _write(root, "outputs/T1/task_agent_result.json",
           json.dumps({"task_id": "T1", "delivery_files": entries}))


def _by_path(files):
    return {(entry["role"], entry["relative_path"]): entry for entry in files}


def test_declared_outputs_keep_runtime_tree_inputs_and_per_file_descriptions(tmp_path):
    task = tmp_path / "task"
    runtime = {
        "run_experiment.py": "print('fixture')\n",
        "src/math_impl.py": '"""Compute the scalar result."""\n',
        "tasks/T1.py": "VALUE = 1\n",
        "configs/T1/full.json": '{"samples": 3}\n',
        "config.json": '{"task": "T1"}\n',
        "requirements.txt": "# No external dependency\n",
        "requirements.repro.txt": "-r requirements.txt\n",
        "constraints.repro.txt": "# Empty constraints\n",
        "tasks_manifest.json": '{"tasks": []}\n',
    }
    for relative, content in runtime.items():
        _write(task, relative, content)
    _write(task, "special/input.bin", b"\x00\xff\x01")
    _write(task, "outputs/T1/curve data.csv", "x,y\n1,2\n")
    _write(task, "outputs/T1/undeclared.csv", "not selected by complete metadata")
    _metadata(task, [
        {"path": "outputs/T1/curve data.csv", "role": "result", "description": "Measured curve samples."},
        {"path": "special/input.bin", "role": "input", "description": "Original binary input."},
    ])

    files = collect_task_delivery(task, case_root=tmp_path, task_id="T1")
    selected = _by_path(files)
    assert set(selected) == ({("code", name) for name in runtime}
                             | {("code", "special/input.bin"), ("result", "outputs/T1/curve data.csv")})
    assert selected[("code", "special/input.bin")]["source"].read_bytes() == b"\x00\xff\x01"
    assert selected[("result", "outputs/T1/curve data.csv")]["description"] == "Measured curve samples."
    readme = render_task_readme("T1", files)
    assert "Measured curve samples." in readme and "Original binary input." in readme
    assert "python run_experiment.py config.json" in readme
    assert 'python -m pip install -r "requirements.repro.txt"' in readme
    for entry in files:
        relative = ("代码/" if entry["role"] == "code" else "复现结果/") + entry["relative_path"]
        assert relative in readme
        assert quote(relative, safe="/") in readme
        assert entry["description"].strip()


@pytest.mark.parametrize("metadata", [
    None,
    "{broken json",
    {"task_id": "T1"},
    {"task_id": "T1", "delivery_files": {"path": "outputs/T1/result.csv"}},
    {"task_id": "T1", "delivery_files": [None]},
    {"task_id": "T1", "delivery_files": [
        {"path": "outputs/T1/result.csv", "role": "result"},
        {"path": "outputs/T1/missing.csv", "role": "result"},
    ]},
    {"task_id": "T1", "delivery_files": [
        {"path": "outputs/T1/result.csv", "role": "result"},
        {"path": "results/extra.csv", "role": "unknown"},
    ]},
    {"task_id": "T1", "delivery_files": [{"path": "outputs/T1/result.csv", "role": ["result"]}]},
    {"task_id": "T1", "delivery_files": [{"path": "outputs/T1/result.csv", "role": {"type": "result"}}]},
], ids=["missing", "broken-json", "missing-list", "wrong-list-type", "bad-entry", "missing-file",
        "bad-role", "list-role", "dict-role"])
def test_missing_or_malformed_metadata_falls_back_to_original_result_directories(tmp_path, metadata):
    task = tmp_path / "task"
    results = {
        "outputs/T1/result.csv": b"x,value\n1,0.5\n",
        "results/extra.csv": b"index,score\n2,0.75\n",
        "figures/curve.png": b"\x89PNG\r\nfixture",
        "plots/curve.svg": b"<svg>fixture</svg>",
    }
    for relative, content in results.items():
        _write(task, relative, content)
    _write(task, "run.py", "print('fixture')\n")
    if metadata is not None:
        _write(task, "outputs/T1/task_agent_result.json",
               metadata if isinstance(metadata, str) else json.dumps(metadata))

    selected = _by_path(collect_task_delivery(task, case_root=tmp_path, task_id="T1"))
    assert set(selected) == {("code", "run.py")} | {("result", path) for path in results}
    for relative, content in results.items():
        assert selected[("result", relative)]["source"].read_bytes() == content


@pytest.mark.parametrize("declare_internal", [False, True], ids=["legacy", "explicit-metadata"])
def test_internal_files_are_excluded_even_when_declared_for_delivery(tmp_path, declare_internal):
    task = tmp_path / "task"
    internal = [
        "audit/transcript.json", "src/__pycache__/module.pyc", ".env", ".venv/site.py",
        "outputs/T1/execution_receipt.json", "outputs/T1/task_agent_result.md",
        "outputs/T1/reproduction_report.docx", "outputs/T1/result_review.md",
        "outputs/T1/report_assets/chart.png", "outputs/T1/logs/run.log",
        "outputs/T1/cache/intermediate.csv", "task_notes/plan.md",
        "execution_records/T1/receipt.json", "source_inventory.json", "package_index.json",
        "src/debug_probe.py", "src/credentials.json",
    ]
    for relative in internal:
        _write(task, relative, "internal fixture")
    _write(task, "src/main.py", "print('fixture')\n")
    _write(task, "outputs/T1/results.csv", "x,y\n1,2\n")
    if declare_internal:
        _metadata(task, [{"path": "outputs/T1/results.csv", "role": "result"}]
                  + [{"path": relative, "role": "result"} for relative in internal])

    files = collect_task_delivery(task, case_root=tmp_path, task_id="T1")
    assert set(_by_path(files)) == {("code", "src/main.py"), ("result", "outputs/T1/results.csv")}
    assert not any(relative in render_task_readme("T1", files) for relative in internal)


def test_host_observed_inputs_keep_original_paths_including_upstream_result_inputs(tmp_path):
    task = tmp_path / "task"
    _write(task, "src/main.py", "print('fixture')\n")
    _write(task, "outputs/upstream/data.csv", "input\n42\n")
    _write(task, "outputs/T1/result.csv", "value\n42\n")
    archived = _write(task, "execution_records/T1/inputs/reference.bin", b"\x00\xffarchived")
    _write(task, "execution_evidence.json", json.dumps({"tasks": [{"task_id": "T1", "files": [{
        "kind": "input_hashes", "original_path": "custom/reference.bin",
        "packaged_path": "execution_records/T1/inputs/reference.bin",
    }]}]}))
    _write(task, "outputs/T1/execution_receipt.json", json.dumps({
        "observer": "orchestration_host", "task_id": "T1",
        "input_hashes": {"outputs/upstream/data.csv": "fixture-hash"},
    }))
    _metadata(task, [{"path": "outputs/T1/result.csv", "role": "result"}])

    selected = _by_path(collect_task_delivery(task, case_root=tmp_path, task_id="T1"))
    assert set(selected) == {("code", "src/main.py"), ("code", "custom/reference.bin"),
                             ("code", "outputs/upstream/data.csv"), ("result", "outputs/T1/result.csv")}
    assert selected[("code", "custom/reference.bin")]["source"] == archived
    assert selected[("code", "custom/reference.bin")]["source"].read_bytes() == b"\x00\xffarchived"
    assert selected[("code", "outputs/upstream/data.csv")]["source"].read_bytes() == b"input\n42\n"


def test_readme_preserves_installation_guidance_without_internal_environment_record(tmp_path):
    task = tmp_path / "task"
    _write(task, "run_experiment.py", "print('fixture')\n")
    _write(task, "requirements.repro.txt", "-c constraints.repro.txt\nnumpy\n")
    _write(task, "constraints.repro.txt", "numpy==2.0.0\n")
    _write(task, "installation.json", json.dumps({
        "python": {"python_full_version": "3.11.9", "executable": "C:/private/python.exe"},
        "install_file": "requirements.repro.txt",
        "indexes": ["https://pypi.org/simple", "https://user:secret@example.com/simple",
                    "https://example.com/simple?token=private", "C:/private/cache"],
        "accelerator_sources": [{"url": "https://download.pytorch.org/whl/cpu"}],
        "warnings": ["INTERNAL WARNING C:/private/cache and token=private"],
    }))

    files = collect_task_delivery(task, case_root=tmp_path, task_id="T1")
    assert "installation.json" not in {entry["relative_path"] for entry in files}
    readme = render_task_readme("T1", files, task_root=task)
    assert "3.11.9" in readme
    assert 'python -m pip install -r "requirements.repro.txt"' in readme
    assert 'https://pypi.org/simple' in readme
    assert 'https://download.pytorch.org/whl/cpu' in readme
    assert "constraints.repro.txt" in readme
    for internal in ("C:/private", "secret", "token=private", "INTERNAL WARNING"):
        assert internal not in readme


def test_writer_result_role_does_not_remove_a_required_code_module(tmp_path):
    task = tmp_path / "task"
    _write(task, "tasks/T1.py", "VALUE = 1\n")
    _metadata(task, [{"path": "tasks/T1.py", "role": "result", "description": "Writer supplied label"}])
    selected = _by_path(collect_task_delivery(task, case_root=tmp_path, task_id="T1"))
    assert selected[("code", "tasks/T1.py")]["source"].read_text() == "VALUE = 1\n"
    assert ("result", "tasks/T1.py") in selected


def test_generated_task_index_is_not_mistaken_for_a_file_description(tmp_path):
    task = tmp_path / "task"
    _write(task, "configs/full.json", '{"samples": 3}')
    _write(task, "README.md", "| Task | Config | Output |\n| T1 | `configs/full.json` | `outputs/T1` |\n")
    selected = _by_path(collect_task_delivery(task, case_root=tmp_path, task_id="T1"))
    assert "samples" not in selected[("code", "configs/full.json")]["description"]
    assert "未提供" in selected[("code", "configs/full.json")]["description"]
    assert selected[("code", "configs/full.json")]["description"] != "`outputs/T1`"


def test_legacy_project_export_preserves_inputs_for_its_actual_task_ids(tmp_path):
    from uuid import uuid4
    from zipfile import ZipFile
    from geng_agent.web.delivery import REPORTS, build_delivery

    project = tmp_path / "repro_project"
    _write(project, "run.py", "print('legacy fixture')\n")
    for task_id in ("T1", "T2"):
        path = f"outputs/{task_id}/input.csv"
        _write(project, path, f"input\n{task_id}\n")
        _write(project, f"outputs/{task_id}/execution_receipt.json", json.dumps({
            "observer": "orchestration_host", "task_id": task_id, "input_hashes": {path: "recorded"},
        }))
    for name in REPORTS:
        _write(tmp_path, name, b"offline report fixture")
    with ZipFile(build_delivery(tmp_path, str(uuid4()))) as archive:
        for task_id in ("T1", "T2"):
            assert archive.read(f"复现交付包/复现任务/t01_task/代码/outputs/{task_id}/input.csv") == f"input\n{task_id}\n".encode()


def test_writer_guide_is_preserved_without_host_rewriting(tmp_path):
    task = tmp_path / "task"
    _write(task, "src/channel.py", '"""Some internal English API documentation."""\n')
    _write(task, "outputs/T1/ber.csv", "snr,ber\n0,0.1\n")
    guide = (
        "# 衰落信道下的误码率实验\r\n\r\n"
        "先看误码率曲线，了解接收信噪比增加后检测性能如何变化。\r\n\r\n"
        "- `代码/src/channel.py`：生成瑞利衰落和高斯噪声，模拟无线传输。\r\n"
        "- `复现结果/outputs/T1/ber.csv`：记录不同信噪比下判错的比特比例，用于画误码率曲线。\r\n"
    )
    _write(task, "delivery_readme.md", guide)
    files = collect_task_delivery(task, case_root=tmp_path, task_id="T1")
    assert render_task_readme("T1", files, task_root=task) == guide
    assert all(item["relative_path"] != "delivery_readme.md" for item in files)


@pytest.mark.parametrize("guide_bytes", [None, b"", b"\xff\xfe"])
def test_missing_or_unreadable_guide_keeps_legacy_delivery_available(tmp_path, guide_bytes):
    task = tmp_path / "task"
    _write(task, "src/channel.py", '"""Unique raw English docstring."""\n')
    _write(task, "outputs/T1/result.csv", "raw_field_a,raw_field_b\n1,2\n")
    _write(task, "outputs/T1/result.json", '{"raw_json_key": 1}')
    if guide_bytes is not None:
        _write(task, "delivery_readme.md", guide_bytes)
    files = collect_task_delivery(task, case_root=tmp_path, task_id="T1")
    guide = render_task_readme("T1", files, task_root=task)
    assert len(files) == 3
    assert "未提供" in guide
    for internal in ("Unique raw English docstring", "raw_field_a", "raw_field_b", "raw_json_key"):
        assert internal not in guide


def test_linked_writer_guide_is_not_read(tmp_path, monkeypatch):
    from geng_agent import final_delivery

    task = tmp_path / "task"
    _write(task, "run.py", "print('fixture')\n")
    guide = _write(task, "delivery_readme.md", "outside private document")
    actual = final_delivery.path_is_link
    monkeypatch.setattr(final_delivery, "path_is_link", lambda path: path == guide or actual(path))
    files = collect_task_delivery(task, case_root=tmp_path, task_id="T1")
    assert "outside private document" not in render_task_readme("T1", files, task_root=task)
