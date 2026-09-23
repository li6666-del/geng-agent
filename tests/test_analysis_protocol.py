"""Offline counterexamples for supervisor-owned scientific handoffs."""
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from geng_agent.analysis_protocol import analysis_protocol_issues
from geng_agent.agentic_analysis import run_codex_json_stage
from geng_agent.consolidated_analysis import supervised_analysis_stage
from geng_agent.pipeline import ReviewPipeline
from geng_agent.schemas import validate_stage
from geng_agent.supervisor import NodeFailure, RunSupervisor, StageBlocked, supervisor_scope
from geng_agent.targeted_backfill_loop import run_targeted_backfill_loop


def test_descriptive_shapes_do_not_rewrite_science_or_bindings():
    document = {"tasks": {"repro_tasks": [{"task_id": "ber", "target": "误码概率随信噪比降低",
        "metric": "Probability of bit error", "required_facts": ["unnormalized paper reference"],
        "assumptions": [{"说明": "缺少参数，请主持人判断"}],
        "scientific_acceptance": {"tolerance": 1e-10, "method_order": ["A", "B"]},
        "variant_note": {"sample_size": 1000}}]},
        "scientific_architecture": {"bindings": [{"task_id": "ber", "experiment_id": "main"},
                                                  {"task_id": "ber", "experiment_id": "sensitivity"}]}}
    original = deepcopy(document)
    assert validate_stage("experiment_plan", document) == []
    assert document == original


@pytest.mark.parametrize("task_document", [
    {"repro_tasks": [{"target": "No machine address"}]},
    {"repro_tasks": [{"task_id": "same"}, {"task_id": "same"}]},
    {"repro_tasks": [{"task_id": "a"}], "execution_relationships": [{
        "strength": "strong", "task_ids": ["a", "missing"]}]},
    {"repro_tasks": [{"task_id": "a"}], "backfill_handoff": {
        "ready_for_writer": False, "blocking_request_ids": ["not_declared"]}},
])
def test_only_unaddressable_execution_handoffs_need_owner_repair(task_document):
    original = deepcopy(task_document)
    assert analysis_protocol_issues("repro_tasks", task_document)
    assert task_document == original


def test_code_path_cannot_escape_but_missing_description_is_allowed():
    assert not validate_stage("scientific_architecture", {"components": [{"id": "q"}]})
    assert validate_stage("scientific_architecture", {"components": [{"module": "../outside.py"}]})


def test_codex_optional_prose_file_does_not_discard_valid_facts(tmp_path, monkeypatch):
    facts = {"engineering_facts": [{"type": "新指标", "value": -2, "raw_condition": "log scale"}]}
    worker = Mock()

    def generate(**kwargs):
        (kwargs["work_dir"] / "facts.json").write_text(json.dumps(facts), encoding="utf-8")
        (kwargs["work_dir"] / "paper_thesis.json").write_text("{unfinished prose", encoding="utf-8")
        return {"ok": True}

    worker.side_effect = generate
    monkeypatch.setattr("geng_agent.agentic_analysis.run_codex_subprocess", worker)
    result = run_codex_json_stage(prompt="read the paper", stage_label="understand",
        schema_stage="paper_understanding", output_dir=tmp_path, audit_dir=tmp_path / "audit", max_attempts=5)
    assert worker.call_count == 1
    assert result["facts"] == facts
    assert result["_meta"]["document_observations"]
    assert list((tmp_path / "audit/analysis_candidates/understand").glob("*/candidate.json"))


def test_invalid_json_has_one_owner_call_and_never_uses_fallback(tmp_path):
    class Client:
        def __init__(self):
            self.calls = 0
        def complete(self, *_args, **_kwargs):
            self.calls += 1
            return '{"facts": {"engineering_facts": ['
    client = Client()
    fallback = Mock(return_value={"facts": {"engineering_facts": [{"invented": True}]}})
    pipeline = ReviewPipeline(client=client)
    with pytest.raises(NodeFailure):
        pipeline._load_or_create_analysis_stage_json(output_path=tmp_path / "understanding.json",
            output_dir=tmp_path, audit_dir=tmp_path / "audit", prompt="paper", stage_label="understand",
            cleanup_stage="facts", schema_stage="paper_understanding", max_attempts=5, resume=False,
            fallback_factory=fallback)
    assert client.calls == 1
    fallback.assert_not_called()
    assert not (tmp_path / "understanding.json").exists()
    assert list((tmp_path / "audit/api_prompt_inputs").glob("*/raw.txt"))


