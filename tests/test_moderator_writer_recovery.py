"""Exercise exceptional Writer recovery without invoking any model service."""

from __future__ import annotations

from copy import deepcopy
from contextvars import ContextVar
import hashlib
import json
from pathlib import Path
from threading import Barrier, Lock

import pytest

from geng_agent import task_recovery
from geng_agent import task_writer_runner as runner
from geng_agent import foundation_snapshot
from geng_agent.foundation_revision import validate_foundation_revision_request


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


@pytest.mark.parametrize(
    ("target", "owner", "component_ids"),
    [
        ("private", "private", []),
        ("src/private.py:compute", "private", []),
        ("shared", "shared", ["shared"]),
        ("src/data.py:sample", "shared", ["shared"]),
        ("src/data.py:new_function", "shared", ["shared"]),
    ],
)
def test_exact_component_and_module_targets_have_structural_owners(recovery_case, target, owner, component_ids):
    result = task_recovery.resolve_targets(
        targets=[target], architecture=recovery_case["architecture"],
        execution_plan=recovery_case["plan"], foundation_present=True,
    )
    assert result["owner"] == owner
    assert result["component_ids"] == component_ids
    assert not result["unresolved"]


def test_shorter_path_never_matches_a_shared_module_by_substring(recovery_case):
    result = task_recovery.resolve_targets(
        targets=["src/a.py:sample"], architecture=recovery_case["architecture"],
        execution_plan=recovery_case["plan"], foundation_present=True,
    )
    assert result["owner"] != "shared"
    assert "shared" not in result["component_ids"]


def test_unmapped_foundation_config_is_not_assumed_private(recovery_case):
    result = task_recovery.resolve_targets(
        targets=["configs/foundation.json"], architecture=recovery_case["architecture"],
        execution_plan=recovery_case["plan"], foundation_present=True,
    )
    assert result["owner"] != "private"


def test_missing_architecture_cannot_authorize_private_edits_with_foundation():
    result = task_recovery.resolve_targets(
        targets=["src/shared.py"], architecture={}, execution_plan=None, foundation_present=True,
    )
    assert result["owner"] != "private"


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


@pytest.mark.parametrize("target,expected", [("private", "revise_writer"), ("shared", "revise_foundation")])
def test_unambiguous_routing_is_presented_to_moderator(recovery_case, monkeypatch, target, expected):
    verification = recovery_case["verification"]
    verification["rerun_evidence"]["change_targets"] = [target]
    before = deepcopy(verification)
    calls = _moderator(monkeypatch, action=expected, component_ids=["shared"] if expected == "revise_foundation" else [])
    result = task_recovery.resolve_revision_owner(
        record=recovery_case["record"], reporter=recovery_case["reporter"], verification=verification,
    )
    assert result["action"] == expected
    assert len(calls) == 1
    assert verification == before
    if expected == "revise_foundation":
        request = result["request"]
        assert request["component_ids"] == ["shared"]
        assert request["affected_task_ids"] == ["task_a", "task_b"]
        assert request["paper_evidence"][0]["sha256"] == hashlib.sha256(recovery_case["paper_path"].read_bytes()).hexdigest()


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


def test_moderated_foundation_revision_revalidates_original_reporter_evidence(recovery_case, monkeypatch):
    verification = recovery_case["verification"]
    verification["rerun_evidence"]["change_targets"] = ["shared", "private"]
    calls = _moderator(monkeypatch, action="revise_foundation", component_ids=["shared"])
    result = task_recovery.resolve_revision_owner(
        record=recovery_case["record"], reporter=recovery_case["reporter"], verification=verification,
    )
    assert len(calls) == 1
    assert result["action"] == "revise_foundation"
    assert result["request"]["evidence_root"] == recovery_case["reporter"]["workspace"]
    assert result["request"]["paper_evidence_files"] == ["paper_evidence/source/paper.txt"]
    assert result["request"]["paper_evidence"][0]["sha256"] == hashlib.sha256(recovery_case["paper_path"].read_bytes()).hexdigest()
    # The pipeline authenticates the request again before the Foundation Writer.
    validated_again = validate_foundation_revision_request(
        result["request"], architecture=recovery_case["architecture"],
        execution_plan=recovery_case["plan"], evidence_root=Path(recovery_case["reporter"]["workspace"]),
    )
    assert validated_again["request_id"] == result["request"]["request_id"]
    assert validated_again["moderator_recovery"]["decision_id"] == "offline-moderation-decision"
    assert validated_again["moderator_recovery"]["instructions"] == "Correct the variance using the cited equation, then rerun."
    assert validated_again["moderator_recovery"]["expected_change"] == "The variance matches the cited equation."


