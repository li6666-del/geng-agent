from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
import json
from pathlib import Path
from threading import Barrier

import pytest

from geng_agent import moderator as moderation
from geng_agent import supervisor as module
from geng_agent.progress import PipelineCancelled


def _transport(monkeypatch, decide=None):
    calls = []

    def run(**kwargs):
        packet = json.loads((kwargs["work_dir"] / "incident.json").read_text(encoding="utf-8"))
        calls.append(packet)
        action = "block"
        if packet["trigger"] == "run_dispatch":
            action = "start" if packet["context"]["ready"] else "finish"
        decision = {"schema_version": "1.0", "action": action, "diagnosis": "交接证据已检查",
                    "instructions": "按当前有效结果继续", "component_ids": [],
                    "evidence_refs": [{"root": "handoff", "path": "handoff.json"}],
                    "expected_change": "后续阶段收到真实交付"}
        if action == "start":
            decision["next_node"] = packet["context"]["ready"][0]
        if decide:
            decision.update(decide(packet, kwargs) or {})
        (kwargs["audit_dir"] / "moderator_last_message.txt").write_text(json.dumps(decision), encoding="utf-8")
        return {"ok": True}

    monkeypatch.setattr(moderation, "run_codex_subprocess", run)
    return calls


def _supervisor(tmp_path):
    return module.RunSupervisor(tmp_path / "case", tmp_path / "audit", {"goal": "固定三点BER，不调参"})


def test_no_scope_keeps_standalone_operation_compatible(monkeypatch):
    calls = _transport(monkeypatch)
    assert module.supervised_call("writer", lambda: 17) == 17
    assert not calls


def test_scientific_non_support_is_a_valid_handoff(monkeypatch, tmp_path):
    calls = _transport(monkeypatch)
    supervisor = _supervisor(tmp_path)
    result = {"ok": False, "scientific_outcome": "not_reproduced"}
    with module.supervisor_scope(supervisor):
        assert module.supervised_call("reporter:t1", lambda: result) == result
        with moderation.moderator_scope(tmp_path / "audit") as nested:
            assert nested is supervisor.moderator
    assert not calls  # A scientific disagreement is a completed handoff.
    assert supervisor.snapshot()["nodes"][0]["status"] == "published"


def test_explicit_failed_operation_uses_moderator_stop(monkeypatch, tmp_path):
    calls = _transport(monkeypatch, lambda packet, kwargs: {"action":"block"})

    def operation():
        raise module.NodeFailure("full进程失败", result={"returncode": 1})

    with module.supervisor_scope(_supervisor(tmp_path)), pytest.raises(module.StageBlocked) as error:
        module.supervised_call("writer:t1", operation)
    assert isinstance(error.value.original_error, module.NodeFailure)
    assert calls[0]["context"]["result"]["result"]["returncode"] == 1
    assert "approve" not in calls[0]["allowed_actions"]


def test_routine_handoff_does_not_spend_moderator_budget(monkeypatch, tmp_path):
    monkeypatch.setenv("GENG_MODERATOR_MAX_DECISIONS_PER_SCOPE", "0")
    calls = _transport(monkeypatch)
    supervisor = _supervisor(tmp_path)
    with module.supervisor_scope(supervisor):
        for _ in range(2):
            assert module.supervised_call("analysis", lambda: {"facts": [1]}) == {"facts": [1]}
    assert not calls
    assert supervisor.snapshot()["nodes"][0]["status"] == "published"


def test_evidence_changes_do_not_add_a_routine_model_gate(monkeypatch, tmp_path):
    calls = _transport(monkeypatch)
    source = tmp_path / "source"
    source.mkdir()
    code = source / "task.py"
    code.write_text("value = 1")
    supervisor = _supervisor(tmp_path)
    with module.supervisor_scope(supervisor):
        module.supervised_call("writer", lambda: {"handoff": "complete"}, evidence_roots={"source": source})
        code.write_text("value = 2")
        module.supervised_call("writer", lambda: {"handoff": "complete"}, evidence_roots={"source": source})
    assert not calls
    assert code.read_text() == "value = 2"