def test_mechanical_failure_returns_to_same_planner_with_raw_candidate_preserved(tmp_path, monkeypatch):
    from geng_agent import supervisor
    original = {"tasks": {"repro_tasks": [{"target": "all three SNR points", "conditions": [0, 3, 6]}]}}
    fixed = deepcopy(original)
    fixed["tasks"]["repro_tasks"][0]["task_id"] = "ber"
    prompts = []
    class Client:
        def complete(self, prompt, **_kwargs):
            prompts.append(prompt)
            return json.dumps(original if len(prompts) == 1 else fixed)
    owner = ReviewPipeline(client=Client())
    scope = RunSupervisor(tmp_path, tmp_path / "audit", {"goal": "all three SNR points"})
    decisions = Mock(side_effect=[{"action": "retry", "diagnosis": "task lacks address", "instructions": "Add task_id; keep 0,3,6"},
                                  {"action": "approve", "diagnosis": "address fixed, scope retained"}])
    monkeypatch.setattr(scope, "_request", decisions)
    with supervisor_scope(scope):
        result = supervised_analysis_stage(owner, SimpleNamespace(output_dir=tmp_path), node_id="experiment_planning",
            output_path=tmp_path / "plan.json", output_dir=tmp_path, audit_dir=tmp_path / "audit",
            prompt="Keep all paper conditions", stage_label="plan", schema_stage="experiment_plan",
            cleanup_stage="tasks", max_attempts=5, resume=False, cache_inputs={"paper": "same"})
    assert len(prompts) == 2
    assert "Add task_id; keep 0,3,6" in prompts[1]
    assert result["tasks"] == fixed["tasks"]
    candidates = [json.loads(path.read_text(encoding="utf-8")) for path in (tmp_path / "audit/api_prompt_inputs").glob("*/candidate.json")]
    assert original in candidates and fixed in candidates


def _waiting_tasks():
    return {"repro_tasks": [{"task_id": "ber", "target": "keep original scope", "missing_fact_requests": [{
        "request_id": "variance", "name": "sigma", "why_needed": "defines noise", "required_fields": [{"field_id": "value"}]}]}],
        "backfill_handoff": {"ready_for_writer": False, "blocking_request_ids": ["variance"]}}


def test_backfill_failure_keeps_new_and_original_facts_and_never_degrades_to_writer(tmp_path):
    initial = {"engineering_facts": [{"name": "sigma", "value": 1, "source": "paper A"}]}
    added = {"engineering_facts": [{"name": "sigma", "value": 10, "source": "paper B"}]}
    observed = []
    with pytest.raises(StageBlocked):
        run_targeted_backfill_loop(initial_facts=initial, preliminary_tasks=_waiting_tasks(),
            run_backfill=lambda *_: added, refresh_tasks=Mock(side_effect=RuntimeError("Planner unavailable")),
            on_round=lambda _round, summary: observed.append(summary), audit_dir=tmp_path / "audit")
    assert observed[-1]["handoff_incomplete"]
    assert "Planner unavailable" in observed[-1]["error"]
    assert observed[-1]["facts"]["engineering_facts"] == initial["engineering_facts"] + added["engineering_facts"]
    assert observed[-1]["tasks"]["backfill_handoff"]["ready_for_writer"] is False
    assert initial["engineering_facts"][0]["value"] == 1


def test_search_count_does_not_override_explicit_planner_followup(tmp_path):
    rounds = []
    def refresh(round_index, tasks, *_rest):
        rounds.append(round_index)
        result = deepcopy(tasks)
        if round_index == 3:
            result["backfill_handoff"]["ready_for_writer"] = True
        return result
    result = run_targeted_backfill_loop(initial_facts={"engineering_facts": []}, preliminary_tasks=_waiting_tasks(),
        run_backfill=lambda *_: {"engineering_facts": []}, refresh_tasks=refresh, audit_dir=tmp_path / "audit")
    assert rounds == [1, 2, 3]
    assert result["final_handoff"]["ready_for_writer"] is True


def test_missing_handoff_is_unknown_not_host_approved(tmp_path):
    result = run_targeted_backfill_loop(initial_facts={"engineering_facts": []},
        preliminary_tasks={"repro_tasks": [{"task_id": "ber"}]}, run_backfill=Mock(), refresh_tasks=Mock(),
        audit_dir=tmp_path / "audit")
    assert result["final_handoff"]["ready_for_writer"] is None
    assert "backfill_handoff" not in result["tasks"]
