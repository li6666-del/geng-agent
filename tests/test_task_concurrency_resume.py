"""Resume must obey the same concurrency and input-freeze rules as a new run."""
from pathlib import Path
from threading import Barrier, Lock

import pytest

from geng_agent import task_writer_runner as runner
from geng_agent.task_writer_dispatch import _dispatch_task_writers, _refresh_cached_task_reporters


def _verdict(task_id, outcome="reproduced"):
    return {"ok": True, "cached": False, "task_id": task_id, "task_verification": {
        "schema_version": "2.0", "task_id": task_id, "outcome": outcome,
        "host_action": "complete", "rerun_reason": "none", "run_valid": True,
    }}


def _record(index, root):
    task_id = f"t{index}"
    return {"index": index, "task_id": task_id, "sandbox": str(root),
            "writer_completed": True, "host_execution": {"passed": True}, "task_writer_status": "ready_for_review",
            "writer_session_count": index, "task_reporter_successful": True,
            "task_reporter_terminal": True,
            "task_verification": _verdict(task_id)["task_verification"]}


def _dispatch(tmp_path, pairs, **kwargs):
    return _dispatch_task_writers(
        task_pairs=pairs, facts={}, experiment_index={}, paper={},
        paper_path=tmp_path / "paper.pdf", paper_context_json="", paper_images=[],
        paper_thesis=None, analysis_snapshot_hash="test", analysis_artifacts={},
        task_root=tmp_path / "sandboxes", audit_dir=tmp_path / "audit",
        run_repro=True, **kwargs,
    )


def test_partial_resume_validates_reporters_while_another_writer_runs(monkeypatch, tmp_path):
    pairs = [({"task_id": f"t{i}"}, {"task_id": f"t{i}"}) for i in range(1, 4)]
    saved = {i: _record(i, tmp_path / "shared") for i in (2, 3)}
    barrier = Barrier(3)
    calls = []
    lock = Lock()

    def writer(**kwargs):
        assert kwargs["task"]["task_id"] == "t1"
        barrier.wait(timeout=5)
        return _record(1, tmp_path / "new")

    def reporter(index, task, record, round_no):
        assert index in (2, 3) and round_no == index
        assert record is saved[index]
        barrier.wait(timeout=5)
        with lock:
            calls.append(index)
        return _verdict(task["task_id"], "not_reproduced")

    monkeypatch.setattr("geng_agent.task_writer_dispatch._run_one_task_writer", writer)
    records, audit = _dispatch(
        tmp_path, pairs, initial_records_by_index=saved, task_review_callback=reporter,
        execution_plan={"execution_units": [{"unit_id": "shared", "task_ids": ["t2", "t3"]}]},
    )
    assert sorted(calls) == [2, 3]
    assert [record["index"] for record in records] == [1, 2, 3]
    assert all(record["task_verification"]["outcome"] == "not_reproduced" for record in records[1:])
    assert audit["cached_writer_reporter_validation_unit_ids"] == ["task_02_t2", "task_03_t3"]


def test_resume_hands_failed_execution_to_reporter_without_relaunching_writer(monkeypatch, tmp_path):
    pair = ({"task_id": "t1"}, {"task_id": "t1"})
    saved = _record(1, tmp_path / "writer")
    saved["host_execution"] = {"passed": False, "issues": ["source changed after full execution"]}
    launches = []

    def unexpected_launch(**kwargs):
        launches.append(kwargs)
        raise AssertionError("observed Writer output must be handed to Reporter")

    monkeypatch.setattr("geng_agent.task_writer_dispatch._run_one_task_writer", unexpected_launch)
    records, audit = _dispatch(
        tmp_path, [pair], initial_records_by_index={1: saved},
        task_review_callback=lambda _index, _task, record, _round: (
            _verdict(record["task_id"], "not_reproduced")
        ),
    )
    assert not launches
    assert records[0]["host_execution"]["passed"] is False
    assert records[0]["task_verification"]["outcome"] == "not_reproduced"
    assert audit["cached_writer_reporter_validation_unit_ids"] == ["task_01_t1"]


