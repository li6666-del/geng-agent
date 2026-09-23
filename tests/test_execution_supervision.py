"""Offline execution-boundary tests: real coordinator, no model calls."""
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from geng_agent import pipeline_execution_flow as flow
from geng_agent import task_writer_runner as runner
from geng_agent.execution_receipts import trusted_input_snapshot
from geng_agent.task_writer_delivery import _collect_task_writer_delivery
from geng_agent.supervisor import RunSupervisor, StageBlocked, supervisor_scope


@pytest.fixture(autouse=True)
def no_model_process(monkeypatch):
    def forbidden(**_kwargs):
        raise AssertionError("unit tests must never launch a model")
    monkeypatch.setattr("geng_agent.codex_runner.run_codex_subprocess", forbidden)
    monkeypatch.setattr("geng_agent.moderator.run_codex_subprocess", forbidden)


def supervisor(root, decisions=None):
    coordinator = RunSupervisor(root, root / "audit", {"paper": "synthetic", "scope": "two points"})
    calls = []

    def request(node_id, **kwargs):
        calls.append((node_id, kwargs))
        if decisions is not None:
            chosen = decisions(node_id, kwargs)
            if chosen:
                return chosen
        if kwargs.get("trigger") == "tool_dispatch":
            from tests.supervisor_decisions import choose_tools
            return choose_tools(kwargs["context"])
        return {"action": "approve", "diagnosis": "Handoff is complete", "decision_id": f"approve-{len(calls)}"}

    coordinator._request = request
    return coordinator, calls


def run_writer_boundary(coordinator, root, produce, archive=None, task_id="t"):
    sandbox = root / task_id
    sandbox.mkdir(exist_ok=True)
    with supervisor_scope(coordinator):
        return runner._supervise_writer_delivery(
            node_id=f"writer:{task_id}:round:1", produce=produce, archive=archive or Mock(),
            tasks=[{"task_id": task_id, "goal": "compare the fixed two points"}],
            sandbox=sandbox, analysis_snapshot_hash="fixed-analysis",
        )


def delivery(*, passed=True, completed=True):
    return {"ok": True}, [{"task_id": "t", "writer_completed": completed,
                           "host_execution": {"passed": passed, "run_id": "host-owned"},
                           "result_json": {"reported_outcome": "not_reproduced"}}]


def test_writer_receipt_anomaly_reaches_reporter_without_moderator_gate(tmp_path):
    coordinator, calls = supervisor(tmp_path)
    original = delivery(passed=False)
    archive = Mock()
    result = run_writer_boundary(coordinator, tmp_path, lambda *_: original, archive)
    assert not result[1][0].get("supervisor_blocked")
    assert result[1][0]["host_execution"]["passed"] is False
    assert not calls
    archive.assert_not_called()


def test_added_writer_draft_is_observed_without_blocking_paper_handoff(tmp_path):
    paper = tmp_path / "paper_evidence"
    paper.mkdir()
    (paper / "source.pdf").write_bytes(b"original paper")
    before = trusted_input_snapshot(tmp_path, ("paper_evidence",))
    (paper / "draft_zoom.png").write_bytes(b"writer crop")
    status = runner._inspect_task_writer_completion(
        status={"ok": True}, sandbox=tmp_path, audit_dir=tmp_path / "audit",
        case_runtime=None, request_source="writer:t", require_execution_receipt=False,
        evidence_before=before)
    assert status["ok"] is True
    assert status["paper_evidence_added_files"] == ["paper_evidence/draft_zoom.png"]
    record = _collect_task_writer_delivery(
        index=1, task={"task_id": "t"},
        manifest_entry={"task_id": "t", "output_subdir": "t"},
        sandbox=tmp_path, writer_status=status)
    assert record["writer_observations"]["paper_evidence_added_files"] == status["paper_evidence_added_files"]


def test_changed_original_paper_is_reported_as_paper_change(tmp_path):
    paper = tmp_path / "paper_evidence"
    paper.mkdir()
    original = paper / "source.pdf"
    original.write_bytes(b"original paper")
    before = trusted_input_snapshot(tmp_path, ("paper_evidence",))
    original.write_bytes(b"changed paper")
    status = runner._inspect_task_writer_completion(
        status={"ok": True}, sandbox=tmp_path, audit_dir=tmp_path / "audit",
        case_runtime=None, request_source="writer:t", require_execution_receipt=False,
        evidence_before=before)
    assert status["error_kind"] == "evidence_modified"
    assert status["paper_evidence_changed_files"] == ["paper_evidence/source.pdf"]


