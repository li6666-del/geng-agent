"""The host records facts; the coordinator owns contextual handoff decisions."""
from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from geng_agent import project_portability as portability
from geng_agent import task_writer_packaging as packaging
from geng_agent import task_writer_state as state
from geng_agent.outputs import write_json


def test_package_freeze_does_not_run_an_extra_experiment_or_static_judgment(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    (project / "optional_diagnostic.py").write_text("not valid python!", encoding="utf-8")
    write_json(project / "config.json", {"paper_example_path": "C:/example/missing"})
    monkeypatch.setattr("geng_agent.delivery_environment.export_installation", lambda *_a, **_k: set())
    monkeypatch.setattr(portability, "_run_relocated_smoke", Mock(side_effect=AssertionError("No second experiment")))
    manifest, result = packaging._freeze_repro_project_package(
        repro_project_dir=project, output_dir=tmp_path, audit_path=tmp_path / "audit.json",
        task_manifest={"tasks": []}, expected_paths={"optional_diagnostic.py", "config.json"},
        analysis_snapshot_hash="a", foundation_snapshot_hash="", environment_hash="e", run_smoke=True,
        contextual_findings=[{"code": "optional_diagnostic_unchecked"}])
    assert result["portable"] is True
    assert result["smoke"]["ran"] is False
    assert result["observations"] == [{"code": "optional_diagnostic_unchecked"}]
    assert manifest["files"]


def test_actual_inventory_hash_mismatch_still_prevents_certification(tmp_path):
    source = tmp_path / "run.py"
    source.write_text("VALUE = 1", encoding="utf-8")
    write_json(tmp_path / "source_inventory.json", portability.build_source_inventory(tmp_path))
    source.write_text("VALUE = 2", encoding="utf-8")
    with pytest.raises(portability.ProjectPortabilityError):
        portability.validate_repro_project_portability(tmp_path)


def lineage_fixture(tmp_path, path="execution_units/unit/model.bin"):
    writer, project = tmp_path / "writer", tmp_path / "project"
    for root in (writer, project):
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"observed model bytes")
    write_json(writer / "execution_unit_result.json", {"artifact_lineage": [{
        "artifact_id": "model", "path": path, "producer_task_id": "different-wording"}]})
    return writer, project


def test_lineage_description_conflicts_are_observations_and_not_invented_provenance(tmp_path):
    writer, project = lineage_fixture(tmp_path)
    plan = {"execution_units": [{"dependencies": [
        {"artifact_id": "model", "producer_task_id": "train", "consumer_task_id": "evaluate", "strength": "strong"},
        {"artifact_id": "missing-note", "producer_task_id": "train", "consumer_task_id": "evaluate", "strength": "strong"},
    ]}]}
    result = packaging._build_artifact_lineage(repro_project_dir=project, execution_plan=plan,
        task_records=[{"task_id": "train", "execution_unit_id": "unit", "sandbox": str(writer)}], require_lineage=True)
    assert result["artifacts"][0]["producer_task_id"] == "different-wording"
    assert {item["code"] for item in result["observations"]} == {
        "producer_description_differs", "strong_artifact_description_missing"}


def test_lineage_never_resolves_an_unsafe_path(tmp_path):
    writer, project = lineage_fixture(tmp_path)
    write_json(writer / "execution_unit_result.json", {"artifact_lineage": [{
        "artifact_id": "model", "path": "../outside.bin"}]})
    with pytest.raises(RuntimeError, match="unsafe path"):
        packaging._build_artifact_lineage(repro_project_dir=project, execution_plan={},
            task_records=[{"execution_unit_id": "unit", "sandbox": str(writer)}], require_lineage=False)


def test_coordination_stop_keeps_original_reporter_request(tmp_path):
    verification = {"task_id": "t", "outcome": "not_reproduced", "host_action": "rerun_writer",
                    "rerun_reason": "needs clarification", "rerun_evidence": {"comment": "see original note"}}
    before = deepcopy(verification)
    record = {"task_reporter": {"task_verification": verification}}
    state._terminalize_rerun_request(record=record, verification=verification,
        stop_reason="moderator_stopped", uncertainty="No further action was authorized")
    assert verification == before
    assert record["task_verification"] == before
    assert record["coordination_status"] == "stopped"
