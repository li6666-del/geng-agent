from copy import deepcopy
import pytest
from geng_agent.tasks_normalize import finalize_repro_tasks, normalize_repro_tasks_candidate, recover_truncated_repro_tasks
from geng_agent.schemas import validate_stage


def test_task_copy_never_guesses_metrics_references_or_acceptance():
    source = {"repro_tasks": [{"task_id": "ber", "goal": "所有三个测试点", "metric": "P(error)",
        "required_facts": ["unresolved identifier"], "assumptions": [{"original wording": "n=100"}],
        "scientific_acceptance": {"tolerance": 1e-10, "paper_values": [0, -1, 10]}, "extra": "retain"}]}
    original = deepcopy(source)
    assert finalize_repro_tasks(source, {"engineering_facts": []}) == original
    assert normalize_repro_tasks_candidate(source, {}) == (original, [])
    assert validate_stage("repro_tasks", original) == []
    assert source == original


def test_no_seed_task_or_readiness_is_fabricated():
    assert finalize_repro_tasks({"repro_tasks": []}, {}) == {"repro_tasks": []}
    raw = {"repro_tasks": [{"target": "no machine address"}]}
    assert finalize_repro_tasks(raw, {}) == raw
    assert validate_stage("repro_tasks", raw)
    with pytest.raises(ValueError):
        finalize_repro_tasks([], {})


def test_partial_task_stream_cannot_drop_unparsed_tasks():
    assert recover_truncated_repro_tasks('{"repro_tasks":[{"task_id":"one"},') is None