def test_missing_writer_delivery_returns_to_same_owner(tmp_path):
    advice = {"action": "retry", "diagnosis": "The required full receipt is missing",
              "instructions": "Finish the existing full execution and bind its receipt",
              "expected_change": "The host validates a current full receipt", "decision_id": "receipt-repair"}
    coordinator, _ = supervisor(tmp_path, lambda _node, kwargs: advice if kwargs["trigger"] == "node_failed" else None)
    attempts = []
    def produce(instruction, attempt):
        attempts.append((instruction, attempt))
        return delivery(passed=attempt == 2, completed=attempt == 2)
    archive = Mock()
    result = run_writer_boundary(coordinator, tmp_path, produce, archive)
    assert attempts == [(None, 1), ({**advice, "preserved_full_receipts": {}}, 2)]
    assert archive.call_count == 1
    assert result[1][0]["host_execution"]["passed"] is True


def test_writer_dispatch_assignment_reaches_original_owner(tmp_path):
    coordinator, _ = supervisor(tmp_path)
    advice = {"action": "start", "next_node": "writer:t",
              "instructions": "Repair the missing task import without changing the paper parameters",
              "expected_change": "The existing task entrypoint loads"}
    coordinator._instructions["tool:writers:writer:t"] = advice
    seen = []
    def produce(instruction, attempt):
        seen.append(instruction)
        return delivery()
    run_writer_boundary(coordinator, tmp_path, produce)
    assert seen == [advice]


def test_two_writer_handoffs_continue_without_serializing_workers(tmp_path):
    barrier = Barrier(2)
    def produce(*_args):
        barrier.wait(timeout=5)
        return delivery()
    coordinator, calls = supervisor(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(run_writer_boundary, coordinator, tmp_path,
                    produce, task_id=task_id) for task_id in ("a", "b")]
        results = [future.result(timeout=10) for future in futures]
    assert not calls
    assert all(not result[1][0].get("supervisor_blocked") for result in results)


def test_interrupted_writer_handoff_recollects_receipts_without_repeating_model(tmp_path):
    first, _ = supervisor(tmp_path)
    attempts = []
    def produce(_instruction, attempt):
        attempts.append(attempt)
        return delivery()
    run_writer_boundary(first, tmp_path, produce)
    first._save("writer:t:round:1", status="dispatched")
    resumed, _ = supervisor(tmp_path)
    run_writer_boundary(resumed, tmp_path, produce)
    assert attempts == [1, 0]  # Zero is read-only collection, not another Writer.


class WriterReached(BaseException):
    pass


def execution_inputs(root):
    output = root / "case"
    audit = output / "audit"
    audit.mkdir(parents=True)
    context = SimpleNamespace(output_dir=output, audit_dir=audit,
        options=SimpleNamespace(resume=True, run_repro=True, run_timeout=30),
        progress_tracker=SimpleNamespace(reporter=None), begin=Mock(), mark=Mock())
    analysis = SimpleNamespace(paper={}, paper_path=root / "paper.md", facts={},
        tasks={"repro_tasks": [{"task_id": "t", "goal": "BER at two points"}]},
        experiment_index={}, paper_thesis={}, paper_images=[], figure_index={},
        scientific_architecture={"components": []}, execution_plan={},
        repro_project_dir=output / "repro_project", paper_context="synthetic")
    runtime = SimpleNamespace(environment_hash="env", python_executable=Path("python.exe"))
    return context, analysis, runtime


def test_execution_handoff_keeps_actual_packaging_observations(monkeypatch, tmp_path):
    context, analysis, runtime = execution_inputs(tmp_path)
    validation = {"required_files_present": False, "python_compiles": None,
                  "missing_files": ["declared_optional_note.txt"], "host_validation_skipped": True}
    monkeypatch.setattr("geng_agent.case_runtime.ensure_case_runtime", Mock(return_value=runtime))
    monkeypatch.setattr("geng_agent.agentic_foundation.run_codex_foundation_writer_workflow", Mock(return_value=None))
    monkeypatch.setattr("geng_agent.agentic_task_writers.run_codex_task_writer_workflow", Mock(return_value={
        "manifest": {}, "task_records": [], "runtime_result": {
            "enabled": True, "passed": False, "delivery_status": "partial", "validation": validation}}))
    coordinator, _ = supervisor(context.output_dir)
    with supervisor_scope(coordinator):
        result = flow.run_execution_flow(context, analysis)
    assert result.validation is validation
    assert result.risk_report["validation"] is validation
    assert not any(item["type"] == "generated_code_compile_error" for item in result.risk_report["findings"])