@pytest.mark.parametrize("fails", [False, True])
def test_cached_review_barrier_preserves_all_inputs_until_every_callback_exits(monkeypatch, tmp_path, fails):
    pairs = [({"task_id": f"t{i}"}, {"task_id": f"t{i}"}) for i in (1, 2, 3)]
    records = [_record(i, tmp_path / "shared") for i in (1, 2, 3)]
    barrier = Barrier(3)
    completed = set()
    lock = Lock()
    original_attach = runner._attach_task_reporter_review

    def reporter(index, task, record, round_no):
        barrier.wait(timeout=5)
        with lock:
            completed.add(index)
        if fails and index == 1:
            raise RuntimeError("local fixture failure")
        return _verdict(task["task_id"])

    def attach(**kwargs):
        assert completed == {1, 2, 3}
        return original_attach(**kwargs)

    monkeypatch.setattr(runner, "_attach_task_reporter_review", attach)
    refreshed, _, revisions, audit = _refresh_cached_task_reporters(
        task_pairs=pairs, cached_records=records, experiment_index={}, task_review_callback=reporter,
    )
    assert not revisions and audit["reporter_count"] == 3
    if fails:
        assert "task_verification" not in refreshed[0]
        assert "task_reporter_successful" not in refreshed[0]
        assert "task_reporter_terminal" not in refreshed[0]
    assert all(record["task_reporter_terminal"] for record in refreshed[1:])


def test_failed_fresh_review_does_not_retain_previous_success():
    record = _record(1, Path("unused"))
    record.update(task_writer_status="matched", verification_verified=True,
                  verification_result=_verdict("t1")["task_verification"],
                  scientific_outcome="reproduced")
    action, _ = runner._attach_task_reporter_review(
        callback=lambda *_: {"ok": False, "error": "fixture failure"},
        index=1, task={"task_id": "t1"}, record=record, session_round=1,
    )
    assert action == "failed"
    assert "task_verification" not in record
    assert "task_reporter_successful" not in record
    assert "verification_result" not in record
    assert "verification_verified" not in record
    assert "scientific_outcome" not in record
    assert record["task_writer_status"] == "ready_for_review"


def test_partial_resume_replays_causal_revision_once_and_does_not_repeat_model_review(monkeypatch, tmp_path):
    from geng_agent.task_writer_dispatch import _resume_or_run_writer

    members = [(i, {"task_id": f"t{i}"}, {}) for i in (2, 3)]
    records = {i: _record(i, tmp_path / "shared") for i in (2, 3)}
    calls = []

    def callback(index, task, record, round_no):
        calls.append(index)
        return _verdict(task["task_id"])

    def attach(**kwargs):
        result = kwargs["callback"](kwargs["index"], kwargs["task"], kwargs["record"], kwargs["session_round"])
        kwargs["record"]["task_reporter"] = result
        return ("writer_revision", {"causal_change": "fixture"}) if kwargs["index"] == 2 else ("terminal", None)

    def continuation(**kwargs):
        assert set(kwargs["review_feedback"]) == {"t2"}
        for index, task, _ in members:
            assert kwargs["task_review_callback"](index, task, records[index], index) is records[index]["task_reporter"]
        assert sorted(calls) == [2, 3]
        kwargs["task_review_callback"](2, members[0][1], records[2], 4)
        assert sorted(calls) == [2, 2, 3]
        return list(records.values())

    monkeypatch.setattr(runner, "_attach_task_reporter_review", attach)
    result = _resume_or_run_writer(
        writer_runner=continuation, cached_members=members, cached_records=records,
        task_review_callback=callback, experiment_index={}, review_feedback={},
    )
    assert len(result) == 2
