"""Observe overlapping local child processes without calling any model API."""

from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
import json
from pathlib import Path
import sys
import threading
import time

import pytest

from geng_agent.agent_activity import (
    agent_activity_scope,
    record_agent_cached,
    session_finished,
    session_started,
)
from geng_agent.codex_runner import _clear_ephemeral_capability_cache, run_codex_subprocess
from geng_agent.progress import CallbackProgressReporter, ConsoleProgressReporter


def _read(audit: Path) -> dict:
    return json.loads((audit / "agent_activity.json").read_text(encoding="utf-8"))


def test_actual_child_processes_publish_live_overlap_and_failures(monkeypatch, tmp_path: Path) -> None:
    """Exercise the real subprocess transport using a local CLI test double."""
    _clear_ephemeral_capability_cache()
    worker = tmp_path / "local_worker.py"
    worker.write_text(
        "import pathlib, sys, time\n"
        "if sys.argv[-2:] == ['exec', '--help']:\n"
        "    print('--ephemeral --ignore-user-config')\n"
        "    raise SystemExit(0)\n"
        "sys.stdin.read()\n"
        "root = pathlib.Path.cwd()\n"
        "(root / 'child_started').write_text('running')\n"
        "deadline = time.monotonic() + 15\n"
        "while not (root / 'release_child').exists():\n"
        "    if time.monotonic() > deadline: raise SystemExit(9)\n"
        "    time.sleep(0.01)\n"
        "print('local worker complete')\n"
        "raise SystemExit(3 if root.name.endswith('failure') else 0)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("geng_agent.codex_runner.get_config_value", lambda _: None)
    calls = [("task_writer", "writer_a"), ("task_writer", "writer_b"),
             ("task_reporter", "reporter_a"), ("task_reporter", "reporter_failure")]
    for _, label in calls:
        (tmp_path / label).mkdir()
    events: list[dict] = []
    audit = tmp_path / "audit"
    with agent_activity_scope(audit, CallbackProgressReporter(events.append)):
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(copy_context().run, run_codex_subprocess,
                                   role=role, work_dir=tmp_path / label, prompt="local fixture",
                                   audit_dir=audit / label, label=label, sandbox="read-only",
                                   command_override=f'"{sys.executable}" -B "{worker}"')
                       for role, label in calls]
            try:
                deadline = time.monotonic() + 10
                while not all((tmp_path / label / "child_started").exists() for _, label in calls):
                    if time.monotonic() > deadline:
                        pytest.fail("all four local worker processes did not overlap")
                    time.sleep(0.01)
                live = _read(audit)
                assert live["active"] == {"task_writer": 2, "task_reporter": 2, "total": 4}
                assert len(live["sessions"]) == 4
                assert all(item["status"] == "running" for item in live["sessions"].values())
            finally:
                for _, label in calls:
                    (tmp_path / label / "release_child").touch()
            results = [future.result(timeout=15) for future in futures]
    final = _read(audit)
    assert final["active"] == {"task_writer": 0, "task_reporter": 0, "total": 0}
    assert final["peak"] == {"task_writer": 2, "task_reporter": 2, "total": 4}
    assert final["scope_status"] == "completed"
    assert [item["ok"] for item in results] == [True, True, True, False]
    assert [event["data"]["sequence"] for event in events] == list(range(1, 9))
    assert [event["type"] for event in events].count("agent.started") == 4
    assert [event["type"] for event in events].count("agent.completed") == 3
    assert [event["type"] for event in events].count("agent.failed") == 1
    assert all(event["data"]["active"]["total"] >= 0 for event in events)
    assert all("task_id" not in event["data"] and "round" not in event["data"] for event in events)


def test_cache_hit_is_not_a_launched_session_and_resume_preserves_history(tmp_path: Path) -> None:
    audit = tmp_path / "audit"
    events: list[dict] = []
    with agent_activity_scope(audit, CallbackProgressReporter(events.append)):
        record_agent_cached(role="task_reporter", label="t1", work_dir=tmp_path, task_id="t1")
        assert session_started(invocation_id="ignored", role="report_editor", label="editor", work_dir=tmp_path) is None
    first = _read(audit)
    with agent_activity_scope(audit):
        handle = session_started(invocation_id="writer", role="task_writer", label="w", work_dir=tmp_path)
        session_finished(handle, ok=True)
        session_finished(handle, ok=True)  # A repeated cleanup cannot make active negative.
    second = _read(audit)
    assert first["sessions"] == {}
    assert first["peak"]["total"] == 0
    assert first["cached"] == {"task_writer": 0, "task_reporter": 1}
    assert first["cache_hits"][0]["task_id"] == "t1"
    assert [event["type"] for event in events] == ["agent.cached"]
    assert first["run_id"] != second["run_id"]
    saved = json.loads((audit / "agent_activity_runs" / f"{first['run_id']}.json").read_text(encoding="utf-8"))
    assert saved == first
    assert second["active"]["total"] == 0
    assert session_started(invocation_id="outside", role="task_writer", label="x", work_dir=tmp_path) is None