def test_initial_foundation_hard_failure_is_repaired_by_original_owner(monkeypatch, tmp_path):
    context, analysis, runtime = execution_inputs(tmp_path)
    foundation = Mock(side_effect=[RuntimeError("invalid import"), {"snapshot_hash": "verified", "manifest": {}}])
    monkeypatch.setattr("geng_agent.case_runtime.ensure_case_runtime", Mock(return_value=runtime))
    monkeypatch.setattr("geng_agent.agentic_foundation.run_codex_foundation_writer_workflow", foundation)
    monkeypatch.setattr("geng_agent.agentic_task_writers.run_codex_task_writer_workflow", Mock(side_effect=WriterReached))
    advice = {"action": "start", "next_node": "foundation", "diagnosis": "Import refers to a missing local module",
              "instructions": "Correct the import and rerun shared tests", "expected_change": "Contract tests pass",
              "decision_id": "foundation-repair"}
    coordinator, calls = supervisor(context.output_dir,
        lambda node, kwargs: advice if node == "tools:execution" and kwargs["context"]["state"]["failures"].get("foundation") else None)
    with supervisor_scope(coordinator), pytest.raises(WriterReached):
        flow.run_execution_flow(context, analysis)
    assert foundation.call_count == 2
    assert foundation.call_args.kwargs["resume"] is True
    assert foundation.call_args.kwargs["recovery_instructions"] == advice
    assert any(kwargs["context"]["state"]["failures"].get("foundation")
               for node, kwargs in calls if node == "tools:execution")


def test_environment_first_attempt_honors_fresh_run_and_retry_preserves_state(monkeypatch, tmp_path):
    context, analysis, runtime = execution_inputs(tmp_path)
    context.options.resume = False
    environment = Mock(side_effect=[RuntimeError("probe temporarily unavailable"), runtime])
    monkeypatch.setattr("geng_agent.case_runtime.ensure_case_runtime", environment)
    monkeypatch.setattr("geng_agent.agentic_foundation.run_codex_foundation_writer_workflow", Mock(return_value=None))
    monkeypatch.setattr("geng_agent.agentic_task_writers.run_codex_task_writer_workflow", Mock(side_effect=WriterReached))
    coordinator, _ = supervisor(context.output_dir, lambda node, kwargs:
        {"action": "start", "next_node": "environment", "instructions": "Repeat the failed probe; preserve installed packages"}
        if node == "tools:execution" and kwargs["context"]["state"]["failures"].get("environment") else None)
    with supervisor_scope(coordinator), pytest.raises(WriterReached):
        flow.run_execution_flow(context, analysis)
    assert [call.kwargs["resume"] for call in environment.call_args_list] == [False, True]


def test_resumed_execution_rehydrates_foundation_environment_request(monkeypatch, tmp_path):
    from geng_agent.outputs import write_json
    context, analysis, runtime = execution_inputs(tmp_path)
    write_json(context.audit_dir / "03a_pending_environment.json", {
        "schema_version": 1,
        "state": "awaiting_environment",
        "source": "foundation_writer",
        "requests": [{"requirement": "scipy>=1.11", "import_names": ["scipy"],
                      "requested_by": "foundation_writer", "reason": "fixture"}],
    })
    environment = Mock(return_value=runtime)
    monkeypatch.setattr("geng_agent.case_runtime.ensure_case_runtime", environment)
    monkeypatch.setattr("geng_agent.agentic_foundation.run_codex_foundation_writer_workflow",
                        Mock(return_value=None))
    monkeypatch.setattr("geng_agent.agentic_task_writers.run_codex_task_writer_workflow",
                        Mock(side_effect=WriterReached))
    coordinator, _ = supervisor(context.output_dir)
    with supervisor_scope(coordinator), pytest.raises(WriterReached):
        flow.run_execution_flow(context, analysis)
    requested = environment.call_args.kwargs["extra_requirements"]
    assert [item.requirement for item in requested] == ["scipy>=1.11"]
    pending = (context.audit_dir / "03a_pending_environment.json").read_text(encoding="utf-8")
    assert '"state": "resolved"' in pending


def test_foundation_block_preserves_files_and_does_not_start_dependent_writers(monkeypatch, tmp_path):
    context, analysis, runtime = execution_inputs(tmp_path)
    preserved = context.audit_dir / "failed-source.py"
    preserved.write_text("raise ImportError('missing')", encoding="utf-8")
    monkeypatch.setattr("geng_agent.case_runtime.ensure_case_runtime", Mock(return_value=runtime))
    monkeypatch.setattr("geng_agent.agentic_foundation.run_codex_foundation_writer_workflow", Mock(side_effect=RuntimeError("missing")))
    writer = Mock()
    monkeypatch.setattr("geng_agent.agentic_task_writers.run_codex_task_writer_workflow", writer)
    coordinator, _ = supervisor(context.output_dir, lambda node, kwargs:
        {"action": "finish", "diagnosis": "Required external dependency unavailable"}
        if node == "tools:execution" and kwargs["context"]["state"]["failures"].get("foundation") else None)
    with supervisor_scope(coordinator), pytest.raises(StageBlocked):
        flow.run_execution_flow(context, analysis)
    writer.assert_not_called()
    assert preserved.read_text(encoding="utf-8") == "raise ImportError('missing')"
    assert (context.audit_dir / "execution_tool_failures.json").is_file()