@pytest.mark.parametrize("evidence", [[], ["paper_evidence/source/missing.txt"], ["paper_evidence/../private.txt"]])
def test_missing_foundation_evidence_is_observed_but_unsafe_paths_are_rejected(recovery_case, monkeypatch, evidence):
    verification = recovery_case["verification"]
    verification["rerun_evidence"].update(change_targets=["shared", "private"], paper_evidence_files=evidence)
    _moderator(monkeypatch, action="revise_foundation", component_ids=["shared"])
    result = task_recovery.resolve_revision_owner(
        record=recovery_case["record"], reporter=recovery_case["reporter"], verification=verification,
    )
    if evidence == ["paper_evidence/../private.txt"]:
        assert result["action"] != "revise_foundation"
    else:
        assert result["action"] == "revise_foundation"
        assert result["request"]["paper_evidence"] == []
        assert result["request"]["original_request"]["paper_evidence_files"] == evidence


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


def test_exhausted_writer_budget_cannot_be_overruled_by_moderator(recovery_case, monkeypatch):
    calls = _moderator(monkeypatch, action="revise_writer", component_ids=["private"])
    result = task_recovery.recover_writer_stall(
        record=recovery_case["record"], verification=recovery_case["verification"],
        trigger="writer_round_budget_exhausted", writer_budget_available=False,
    )
    assert len(calls) == 1
    assert "revise_writer" not in calls[0]["allowed_actions"]
    assert result["action"] != "revise_writer"
    assert not result.get("feedback")


def test_moderator_feedback_preserves_reporter_scientific_verdict(recovery_case, monkeypatch):
    before = deepcopy(recovery_case["verification"])
    calls = _moderator(monkeypatch, action="revise_writer", component_ids=["private"])
    result = task_recovery.recover_writer_stall(
        record=recovery_case["record"], verification=recovery_case["verification"],
        trigger="writer_no_progress", writer_budget_available=True,
    )
    assert len(calls) == 1
    assert result["action"] == "revise_writer"
    assert recovery_case["verification"] == before
    assert result["feedback"]["outcome"] == "not_reproduced"
    assert result["feedback"]["core_conclusions"] == before["core_conclusions"]
    assert result["feedback"]["moderator_instructions"]


def test_state_identity_ignores_rephrased_reasons(recovery_case, monkeypatch):
    calls = _moderator(monkeypatch)
    for reason in ["The implementation repeats the same state.", "Still unchanged after the previous attempt."]:
        verification = deepcopy(recovery_case["verification"])
        verification["decision_reason"] = reason
        verification["rerun_evidence"]["causal_change"] = reason
        task_recovery.recover_writer_stall(
            record=deepcopy(recovery_case["record"]), verification=verification,
            trigger="writer_no_progress", writer_budget_available=True,
        )
    assert len(calls) == 2
    assert calls[0]["state_id"] == calls[1]["state_id"]
    assert calls[0]["scope_id"] == calls[1]["scope_id"] == "unit_a:task:task_a"


def test_analysis_snapshot_change_invalidates_recovery_state(recovery_case):
    record = deepcopy(recovery_case["record"])
    record["analysis_snapshot_hash"] = "analysis-before"
    before = task_recovery.recovery_state_id(record, "writer_no_progress")
    record["analysis_snapshot_hash"] = "analysis-after"
    after = task_recovery.recovery_state_id(record, "writer_no_progress")
    assert before != after


@pytest.mark.parametrize("relative", ["src/private.py", "configs/task_a.json", "outputs/task_a/result.csv", "environment.lock.json"])
def test_state_identity_changes_with_real_execution_content(recovery_case, monkeypatch, relative):
    calls = _moderator(monkeypatch)

    def recover():
        task_recovery.recover_writer_stall(
            record=deepcopy(recovery_case["record"]), verification=deepcopy(recovery_case["verification"]),
            trigger="writer_no_progress", writer_budget_available=True,
        )

    recover()
    path = recovery_case["writer"] / relative
    path.write_bytes(path.read_bytes() + b"\nactual state change\n")
    recover()
    assert len(calls) == 2
    assert calls[0]["state_id"] != calls[1]["state_id"]


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
    original_link_check = foundation_snapshot.path_is_foundation_link
    original_open = Path.open

    def is_link(path):
        return Path(path) == link or original_link_check(path)

    def guarded_open(path, *args, **kwargs):
        assert not (path == link or path.is_relative_to(link)), "Recovery followed a linked file or junction."
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(task_recovery, "path_is_foundation_link", is_link)
    monkeypatch.setattr(foundation_snapshot, "path_is_foundation_link", is_link)
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


