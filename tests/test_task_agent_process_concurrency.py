"""Exercise production scheduling/transport with real, offline worker processes.

The subprocesses are explicitly local fixtures, not Codex or scientific agents.
Their file barriers fail under serial dispatch. These tests establish process
overlap, model-context propagation and output isolation, not LLM capability.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import textwrap

import pytest

from geng_agent.codex_runner import _clear_ephemeral_capability_cache, run_codex_subprocess
from geng_agent.model_config import CodexModelConfig, model_config_scope
from geng_agent.task_writer_dispatch import _dispatch_task_writers
from geng_agent.task_writer_runner import _review_execution_unit_tasks


_LOCAL_WORKER = r'''
import json
import os
from pathlib import Path
import sys
import time

if sys.argv[1:] == ["exec", "--help"]:
    print("Local offline test fixture: --ephemeral --ignore-user-config")
    raise SystemExit(0)

request = json.loads(sys.stdin.read())
root = Path(request["barrier_root"])
name = request["name"]
started = time.time_ns()
(root / (name + ".started")).write_text(str(os.getpid()), encoding="utf-8")
deadline = time.monotonic() + 15
while not all((root / (peer + ".started")).is_file() for peer in request["wait_for"]):
    if time.monotonic() > deadline:
        print("Offline process barrier timed out: " + name, file=sys.stderr)
        raise SystemExit(73)
    time.sleep(0.02)
time.sleep(0.1)
observation = {
    "fixture": "offline_process_concurrency",
    "name": name,
    "pid": os.getpid(),
    "cwd": str(Path.cwd()),
    "started_ns": started,
    "finished_ns": time.time_ns(),
    "model": sys.argv[sys.argv.index("--model") + 1],
    "managed": "--ignore-user-config" in sys.argv,
    "maximum_effort": 'model_reasoning_effort="max"' in sys.argv,
    "provider_selected": 'model_provider="geng_offline"' in sys.argv,
    "credential_delivered": os.environ.get("GENG_ACTIVE_PROVIDER_CREDENTIAL") == "local-fixture-value",
}
Path("worker_result.json").write_text(json.dumps(observation), encoding="utf-8")
Path(sys.argv[sys.argv.index("--output-last-message") + 1]).write_text(name, encoding="utf-8")
print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": name}}))
raise SystemExit(request.get("exit_code", 0))
'''


@pytest.fixture
def offline_workers(tmp_path: Path, monkeypatch):
    script = tmp_path / "local_offline_worker.py"
    script.write_text(textwrap.dedent(_LOCAL_WORKER), encoding="utf-8")
    barriers = tmp_path / "process_barriers"
    barriers.mkdir()
    profile = CodexModelConfig(
        provider="offline", model="offline-concurrency-fixture", reasoning_effort="max",
        base_url="http://127.0.0.1:1", env_key="GENG_OFFLINE_FIXTURE_KEY", managed=True,
    )
    monkeypatch.setenv("GENG_OFFLINE_FIXTURE_KEY", "local-fixture-value")
    _clear_ephemeral_capability_cache()
    command = f'"{sys.executable}" -B "{script}"'

    def invoke(name: str, role: str, peers: list[str], *, fail: bool = False):
        workspace = tmp_path / "workspaces" / name
        workspace.mkdir(parents=True)
        status = run_codex_subprocess(
            role=role, work_dir=workspace,
            prompt=json.dumps({"barrier_root": str(barriers), "name": name,
                               "wait_for": peers, "exit_code": 7 if fail else 0}),
            audit_dir=tmp_path / "audit" / name, label="worker",
            sandbox="workspace-write", command_override=command,
        )
        return status, workspace

    with model_config_scope(profile):
        yield invoke, profile
    _clear_ephemeral_capability_cache()


def _observations(tmp_path: Path, names: list[str]) -> list[dict]:
    observations = [json.loads((tmp_path / "workspaces" / name / "worker_result.json")
                              .read_text(encoding="utf-8")) for name in names]
    assert len({item["pid"] for item in observations}) == len(names)
    assert len({item["cwd"] for item in observations}) == len(names)
    for name, item in zip(names, observations):
        assert item["name"] == name
        assert Path(item["cwd"]).resolve() == (tmp_path / "workspaces" / name).resolve()
        assert item["model"] == "offline-concurrency-fixture"
        assert item["managed"] and item["maximum_effort"]
        assert item["provider_selected"] and item["credential_delivered"]
        assert (tmp_path / "audit" / name / "worker_last_message.txt").read_text(encoding="utf-8") == name
    return observations


def _assert_overlap(observations: list[dict]) -> None:
    assert max(item["started_ns"] for item in observations) < min(item["finished_ns"] for item in observations)


def _review_payload(task_id: str) -> dict:
    return {"ok": True, "task_id": task_id, "task_verification": {
        "schema_version": "3.0", "task_id": task_id, "outcome": "not_reproduced",
        "run_valid": True, "host_action": "complete",
        "decision_reason": "Synthetic offline transport fixture; no scientific claim is tested.",
    }}


@pytest.mark.parametrize("pipeline_reviews", [False, True])
def test_writer_dispatch_starts_real_processes_concurrently(
    tmp_path: Path, monkeypatch, offline_workers, pipeline_reviews: bool,
) -> None:
    invoke, profile = offline_workers
    writer_names = [f"writer_{i}" for i in range(3)]
    reporter_names = [f"reporter_{i}" for i in range(3)]
    pairs = [({"task_id": str(i)}, {"task_id": str(i), "module": f"task_{i}",
              "output_subdir": str(i)}) for i in range(3)]

    def local_writer(**kwargs):
        # Replace only model-generated task content, retaining the production
        # dispatcher, copied ContextVar and real run_codex_subprocess boundary.
        index = int(kwargs["task"]["task_id"])
        peers = list(writer_names)
        if pipeline_reviews and index == 1:
            # Writer 1 cannot finish until Writer 0 has started its Reporter.
            peers.append("reporter_0")
        status, workspace = invoke(writer_names[index], "task_writer", peers)
        assert status["ok"], status
        assert status["model_config"] == profile.identity()
        record = {"task_id": str(index), "writer_completed": True,
                  "task_writer_status": "ready_for_review", "sandbox": str(workspace),
                  "result_json": {"status": "ready_for_review"}}
        if pipeline_reviews:
            status, _ = invoke(reporter_names[index], "task_reporter", reporter_names)
            assert status["ok"], status
            assert status["model_config"] == profile.identity()
            record["task_reporter"] = _review_payload(str(index))
        return record

    monkeypatch.setattr("geng_agent.task_writer_dispatch._run_one_task_writer", local_writer)
    records, audit = _dispatch_task_writers(
        task_pairs=pairs, facts={}, experiment_index={}, paper={},
        paper_path=tmp_path / "synthetic_unused_paper.md", paper_context_json="",
        paper_images=[], paper_thesis=None, analysis_snapshot_hash="offline-fixture",
        analysis_artifacts={}, task_root=tmp_path / "sandboxes", audit_dir=tmp_path / "dispatch_audit",
        run_repro=False,
    )
    assert all(record["writer_completed"] for record in records), records
    assert [record["task_id"] for record in records] == ["0", "1", "2"]
    assert audit["completed_task_count"] == 3
    writers = _observations(tmp_path, writer_names)
    _assert_overlap(writers)
    if pipeline_reviews:
        reporters = _observations(tmp_path, reporter_names)
        _assert_overlap(reporters)
        _assert_overlap([writers[1], reporters[0]])
        assert writers[0]["finished_ns"] < reporters[0]["started_ns"]


@pytest.mark.parametrize("fail_one", [False, True])
def test_compound_reporter_helper_starts_real_processes_and_isolates_failure(
    tmp_path: Path, offline_workers, fail_one: bool,
) -> None:
    invoke, profile = offline_workers
    names = [f"reporter_{i}" for i in range(3)]
    members = [(i + 1, {"task_id": str(i)}, {}) for i in range(3)]
    records = [{"task_id": str(i)} for i in range(3)]

    def review(index, task, record, session_round):
        status, _ = invoke(names[index - 1], "task_reporter", names,
                           fail=fail_one and index == 2)
        assert status["model_config"] == profile.identity()
        if not status["ok"]:
            raise RuntimeError(f"Offline Reporter process exited with {status['returncode']}")
        return _review_payload(task["task_id"])

    feedback = _review_execution_unit_tasks(
        members=members, records=records, callback=review, session_round=1,
    )
    assert feedback == {}
    _assert_overlap(_observations(tmp_path, names))
    for index, record in enumerate(records):
        if fail_one and index == 1:
            assert record["task_reporter_error_kind"] == "task_reporter_callback_failed"
        else:
            assert record["task_reporter_terminal"] is True
            assert record["task_verification"]["task_id"] == str(index)