def test_two_run_contexts_keep_counts_isolated(tmp_path: Path) -> None:
    barrier = threading.Barrier(2)

    def case(name: str) -> dict:
        root = tmp_path / name
        with agent_activity_scope(root):
            handle = session_started(invocation_id=name, role="task_writer", label=name, work_dir=root)
            barrier.wait(timeout=5)
            assert _read(root)["active"]["total"] == 1
            session_finished(handle, ok=True)
        return _read(root)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(case, ("a", "b")))
    assert set(results[0]["sessions"]) == {"a"}
    assert set(results[1]["sessions"]) == {"b"}
    assert all(item["peak"]["total"] == 1 for item in results)


def test_callback_outage_does_not_interrupt_session_accounting(tmp_path: Path) -> None:
    def failed_callback(_):
        raise RuntimeError("web unavailable")

    with agent_activity_scope(tmp_path, CallbackProgressReporter(failed_callback)):
        handle = session_started(invocation_id="w", role="task_writer", label="w", work_dir=tmp_path)
        session_finished(handle, ok=False, error_kind="subprocess_error")
    result = _read(tmp_path)
    assert result["active"]["total"] == 0
    assert result["sessions"]["w"]["status"] == "failed"
    assert result["warnings"] == ["activity event unavailable: RuntimeError"]


def test_execution_flow_binds_activity_to_existing_progress_reporter(tmp_path: Path) -> None:
    from types import SimpleNamespace
    from geng_agent.pipeline_execution_flow import _observe_agent_activity

    events: list[dict] = []
    context = SimpleNamespace(audit_dir=tmp_path, progress_tracker=SimpleNamespace(reporter=CallbackProgressReporter(events.append)))

    def execution(_context, analysis):
        handle = session_started(invocation_id="w", role="task_writer", label="w", work_dir=tmp_path)
        session_finished(handle, ok=True)
        return analysis

    expected = object()
    assert _observe_agent_activity(execution)(context, expected) is expected
    assert [event["type"] for event in events] == ["agent.started", "agent.completed"]


def test_console_exposes_worker_events(capsys) -> None:
    ConsoleProgressReporter().emit("agent.started", phase="task_reproduction", message="Writer t1 开始；当前运行 Writer 2，Reporter 1")
    assert "当前运行 Writer 2，Reporter 1" in capsys.readouterr().err


def test_reporter_workflow_cache_hit_is_observed_without_starting_cli(monkeypatch, tmp_path: Path) -> None:
    from geng_agent.agentic_task_reporters import run_codex_task_reporter_workflow

    monkeypatch.setattr("geng_agent.agentic_task_reporters._task_reporter_input_hash", lambda **_: "current")
    monkeypatch.setattr("geng_agent.agentic_task_reporters._load_task_reporter_cache",
                        lambda **_: {"ok": True, "workspace": str(tmp_path / "prior_reporter")})

    def unexpected_model(**_):
        raise AssertionError("a cache hit must not launch a model session")

    monkeypatch.setattr("geng_agent.agentic_task_reporters.run_codex_subprocess", unexpected_model)
    with agent_activity_scope(tmp_path / "audit"):
        result = run_codex_task_reporter_workflow(
            index=1, task={"task_id": "t1"}, task_record={}, paper={},
            paper_path=tmp_path / "paper.pdf", facts={}, experiment_index={},
            paper_thesis=None, paper_images=None, output_dir=tmp_path,
            audit_dir=tmp_path / "audit", resume=True,
        )
    observed = _read(tmp_path / "audit")
    assert result["cached"] is True
    assert observed["cached"]["task_reporter"] == 1
    assert observed["cache_hits"][0]["task_id"] == "t1"
    assert observed["sessions"] == {}
    assert observed["peak"]["total"] == 0


def test_cli_preflight_failure_does_not_claim_a_running_process(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("geng_agent.codex_runner.get_config_value", lambda _: None)
    monkeypatch.setattr("geng_agent.codex_runner.shutil.which", lambda _: None)
    with agent_activity_scope(tmp_path / "audit"):
        status = run_codex_subprocess(role="task_writer", work_dir=tmp_path,
                                      prompt="local fixture", audit_dir=tmp_path / "audit",
                                      label="missing_cli", sandbox="read-only")
    observed = _read(tmp_path / "audit")
    assert status["error_kind"] == "missing_cli"
    assert observed["sessions"] == {}
    assert observed["peak"]["total"] == 0
