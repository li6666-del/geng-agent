"""Exercise exceptional Writer recovery without invoking any model service."""

from __future__ import annotations

from copy import deepcopy
from contextvars import ContextVar
import hashlib
import json
from pathlib import Path
from threading import Barrier, Lock

import pytest

from geng_agent import task_recovery, artifact_paths
from geng_agent import task_writer_runner as runner


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.fixture
def recovery_case(tmp_path: Path) -> dict:
    writer = tmp_path / "writer"
    reporter = tmp_path / "reporter"
    architecture = {
        "components": [
            {"id": "private", "module": "src/private.py", "callable": "compute"},
            {"id": "shared", "module": "src/data.py", "callable": "sample"},
        ],
        "bindings": [
            {"task_id": "task_a", "components": ["private", "shared"]},
            {"task_id": "task_b", "components": ["shared"]},
        ],
    }
    plan = {"task_to_execution_unit": {"task_a": "unit_a", "task_b": "unit_b"}}
    analysis = writer / "paper_evidence" / "analysis_artifacts"
    _write_json(analysis / "scientific_architecture.json", architecture)
    _write_json(analysis / "execution_plan.json", plan)
    _write_json(writer / "foundation_manifest.json", {"snapshot_hash": "foundation-v1"})
    _write_json(writer / "configs/task_a.json", {"sample_count": 10})
    _write_json(writer / "environment.lock.json", {"numpy": "1.0"})
    for relative, content in {
        "src/private.py": "def compute(): return 1\n",
        "src/data.py": "def sample(): return [1]\n",
        "outputs/task_a/result.csv": "snr,ber\n10,0.1\n",
    }.items():
        path = writer / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    paper_relative = "paper_evidence/source/paper.txt"
    paper = reporter / paper_relative
    paper.parent.mkdir(parents=True)
    paper.write_text("The noise variance per quadrature is 1/(2*SNR).", encoding="utf-8")
    verification = {
        "schema_version": "3.0",
        "task_id": "task_a",
        "host_action": "rerun_writer",
        "outcome": "not_reproduced",
        "run_valid": True,
        "rerun_reason": "core_conclusion_failed",
        "decision_reason": "The variance in the code differs from the paper.",
        "core_conclusions": [{"claim_id": "claim_a", "status": "unsupported"}],
        "rerun_evidence": {
            "rerun_reason": "core_conclusion_failed",
            "contract_item_ids": ["claim_a"],
            "paper_evidence_files": [paper_relative],
            "change_targets": ["src/private.py:compute"],
            "causal_change": "Apply the stated per-quadrature variance.",
            "predicted_effect": "The implemented channel matches the paper definition.",
        },
    }
    reporter_record = {"workspace": str(reporter), "task_verification": verification, "ok": True}
    record = {
        "task_id": "task_a", "execution_unit_id": "unit_a", "sandbox": str(writer),
        "output_subdir": "task_a", "task_writer_status": "ready_for_review",
        "task_reporter": reporter_record, "task_verification": verification,
    }
    return {
        "writer": writer, "reporter": reporter_record, "record": record,
        "verification": verification, "architecture": architecture, "plan": plan,
        "paper_path": paper,
    }


def _moderator(monkeypatch: pytest.MonkeyPatch, *, action: str = "stop", **overrides) -> list[dict]:
    calls = []

    def request(**kwargs):
        calls.append(deepcopy(kwargs))
        return {
            "action": action,
            "diagnosis": "The correction belongs to the identified component owner.",
            "instructions": "Correct the variance using the cited equation, then rerun.",
            "component_ids": [],
            "evidence_refs": [{"root": "reporter", "path": "paper_evidence/source/paper.txt"}],
            "expected_change": "The variance matches the cited equation.",
            "decision_id": "offline-moderation-decision",
            **overrides,
        }

    monkeypatch.setattr(task_recovery, "request_moderation", request)
    return calls










def test_missing_foundation_keeps_ordinary_code_revision_with_writer(recovery_case, monkeypatch):
    (recovery_case["writer"] / "foundation_manifest.json").unlink()
    recovery_case["verification"]["rerun_evidence"]["change_targets"] = ["src/data.py:sample"]
    calls = _moderator(monkeypatch, action="revise_writer")
    result = task_recovery.resolve_revision_owner(
        record=recovery_case["record"], reporter=recovery_case["reporter"],
        verification=recovery_case["verification"],
    )
    assert result["action"] == "revise_writer"
    assert len(calls) == 1




@pytest.mark.parametrize("targets", [["private", "shared"], ["fix the normalization path"]])
def test_mixed_or_unknown_ownership_requests_one_exceptional_decision(recovery_case, monkeypatch, targets):
    verification = recovery_case["verification"]
    verification["rerun_evidence"]["change_targets"] = targets
    before = deepcopy(verification)
    calls = _moderator(monkeypatch, action="revise_writer", component_ids=["private"])
    result = task_recovery.resolve_revision_owner(
        record=recovery_case["record"], reporter=recovery_case["reporter"], verification=verification,
    )
    assert len(calls) == 1
    assert result["action"] == "revise_writer"
    assert verification == before
    feedback = result["feedback"]
    assert feedback is not verification
    assert feedback["moderator_instructions"]
    assert {key: value for key, value in feedback.items() if key != "moderator_instructions"} == before
    feedback["core_conclusions"][0]["status"] = "mutated-by-test"
    assert verification["core_conclusions"][0]["status"] == "unsupported"






