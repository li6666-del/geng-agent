from copy import deepcopy
from geng_agent.schemas import validate_stage
from geng_agent.tasks_normalize import finalize_repro_tasks


def test_missing_relationships_remain_absent_without_inventing_readiness():
    raw = {"repro_tasks": [{"task_id": "a"}, {"task_id": "b"}]}
    assert finalize_repro_tasks(raw, {}) == raw
    assert not validate_stage("repro_tasks", raw)


def test_invalid_strength_is_returned_to_planner_without_host_guess():
    raw = {"repro_tasks": [{"task_id": "a"}, {"task_id": "b"}], "execution_relationships": [{
        "strength": "uncertain", "task_ids": ["a", "b"], "rationale": "requires scientific review"}]}
    copied = finalize_repro_tasks(raw, {})
    assert copied == raw
    assert not validate_stage("repro_tasks", copied)


def test_legacy_relationship_strength_does_not_veto_handoff():
    raw = {"repro_tasks": [{"task_id": "train"}, {"task_id": "eval"}], "execution_relationships": [{
        "kind": "checkpoint_flow", "strength": "weak", "task_ids": ["train", "eval"],
        "producer_task_id": "train", "consumer_task_ids": ["eval"], "artifact_ids": ["checkpoint"]}]}
    original = deepcopy(raw)
    assert not validate_stage("repro_tasks", raw)
    assert raw == original
    raw["execution_relationships"][0]["strength"] = "strong"
    assert not validate_stage("repro_tasks", raw)
