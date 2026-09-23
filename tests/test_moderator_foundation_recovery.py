"""Supervisor-selected shared repairs, preserving actual versions and siblings."""
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock

import pytest

from geng_agent import pipeline_execution_flow as flow
from geng_agent.case_environment import RequirementRequest
from geng_agent.case_runtime import EnvironmentRequestRequired, EnvironmentResolutionError
from geng_agent.foundation_revision import FoundationRevisionRequired, validate_foundation_revision_request
from geng_agent.supervisor import supervisor_scope
from tests.test_execution_supervision import execution_inputs, supervisor
from tests.supervisor_decisions import choose_tools


def setup_case(tmp_path, monkeypatch, foundation, writer, choose, environment=None):
    context, analysis, runtime = execution_inputs(tmp_path)
    monkeypatch.setattr("geng_agent.case_runtime.ensure_case_runtime", environment or Mock(return_value=runtime))
    monkeypatch.setattr("geng_agent.agentic_foundation.run_codex_foundation_writer_workflow", foundation)
    monkeypatch.setattr("geng_agent.agentic_task_writers.run_codex_task_writer_workflow", writer)
    def decide(node, kwargs):
        if node == "tools:execution":
            return choose(kwargs["context"])
        return None
    coordinator, calls = supervisor(context.output_dir, decide)
    with supervisor_scope(coordinator):
        result = flow.run_execution_flow(context, analysis)
    return result, calls


def snapshot(version):
    return {"snapshot_hash": version, "manifest": {"snapshot_hash": version, "files": []}}


def request(root):
    return {"request_id": "fixed-scientific-request", "component_ids": ["normalizer"],
            "affected_task_ids": ["t"], "evidence_root": str(root / "evidence"),
            "causal_change": "Restore the paper normalization"}


def delivered(records=None):
    return {"manifest": {"files": []}, "task_records": records or [], "runtime_result": {
        "passed": True, "enabled": True, "validation": {"required_files_present": True}},
        "status": {}, "written_files": []}


def start(name, **extra):
    return {"action": "start", "next_node": name, "decision_id": "test-" + name,
            "instructions": "Repair the observed shape error without changing the paper normalization",
            "expected_change": "The existing regression succeeds", **extra}


def test_supervisor_can_authorize_two_causal_repairs_without_one_retry_host_rule(tmp_path, monkeypatch):
    prior, replacement = snapshot("original"), snapshot("replacement")
    original = deepcopy(prior)
    revision = request(tmp_path)
    foundation = Mock(side_effect=[prior, RuntimeError("shape error"), RuntimeError("dtype error"), replacement])
    writer = Mock(side_effect=[FoundationRevisionRequired(revision), delivered()])
    def choose(packet):
        if "foundation" in packet["ready"]:
            attempts = next(tool["inputs"]["attempts"] for tool in packet["tools"] if tool["name"] == "foundation")
            return start("foundation", instructions=f"Address concrete failure of attempt {attempts}")
        return choose_tools(packet)
    result, _ = setup_case(tmp_path, monkeypatch, foundation, writer, choose)
    assert foundation.call_count == 4 and writer.call_count == 2
    assert prior == original and writer.call_args.kwargs["foundation"] is replacement
    revisions = [call.kwargs for call in foundation.call_args_list[1:]]
    assert all(call["revision_request"]["request_id"] == revision["request_id"] for call in revisions)
    assert revisions[-1]["recovery_instructions"]["instructions"] == "Address concrete failure of attempt 3"
    assert result.runtime_result["passed"] is True


def test_supervisor_can_retain_old_version_without_rebuilding_it(tmp_path, monkeypatch):
    prior = snapshot("original")
    revision = request(tmp_path)
    foundation = Mock(side_effect=[prior, RuntimeError("unavailable dependency")])
    writer = Mock(side_effect=[FoundationRevisionRequired(revision), delivered()])
    def choose(packet):
        if packet["state"]["failures"].get("foundation"):
            return start("retain_foundation")
        return choose_tools(packet)
    setup_case(tmp_path, monkeypatch, foundation, writer, choose)
    assert foundation.call_count == 2
    assert writer.call_args.kwargs["foundation"] is prior
    assert writer.call_args.kwargs["declined_foundation_revision_ids"] == {revision["request_id"]}
    assert writer.call_args.kwargs["force_task_ids"] == set()