def test_same_unit_tasks_have_separate_moderation_scopes(recovery_case, monkeypatch):
    calls = _moderator(monkeypatch)
    for _index, _task, record, verification in _recovery_work(recovery_case):
        task_recovery.recover_writer_stall(
            record=record, verification=verification,
            trigger="writer_no_progress", writer_budget_available=True,
        )
    assert len(calls) == 2
    assert calls[0]["state_id"] == calls[1]["state_id"]
    assert {call["scope_id"] for call in calls} == {"unit_a:task:task_0", "unit_a:task:task_1"}


def test_exception_moderation_and_reporter_clarification_overlap_before_feedback(recovery_case, monkeypatch):
    work = _recovery_work(recovery_case)
    decisions = Barrier(2, timeout=5)
    clarifications = Barrier(2, timeout=5)
    lock = Lock()
    completed = set()
    localized = []
    model_context = ContextVar("test_recovery_model", default="wrong-default")
    model_context.set("unified-deepseek-model")

    def recover(**kwargs):
        assert model_context.get() == "unified-deepseek-model"
        decisions.wait()
        return {"action": "repair_reporter", "decision": {
            "decision_id": f"decision-{kwargs['record']['task_id']}",
            "instructions": "Clarify the exact private code correction.",
        }}

    def clarify(_index, task, record, _round):
        assert model_context.get() == "unified-deepseek-model"
        assert record["moderator_review_request"]["instructions"]
        clarifications.wait()
        with lock:
            completed.add(task["task_id"])
        return _reporter_return(record, record["task_verification"])

    def localize(feedback, **_kwargs):
        assert completed == {"task_0", "task_1"}
        localized.append(feedback["task_id"])
        return deepcopy(feedback)

    _moderator(monkeypatch, action="revise_writer")
    monkeypatch.setattr(runner, "recover_writer_stall", recover)
    monkeypatch.setattr(runner, "localize_writer_feedback", localize)
    result = runner._recover_writer_exceptions(
        work=work, trigger="writer_no_progress", writer_budget_available=True,
        callback=clarify, session_round=2,
    )
    assert set(result) == {"task_0", "task_1"}
    assert set(localized) == {"task_0", "task_1"}
    assert all(value["outcome"] == "not_reproduced" for value in result.values())


def test_exception_stop_keeps_original_science_and_records_stop_reason(recovery_case, monkeypatch):
    work = _recovery_work(recovery_case, count=1)
    original_claims = deepcopy(work[0][3]["core_conclusions"])
    monkeypatch.setattr(runner, "recover_writer_stall", lambda **_kwargs: {
        "action": "stop", "reason": "causal_stall_unresolved",
        "decision": {"decision_id": "stop-decision", "diagnosis": "No supported correction remains."},
    })
    result = runner._recover_writer_exceptions(
        work=work, trigger="writer_no_progress", writer_budget_available=True,
        callback=None, session_round=2,
    )
    record = work[0][2]
    assert result == {}
    assert record["task_verification"]["outcome"] == "not_reproduced"
    assert record["task_verification"]["core_conclusions"] == original_claims
    assert record["task_verification"]["host_action"] == "rerun_writer"
    assert record["coordination_status"] == "stopped"
    assert record["scientific_stop_reason"]


def test_exception_foundation_route_records_pending_shared_request(recovery_case, monkeypatch):
    work = _recovery_work(recovery_case, count=1)
    request = {"request_id": "verified-shared-fix", "component_ids": ["shared"],
               "evidence_root": recovery_case["reporter"]["workspace"]}
    monkeypatch.setattr(runner, "recover_writer_stall", lambda **_kwargs: {
        "action": "revise_foundation", "request": deepcopy(request), "decision": {"decision_id": "shared-fix"},
    })
    result = runner._recover_writer_exceptions(
        work=work, trigger="writer_no_progress", writer_budget_available=True,
        callback=None, session_round=2,
    )
    assert result == {}
    assert work[0][2]["foundation_revision_request"] == request
    assert work[0][2]["task_verification"]["outcome"] == "not_reproduced"


