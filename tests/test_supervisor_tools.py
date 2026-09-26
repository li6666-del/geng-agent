"""Real tool controller, simulated decisions. No model or scientific claims."""
from contextvars import ContextVar
import json
from threading import Barrier, Event
import time

import pytest

from geng_agent.supervisor import RunSupervisor, StageBlocked
from geng_agent.supervisor_tools import SupervisorTool
from geng_agent.progress import PipelineCancelled


def controller(tmp_path, monkeypatch, choose):
    supervisor = RunSupervisor(tmp_path, tmp_path / "audit", {"paper": "fixed source"})
    calls = []

    def model(**kwargs):
        packet = json.loads((kwargs["work_dir"] / "incident.json").read_text(encoding="utf-8"))
        context = packet["context"]
        calls.append(context)
        decision = {"schema_version": "1.0", "action": "finish", "diagnosis": "测试调度决定",
                    "instructions": "只执行指定工具，保留实际失败", "component_ids": [],
                    "evidence_refs": [{"root": "handoff", "path": "handoff.json"}],
                    "expected_change": "实际工具结果进入账本", **choose(context)}
        (kwargs["audit_dir"] / "moderator_last_message.txt").write_text(json.dumps(decision), encoding="utf-8")
        return {"ok": True}

    monkeypatch.setattr("geng_agent.moderator.run_codex_subprocess", model)
    return supervisor, calls


def batch_or_finish(context):
    if context["ready"]:
        return {"action": "start", "next_nodes": context["ready"]}
    return {"action": "wait" if context["active"] else "finish"}


def test_independent_tools_actually_overlap_and_keep_context(tmp_path, monkeypatch):
    supervisor, calls = controller(tmp_path, monkeypatch, batch_or_finish)
    rendezvous = Barrier(2)
    variable = ContextVar("selected_model", default="wrong")
    variable.set("unified-profile")
    results = {}

    def operate(_decision):
        rendezvous.wait(timeout=5)
        return variable.get()

    tools = [SupervisorTool(name, name, operate, resources=(name,)) for name in ("a", "b")]
    decision = supervisor.tools.run("test", lambda: [t for t in tools if t.name not in results],
        state=lambda: results, on_result=lambda name, value: results.update({name: value}),
        on_error=lambda name, error: pytest.fail(f"{name}: {error}"))
    assert results == {"a": "unified-profile", "b": "unified-profile"}
    assert decision["action"] == "finish"
    assert calls[0]["ready"] == ["a", "b"]
    events = [json.loads(path.read_text(encoding="utf-8")) for path in supervisor.tools.events.glob("*.json")]
    assert len([event for event in events if event["kind"] == "tool_completed"]) == 2


def test_model_can_choose_order_and_finish_with_unattempted_work(tmp_path, monkeypatch):
    results = []
    def choose(context):
        return {"action": "start", "next_node": "second"} if not results else {"action": "finish"}
    supervisor, _ = controller(tmp_path, monkeypatch, choose)
    tools = [SupervisorTool(name, name, lambda _, name=name: name) for name in ("first", "second")]
    supervisor.tools.run("test", lambda: [t for t in tools if t.name not in results], state=lambda: {},
        on_result=lambda name, value: results.append(value), on_error=lambda *_: pytest.fail("unexpected error"))
    assert results == ["second"]
    assert supervisor.tools.status("test", "first") == {}


def test_failure_does_not_drop_sibling_or_force_retry(tmp_path, monkeypatch):
    results, failures = {}, {}
    def choose(context):
        ready = [name for name in context["ready"] if name not in failures]
        return {"action": "start", "next_nodes": ready} if ready else {"action": "wait" if context["active"] else "finish"}
    supervisor, _ = controller(tmp_path, monkeypatch, choose)
    def fail(_):
        raise OSError("missing GPU")
    tools = [SupervisorTool("bad", "bad", fail), SupervisorTool("good", "good", lambda _: "valid receipt")]
    supervisor.tools.run("test", lambda: [t for t in tools if t.name not in results], state=lambda: failures,
        on_result=lambda name, value: results.update({name: value}),
        on_error=lambda name, error: failures.update({name: str(error)}))
    assert results == {"good": "valid receipt"}
    assert failures == {"bad": "missing GPU"}
    assert supervisor.tools.status("test", "bad")["attempt"] == 1


def test_interrupted_side_effect_uses_reconciler_not_fresh_invoke(tmp_path):
    supervisor = RunSupervisor(tmp_path, tmp_path / "audit", {"fixed": 1})
    calls = []
    def interrupted(_):
        calls.append("execute")
        raise KeyboardInterrupt()
    tool = SupervisorTool("full", "full", interrupted, resume=lambda _: calls.append("reconcile") or "existing receipt")
    with pytest.raises(KeyboardInterrupt):
        supervisor.tools.call("test", tool, {"action": "start"})
    restored = RunSupervisor(tmp_path, tmp_path / "audit", {"fixed": 1})
    assert restored.tools.call("test", tool, {"action": "start"}) == "existing receipt"
    assert calls == ["execute", "reconcile"]