def test_owner_repair_is_verified_and_journaled(monkeypatch, tmp_path):
    def decide(packet, _):
        if packet["trigger"] == "node_failed":
            return {"action": "retry", "instructions": "删除不存在的兼容导入，使用现有本地模块", "expected_change": "同一入口导入成功"}

    calls = _transport(monkeypatch, decide)
    supervisor = _supervisor(tmp_path)
    state = {"fixed": False, "runs": 0}

    def operation():
        state["runs"] += 1
        if not state["fixed"]:
            raise module.NodeFailure("module not found")
        return {"full": "verified"}

    def repair(decision):
        assert "导入" in decision["instructions"]
        state["fixed"] = True

    with module.supervisor_scope(supervisor):
        assert module.supervised_call("foundation", operation, repair=repair) == {"full": "verified"}
    assert state["runs"] == 2 and len(calls) == 1
    actions = list((supervisor.root / "nodes").glob("*/actions/*/state.json"))
    assert len(actions) == 1
    assert json.loads(actions[0].read_text(encoding="utf-8"))["status"] == "published"


def test_parallel_node_reviews_do_not_hold_a_global_model_lock(monkeypatch, tmp_path):
    barrier = Barrier(2)
    _transport(monkeypatch, lambda *_: barrier.wait(timeout=5) and None)
    supervisor = _supervisor(tmp_path)
    with module.supervisor_scope(supervisor), ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(copy_context().run, module.supervised_call, f"writer:{i}", lambda: {"ok": True})
                   for i in range(2)]
        assert all(future.result() == {"ok": True} for future in futures)


def test_control_handoff_and_parent_block_do_not_trigger_duplicate_diagnosis(monkeypatch, tmp_path):
    calls = _transport(monkeypatch)

    class Handoff(Exception):
        pass

    def operation():
        raise Handoff("request environment")

    with module.supervisor_scope(_supervisor(tmp_path)), pytest.raises(Handoff):
        module.supervised_call("writer", operation, passthrough=(Handoff,))
    assert not calls


def test_interrupted_execution_without_adapter_is_routed_to_moderator(monkeypatch, tmp_path):
    calls = _transport(monkeypatch)
    supervisor = _supervisor(tmp_path)
    supervisor._save("writer", status="dispatched", inputs={}, attempt=0)
    executed = []
    with module.supervisor_scope(supervisor), pytest.raises(module.StageBlocked) as error:
        module.supervised_call("writer", lambda: executed.append(True))
    assert error.value.decision["action"] == "block"
    assert not executed and len(calls) == 1
    assert "resume adapter" in calls[0]["context"]["result"]["message"]


def test_completed_execution_is_reconciled_without_reexecution(monkeypatch, tmp_path):
    calls = _transport(monkeypatch)
    supervisor = _supervisor(tmp_path)
    supervisor._save("writer", status="dispatched", inputs={}, attempt=0)
    executed = []
    with module.supervisor_scope(supervisor):
        result = module.supervised_call("writer", lambda: executed.append(True),
                                        reconcile=lambda record: {"receipt": "verified-existing-full"})
    assert result == {"receipt": "verified-existing-full"}
    assert not executed and not calls


def test_interrupted_asset_repair_uses_registered_reconciliation(monkeypatch, tmp_path):
    _transport(monkeypatch)
    supervisor = _supervisor(tmp_path)
    executed = []
    supervisor.register_repair_handler("publish", lambda args: executed.append(args),
                                        reconcile=lambda args, record: {"already_published": True})
    decision = {"action": "repair_artifacts", "repair_operation": "publish", "repair_arguments": {"asset": "image"},
                "decision_id": "1234567890"}
    path = supervisor._node_dir("delivery") / "actions/1234567890/state.json"
    moderation.write_json(path, {"status": "dispatched", "decision": decision})
    supervisor._apply_repair("delivery", decision, None)
    assert not executed
    assert json.loads(path.read_text())["result"]["already_published"] is True