def test_clarified_reporter_cannot_restart_writer_after_budget_exhaustion(recovery_case, monkeypatch):
    work = _recovery_work(recovery_case, count=1)
    clarified = []
    monkeypatch.setattr(runner, "recover_writer_stall", lambda **_kwargs: {
        "action": "repair_reporter", "decision": {"decision_id": "clarify-at-cap", "instructions": "Clarify the cause."},
    })

    def clarify(_index, task, record, _round):
        clarified.append(task["task_id"])
        return _reporter_return(record, record["task_verification"])

    def cannot_localize(*_args, **_kwargs):
        pytest.fail("An exhausted Writer must not receive continuation feedback.")

    monkeypatch.setattr(runner, "localize_writer_feedback", cannot_localize)
    result = runner._recover_writer_exceptions(
        work=work, trigger="writer_round_budget_exhausted", writer_budget_available=False,
        callback=clarify, session_round=3,
    )
    assert clarified == ["task_0"]
    assert result == {}
    assert work[0][2]["task_verification"]["outcome"] == "not_reproduced"
    assert work[0][2]["task_verification"]["host_action"] == "rerun_writer"
    assert work[0][2]["coordination_status"] == "stopped"


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
        owner = kwargs["context"]["ownership"]["owner"]
        return {"action": "revise_writer" if owner == "private" else "repair_reporter", "decision_id": "clarify-owner",
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


@pytest.mark.parametrize("resume", [False, True])
def test_compound_shared_repair_is_returned_before_any_private_continuation(recovery_case, monkeypatch, tmp_path, resume):
    work = _recovery_work(recovery_case)
    unit = {"unit_id": "mixed_unit", "unit_index": 1, "task_ids": ["task_0", "task_1"],
            "members": [(index, task, {"task_id": task["task_id"], "module": task["task_id"],
                                      "output_subdir": task["task_id"]})
                        for index, task, _record, _verification in work]}
    sessions = []
    archives = []
    request = {"request_id": "pending-shared-correction", "component_ids": ["shared"]}

    def run_writer(**kwargs):
        sessions.append(kwargs["label"])
        assert len(sessions) <= (0 if resume else 1), "A pending Foundation correction must reach the host before another Writer run."
        return {"ok": True}

    def collect(**kwargs):
        record = deepcopy(work[kwargs["index"]][2])
        record.update(sandbox=str(kwargs["sandbox"]), writer_completed=True,
                      task_writer_status="ready_for_review")
        return record

    def review(**kwargs):
        kwargs["records"][0]["foundation_revision_request"] = deepcopy(request)
        return {"task_1": deepcopy(work[1][3])}

    monkeypatch.setattr(runner, "_prepare_execution_unit_writer_sandbox",
                        lambda **kwargs: Path(kwargs["sandbox"]).mkdir(parents=True, exist_ok=True))
    monkeypatch.setattr(runner, "_load_task_execution_binding", lambda *_args: None)
    monkeypatch.setattr(runner, "write_writer_input", lambda **_kwargs: None)
    monkeypatch.setattr(runner, "_build_execution_unit_writer_brief", lambda **_kwargs: "offline base prompt")
    monkeypatch.setattr(runner, "_build_execution_unit_continuation_brief", lambda **_kwargs: "offline continuation")
    monkeypatch.setattr(runner, "_run_task_writer_codex_session", run_writer)
    monkeypatch.setattr(runner, "_restore_trusted_files", lambda *_args: None)
    monkeypatch.setattr(runner, "_collect_task_writer_delivery", collect)
    monkeypatch.setattr(runner, "_review_execution_unit_tasks", review)
    monkeypatch.setattr(runner, "_archive_execution_unit_delivery", lambda **kwargs: archives.append(kwargs))
    monkeypatch.setattr(runner, "_external_writer_rerun_budget", lambda: 3)
    records = runner._run_one_execution_unit_writer(
        unit=unit, reuse_existing=resume, runtime_refresh_required=False,
        facts={}, experiment_index={}, paper={}, paper_path=tmp_path / "paper.pdf",
        paper_context_json="{}", paper_images=[], paper_thesis=None, foundation=None,
        analysis_snapshot_hash="analysis", analysis_artifacts={},
        task_root=tmp_path / "sandboxes", audit_dir=tmp_path / "audit",
        run_repro=True, review_feedback={}, task_review_callback=lambda *_args: {}, case_runtime=None,
    )
    assert records[0]["foundation_revision_request"] == request
    assert len(sessions) == (0 if resume else 1)
    assert archives == []


def test_exceptional_recovery_failure_does_not_discard_sibling_feedback(recovery_case, monkeypatch):
    work = _recovery_work(recovery_case)
    started = Barrier(2, timeout=5)

    def recover(**kwargs):
        started.wait()
        if kwargs["record"]["task_id"] == "task_0":
            raise RuntimeError("one task's local moderation cache is unreadable")
        return {"action": "revise_writer", "feedback": deepcopy(kwargs["verification"])}

    monkeypatch.setattr(runner, "recover_writer_stall", recover)
    monkeypatch.setattr(runner, "localize_writer_feedback", lambda feedback, **_kwargs: deepcopy(feedback))
    result = runner._recover_writer_exceptions(
        work=work, trigger="writer_no_progress", writer_budget_available=True,
        callback=None, session_round=2,
    )
    assert set(result) == {"task_1"}
    assert result["task_1"]["outcome"] == "not_reproduced"
    assert work[0][2]["task_verification"]["outcome"] == "not_reproduced"
    assert work[0][2]["task_verification"]["host_action"] == "rerun_writer"
    assert work[0][2]["coordination_status"] == "stopped"
    assert work[0][2]["scientific_stop_reason"]


def test_new_terminal_reporter_result_does_not_reopen_old_compound_feedback(recovery_case, monkeypatch, tmp_path):
    work = _recovery_work(recovery_case)
    unit = {"unit_id": "completed_unit", "unit_index": 1, "task_ids": ["task_0", "task_1"],
            "members": [(index, task, {"task_id": task["task_id"], "module": task["task_id"],
                                      "output_subdir": task["task_id"]})
                        for index, task, _record, _verification in work]}
    sessions = []
    review_rounds = []
    recoveries = []

    def run_writer(**kwargs):
        sessions.append(kwargs["label"])
        assert len(sessions) <= 2
        return {"ok": True}

    def collect(**kwargs):
        record = deepcopy(work[kwargs["index"]][2])
        record.update(sandbox=str(kwargs["sandbox"]), writer_completed=True,
                      task_writer_status="ready_for_review")
        return record

    def review(**kwargs):
        review_rounds.append(kwargs["session_round"])
        if len(review_rounds) == 1:
            return {"task_0": deepcopy(work[0][3])}
        for record in kwargs["records"]:
            record["task_verification"].update(host_action="complete", rerun_reason="none")
        return {}

    def recover(**kwargs):
        recoveries.append(kwargs)
        return {"action": "stop"}

    monkeypatch.setattr(runner, "_prepare_execution_unit_writer_sandbox",
                        lambda **kwargs: Path(kwargs["sandbox"]).mkdir(parents=True, exist_ok=True))
    monkeypatch.setattr(runner, "_load_task_execution_binding", lambda *_args: None)
    monkeypatch.setattr(runner, "write_writer_input", lambda **_kwargs: None)
    monkeypatch.setattr(runner, "_build_execution_unit_writer_brief", lambda **_kwargs: "offline base prompt")
    monkeypatch.setattr(runner, "_build_execution_unit_continuation_brief", lambda **_kwargs: "offline continuation")
    monkeypatch.setattr(runner, "_run_task_writer_codex_session", run_writer)
    monkeypatch.setattr(runner, "_restore_trusted_files", lambda *_args: None)
    monkeypatch.setattr(runner, "_collect_task_writer_delivery", collect)
    monkeypatch.setattr(runner, "_review_execution_unit_tasks", review)
    monkeypatch.setattr(runner, "_archive_execution_unit_delivery", lambda **_kwargs: None)
    monkeypatch.setattr(runner, "_external_writer_rerun_budget", lambda: 3)
    monkeypatch.setattr(runner, "recover_writer_stall", recover)
    records = runner._run_one_execution_unit_writer(
        unit=unit, reuse_existing=False, runtime_refresh_required=False,
        facts={}, experiment_index={}, paper={}, paper_path=tmp_path / "paper.pdf",
        paper_context_json="{}", paper_images=[], paper_thesis=None, foundation=None,
        analysis_snapshot_hash="analysis", analysis_artifacts={},
        task_root=tmp_path / "sandboxes", audit_dir=tmp_path / "audit",
        run_repro=True, review_feedback={}, task_review_callback=lambda *_args: {}, case_runtime=None,
    )
    assert len(sessions) == 2
    assert review_rounds == [1, 2]
    assert recoveries == []
    assert all(record["task_verification"]["host_action"] == "complete" for record in records)
    assert all("scientific_stop_reason" not in record for record in records)