def test_unknown_interrupted_side_effect_is_not_replayed_even_after_input_change(tmp_path):
    supervisor = RunSupervisor(tmp_path, tmp_path / "audit", {"fixed": 1})
    def interrupted(_):
        raise KeyboardInterrupt()
    tool = SupervisorTool("full", "full", interrupted)
    with pytest.raises(KeyboardInterrupt):
        supervisor.tools.call("test", tool, {})
    tool.inputs = {"new_version": 2}
    tool.invoke = lambda _: pytest.fail("must reconcile before another side effect")
    from geng_agent.supervisor import NodeFailure
    with pytest.raises(NodeFailure, match="中断"):
        supervisor.tools.call("test", tool, {})


def test_resource_conflict_is_split_into_sequential_batches(tmp_path, monkeypatch):
    supervisor, _ = controller(tmp_path, monkeypatch, batch_or_finish)
    results, active, peak = {}, [], []
    def work(name):
        active.append(name)
        peak.append(len(active))
        time.sleep(.01)
        active.remove(name)
        return name
    tools = [SupervisorTool(name, name, lambda _, name=name: work(name),
                            resources=("shared-checkpoint",)) for name in ("a", "b")]
    supervisor.tools.run("test", lambda: [t for t in tools if t.name not in results],
        state=lambda: {}, on_result=lambda name,value:results.update({name:value}),
        on_error=lambda name,error:pytest.fail(str(error)))
    assert results == {"a":"a","b":"b"}
    assert max(peak) == 1


def test_heartbeat_wakes_without_restarting_live_tool(tmp_path, monkeypatch):
    started = []
    results = {}
    supervisor, calls = controller(tmp_path, monkeypatch, batch_or_finish)
    def work(_):
        started.append(1)
        time.sleep(0.06)
        return "completed"
    tool = SupervisorTool("slow", "slow", work)
    supervisor.tools.run("test", lambda: [] if results else [tool], state=lambda: {},
        on_result=lambda name, value: results.update({name: value}), on_error=lambda *_: pytest.fail("failure"),
        check_interval=0.01, poll_seconds=0.005)
    assert started == [1] and results == {"slow": "completed"}
    assert not any(packet["active"] == ["slow"] for packet in calls)


def test_cancel_does_not_become_repair_or_start_another_tool(tmp_path, monkeypatch):
    supervisor, calls = controller(tmp_path, monkeypatch, batch_or_finish)
    def cancelled(_):
        raise PipelineCancelled("user stop")
    with pytest.raises(PipelineCancelled):
        supervisor.tools.run("test", lambda: [SupervisorTool("task", "task", cancelled)], state=lambda: {},
            on_result=lambda *_: pytest.fail("not completed"), on_error=lambda *_: pytest.fail("not a repair"))
    assert len(calls) == 1


def test_reducer_failure_is_reported_without_losing_other_completions(tmp_path, monkeypatch):
    results, errors = {}, {}
    def choose(context):
        if not results and not errors:
            return batch_or_finish(context)
        return {"action": "wait" if context["active"] else "finish"}
    supervisor, _ = controller(tmp_path, monkeypatch, choose)
    tools = [SupervisorTool(name, name, lambda _: 1) for name in ("a", "b")]
    def receive(name, value):
        if name == "a":
            raise ValueError("host reducer failed")
        results[name] = value
    supervisor.tools.run("test", lambda: [t for t in tools if t.name not in results and t.name not in errors],
        state=lambda: {}, on_result=receive, on_error=lambda name, exc: errors.update({name: str(exc)}))
    assert results == {"b": 1} and errors == {"a": "host reducer failed"}


def test_tool_addresses_with_colons_have_valid_deduplicated_evidence_roots(tmp_path, monkeypatch):
    supervisor, calls = controller(tmp_path, monkeypatch, batch_or_finish)
    source = tmp_path / "facts.json"
    source.write_text('{"source": "paper"}', encoding="utf-8")
    results = {}
    tools = [SupervisorTool(name, name, lambda _: True, evidence={"facts": source})
             for name in ("writer:unit:a", "reporter:task:b")]
    decision = supervisor.tools.run("test", lambda: [t for t in tools if t.name not in results], state=lambda: {},
        on_result=lambda name, value: results.update({name: value}), on_error=lambda *_: pytest.fail("unexpected"))
    assert decision["action"] == "finish" and len(results) == 2
    roots = [mapping["facts"] for mapping in calls[0]["tool_evidence"].values()]
    assert roots[0] == roots[1] and ":" not in roots[0]