def test_supervisor_state_does_not_contaminate_node_evidence(tmp_path):
    supervisor = _supervisor(tmp_path)
    audit = tmp_path / "audit"
    before = module._evidence_identity({"audit": audit})
    supervisor.record_phase("analysis", "completed", summary={"facts": 10})
    assert before == module._evidence_identity({"audit": audit})


def test_cancellation_is_not_diagnosed_as_an_engineering_failure(monkeypatch, tmp_path):
    calls = _transport(monkeypatch)

    def operation():
        raise PipelineCancelled("用户取消")

    with module.supervisor_scope(_supervisor(tmp_path)), pytest.raises(PipelineCancelled):
        module.supervised_call("writer", operation)
    assert not calls


def test_cancel_between_operation_and_review_does_not_start_a_model(monkeypatch, tmp_path):
    calls = _transport(monkeypatch)
    cancelled = False

    class Reporter:
        def emit(self, *args, **kwargs):
            pass

        def check_cancelled(self):
            if cancelled:
                raise PipelineCancelled("用户取消")

    def operation():
        nonlocal cancelled
        cancelled = True
        return {"done": True}

    supervisor = module.RunSupervisor(tmp_path / "case", tmp_path / "audit", {}, reporter=Reporter())
    with module.supervisor_scope(supervisor), pytest.raises(PipelineCancelled):
        module.supervised_call("writer", operation)
    assert not calls


def test_same_live_node_cannot_be_dispatched_twice(monkeypatch, tmp_path):
    _transport(monkeypatch)
    supervisor = _supervisor(tmp_path)
    with moderation._event_claim(supervisor._node_dir("writer") / "owner") as acquired:
        assert acquired
        with module.supervisor_scope(supervisor), pytest.raises(module.StageBlocked) as blocked:
            module.supervised_call("writer", lambda: pytest.fail("duplicate execution"))
    assert blocked.value.decision["status"] == "in_progress"


def test_abandoned_reservation_file_does_not_own_the_node_forever(monkeypatch, tmp_path):
    _transport(monkeypatch)
    supervisor = _supervisor(tmp_path)
    node = supervisor._node_dir("writer") / "owner"
    moderation.write_json(node / "active.json", {"pid": 999999999, "token": "abandoned"})
    with module.supervisor_scope(supervisor):
        assert module.supervised_call("writer", lambda: {"done": True}) == {"done": True}


def test_single_file_evidence_ignores_unrelated_case_outputs(monkeypatch, tmp_path):
    calls = _transport(monkeypatch)
    case = tmp_path / "case"
    case.mkdir()
    facts = case / "facts.json"
    facts.write_text('{"facts": [1]}')
    roots = {"facts": facts}
    supervisor = _supervisor(tmp_path)
    with module.supervisor_scope(supervisor):
        module.supervised_call("understanding", lambda: {"facts": [1]}, evidence_roots=roots)
        (case / "report.md").write_text("a later report")
        module.supervised_call("understanding", lambda: {"facts": [1]}, evidence_roots=roots)
    assert not calls
    with pytest.raises(ValueError):
        moderation._regular_source(facts, "report.md")


def test_node_can_add_its_actual_output_evidence_after_execution(monkeypatch, tmp_path):
    calls = _transport(monkeypatch)
    roots = {}
    output = tmp_path / "review.json"

    def operation():
        output.write_text('{"scientific_outcome": "not_reproduced"}')
        roots["reporter"] = output
        return {"review_path": str(output)}

    supervisor = _supervisor(tmp_path)
    with module.supervisor_scope(supervisor):
        module.supervised_call("reporter", operation, evidence_roots=roots)
    assert not calls
    assert output.is_file()
    assert supervisor.snapshot()["nodes"][0]["status"] == "published"


