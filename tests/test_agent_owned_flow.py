"""No model calls: real local processes and agent-boundary regression tests."""
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from geng_agent.execution_plan import compile_execution_plan
from geng_agent.execution_receipts import ExecutionBroker
from geng_agent.outputs import write_json
from geng_agent.progress import PipelineCancelled
from geng_agent.supervisor import RunSupervisor, supervisor_scope
from geng_agent.supervisor_process import run_observed_process
from geng_agent.task_writer_runner import _supervise_writer_delivery, _inspect_task_writer_completion
from geng_agent.task_writer_units import _execution_unit_work_items


def test_planner_merged_task_keeps_every_goal_and_is_one_dispatch():
    task = {"task_id": "combined", "target": "accuracy and ranking",
            "experiments": [{"id": "accuracy"}, {"id": "ranking"}],
            "scientific_acceptance": {"core_conclusions": ["accuracy", "ranking"]}}
    plan = compile_execution_plan({"repro_tasks": [task]})
    work = _execution_unit_work_items([(task, {"task_id": "combined"})], plan)
    assert len(work) == 1
    assert work[0]["members"][0][1] == task
    assert plan["execution_unit_count"] == 1


def test_legacy_strong_relationship_does_not_regroup_final_tasks():
    tasks = [{"task_id": "a"}, {"task_id": "b"}]
    plan = compile_execution_plan({"repro_tasks": tasks, "execution_relationships": [
        {"strength": "strong", "task_ids": ["a", "b"]}]})
    assert [unit["task_ids"] for unit in plan["execution_units"]] == [["a"], ["b"]]
    old = {"execution_units": [{"unit_id": "old", "task_ids": ["a", "b"]}]}
    assert len(_execution_unit_work_items([(t, t) for t in tasks], old)) == 2


def test_observation_failure_does_not_kill_real_model_process(tmp_path):
    marker = tmp_path / "finished.txt"
    tools = SimpleNamespace(observe_process=Mock(side_effect=OSError("display unavailable")))
    supervisor = SimpleNamespace(tools=tools, _check_cancelled=lambda: None)
    command = [sys.executable, "-c", "import time,pathlib; time.sleep(.12); pathlib.Path('finished.txt').write_text('done'); print('done')"]
    result = run_observed_process(command, supervisor=supervisor, label="fixture", cwd=tmp_path,
                                  env=os.environ.copy(), input="", check_interval=.02)
    assert result.returncode == 0 and marker.read_text() == "done"
    assert "done" in result.stdout
    assert result.observation_errors


def test_user_stop_still_terminates_owned_process(tmp_path):
    checks = 0
    def cancel():
        nonlocal checks
        checks += 1
        if checks > 2:
            raise PipelineCancelled("user stopped")
    supervisor = SimpleNamespace(tools=SimpleNamespace(observe_process=Mock()), _check_cancelled=cancel)
    with pytest.raises(PipelineCancelled):
        run_observed_process([sys.executable, "-c", "import time; time.sleep(30)"],
            supervisor=supervisor, label="fixture", cwd=tmp_path, env=os.environ.copy(), input="", check_interval=.02)


def test_broker_finishes_inflight_work_after_unrelated_host_exception(tmp_path):
    broker = ExecutionBroker(tmp_path, tmp_path / "audit", Path(sys.executable))
    finished = Event()
    broker.thread = Thread(target=lambda: (Event().wait(.05), finished.set()))
    broker.heartbeat_thread = Thread(target=lambda: None)
    broker.thread.start()
    broker.heartbeat_thread.start()
    broker._stop_process = Mock()
    broker.__exit__(ValueError, ValueError("bad status record"), None)
    assert finished.is_set()
    assert not broker.cancelled.is_set()
    broker._stop_process.assert_not_called()


def test_incomplete_writer_handoff_does_not_request_approval(tmp_path):
    supervisor = RunSupervisor(tmp_path, tmp_path / "audit", {"task": "t"})
    supervisor._request = Mock(side_effect=AssertionError("unnecessary moderator approval"))
    raw = ({"ok": False, "error": "worker exit"}, [{"task_id": "t", "writer_completed": False}])
    with supervisor_scope(supervisor):
        result = _supervise_writer_delivery(node_id="writer:t", produce=lambda *_: raw,
            archive=Mock(), tasks=[{"task_id": "t"}], sandbox=tmp_path,
            analysis_snapshot_hash="a")
    assert result == raw
    supervisor._request.assert_not_called()


def test_moderator_can_choose_more_than_three_repairs(tmp_path):
    supervisor = RunSupervisor(tmp_path, tmp_path / "audit", {"task": "t"})
    supervisor._request = Mock(return_value={"action": "retry", "instructions": "repair"})
    calls = 0
    def work():
        nonlocal calls
        calls += 1
        if calls <= 5:
            raise OSError("fixture transport unavailable")
        return "done"
    with supervisor_scope(supervisor):
        result = supervisor.run_node("repair", work, repair=lambda _: None)
    assert result == "done" and calls == 6


