from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
import json
from pathlib import Path
from threading import Barrier

import pytest

from geng_agent import moderator as module
from geng_agent.model_config import CodexModelConfig, get_current_model_config, model_config_scope


def _decision(**overrides):
    return {"schema_version": "1.0", "action": "revise_writer", "diagnosis": "现有公式实现与输入证据不符",
            "instructions": "修正指定函数的归一化，不修改目标", "component_ids": [],
            "evidence_refs": [{"root": "writer", "path": "task.py"}],
            "expected_change": "相同输入下得到定义中的量纲", **overrides}


def _request(root, **overrides):
    return {"trigger": "stalled_revision", "scope_id": "task:one", "state_id": "immutable-state",
            "context": {"current_goal": "check the recorded normalization"},
            "evidence_roots": {"writer": root / "source"},
            "allowed_actions": ("revise_writer", "repair_reporter", "stop"), **overrides}


@pytest.fixture
def evidence(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "task.py").write_text("VALUE = 2\n", encoding="utf-8")
    return tmp_path


def _transport(monkeypatch, result=None, during=None):
    calls = []

    def run(**kwargs):
        calls.append(kwargs)
        assert kwargs["role"] == "moderator"
        assert kwargs["sandbox"] == "read-only"
        packet = json.loads((kwargs["work_dir"] / "incident.json").read_text(encoding="utf-8"))
        assert packet["evidence"][0]["path"] == "task.py"
        if during:
            during(kwargs)
        payload = _decision() if result is None else result
        (kwargs["audit_dir"] / "moderator_last_message.txt").write_text(json.dumps(payload), encoding="utf-8")
        return {"ok": True}

    monkeypatch.setattr(module, "run_codex_subprocess", run)
    return calls


def test_inactive_moderator_never_calls_a_model(monkeypatch, evidence):
    calls = _transport(monkeypatch)
    result = module.request_moderation(**_request(evidence))
    assert result["action"] == "stop" and result["status"] == "not_active"
    assert calls == []


def test_decision_is_evidence_bound_and_never_reissued_after_resume(monkeypatch, evidence):
    calls = _transport(monkeypatch)
    audit = evidence / "audit"
    with module.moderator_scope(audit):
        result = module.request_moderation(**_request(evidence))
    assert result["action"] == "revise_writer" and result["decision_id"]
    assert (evidence / "source/task.py").read_text() == "VALUE = 2\n"
    with module.moderator_scope(audit):
        again = module.request_moderation(**{**_request(evidence), "context": {"reason": "same request rephrased"}})
    assert again["status"] == "already_diagnosed" and again["action"] == "stop"
    assert len(calls) == 1
    assert len(list((audit / "moderator").glob("*/*/decision.json"))) == 1


@pytest.mark.parametrize("override", [
    {"outcome": "reproduced"},
    {"action": "run_arbitrary_shell"},
    {"action": "revise_foundation", "component_ids": ["unassigned"]},
    {"instructions": ""}, {"expected_change": ""}, {"evidence_refs": []},
    {"evidence_refs": [{"root": "writer", "path": "../outside.txt"}]},
    {"evidence_refs": [{"root": "writer", "path": "not_present.py"}]},
])
def test_invalid_decision_cannot_authorize_recovery(monkeypatch, evidence, override):
    _transport(monkeypatch, _decision(**override))
    with module.moderator_scope(evidence / "audit"):
        result = module.request_moderation(**_request(evidence))
    assert result["action"] == "stop"
    assert result["status"] == "invalid_or_unavailable"


@pytest.mark.parametrize("target", ["source", "snapshot", "packet"])
def test_changed_evidence_invalidates_the_decision(monkeypatch, evidence, target):
    def mutate(kwargs):
        path = (evidence / "source/task.py" if target == "source" else
                kwargs["work_dir"] / "evidence/0000.py" if target == "snapshot" else
                kwargs["work_dir"] / "incident.json")
        path.write_text("changed", encoding="utf-8")

    _transport(monkeypatch, during=mutate)
    with module.moderator_scope(evidence / "audit"):
        result = module.request_moderation(**_request(evidence))
    assert result["status"] == "stale_evidence" and result["action"] == "stop"


def test_parallel_incidents_do_not_share_a_model_call_lock(monkeypatch, evidence):
    barrier = Barrier(2)
    profile = CodexModelConfig(provider="deepseek", model="fixture", reasoning_effort="max")

    def inspect(kwargs):
        assert get_current_model_config() is profile
        barrier.wait(timeout=5)

    calls = _transport(monkeypatch, during=inspect)
    with model_config_scope(profile), module.moderator_scope(evidence / "audit"):
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(copy_context().run, module.request_moderation,
                                       **{**_request(evidence), "scope_id": f"task:{index}"}) for index in range(2)]
            results = [future.result() for future in futures]
    assert all(result["action"] == "revise_writer" for result in results)
    assert len(calls) == 2
    assert len({str(call["work_dir"]) for call in calls}) == 2