def test_failed_revision_dependency_does_not_contaminate_later_writer_environment(tmp_path, monkeypatch):
    from types import SimpleNamespace
    prior = snapshot("original")
    revision = request(tmp_path)
    runtime = SimpleNamespace(environment_hash="first", python_executable=Path("python.exe"))
    updated = SimpleNamespace(environment_hash="second", python_executable=Path("python.exe"))
    environment = Mock(side_effect=[runtime, EnvironmentResolutionError("offline", "scipy unavailable"), updated])
    foundation = Mock(side_effect=[prior, EnvironmentRequestRequired(
        [RequirementRequest("scipy>=1.11")], source="foundation_writer")])
    writer = Mock(side_effect=[FoundationRevisionRequired(revision), EnvironmentRequestRequired(
        [RequirementRequest("matplotlib>=3.8")], source="task_writers"), delivered()])
    def choose(packet):
        if packet["state"]["failures"].get("environment") and "retain_foundation" in packet["ready"]:
            return start("retain_foundation")
        return choose_tools(packet)
    setup_case(tmp_path, monkeypatch, foundation, writer, choose, environment)
    assert environment.call_count == 3 and foundation.call_count == 2
    assert [item.requirement for item in environment.call_args.kwargs["extra_requirements"]] == ["matplotlib>=3.8"]
    assert writer.call_args.kwargs["foundation"] is prior


def test_same_environment_hash_is_an_observation_not_automatic_global_stop(tmp_path, monkeypatch):
    from types import SimpleNamespace
    runtime = SimpleNamespace(environment_hash="unchanged", python_executable=Path("python.exe"))
    environment = Mock(return_value=runtime)
    foundation = Mock(return_value=snapshot("same"))
    writer = Mock(side_effect=[EnvironmentRequestRequired([RequirementRequest("scipy")], source="task_writers"), delivered()])
    result, _ = setup_case(tmp_path, monkeypatch, foundation, writer, choose_tools, environment)
    assert result.runtime_result["passed"] is True
    assert environment.call_count == 2 and foundation.call_count == 1


def test_out_of_scope_revision_is_not_sent_to_foundation_writer(tmp_path, monkeypatch):
    prior = snapshot("original")
    revision = request(tmp_path)
    foundation = Mock(return_value=prior)
    writer = Mock(side_effect=[FoundationRevisionRequired(revision), delivered()])
    def choose(packet):
        if packet["state"]["pending_revision"]:
            if packet["state"]["failures"].get("foundation"):
                return start("retain_foundation")
            return start("foundation", component_ids=["normalizer", "unrelated"])
        return choose_tools(packet)
    setup_case(tmp_path, monkeypatch, foundation, writer, choose)
    assert foundation.call_count == 1
    assert writer.call_args.kwargs["foundation"] is prior


def test_recovery_metadata_survives_validation_without_changing_scientific_request_id(tmp_path):
    paper = tmp_path / "paper_evidence/paper.txt"
    paper.parent.mkdir()
    paper.write_text("Paper states variance normalization.", encoding="utf-8")
    architecture = {"components": [{"id": "normalizer", "module": "src/noise.py"}],
                    "bindings": [{"task_id": task, "components": ["normalizer"]} for task in ("a", "b")]}
    request = {"component_ids": ["normalizer"], "paper_evidence_files": ["paper_evidence/paper.txt"],
               "causal_change": "Apply the paper variance normalization"}
    original = validate_foundation_revision_request(request, architecture=architecture, evidence_root=tmp_path)
    request["moderator_recovery"] = {"decision_id": "diagnosis-one", "instructions": "Repair the failed array shape.",
                                     "expected_change": "Shared tests pass"}
    recovered = validate_foundation_revision_request(request, architecture=architecture, evidence_root=tmp_path)
    assert original["request_id"] == recovered["request_id"]
    assert original["affected_task_ids"] == recovered["affected_task_ids"] == ["a", "b"]
    assert recovered["moderator_recovery"]["instructions"] == request["moderator_recovery"]["instructions"]
    request["moderator_recovery"]["instructions"] = "Restore the original scalar broadcasting."
    retried = validate_foundation_revision_request(request, architecture=architecture, evidence_root=tmp_path)
    revalidated = validate_foundation_revision_request(retried, architecture=architecture, evidence_root=tmp_path)
    assert original["request_id"] == retried["request_id"] == revalidated["request_id"]
    assert recovered["original_request"]["moderator_recovery"]["instructions"] == "Repair the failed array shape."
    request["causal_change"] = "Use a different noise normalization"
    changed = validate_foundation_revision_request(request, architecture=architecture, evidence_root=tmp_path)
    assert changed["request_id"] != original["request_id"]
    request["moderator_recovery"]["instructions"] = ""
    with pytest.raises(ValueError, match="concrete instructions"):
        validate_foundation_revision_request(request, architecture=architecture, evidence_root=tmp_path)