def test_reporter_failure_is_recorded_without_forcing_a_repair(monkeypatch, tmp_path):
    context, analysis, runtime = execution_inputs(tmp_path)
    monkeypatch.setattr("geng_agent.case_runtime.ensure_case_runtime", Mock(return_value=runtime))
    monkeypatch.setattr("geng_agent.agentic_foundation.run_codex_foundation_writer_workflow", Mock(return_value=None))
    reporter = Mock(return_value={"ok": False, "error": "incomplete JSON", "recovery_kind": "structure"})
    monkeypatch.setattr("geng_agent.agentic_task_reporters.run_codex_task_reporter_workflow", reporter)
    result = []
    def writer(**kwargs):
        result.append(kwargs["task_review_callback"](1, {"task_id": "t"},
            {"task_id": "t", "sandbox": str(context.audit_dir), "writer_completed": True}, 1))
        raise WriterReached()
    monkeypatch.setattr("geng_agent.agentic_task_writers.run_codex_task_writer_workflow", writer)
    moderator = Mock(side_effect=AssertionError("a reportable review failure must not force repair"))
    coordinator, _ = supervisor(context.output_dir, moderator)
    with supervisor_scope(coordinator), pytest.raises(WriterReached):
        flow.run_execution_flow(context, analysis)
    reporter.assert_called_once()
    moderator.assert_not_called()
    assert result[0]["ok"] is False
    assert result[0]["error"] == "incomplete JSON"


def test_packaging_block_retains_current_run_scientific_results_for_partial_delivery(monkeypatch, tmp_path):
    from geng_agent import agentic_task_writers as writers
    from geng_agent.outputs import write_json
    from tests.test_agentic_task_writers import _pair, _delivery

    output = tmp_path / "case"
    audit = output / "audit"
    paper = tmp_path / "paper.md"
    paper.write_text("Synthetic two-point communications calculation", encoding="utf-8")
    task, _entry = _pair("task_1")
    tasks = {"repro_tasks": [task]}
    write_json(output / "engineering_facts.json", {"engineering_facts": []})
    write_json(output / "repro_tasks.json", tasks)
    write_json(output / "experiment_index.json", {"experiments": []})
    record = _delivery("task_1", audit / "03c_task_writer_sandboxes" / "01_task_1")
    record.update(index=1, writer_session_count=1,
                  task_verification={"task_id": "task_1", "outcome": "not_reproduced", "host_action": "complete"})
    csv = Path(record["sandbox"]) / "outputs" / "result.csv"
    def dispatched(**_kwargs):
        csv.parent.mkdir(parents=True, exist_ok=True)
        csv.write_text("snr,ber\n0,0.12\n", encoding="utf-8")
        return [record], {}
    dispatch = Mock(side_effect=dispatched)
    monkeypatch.setattr(writers, "_dispatch_task_writers", dispatch)
    monkeypatch.setattr(writers, "_merge_task_writer_deliveries", Mock(side_effect=OSError("copy failed")))
    coordinator, calls = supervisor(output, lambda node, kwargs:
        {"action": "block", "diagnosis": "Cannot assemble the project on the current volume"}
        if node == "packaging" else None)
    with supervisor_scope(coordinator):
        result = writers.run_codex_task_writer_workflow(
            facts={"engineering_facts": []}, tasks=tasks, experiment_index={"experiments": []},
            paper={}, paper_path=paper, paper_context_json="", paper_images=[], paper_thesis=None,
            output_dir=output, audit_dir=audit, repro_project_dir=output / "repro_project",
            run_repro=True, resume=False,
        )
    assert dispatch.call_count == 1
    assert result["task_records"][0] is record
    assert result["task_records"][0]["task_verification"]["outcome"] == "not_reproduced"
    assert csv.read_text(encoding="utf-8") == "snr,ber\n0,0.12\n"
    assert result["runtime_result"]["delivery_status"] == "partial"
    assert result["runtime_result"]["engineering_failures"][0]["node_id"] == "packaging"
    assert result["manifest"]["files"] == []
    assert calls[-1][1]["trigger"] == "node_failed"
