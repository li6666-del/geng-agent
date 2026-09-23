"""Cancellation is control flow; a sibling's environment gap is partial delivery."""
from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from geng_agent import agentic_task_writers as writers
from geng_agent import pipeline_execution_flow as flow
from geng_agent import task_writer_runner as runner
from geng_agent.case_runtime import EnvironmentResolutionError
from geng_agent.outputs import write_json
from geng_agent.progress import PipelineCancelled
from geng_agent.supervisor import supervisor_scope
from tests.test_agentic_task_writers import _delivery, _pair
from tests.test_execution_supervision import execution_inputs, supervisor, no_model_process
from tests.test_pipeline import case_runtime_fixture


def test_reporter_cancellation_is_not_recorded_as_a_failed_review(tmp_path):
    record = {"task_id": "t", "sandbox": str(tmp_path), "writer_completed": True,
              "task_verification": {"task_id": "t", "outcome": "not_reproduced"}}
    before = deepcopy(record)
    callback = Mock(side_effect=PipelineCancelled("user stopped"))
    with pytest.raises(PipelineCancelled, match="user stopped"):
        runner._attach_task_reporter_review(callback=callback, index=1, task={"task_id": "t"},
                                            record=record, session_round=1)
    assert record == before
    assert callback.call_count == 1


@pytest.mark.parametrize("location", ["moderator", "clarification"])
def test_writer_recovery_cancellation_is_not_a_stop_decision(monkeypatch, tmp_path, location):
    cancelled = PipelineCancelled("user stopped")
    recover = Mock(side_effect=cancelled) if location == "moderator" else Mock(
        return_value={"action": "repair_reporter", "decision": {"instructions": "clarify"}})
    monkeypatch.setattr(runner, "recover_writer_stall", recover)
    callback = Mock(side_effect=cancelled)
    record = {"task_id": "t", "sandbox": str(tmp_path)}
    with pytest.raises(PipelineCancelled, match="user stopped"):
        runner._recover_writer_exceptions(
            work=[(1, {"task_id": "t"}, record, {"task_id": "t", "host_action": "rerun_writer"})],
            trigger="writer_stall", writer_budget_available=True, callback=callback, session_round=1)
    assert "moderator_error" not in record
    assert "coordination_status" not in record
    assert "scientific_stop_reason" not in record
    assert callback.call_count == (1 if location == "clarification" else 0)


def test_environment_extension_block_preserves_current_dispatch_sibling(monkeypatch, tmp_path):
    context, analysis, _runtime = execution_inputs(tmp_path)
    context.options.resume = False
    analysis.tasks = {"repro_tasks": [_pair(name)[0] for name in ("done", "needs_dependency")]}
    analysis.paper_path.write_text("Synthetic two-point communications calculation", encoding="utf-8")
    for name, document in {"engineering_facts.json": {"engineering_facts": []},
                           "repro_tasks.json": analysis.tasks, "experiment_index.json": {"experiments": []}}.items():
        write_json(context.output_dir / name, document)
    done = _delivery("done", context.audit_dir / "03c_task_writer_sandboxes/01_done")
    note = {"task_id": "done", "outcome": "not_reproduced", "host_action": "complete"}
    done.update(task_verification=note, task_reporter={"ok": True, "task_verification": note}, index=1)
    csv = Path(done["sandbox"]) / "outputs/result.csv"
    pending = {"task_id": "needs_dependency", "writer_completed": False,
               "host_execution": {"passed": False}, "environment_requests": [
                   {"requirement": "missing-scientific-package", "requested_by": "needs_dependency"}]}
    records = [done, pending]
    def dispatch_current(**_kwargs):
        csv.parent.mkdir(parents=True)
        csv.write_text("snr,ber\n0,0.12\n", encoding="utf-8")
        return records, {}
    dispatch = Mock(side_effect=dispatch_current)
    monkeypatch.setattr(writers, "_dispatch_task_writers", dispatch)
    monkeypatch.setattr(writers, "_prepare_project_workspace", Mock(side_effect=AssertionError("No complete package exists")))
    monkeypatch.setattr("geng_agent.agentic_foundation.run_codex_foundation_writer_workflow", Mock(return_value=None))
    ensure = Mock(side_effect=[case_runtime_fixture(context.output_dir, "original-environment"),
                              EnvironmentResolutionError("unavailable", "Required package is unavailable")])
    monkeypatch.setattr("geng_agent.case_runtime.ensure_case_runtime", ensure)
    # A stale on-disk record must not replace this invocation's actual result.
    write_json(context.audit_dir / "03c_task_writers_records.json", {"tasks": [{"task_id": "obsolete", "host_execution": {"passed": True}}]})
    coordinator, calls = supervisor(context.output_dir, lambda node, _kwargs:
        {"action": "block", "diagnosis": "The necessary dependency is unavailable"}
        if node.startswith("environment:extension:") else None)
    with supervisor_scope(coordinator):
        result = flow.run_execution_flow(context, analysis)
    assert result.task_records is records
    assert result.task_records[0] is done
    assert done["task_reporter"]["task_verification"] == note
    assert result.runtime_result["tasks_passed"] == 1
    assert result.runtime_result["tasks_total"] == 2
    assert result.runtime_result["delivery_status"] == "partial"
    assert result.runtime_result["partial_success"]["valid_task_ids"] == ["done"]
    assert any(item["node_id"] == "environment:extension" for item in result.runtime_result["engineering_failures"])
    assert result.manifest["files"] == []
    assert result.validation["packaging_completed"] is False
    assert csv.read_text(encoding="utf-8") == "snr,ber\n0,0.12\n"
    assert dispatch.call_count == 1 and ensure.call_count == 2
    blocked = json.loads((context.audit_dir / "03a_environment_blocked.json").read_text(encoding="utf-8"))
    assert blocked["pipeline_can_continue"] is True
    assert blocked["preserved_task_ids"] == ["done", "needs_dependency"]
    assert calls[-1][1]["trigger"] == "tool_dispatch"