def test_inner_controller_owns_heartbeat_without_duplicate_outer_wakes(tmp_path, monkeypatch):
    outer_results, inner_results = {}, {}
    supervisor, calls = controller(tmp_path, monkeypatch, batch_or_finish)
    def work(_):
        time.sleep(0.12)
        return "observed"
    def nested(_):
        return supervisor.tools.run("inner", lambda: [] if inner_results else [SupervisorTool("work", "work", work)],
            state=lambda: {}, on_result=lambda n, v: inner_results.update({n: v}),
            on_error=lambda *_: pytest.fail("inner failed"), check_interval=0.08, poll_seconds=0.005)
    supervisor.tools.run("outer", lambda: [] if outer_results else [SupervisorTool("nested", "nested", nested)],
        state=lambda: {}, on_result=lambda n, v: outer_results.update({n: v}),
        on_error=lambda *_: pytest.fail("outer failed"), check_interval=0.08, poll_seconds=0.005)
    assert inner_results == {"work": "observed"}
    assert not any(packet["active"] == ["nested"] for packet in calls)


def test_model_process_observation_collects_actual_output_and_exit(tmp_path):
    import sys
    from geng_agent.supervisor_process import run_observed_process
    supervisor = RunSupervisor(tmp_path, tmp_path / "audit", {"synthetic": True})
    result = run_observed_process([sys.executable, "-u", "-c",
        "import sys,time; s=sys.stdin.read(); print(s,flush=True); time.sleep(.12); sys.exit(7)"],
        supervisor=supervisor, label="offline-child", cwd=tmp_path, env=None, input="test input", check_interval=.02)
    assert result.returncode == 7 and result.stdout.strip() == "test input"
    record = supervisor.tools.process_observations()[0]
    assert record["status"] == "completed" and record["returncode"] == 7
    assert record["output_bytes"] > 0


def test_cancel_reaches_real_child_process_and_keeps_interrupted_journal(tmp_path):
    import sys
    from geng_agent.supervisor_process import run_observed_process
    class StopWhenReady:
        def check_cancelled(self):
            if (tmp_path / "ready").exists():
                raise PipelineCancelled("test stop")
    supervisor = RunSupervisor(tmp_path, tmp_path / "audit", {"synthetic": True}, reporter=StopWhenReady())
    def run(_):
        return run_observed_process([sys.executable, "-u", "-c",
            "from pathlib import Path; import time; print('preserved output',flush=True); Path('ready').touch(); time.sleep(20)"],
            supervisor=supervisor, label="offline-child", cwd=tmp_path, env=None, input="", check_interval=.02)
    started = time.monotonic()
    with pytest.raises(PipelineCancelled) as caught:
        supervisor.tools.call("test", SupervisorTool("child", "child", run), {})
    assert time.monotonic() - started < 10
    assert "preserved output" in caught.value.stdout
    record = supervisor.tools.process_observations()[0]
    assert record["status"] == "cancelled" and record["returncode"] is not None
    assert supervisor.tools.status("test", "child")["status"] == "interrupted"


def test_project_and_execution_controllers_deliver_negative_result_with_real_moderator_protocol(tmp_path, monkeypatch):
    from unittest.mock import Mock
    from geng_agent.pipeline_execution_flow import run_execution_flow
    from geng_agent.pipeline_supervision import run_supervised_pipeline
    from geng_agent.pipeline_models import PipelineResult
    from tests.test_execution_supervision import execution_inputs
    context, analysis, runtime = execution_inputs(tmp_path)
    context.run_id = "synthetic-current"
    context.options.analysis_only = False
    verdict = {"task_id": "t", "outcome": "not_reproduced", "host_action": "complete"}
    def choose(packet):
        if "ready" not in packet:
            return {"action": "approve"}
        if packet["state"].get("delivery_available"):
            return {"action": "finish"}
        ready = [n for n in packet["ready"] if not n.startswith("revise_")]
        return {"action": "start", "next_node": ready[0]} if ready else {"action": "wait" if packet["active"] else "finish"}
    supervisor, calls = controller(context.output_dir, monkeypatch, choose)
    monkeypatch.setattr("geng_agent.case_runtime.ensure_case_runtime", Mock(return_value=runtime))
    reviewer = Mock(return_value={"ok": True, "task_verification": verdict})
    monkeypatch.setattr("geng_agent.agentic_task_reporters.run_codex_task_reporter_workflow", reviewer)
    def writer(**kwargs):
        record = {"task_id": "t", "writer_completed": True, "sandbox": str(context.audit_dir),
                  "host_execution": {"passed": True}}
        review = kwargs["task_review_callback"](1, {"task_id": "t"}, record, 1)
        record["task_verification"] = review["task_verification"]
        return {"manifest": {}, "task_records": [record], "runtime_result": {
            "enabled": True, "passed": True, "delivery_status": "complete", "scientific_all_successful": False}}
    monkeypatch.setattr("geng_agent.agentic_task_writers.run_codex_task_writer_workflow", writer)
    def report(_analysis, execution):
        assert execution.task_records[0]["task_verification"] == verdict
        for name in ("result_review.md", "reproduction_report.md"):
            (context.output_dir / name).write_text("合成离线案例：未复现，保留原结论。", encoding="utf-8")
        return PipelineResult(output_dir=context.output_dir, review_path=context.output_dir / "review.md",
            repro_project_dir=analysis.repro_project_dir, risk_report_path=context.output_dir / "risk_report.json",
            result_review_passed=True)
    result = run_supervised_pipeline(context=context, supervisor=supervisor, analyze=lambda: analysis,
        execute=lambda value: run_execution_flow(context, value), report=report, finish_analysis=lambda _: None)
    assert result.delivery_status == "complete" and reviewer.call_count == 1
    assert not calls
    assert supervisor.tools.status("nodes", "reporter:t:round:1")["status"] == "completed"
    outcome = json.loads((context.audit_dir / "supervisor" / "outcome.json").read_text(encoding="utf-8"))
    assert outcome["reports_accepted"] and outcome["delivery_status"] == "complete"