def test_dispatched_owner_instruction_can_be_restored_without_repeating_execution(tmp_path):
    supervisor = _supervisor(tmp_path)
    decision = {"action": "retry", "decision_id": "restore-owner", "instructions": "修正导入"}
    path = supervisor._node_dir("foundation") / "actions/restore-owner/state.json"
    moderation.write_json(path, {"status": "dispatched", "decision": decision})
    prepared = []
    supervisor._apply_repair("foundation", decision, lambda value: prepared.append(value))
    assert supervisor.current_instruction("foundation") == decision
    assert prepared == [decision]
    assert json.loads(path.read_text(encoding="utf-8"))["status"] == "completed"


def test_interrupted_new_execution_cannot_reuse_an_older_in_memory_result(monkeypatch, tmp_path):
    _transport(monkeypatch)
    supervisor = _supervisor(tmp_path)

    def interrupted():
        raise KeyboardInterrupt()

    with module.supervisor_scope(supervisor):
        module.supervised_call("writer", lambda: {"receipt": "old"})
        with pytest.raises(KeyboardInterrupt):
            module.supervised_call("writer", interrupted)
    assert "writer" not in supervisor._results
    with module.supervisor_scope(_supervisor(tmp_path)), pytest.raises(module.StageBlocked) as blocked:
        module.supervised_call("writer", lambda: {"receipt": "must-not-run"})
    assert blocked.value.decision["action"] == "block"


def test_published_owner_instruction_survives_restart_and_changed_inputs_drop_it(monkeypatch, tmp_path):
    def decide(packet, _):
        if packet["trigger"] == "node_failed":
            return {"action": "retry", "instructions": "使用已确认的同一修复提示词", "expected_change": "修复后的缓存可继续使用"}

    _transport(monkeypatch, decide)
    supervisor = _supervisor(tmp_path)

    def operation():
        instruction = module.current_supervisor().current_instruction("analysis")
        if not instruction:
            raise module.NodeFailure("requires concrete repair")
        return {"prompt_instruction": instruction["instructions"]}

    with module.supervisor_scope(supervisor):
        first = module.supervised_call("analysis", operation, inputs={"source": 1}, repair=lambda _: None)
    state = moderation._read_record(supervisor._node_dir("analysis") / "state.json")
    assert state["decision"]["action"] == "continue"
    assert state["owner_instruction"]["action"] == "retry"
    restarted = _supervisor(tmp_path)
    with module.supervisor_scope(restarted):
        restored = module.supervised_call("analysis", operation, inputs={"source": 1}, repair=lambda _: None)
        assert first == restored
        module.supervised_call("analysis", lambda: {"instruction": restarted.current_instruction("analysis")},
                                inputs={"source": 2})
        assert restarted.current_instruction("analysis") is None


def test_global_outline_keeps_every_node_without_repeating_completed_science(tmp_path):
    supervisor = _supervisor(tmp_path)
    repeated_science = "DO_NOT_REPEAT_WHOLE_FACTS_OR_PLAN_" * 4000
    for index in range(12):
        supervisor._save(f"completed:{index}", status="published", attempt=1,
                         summary={"facts": repeated_science, "tasks": [repeated_science]},
                         decision={"action": "approve", "diagnosis": "已核对"})
    supervisor._save("writer:active", status="dispatched", attempt=2,
                     summary={"previous_plan": repeated_science})
    supervisor._save("reporter:failed", status="failed", attempt=3,
                     error={"error_kind": "missing_receipt", "message": "缺少当前full收据", "result": repeated_science})
    snapshot = supervisor.snapshot()
    assert isinstance(snapshot["nodes"], list)
    assert len(snapshot["nodes"]) == 14
    states = {item["node_id"]: item for item in snapshot["nodes"]}
    assert states["writer:active"]["status"] == "dispatched"
    assert states["reporter:failed"]["attempt"] == 3
    assert all("state_record" in item for item in snapshot["nodes"])
    detail_ids = {item["node_id"] for item in snapshot["node_details"]}
    assert {"writer:active", "reporter:failed"} <= detail_ids
    assert len([item for item in detail_ids if item.startswith("completed:")]) <= 2
    serialized = json.dumps(snapshot, ensure_ascii=False)
    assert "DO_NOT_REPEAT_WHOLE_FACTS_OR_PLAN_" not in serialized
    assert len(serialized.encode("utf-8")) < 12000