def test_summary_and_journal_errors_do_not_revoke_result(tmp_path, monkeypatch):
    import geng_agent.observations as observations
    supervisor = RunSupervisor(tmp_path, tmp_path / "audit", {"task": "t"})
    supervisor._request = Mock(side_effect=AssertionError("must not retry completed work"))
    monkeypatch.setattr(observations, "_write_json", Mock(side_effect=OSError("journal unavailable")))
    with supervisor_scope(supervisor):
        result = supervisor.run_node("output", lambda: "real output",
            summarize=Mock(side_effect=ValueError("unexpected note shape")))
    assert result == "real output"
    assert observations.observation_errors()


def test_moderator_missing_descriptive_fields_is_still_dispatchable():
    from geng_agent.moderator import _parse_decision
    decision = _parse_decision('{"action":"retry","instructions":"fix path","extra":"keep me"}')
    assert decision["action"] == "retry" and decision["extra"] == "keep me"


def test_local_paper_edits_are_observations(tmp_path):
    folder = tmp_path / "paper_evidence"
    folder.mkdir()
    (folder / "source.pdf").write_bytes(b"changed")
    status = _inspect_task_writer_completion(status={"ok": True}, sandbox=tmp_path,
        audit_dir=tmp_path / "audit", case_runtime=None, request_source="t", require_execution_receipt=False,
        evidence_before={"paper_evidence/source.pdf": "old"})
    assert status["ok"] is True
    assert status["paper_evidence_changed_files"]


def test_upstream_files_are_copied_with_missing_input_observation(tmp_path):
    from geng_agent.task_inputs import copy_upstream_inputs
    producer = tmp_path / "producer"
    (producer / "checkpoints").mkdir(parents=True)
    (producer / "checkpoints/model.bin").write_bytes(b"actual state")
    consumer = tmp_path / "consumer"
    copy_upstream_inputs(consumer, {"depends_on": [{"task_id": "train", "artifacts": ["checkpoints/model.bin", "absent"]}]},
                         [{"task_id": "train", "sandbox": str(producer), "writer_completed": False}])
    assert (consumer / "upstream_tasks/train/checkpoints/model.bin").read_bytes() == b"actual state"
    assert json.loads((consumer / "upstream_task_inputs.json").read_text(encoding="utf-8"))["inputs"][0]["errors"]


