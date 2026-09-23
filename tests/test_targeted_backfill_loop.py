from copy import deepcopy
import json
from unittest.mock import Mock
import pytest
from geng_agent.progress import PipelineCancelled
from geng_agent.supervisor import RunSupervisor, StageBlocked
from geng_agent.targeted_backfill_loop import run_targeted_backfill_loop


def _tasks():
    return {"repro_tasks": [{"task_id": "a", "missing_fact_requests": [{"request_id": "r", "name": "n"}]}],
        "backfill_handoff": {"ready_for_writer": False, "blocking_request_ids": ["r"]}}


@pytest.mark.parametrize("nonblocking_request", [False, True])
def test_planner_ready_handoff_skips_moderator_and_backfill(tmp_path, monkeypatch, nonblocking_request):
    tasks = _tasks() if nonblocking_request else {"repro_tasks": [{"task_id": "a"}]}
    tasks["backfill_handoff"] = {"ready_for_writer": True, "blocking_request_ids": []}
    monkeypatch.setattr(RunSupervisor, "_request", Mock(side_effect=AssertionError("routine handoff called moderator")))
    search = Mock()
    replan = Mock()
    result = run_targeted_backfill_loop(initial_facts={"engineering_facts": []}, preliminary_tasks=tasks,
        run_backfill=search, refresh_tasks=replan, audit_dir=tmp_path / "audit")
    search.assert_not_called()
    replan.assert_not_called()
    assert result["round_count"] == 0
    assert result["stop_reason"] == "planner_ready_handoff"
    state = json.loads((tmp_path / "audit" / "supervisor" / "backfill_state.json").read_text(encoding="utf-8"))
    assert "accepted" not in state
    assert state["planner_handoff"]["ready_for_writer"] is True


def test_new_search_evidence_returns_to_planner_without_second_moderator_call(tmp_path, monkeypatch):
    tasks = _tasks()
    moderator = Mock(return_value={"action": "start", "next_nodes": ["search"],
                                   "decision_id": "search-gap", "diagnosis": "Planner selected r"})
    monkeypatch.setattr(RunSupervisor, "_request", moderator)
    refreshed = deepcopy(tasks)
    refreshed["backfill_handoff"] = {"ready_for_writer": True, "blocking_request_ids": []}
    search = Mock(return_value={"engineering_facts": [{"name": "n", "value": 1}]})
    replan = Mock(return_value=refreshed)
    result = run_targeted_backfill_loop(initial_facts={"engineering_facts": []}, preliminary_tasks=tasks,
        run_backfill=search, refresh_tasks=replan, audit_dir=tmp_path / "audit")
    assert moderator.call_count == 1
    search.assert_called_once()
    replan.assert_called_once()
    assert result["round_count"] == 1
    assert result["stop_reason"] == "planner_ready_handoff"


def test_round_budget_does_not_change_blocked_handoff_into_ready(tmp_path):
    tasks = _tasks()
    with pytest.raises(StageBlocked):
        run_targeted_backfill_loop(initial_facts={"engineering_facts": []}, preliminary_tasks=tasks,
            run_backfill=lambda *_: {"engineering_facts": []}, refresh_tasks=lambda *_: deepcopy(tasks), max_rounds=2,
            audit_dir=tmp_path / "audit")
    state = json.loads((tmp_path / "audit" / "supervisor" / "backfill_state.json").read_text(encoding="utf-8"))
    assert state["ledger"]["round_count"] == 2
    assert state["planner_handoff"]["ready_for_writer"] is False


def test_cancel_is_not_converted_to_writer_handoff(tmp_path):
    refresh = Mock()
    with pytest.raises(PipelineCancelled):
        run_targeted_backfill_loop(initial_facts={"engineering_facts": []}, preliminary_tasks=_tasks(),
            run_backfill=Mock(side_effect=PipelineCancelled("stop")), refresh_tasks=refresh, audit_dir=tmp_path / "audit")
    refresh.assert_not_called()
