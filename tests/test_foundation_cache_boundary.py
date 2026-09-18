from __future__ import annotations

import inspect
from pathlib import Path
from unittest.mock import patch

import pytest

from geng_agent import writer_lineage
from geng_agent.foundation_prompt_cache import _foundation_input_hash, _load_cached_foundation
from geng_agent.foundation_scope import scoped_foundation_architecture
from geng_agent.foundation_snapshot import file_sha256, foundation_snapshot_hash
from geng_agent.outputs import write_json


def _inputs(root: Path) -> dict:
    paper = root / "paper.txt"
    paper.write_text("Shared noise model: variance = 1.", encoding="utf-8")
    architecture = {
        "schema_version": "1.1",
        "components": [{
            "id": "noise", "module": "src/noise.py", "callable": "sample",
            "parameters": ["variance"],
            "basis": {"evidence_facts": [{"type": "parameter", "name": "variance"}]},
        }],
        "bindings": [
            {"task_id": task, "experiment_id": task, "components": ["noise"]}
            for task in ("a", "b")
        ],
        "quantities": [{"id": "variance", "value": 1}],
    }
    return {
        "architecture": scoped_foundation_architecture(architecture),
        "facts": {"engineering_facts": [{"type": "parameter", "name": "variance", "value": 1}]},
        "paper_path": paper, "case_runtime": None,
    }


def _input_hash(inputs: dict) -> str:
    analysis, architecture, runtime = writer_lineage.foundation_cache_projection(**inputs)
    return _foundation_input_hash(analysis, architecture, environment_hash=runtime)


@pytest.mark.parametrize("filename", [
    "writer_lineage.py", "task_writer_inputs.py", "task_writer_prompts.py", "task_reporter_context.py",
])
def test_task_policy_edit_keeps_actual_foundation_snapshot_reusable(tmp_path: Path, filename: str) -> None:
    inputs = _inputs(tmp_path)
    initial = _input_hash(inputs)
    snapshot = tmp_path / "snapshot"
    module = snapshot / "src/noise.py"
    module.parent.mkdir(parents=True)
    module.write_text("def sample(): return 1\n", encoding="utf-8")
    files = [{"path": "src/noise.py", "sha256": file_sha256(module), "bytes": module.stat().st_size}]
    manifest = tmp_path / "foundation_manifest.json"
    write_json(manifest, {
        "schema_version": "1.0", "workflow_version": "2", "contract_version": "1",
        "input_hash": initial, "analysis_snapshot_hash": "a" * 64,
        "files": files, "frozen_files": files, "snapshot_hash": foundation_snapshot_hash(files),
        "required_modules": ["src/noise.py"],
        "validation": {"tests_passed": True, "local_imports_resolve": True},
    })
    read_text = Path.read_text
    getsource = inspect.getsource

    def edited_file(path, *args, **kwargs):
        value = read_text(path, *args, **kwargs)
        return value + "\n# Changed task-role instructions\n" if path.name == filename else value

    def edited_function(function):
        value = getsource(function)
        return value + "\n# Changed task packet policy\n" if function in (
            writer_lineage.writer_policy_content_hashes, writer_lineage.build_writer_unit_lineage,
        ) else value

    with patch.object(Path, "read_text", edited_file), patch.object(inspect, "getsource", edited_function):
        current = _input_hash(inputs)
        assert current == initial
        assert _load_cached_foundation(
            manifest_path=manifest, snapshot_dir=snapshot, expected_input_hash=current,
            expected_required_modules={"src/noise.py"},
        ) is not None
    # Ignoring task-role edits does not weaken frozen-code integrity checks.
    module.write_text("def sample(): return 9\n", encoding="utf-8")
    assert _load_cached_foundation(
        manifest_path=manifest, snapshot_dir=snapshot, expected_input_hash=initial,
        expected_required_modules={"src/noise.py"},
    ) is None


@pytest.mark.parametrize("function_name", [
    "foundation_policy_content_hashes", "foundation_cache_projection", "foundation_consumed_runtime",
    "_scoped_architecture_metadata", "_runtime_projection", "runtime_distribution_metadata", "_source_closure",
])
def test_shared_projection_and_runtime_policy_changes_invalidate_foundation(tmp_path: Path, function_name: str) -> None:
    inputs = _inputs(tmp_path)
    initial = _input_hash(inputs)
    function = getattr(writer_lineage, function_name)
    getsource = inspect.getsource
    with patch.object(inspect, "getsource", side_effect=lambda item: (
        getsource(item) + "\n# Changed Foundation semantics\n" if item is function else getsource(item)
    )):
        assert _input_hash(inputs) != initial


@pytest.mark.parametrize("filename", ["foundation_prompt_cache.py", "foundation_scope.py", "foundation_revision.py"])
def test_foundation_policy_file_changes_still_invalidate(tmp_path: Path, filename: str) -> None:
    inputs = _inputs(tmp_path)
    initial = _input_hash(inputs)
    read_text = Path.read_text

    def edited_file(path, *args, **kwargs):
        value = read_text(path, *args, **kwargs)
        return value + "\n# Changed shared scientific contract\n" if path.name == filename else value

    with patch.object(Path, "read_text", edited_file):
        assert _input_hash(inputs) != initial


@pytest.mark.parametrize("change", ["paper", "fact", "interface", "quantity", "revision"])
def test_shared_scientific_inputs_still_invalidate(tmp_path: Path, change: str) -> None:
    inputs = _inputs(tmp_path)
    initial = _input_hash(inputs)
    if change == "paper":
        inputs["paper_path"].write_text("Corrected variance = 2.", encoding="utf-8")
    elif change == "fact":
        inputs["facts"]["engineering_facts"][0]["value"] = 2
    elif change == "interface":
        inputs["architecture"]["components"][0]["callable"] = "sample_complex"
    elif change == "quantity":
        inputs["architecture"]["quantities"][0]["value"] = 2
    else:
        inputs["architecture"]["_foundation_revision"] = {"request_id": "correct_normalization"}
    assert _input_hash(inputs) != initial