def test_phase_retry_uses_resume_and_preserves_first_attempt_files(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from geng_agent.pipeline_supervision import run_supervised_pipeline
    from geng_agent.pipeline_models import PipelineResult
    context = SimpleNamespace(output_dir=tmp_path, audit_dir=tmp_path / "audit", run_id="retry",
        options=SimpleNamespace(analysis_only=True, run_repro=False, resume=False))
    seen = []
    def choose(packet):
        if packet["state"]["delivery_available"]:
            return {"action": "finish"}
        ready = [name for name in packet["ready"] if not name.startswith("revise_")]
        return {"action": "start", "next_node": ready[0]}
    supervisor, _ = controller(tmp_path, monkeypatch, choose)
    def analyze():
        seen.append(context.options.resume)
        if len(seen) == 1:
            (tmp_path / "preserved-evidence.txt").write_text("original", encoding="utf-8")
            raise RuntimeError("a repairable handoff failure")
        assert (tmp_path / "preserved-evidence.txt").read_text(encoding="utf-8") == "original"
        return SimpleNamespace(tasks={"repro_tasks": []})
    def finish(_):
        return PipelineResult(output_dir=tmp_path, review_path=tmp_path / "review.md",
            repro_project_dir=tmp_path / "repro_project", risk_report_path=tmp_path / "risk_report.json")
    result = run_supervised_pipeline(context=context, supervisor=supervisor, analyze=analyze,
        execute=lambda _: pytest.fail("analysis only"), report=lambda *_: pytest.fail("analysis only"),
        finish_analysis=finish)
    assert seen == [False, True] and context.options.resume is False
    assert result.delivery_status == "complete"


def test_cancelled_model_call_writes_actual_status_before_propagating(tmp_path, monkeypatch):
    import subprocess
    from geng_agent.codex_runner import run_codex_subprocess, _clear_ephemeral_capability_cache
    from geng_agent.supervisor import supervisor_scope
    supervisor = RunSupervisor(tmp_path, tmp_path / "audit", {"synthetic": True})
    monkeypatch.setattr("geng_agent.codex_runner.shutil.which", lambda _: "C:/offline/codex.exe")
    monkeypatch.setattr("geng_agent.codex_runner.get_config_value", lambda _: None)
    monkeypatch.setattr("geng_agent.codex_runner.subprocess.run", lambda command, **_: subprocess.CompletedProcess(
        command, 0, "Usage: codex exec\n --ephemeral", ""))
    def cancel(*args, **kwargs):
        error = PipelineCancelled("offline user stop")
        error.stdout, error.stderr, error.returncode = "output before stop", "", 1
        raise error
    monkeypatch.setattr("geng_agent.supervisor_process.run_observed_process", cancel)
    _clear_ephemeral_capability_cache()
    with supervisor_scope(supervisor), pytest.raises(PipelineCancelled, match="offline user stop"):
        run_codex_subprocess(role="test", work_dir=tmp_path, prompt="offline", audit_dir=tmp_path / "worker_audit",
            label="cancelled", sandbox="read-only", command_override="C:/offline/codex.exe")
    status = json.loads((tmp_path / "worker_audit" / "cancelled.json").read_text(encoding="utf-8"))
    assert status["error_kind"] == "cancelled" and status["ok"] is False
    assert "output before stop" in (tmp_path / "worker_audit" / "cancelled_transcript.txt").read_text(encoding="utf-8")