def test_scientific_driver_allows_task_owned_repairs_and_keeps_real_exit(tmp_path):
    from geng_agent.scientific_process import DRIVER
    (tmp_path / "tasks").mkdir()
    (tmp_path / "tasks/__init__.py").write_text("")
    (tmp_path / "tasks/t.py").write_text("from pathlib import Path\ndef main(config):\n Path('own_config.txt').write_text('updated')\n return 7\n")
    result = subprocess.run([sys.executable, "-I", "-B", "-c", DRIVER,
        json.dumps({"task_module": "t", "task_config": "config.json", "task_output_prefix": "outputs/t/"})], cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 7, result.stderr
    assert (tmp_path / "own_config.txt").read_text() == "updated"


def test_pipe_observation_failure_drains_large_real_process_output(tmp_path, monkeypatch):
    original = subprocess.Popen
    def broken_communication(*args, **kwargs):
        process = original(*args, **kwargs)
        process.communicate = Mock(side_effect=OSError("observation channel unavailable"))
        return process
    monkeypatch.setattr(subprocess, "Popen", broken_communication)
    supervisor = SimpleNamespace(tools=SimpleNamespace(observe_process=Mock()), _check_cancelled=lambda: None)
    result = run_observed_process([sys.executable, "-c", "import sys; sys.stdout.write('x'*500000); sys.stderr.write('done')"],
        supervisor=supervisor, label="fixture", cwd=tmp_path, env=os.environ.copy(), input="", check_interval=.01)
    assert result.returncode == 0 and len(result.stdout) == 500000
    assert result.stderr == "done" and result.observation_errors


def test_failed_repair_returns_to_moderator_for_another_route(tmp_path):
    supervisor = RunSupervisor(tmp_path, tmp_path / "audit", {"task": "t"})
    decisions = [{"action": "repair_artifacts", "repair_operation": "missing"},
                 {"action": "retry", "instructions": "resume the owner"}]
    supervisor._request = Mock(side_effect=decisions)
    work = Mock(side_effect=[OSError("handoff unavailable"), "done"])
    with supervisor_scope(supervisor):
        assert supervisor.run_node("t", work, repair=Mock()) == "done"
    assert supervisor._request.call_count == 2
    assert "not registered" in supervisor._request.call_args.kwargs["context"]["repair_error"]["message"]


def test_reporter_clarification_has_no_host_retry_quota(tmp_path, monkeypatch):
    import geng_agent.task_writer_runner as runner
    raw = {"ok": True, "task_verification": {"host_action": "rerun_writer", "explanation": "check the algorithm"}}
    routing = Mock(side_effect=[{"action": "repair_reporter", "decision": {"instructions": "clarify"}}] * 4
        + [{"action": "revise_writer", "decision": {"instructions": "repair"}}])
    monkeypatch.setattr(runner, "resolve_revision_owner", routing)
    callback = Mock(return_value=raw)
    returned = runner._prepare_reporter_review(raw, callback, 1, {"task_id": "t"}, {}, 1)
    assert callback.call_count == 4 and returned == raw
    assert returned.revision_route["action"] == "revise_writer"


def test_failed_paper_observation_preserves_writer_handoff(tmp_path, monkeypatch):
    monkeypatch.setattr("geng_agent.task_writer_runner.trusted_input_snapshot", Mock(side_effect=OSError("locked")))
    status = _inspect_task_writer_completion(status={"ok": True}, sandbox=tmp_path,
        audit_dir=tmp_path / "audit", case_runtime=None, request_source="t", require_execution_receipt=True,
        evidence_before={"source.pdf": "old"})
    assert status["ok"] is True and "locked" in status["paper_evidence_snapshot_error"]


def _dispatch_fixture(root, tasks, **overrides):
    from geng_agent.task_writer_dispatch import _dispatch_task_writers
    arguments = dict(task_pairs=[(task, {"task_id": task["task_id"], "module": task["task_id"]}) for task in tasks],
        facts={}, experiment_index={}, paper={}, paper_path=root / "paper.pdf", paper_context_json="{}",
        paper_images=[], paper_thesis=None, analysis_snapshot_hash="paper", analysis_artifacts={},
        task_root=root / "writers", audit_dir=root / "audit", run_repro=False)
    return _dispatch_task_writers(**{**arguments, **overrides})


def test_producer_fanout_starts_consumers_concurrently(tmp_path, monkeypatch):
    from threading import Barrier
    tasks = [{"task_id": "train"}, {"task_id": "a", "depends_on": ["train"]},
             {"task_id": "b", "depends_on": ["train"]}]
    concurrent = Barrier(2)
    def writer(**kwargs):
        task_id = kwargs["task"]["task_id"]
        if task_id != "train":
            assert [item["task_id"] for item in kwargs["upstream_records"]] == ["train"]
            concurrent.wait(timeout=5)
        return {"task_id": task_id, "index": kwargs["index"], "writer_completed": True}
    monkeypatch.setattr("geng_agent.task_writer_dispatch._run_one_task_writer", writer)
    supervisor = RunSupervisor(tmp_path, tmp_path / "audit", {"task": "fanout"})
    supervisor._request = Mock(side_effect=AssertionError("routine dispatch should not call moderator"))
    with supervisor_scope(supervisor):
        records, audit = _dispatch_fixture(tmp_path, tasks)
    assert all(record["writer_completed"] for record in records)
    assert audit["dispatch_batches"][0]["task_ids"] == ["train"]
    assert set(audit["dispatch_batches"][1]["task_ids"]) == {"a", "b"}


def test_missing_dependency_can_be_assigned_by_moderator(tmp_path, monkeypatch):
    calls = []
    def writer(**kwargs):
        calls.append(kwargs)
        return {"task_id": "evaluate", "writer_completed": True}
    monkeypatch.setattr("geng_agent.task_writer_dispatch._run_one_task_writer", writer)
    supervisor = RunSupervisor(tmp_path, tmp_path / "audit", {"task": "missing-input"})
    def choose(_node, **kwargs):
        context = kwargs["context"]
        assert context["tools"][0]["inputs"]["unavailable_upstream_tasks"] == ["absent"]
        return {"action": "start", "next_nodes": context["ready"], "instructions": "examine missing input"}
    supervisor._request = Mock(side_effect=choose)
    with supervisor_scope(supervisor):
        records, _ = _dispatch_fixture(tmp_path, [{"task_id": "evaluate", "depends_on": ["absent"]}])
    assert len(calls) == 1 and records[0]["writer_completed"]
    supervisor._request.assert_called_once()


def test_changed_upstream_input_refreshes_consumer_without_reporter_callback(tmp_path, monkeypatch):
    from geng_agent.task_inputs import upstream_input_identity
    task = {"task_id": "evaluate", "depends_on": [{"task_id": "train", "artifacts": ["model.bin"]}]}
    producer = tmp_path / "producer"
    producer.mkdir()
    (producer / "model.bin").write_bytes(b"old")
    records = {1: {"task_id": "train", "writer_completed": True, "sandbox": str(producer)},
               2: {"task_id": "evaluate", "writer_completed": True}}
    records[2]["upstream_input_identity"] = upstream_input_identity(task, list(records.values()))
    (producer / "model.bin").write_bytes(b"new")
    calls = []
    def writer(**kwargs):
        calls.append(kwargs)
        return {"task_id": "evaluate", "writer_completed": True}
    monkeypatch.setattr("geng_agent.task_writer_dispatch._run_one_task_writer", writer)
    returned, _ = _dispatch_fixture(tmp_path, [{"task_id": "train"}, task], initial_records_by_index=records)
    assert len(calls) == 1 and calls[0]["runtime_refresh_required"]
    assert returned[0]["sandbox"] == str(producer)
    assert (producer / "model.bin").read_bytes() == b"new"


def test_changed_upstream_copy_archives_old_files_and_delivery_keeps_new_inputs(tmp_path):
    from geng_agent.task_inputs import copy_upstream_inputs
    from geng_agent.task_writer_packaging import _package_task_directories
    from tests.test_task_package_delivery import _unit_inputs
    inputs = _unit_inputs(tmp_path)
    producer, consumer = inputs["task_records"]
    sandbox = Path(consumer["sandbox"])
    task = {"depends_on": [{"task_id": "t1", "artifacts": ["outputs"]}]}
    copy_upstream_inputs(sandbox, task, [producer])
    original = Path(producer["sandbox"]) / "outputs/t1/result.csv"
    original.write_text("value\n3\n", encoding="utf-8")
    copy_upstream_inputs(sandbox, task, [producer])
    archived = list((sandbox / "writer_progress").glob("upstream_*/t1/outputs/t1/result.csv"))
    assert len(archived) == 1 and archived[0].read_text(encoding="utf-8") == "value\n1\n"
    _package_task_directories(**inputs)
    delivered = inputs["repro_project_dir"] / "task_packages/t02_t2/upstream_tasks/t1/outputs/t1/result.csv"
    assert delivered.read_text(encoding="utf-8") == "value\n3\n"


def test_queue_hash_observation_failure_does_not_prevent_real_execution(monkeypatch):
    import time
    from tests.test_execution_sandbox import native_sandbox_temporary_directory
    with native_sandbox_temporary_directory() as temporary:
        base = Path(temporary)
        project = base / "project"
        (project / "tasks").mkdir(parents=True)
        (project / "tasks/__init__.py").write_text("", encoding="utf-8")
        (project / "tasks/t.py").write_text(
            "from pathlib import Path\ndef main(config):\n Path('outputs/t/result.csv').write_text('actual result')\n",
            encoding="utf-8")
        write_json(project / "config.json", {})
        write_json(project / "tasks_manifest.json", {"tasks": [{"task_id": "t", "module": "t"}]})
        monkeypatch.setattr("geng_agent.execution_receipts.source_hashes", Mock(side_effect=OSError("observation unavailable")))
        broker = ExecutionBroker(project, base / "audit", Path(sys.executable))
        with broker:
            write_json(broker.queue / "run.request.json", {"task_id": "t", "mode": "full", "device": "cpu"})
            deadline = time.monotonic() + 15
            while not broker.receipts and time.monotonic() < deadline:
                time.sleep(.02)
        assert len(broker.receipts) == 1
        assert broker.receipts[0]["returncode"] == 0
        assert broker.receipts[0]["observation_errors"]
        assert (project / "outputs/t/result.csv").read_text() == "actual result"


def test_model_result_survives_transcript_and_status_write_failure(tmp_path, monkeypatch):
    from geng_agent import codex_runner
    monkeypatch.setattr(codex_runner.shutil, "which", lambda _: sys.executable)
    monkeypatch.setattr(codex_runner, "get_config_value", lambda _: None)
    monkeypatch.setattr(codex_runner, "_ephemeral_capability", lambda *_: {"supported": True})
    run = Mock(return_value=subprocess.CompletedProcess([], 0, "completed output", ""))
    monkeypatch.setattr(codex_runner.subprocess, "run", run)
    monkeypatch.setattr(codex_runner, "write_text", Mock(side_effect=OSError("transcript unavailable")))
    monkeypatch.setattr(codex_runner, "write_json", Mock(side_effect=OSError("status unavailable")))
    monkeypatch.setattr(codex_runner.gzip, "open", Mock(side_effect=OSError("archive unavailable")))
    status = codex_runner.run_codex_subprocess(role="analysis", work_dir=tmp_path,
        prompt="fixture", audit_dir=tmp_path / "audit", label="model", sandbox="read-only")
    assert run.call_count == 1 and status["returncode"] == 0 and status["ok"]
    assert any("transcript" in error for error in status["observation_errors"])
    assert any("status" in error for error in status["observation_errors"])