def test_scope_budget_survives_resume_without_rewording_loophole(monkeypatch, evidence):
    monkeypatch.setenv("GENG_MODERATOR_MAX_DECISIONS_PER_SCOPE", "1")
    calls = _transport(monkeypatch)
    with module.moderator_scope(evidence / "audit"):
        first = module.request_moderation(**_request(evidence))
    with module.moderator_scope(evidence / "audit"):
        second = module.request_moderation(**{**_request(evidence), "state_id": "new-state"})
    assert first["action"] == "revise_writer"
    assert second["action"] == "stop" and second["status"] == "budget_exhausted"
    assert len(calls) == 1


def test_interrupted_diagnosis_can_resume_without_a_permanent_directory_block(monkeypatch, evidence):
    def interrupt(**kwargs):
        raise KeyboardInterrupt()

    monkeypatch.setattr(module, "run_codex_subprocess", interrupt)
    with module.moderator_scope(evidence / "audit"), pytest.raises(KeyboardInterrupt):
        module.request_moderation(**_request(evidence))
    calls = _transport(monkeypatch)
    with module.moderator_scope(evidence / "audit"):
        result = module.request_moderation(**_request(evidence))
    assert result["status"] == "decided" and len(calls) == 1
    with module.moderator_scope(evidence / "audit"):
        again = module.request_moderation(**_request(evidence))
    assert again["status"] == "already_diagnosed"
    assert len(calls) == 1


def test_model_failure_preserves_an_explicit_unresolved_result(monkeypatch, evidence):
    monkeypatch.setattr(module, "run_codex_subprocess", lambda **kwargs: {"ok": False, "error_kind": "fixture"})
    with module.moderator_scope(evidence / "audit"):
        result = module.request_moderation(**_request(evidence))
    assert result["action"] == "stop" and result["status"] == "model_failed"


def test_output_schema_has_only_closed_objects_and_all_properties_required():
    def check(value):
        if isinstance(value, dict):
            if value.get("type") == "object":
                assert value.get("additionalProperties") is False
                assert set(value.get("required", [])) == set(value.get("properties", {}))
            for child in value.values():
                check(child)
        elif isinstance(value, list):
            for child in value:
                check(child)

    check(module._decision_output_schema())


@pytest.mark.parametrize("arguments", [{}, {"path": "图片/结果.png", "options": {"verify": True}, "ids": [1, 2]}])
def test_strict_wire_arguments_preserve_nested_values(monkeypatch, evidence, arguments):
    _transport(monkeypatch, _decision(repair_arguments=json.dumps(arguments, ensure_ascii=False)))
    with module.moderator_scope(evidence / "audit"):
        result = module.request_moderation(**_request(evidence))
    assert result["status"] == "decided"
    assert result["repair_arguments"] == arguments


def test_multiline_text_arrays_are_normalized_without_relaxing_action_validation(monkeypatch, evidence):
    _transport(monkeypatch, _decision(instructions=["修正交接文件。", "保留论文参数。"],
                                      expected_change=["交接字段可读取。"],
                                      diagnosis=["模型把段落编码为数组。 "]))
    with module.moderator_scope(evidence / "audit"):
        result = module.request_moderation(**_request(evidence))
    assert result["status"] == "decided"
    assert result["instructions"] == "修正交接文件。\n保留论文参数。"


def test_qualified_tool_address_is_normalized_only_when_its_local_name_is_ready():
    decision = {"next_nodes": ["backfill:replan"], "next_node": ""}
    assert module._canonical_start_nodes(decision, ["search", "replan"], "run:x:node:tools:backfill") == ["replan"]
    assert module._canonical_start_nodes(decision, ["search"], "run:x:node:tools:backfill") == ["backfill:replan"]


@pytest.mark.parametrize("arguments", ["not json", "[]", "null", "42"])
def test_non_object_wire_arguments_cannot_authorize_recovery(monkeypatch, evidence, arguments):
    _transport(monkeypatch, _decision(repair_arguments=arguments))
    with module.moderator_scope(evidence / "audit"):
        result = module.request_moderation(**_request(evidence))
    assert result["status"] == "invalid_or_unavailable"
    assert result["action"] == "stop"


def test_unwritable_audit_stops_without_calling_model_or_losing_the_task(monkeypatch, evidence):
    calls = _transport(monkeypatch)

    def no_disk(*args, **kwargs):
        raise OSError("fixture: audit disk unavailable")

    monkeypatch.setattr(module, "write_json", no_disk)
    with module.moderator_scope(evidence / "audit"):
        result = module.request_moderation(**_request(evidence))
    assert result["action"] == "stop" and result["status"] == "audit_unavailable"
    assert calls == []
    assert (evidence / "source/task.py").read_text() == "VALUE = 2\n"