def test_moderator_revision_is_not_vetoed_by_missing_reporter_fields(recovery_case, monkeypatch):
    verification = recovery_case["verification"]
    verification["rerun_evidence"].update(
        change_targets=["shared"], paper_evidence_files=["paper_evidence/source/missing.txt"],
    )
    _moderator(monkeypatch, action="revise_writer", component_ids=["private"])
    result = task_recovery.resolve_revision_owner(
        record=recovery_case["record"], reporter=recovery_case["reporter"], verification=verification,
    )
    assert result["action"] == "revise_writer"
    assert result["feedback"]["rerun_evidence"] == verification["rerun_evidence"]








def test_analysis_snapshot_change_invalidates_recovery_state(recovery_case):
    record = deepcopy(recovery_case["record"])
    record["analysis_snapshot_hash"] = "analysis-before"
    before = task_recovery.recovery_state_id(record, "writer_no_progress")
    record["analysis_snapshot_hash"] = "analysis-after"
    after = task_recovery.recovery_state_id(record, "writer_no_progress")
    assert before != after




def test_recovery_hash_streams_training_artifacts_without_read_bytes(recovery_case, monkeypatch):
    checkpoint = recovery_case["writer"] / "execution_units/unit_a/checkpoint.bin"
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_bytes(b"checkpoint" * 300_000)

    def cannot_read_all(_path):
        pytest.fail("Recovery hashing must stream artifacts rather than read them entirely into memory.")

    monkeypatch.setattr(Path, "read_bytes", cannot_read_all)
    first = task_recovery.recovery_state_id(recovery_case["record"], "writer_no_progress")
    with checkpoint.open("ab") as handle:
        handle.write(b"new weights")
    second = task_recovery.recovery_state_id(recovery_case["record"], "writer_no_progress")
    assert len(first) == len(second) == 64
    assert first != second


@pytest.mark.parametrize("relative", ["outputs", "outputs/task_a/junction", "outputs/task_a/linked.bin"])
def test_recovery_hash_never_opens_linked_files_or_junction_children(recovery_case, monkeypatch, relative):
    link = recovery_case["writer"] / relative
    if relative.endswith(".bin"):
        link.write_bytes(b"outside artifact")
    else:
        link.mkdir(parents=True, exist_ok=True)
        (link / "outside.bin").write_bytes(b"outside artifact")
    original_link_check = artifact_paths.path_is_link
    original_open = Path.open

    def is_link(path):
        return Path(path) == link or original_link_check(path)

    def guarded_open(path, *args, **kwargs):
        assert not (path == link or path.is_relative_to(link)), "Recovery followed a linked file or junction."
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(task_recovery, "path_is_link", is_link)
    monkeypatch.setattr(artifact_paths, "path_is_link", is_link)
    monkeypatch.setattr(Path, "open", guarded_open)
    assert len(task_recovery.recovery_state_id(recovery_case["record"], "writer_no_progress")) == 64


def _recovery_work(recovery_case: dict, count: int = 2) -> list[tuple]:
    work = []
    for index in range(count):
        task_id = f"task_{index}"
        record = deepcopy(recovery_case["record"])
        record.update(task_id=task_id, output_subdir=task_id)
        verification = record["task_verification"]
        verification["task_id"] = task_id
        record["task_reporter"]["task_verification"] = verification
        task = {"task_id": task_id}
        work.append((index, task, record, verification))
    return work


def _reporter_return(record: dict, verification: dict) -> dict:
    return {"ok": True, "workspace": record["task_reporter"]["workspace"],
            "task_verification": deepcopy(verification)}












def test_normal_reviews_keep_exceptional_owner_clarification_inside_parallel_callbacks(recovery_case, monkeypatch):
    work = _recovery_work(recovery_case)
    moderation = Barrier(2, timeout=5)
    clarifications = Barrier(2, timeout=5)
    lock = Lock()
    calls = {"task_0": 0, "task_1": 0}
    completed = set()
    moderation_calls = []
    localized = []

    def request(**kwargs):
        moderation.wait()
        with lock:
            moderation_calls.append(kwargs["context"]["task_id"])
        targets = kwargs["context"]["reporter_verification"]["rerun_evidence"]["change_targets"]
        return {"action": "revise_writer" if targets == ["src/private.py:compute"] else "repair_reporter", "decision_id": "clarify-owner",
                "instructions": "Name the exact module requiring correction."}

    def review(_index, task, record, _round):
        task_id = task["task_id"]
        with lock:
            calls[task_id] += 1
            call_no = calls[task_id]
        verification = deepcopy(record["task_verification"])
        if call_no == 1:
            verification["rerun_evidence"]["change_targets"] = ["clarify the normalization ownership"]
        else:
            assert call_no == 2
            assert record["moderator_review_request"]["instructions"]
            clarifications.wait()
            verification["rerun_evidence"]["change_targets"] = ["src/private.py:compute"]
            with lock:
                completed.add(task_id)
        return _reporter_return(record, verification)

    def localize(feedback, **_kwargs):
        assert completed == {"task_0", "task_1"}
        localized.append(feedback["task_id"])
        return deepcopy(feedback)

    monkeypatch.setattr(task_recovery, "request_moderation", request)
    monkeypatch.setattr(runner, "localize_writer_feedback", localize)
    reviews = runner._review_task_records(
        work=[(index, task, record, 1) for index, task, record, _verification in work],
        callback=review,
    )
    assert [action for action, _feedback in reviews] == ["writer_revision", "writer_revision"]
    assert calls == {"task_0": 2, "task_1": 2}
    assert sorted(moderation_calls) == ["task_0", "task_0", "task_1", "task_1"]
    assert sorted(localized) == ["task_0", "task_1"]