def test_many_failures_still_keep_all_nodes_in_the_global_outline(tmp_path):
    supervisor = _supervisor(tmp_path)
    for index in range(20):
        supervisor._save(f"writer:{index}", status="failed", attempt=index,
                         error={"error_kind": "fixture", "message": "错误" * 5000})
    snapshot = supervisor.snapshot()
    assert len(snapshot["nodes"]) == 20
    assert len(snapshot["node_details"]) == 8
    assert snapshot["detail_omissions"] == 12
    assert len(json.dumps(snapshot, ensure_ascii=False).encode("utf-8")) < 18000


def test_cancellation_in_registered_repair_is_propagated_not_blocked(monkeypatch, tmp_path):
    calls = _transport(monkeypatch, lambda *_: {
        "action": "repair_artifacts", "repair_operation": "restore", "repair_arguments": {},
        "instructions": "恢复已核验的图片", "expected_change": "已核验图片重新发布"})
    supervisor = _supervisor(tmp_path)

    def repair(_arguments):
        raise PipelineCancelled("用户取消修复")

    def operation():
        raise module.NodeFailure("图片交接失败")

    supervisor.register_repair_handler("restore", repair)
    with module.supervisor_scope(supervisor), pytest.raises(PipelineCancelled):
        module.supervised_call("delivery", operation)
    assert len(calls) == 1
    record = moderation._read_record(supervisor._node_dir("delivery") / "state.json")
    assert record["status"] == "interrupted"
    assert record["decision"]["action"] == "repair_artifacts"


@pytest.mark.parametrize("node_id", ["paper_understanding", "experiment_planning", "report_editor"])
def test_cache_observation_does_not_repeat_approval_but_business_changes_do(monkeypatch, tmp_path, node_id):
    from copy import deepcopy
    from geng_agent.consolidated_analysis import _analysis_handoff_summary
    from geng_agent.pipeline_report_delivery import _report_editor_handoff_summary

    calls = _transport(monkeypatch)
    editor = node_id == "report_editor"
    summarize = _report_editor_handoff_summary if editor else _analysis_handoff_summary
    payload = {"facts_or_delivery": {"accepted_value": 1}, "error": {"retained_note": "unchanged"}}
    if editor:
        payload["cached"] = False
    else:
        payload["_meta"] = {"cache_reused": False, "scientific_metadata": "preserve"}
    source = tmp_path / "accepted.json"
    source.write_text(json.dumps(payload), encoding="utf-8")
    with module.supervisor_scope(_supervisor(tmp_path)):
        module.supervised_call(node_id, lambda: payload, summarize=summarize, evidence_roots={"accepted": source})
    resumed = deepcopy(payload)
    if editor:
        resumed["cached"] = True
    else:
        resumed["_meta"]["cache_reused"] = True
    original_resumed = deepcopy(resumed)
    with module.supervisor_scope(_supervisor(tmp_path)):
        returned = module.supervised_call(node_id, lambda: resumed, summarize=summarize,
                                          evidence_roots={"accepted": source})
        assert returned is resumed and resumed == original_resumed
        assert not calls
        changed = deepcopy(resumed)
        changed["facts_or_delivery"]["accepted_value"] = 2
        module.supervised_call(node_id, lambda: changed, summarize=summarize,
                                evidence_roots={"accepted": source})
    assert not calls
    state = moderation._read_record(_supervisor(tmp_path)._node_dir(node_id) / "state.json")
    assert state["summary"]["facts_or_delivery"]["accepted_value"] == 2
