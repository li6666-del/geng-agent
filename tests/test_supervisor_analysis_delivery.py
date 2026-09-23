"""Node recovery preserves scientific outputs and retries only their owner."""
from __future__ import annotations

import copy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from geng_agent.consolidated_analysis import supervised_analysis_stage
from geng_agent.pipeline_report_delivery import ReportOperationError, run_supervised_report_editor
from geng_agent.pipeline_report_flow import run_report_flow


def _snapshot_node_evidence(tmp_path, kwargs):
    from geng_agent.moderator import _snapshot_evidence
    snapshots = tmp_path / "snapshots"
    snapshots.mkdir(exist_ok=True)
    workspace = snapshots / str(len(list(snapshots.iterdir())))
    workspace.mkdir()
    return _snapshot_evidence(workspace, kwargs["evidence_roots"])


def test_analysis_owner_retry_disables_only_its_cache_and_receives_guidance(tmp_path, monkeypatch):
    from geng_agent import supervisor

    calls = []
    candidate = {"tasks": {"repro_tasks": [{"task_id": "t", "goal": "retain all three SNR points"}]}}

    def owner(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise RuntimeError("binding refers to an unknown experiment")
        return candidate

    def supervised(node_id, operation, **kwargs):
        _snapshot_node_evidence(tmp_path, kwargs)
        assert node_id == "experiment_planning"
        with pytest.raises(RuntimeError):
            operation()
        kwargs["repair"]({"action": "retry", "instructions": "Correct the binding reference; preserve all SNR points"})
        return operation()

    monkeypatch.setattr(supervisor, "current_supervisor", lambda: SimpleNamespace(current_instruction=lambda _node: None))
    monkeypatch.setattr(supervisor, "supervised_call", supervised)
    fallback = Mock()
    result = supervised_analysis_stage(
        SimpleNamespace(_load_or_create_analysis_stage_json=owner),
        SimpleNamespace(output_dir=tmp_path), node_id="experiment_planning",
        prompt="Keep the paper scope", resume=True, fallback_factory=fallback,
        stage_label="02a_plan_experiments", output_path=tmp_path / "plan.json",
    )
    assert result == candidate
    assert [call["resume"] for call in calls] == [True, False]
    assert all(call["fallback_factory"] is None for call in calls)
    assert "Correct the binding reference" in calls[1]["prompt"]
    assert "preserve all SNR points" in calls[1]["prompt"]
    fallback.assert_not_called()


def test_standalone_analysis_keeps_existing_fallback(tmp_path, monkeypatch):
    from geng_agent import supervisor

    monkeypatch.setattr(supervisor, "current_supervisor", lambda: None)
    fallback = Mock()
    owner = Mock(return_value={"facts": {}})
    supervised_analysis_stage(SimpleNamespace(_load_or_create_analysis_stage_json=owner),
        SimpleNamespace(output_dir=tmp_path), node_id="paper_understanding", prompt="paper",
        resume=True, fallback_factory=fallback, stage_label="01_understand_paper", output_path=tmp_path / "facts.json")
    assert owner.call_args.kwargs["fallback_factory"] is fallback
    assert owner.call_args.kwargs["resume"] is True


def test_interrupted_analysis_handoff_reconciles_through_owner_cache(tmp_path, monkeypatch):
    from geng_agent.supervisor import RunSupervisor, supervisor_scope

    generated = []
    cache = {}

    def owner(**kwargs):
        if kwargs["resume"] and cache:
            return {**cache, "_meta": {"cache_reused": True}}
        generated.append("model operation")
        cache.update(facts={"engineering_facts": []}, limitations=[])
        return dict(cache)

    arguments = dict(node_id="paper_understanding", prompt="paper", resume=True,
        stage_label="01_understand_paper", output_path=tmp_path / "facts.json")
    pipeline = SimpleNamespace(_load_or_create_analysis_stage_json=owner)
    context = SimpleNamespace(output_dir=tmp_path)
    first = RunSupervisor(tmp_path, tmp_path / "audit", {"paper": "stable"})
    monkeypatch.setattr(first, "_request", Mock(side_effect=AssertionError("routine handoff called moderator")))
    with supervisor_scope(first):
        supervised_analysis_stage(pipeline, context, **arguments)
    first._save("paper_understanding", status="dispatched")
    resumed = RunSupervisor(tmp_path, tmp_path / "audit", {"paper": "stable"})
    monkeypatch.setattr(resumed, "_request", Mock(side_effect=AssertionError("routine handoff called moderator")))
    with supervisor_scope(resumed):
        result = supervised_analysis_stage(pipeline, context, **arguments)
    assert result["_meta"]["cache_reused"] is True
    assert generated == ["model operation"]


def test_repaired_analysis_approval_interruption_reuses_guidance_bound_cache(tmp_path, monkeypatch):
    from geng_agent.supervisor import RunSupervisor, supervisor_scope

    calls, cache = [], {}
    guidance = {"action": "retry", "diagnosis": "Correct the reference", "instructions": "Preserve the experiment"}

    def owner(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise RuntimeError("bad reference")
        if kwargs["resume"] and cache.get("prompt") == kwargs["prompt"]:
            return {"tasks": [], "_meta": {"cache_reused": True}}
        cache["prompt"] = kwargs["prompt"]
        return {"tasks": []}

    arguments = dict(node_id="experiment_planning", prompt="original paper scope", resume=True,
                     stage_label="02a_plan_experiments", output_path=tmp_path / "plan.json")
    context = SimpleNamespace(output_dir=tmp_path)
    pipeline = SimpleNamespace(_load_or_create_analysis_stage_json=owner)
    first = RunSupervisor(tmp_path, tmp_path / "audit", {"paper": "stable"})
    monkeypatch.setattr(first, "_request", Mock(return_value=guidance))
    with supervisor_scope(first):
        supervised_analysis_stage(pipeline, context, **arguments)
    first._save("experiment_planning", status="dispatched")
    resumed = RunSupervisor(tmp_path, tmp_path / "audit", {"paper": "stable"})
    monkeypatch.setattr(resumed, "_request", Mock(side_effect=AssertionError("routine handoff called moderator")))
    with supervisor_scope(resumed):
        result = supervised_analysis_stage(pipeline, context, **arguments)
    assert result["_meta"]["cache_reused"]
    assert [call["resume"] for call in calls] == [True, False, True]
    assert calls[1]["prompt"] == calls[2]["prompt"]


def test_analysis_handoff_failure_repairs_planner_without_reextracting_facts(tmp_path, monkeypatch):
    from geng_agent import supervisor
    from geng_agent.pipeline import ReviewPipeline
    from geng_agent.pipeline_analysis_flow import run_analysis_flow
    from geng_agent.targeted_backfill_loop import run_targeted_backfill_loop
    from tests.test_pipeline import fact, fact_doc, task, task_doc, understanding_doc

    paper = tmp_path / "paper.md"
    paper.write_text("# Results\nFig. 1 BER versus SNR.", encoding="utf-8")
    facts = fact_doc(fact("figure_claim", "Fig. 1"))
    tasks = task_doc(task("t", "Fig. 1"))
    tasks["backfill_handoff"] = {"ready_for_writer": True, "blocking_request_ids": [], "reason": "no gaps"}
    owner_calls = []

    def owner(**kwargs):
        owner_calls.append(kwargs)
        return copy.deepcopy(understanding_doc(facts) if kwargs["stage_label"] == "01_understand_paper"
                             else {"tasks": tasks, "scientific_architecture": None})

    def supervised(node_id, operation, **kwargs):
        _snapshot_node_evidence(tmp_path, kwargs)
        if node_id == "analysis_handoff":
            with pytest.raises(RuntimeError, match="index handoff failed"):
                operation()
            kwargs["repair"]({"action": "retry", "instructions": "Repair the index handoff; preserve the experiment"})
        return operation()

    pipeline = ReviewPipeline()
    index = {"experiments": [{"task_id": "t", "experiment_id": "exp_t"}]}
    monkeypatch.setattr(pipeline, "_load_or_create_analysis_stage_json", owner)
    monkeypatch.setattr(pipeline, "_render_paper_images", lambda **k: [])
    index_call = Mock(side_effect=[RuntimeError("index handoff failed"), index])
    monkeypatch.setattr(pipeline, "_load_or_create_experiment_index", index_call)
    monkeypatch.setattr(supervisor, "supervised_call", supervised)
    context = SimpleNamespace(output_dir=tmp_path / "case", audit_dir=tmp_path / "case/audit", paper_path=paper,
        mark=Mock(), begin=Mock(), options=SimpleNamespace(max_pages=None, resume=True, mineru_timeout=1,
            analysis_backend="llm", json_repair_attempts=0, analysis_fallback=False, tasks_timeout=1))
    result = run_analysis_flow(pipeline, context,
        mineru_stage=lambda **k: {"ok": True, "figure_index": {"figures": []}},
        backfill_loop_runner=run_targeted_backfill_loop)
    assert len([call for call in owner_calls if call["stage_label"] == "01_understand_paper"]) == 1
    assert len(owner_calls) == 3
    assert owner_calls[-1]["resume"] is False
    assert "Repair the index handoff" in owner_calls[-1]["prompt"]
    assert index_call.call_args.kwargs["resume"] is False
    assert result.tasks["repro_tasks"][0]["task_id"] == "t"


def _editor_arguments(tmp_path):
    return {"output_dir": tmp_path, "audit_dir": tmp_path / "audit", "resume": True,
            "runtime_result": {"delivery_status": "partial"},
            "task_verifications": [{"task_id": "t", "outcome": "not_reproduced"}]}


def test_editor_repair_is_supervisor_owned_and_keeps_scientific_outcome(tmp_path, monkeypatch):
    from geng_agent import supervisor

    arguments = _editor_arguments(tmp_path)
    before = copy.deepcopy(arguments)
    failed = {"ok": False, "retryable": True, "workspace": str(tmp_path / "first"),
              "missing_outputs": ["result_review.md"], "codex_status": {"role": "report_editor"},
              "result_review_result": {"passed": False, "reason": "comparison report missing"}}
    runner = Mock(side_effect=[failed, {"ok": True, "codex_status": {"role": "report_editor"}}])

    def supervised(node_id, operation, **kwargs):
        _snapshot_node_evidence(tmp_path, kwargs)
        assert node_id == "report_editor"
        with pytest.raises(ReportOperationError) as caught:
            operation()
        assert caught.value.result["missing_outputs"] == ["result_review.md"]
        kwargs["repair"]({"action": "retry", "instructions": "Write the missing comparison from Reporter records"})
        return operation()

    monkeypatch.setattr(supervisor, "supervised_call", supervised)
    result, count = run_supervised_report_editor(runner, arguments=arguments)
    assert result["ok"] and count == 2
    assert arguments == before
    repair = runner.call_args_list[1].kwargs
    assert repair["attempt_no"] == 2 and repair["resume"] is False
    assert repair["repair_context"]["workspace"] == failed["workspace"]
    assert repair["repair_context"]["supervisor_guidance"]["action"] == "retry"


def test_editor_block_keeps_failure_and_does_not_schedule_legacy_retry(tmp_path, monkeypatch):
    from geng_agent import supervisor

    arguments = _editor_arguments(tmp_path)
    runner = Mock(return_value={"ok": False, "retryable": True, "codex_status": {"role": "report_editor"},
                                "result_review_result": {"passed": False, "reason": "missing required report"}})

    def supervised(node_id, operation, **kwargs):
        with pytest.raises(ReportOperationError) as caught:
            operation()
        raise supervisor.StageBlocked(node_id, {"action": "block", "diagnosis": "owner unavailable"}, caught.value)

    monkeypatch.setattr(supervisor, "supervised_call", supervised)
    result, count = run_supervised_report_editor(runner, arguments=arguments)
    assert result["supervisor_blocked"] and not result["ok"]
    assert result["result_review_result"]["reason"] == "missing required report"
    assert runner.call_count == count == 1
    assert arguments["task_verifications"][0]["outcome"] == "not_reproduced"


def test_report_editor_materials_keep_delivery_gaps_out_of_scientific_facts(tmp_path):
    from geng_agent.agentic_report_editor import _report_materials, _build_report_editor_brief

    material = _report_materials(paper={}, runtime_result={"passed": True, "delivery_status": "partial",
        "engineering_failures": [{"stage": "project_delivery", "error": "missing input page"}]},
        task_records=[{"task_id": "t", "result_json": {"parameter_resolution": ["Writer guessed N=100"]}}],
        output_dir=tmp_path)
    technical = material["technical_details"]
    assert technical["delivery_status"] == "partial"
    assert technical["engineering_failures"][0]["stage"] == "project_delivery"
    assert technical["writer_statements"][0]["reported"]["parameter_resolution"] == ["Writer guessed N=100"]
    brief = _build_report_editor_brief(task_count=1)
    assert "不能声称完整项目已经可迁移或可独立重运行" in brief
    assert "不能将任务计划或 Writer 自述升级为已核验事实" in brief


@pytest.mark.parametrize("preexisting_partial,reporter_blocked,word_failed", [
    (True, False, False), (False, False, False), (True, True, False), (True, False, True),
])
def test_packaging_failure_does_not_prevent_report_editor(tmp_path, monkeypatch, preexisting_partial, reporter_blocked, word_failed):
    from geng_agent import agentic_report_editor, agentic_task_writers, supervisor
    from geng_agent import pipeline_report_flow
    from tests.test_agentic_task_reporters import _task, _supported_raw
    from geng_agent.verification_result import normalize_task_verification

    task = _task()
    verification = normalize_task_verification(_supported_raw(), "task_a", task=task, run_valid_hint=True)
    runtime = {"enabled": True, "passed": True, "coverage": {}}
    if preexisting_partial:
        runtime.update(delivery_status="partial", engineering_failures=[{"stage": "assembly", "error": "missing page"}])
    record = {"task_id": "task_a", "task_reporter": {"ok": True, "task_verification": verification}}
    if reporter_blocked:
        verification["task_id"] = "wrong_reporter_task"
        verification["assigned_task_id"] = "untrusted_reporter_assignment"
        record["task_reporter"].update(ok=False, supervisor_blocked=True)
    analysis = SimpleNamespace(paper={}, facts={}, tasks={"repro_tasks": [task]}, experiment_index={},
        paper_thesis=None, repro_project_dir=tmp_path / "repro_project", mineru_result={},
        analysis_warnings={}, analysis_stage_invocations=0, paper_path=tmp_path / "paper.md")
    execution = SimpleNamespace(runtime_result=runtime, task_records=[record], agentic_result={"status": {}},
        validation={}, scientific_check={}, manifest={}, written_files=[])
    context = SimpleNamespace(output_dir=tmp_path, audit_dir=tmp_path / "audit",
        options=SimpleNamespace(resume=True, analysis_backend="llm", json_repair_attempts=1),
        mark=Mock(), begin=Mock(), finish=Mock(), cost_marks=[], elapsed_s=lambda: 1,
        usage_by_model=lambda: {}, wall_start=0, run_id="fixture")
    from geng_agent.task_writer_results import apply_verified_result
    package = Mock(wraps=apply_verified_result)
    editor = Mock(return_value={"ok": True, "codex_status": {"role": "report_editor"},
                                "result_review_result": {"passed": True}})
    monkeypatch.setattr(supervisor, "current_supervisor", lambda: None)
    def supervised(_node, operation, **kwargs):
        _snapshot_node_evidence(tmp_path, kwargs)
        return operation()
    monkeypatch.setattr(supervisor, "supervised_call", supervised)
    monkeypatch.setattr(agentic_task_writers, "apply_verified_result", package)
    monkeypatch.setattr(agentic_report_editor, "run_codex_report_editor_workflow", editor)
    monkeypatch.setattr(pipeline_report_flow, "build_risk_report", lambda *a, **k: {"findings": []})
    monkeypatch.setattr(pipeline_report_flow, "_build_run_cost", lambda *a, **k: {})
    monkeypatch.setattr("geng_agent.codex_cost.persist_pipeline_cost", lambda *a, **k: None)
    for name in ("review", "result_review", "reproduction_report"):
        (tmp_path / f"{name}.md").write_text("# Current report", encoding="utf-8")
        (tmp_path / f"{name}.docx").write_bytes(b"old Word file")
    word_result = {f"{name}_docx": {"passed": False} for name in ("review", "result_review", "reproduction_report")} if word_failed else {}
    result = run_report_flow(SimpleNamespace(_generate_docx_reports=Mock(return_value=word_result)), context, analysis, execution,
        provenance_builder=lambda **k: {})
    assert package.call_count == 1
    editor.assert_called_once()
    actual = editor.call_args.kwargs
    assert actual["runtime_result"].get("delivery_status", "complete") == ("partial" if preexisting_partial else "complete")
    if reporter_blocked:
        assert actual["task_verifications"][0]["outcome"] == verification["outcome"]
        assert actual["task_verifications"][0]["handoff_accepted"] is False
        assert actual["task_verifications"][0]["task_id"] == "wrong_reporter_task"
        assert actual["task_verifications"][0]["assigned_task_id"] == "task_a"
        assert record["verification_result"]["task_id"] == "wrong_reporter_task"
        assert not actual["runtime_result"]["scientific_all_successful"]
        assert record["unaccepted_task_reporter"]["task_verification"] == verification
    else:
        assert actual["task_verifications"][0]["outcome"] == verification["outcome"]
    if preexisting_partial:
        assert actual["runtime_result"]["engineering_failures"]
    if word_failed:
        assert result.delivery_status == "partial"
        assert result.result_review_path == tmp_path / "result_review.md"
        assert result.review_docx_path is result.result_review_docx_path is result.reproduction_report_docx_path is None


def test_word_conversion_failure_preserves_markdown(tmp_path, monkeypatch):
    from geng_agent.pipeline_report_delivery import generate_docx_reports
    from geng_agent import docx_writer

    for name in ("review.md", "result_review.md", "reproduction_report.md"):
        (tmp_path / name).write_text("# 已完成的报告\n原始结论保留。", encoding="utf-8")
    monkeypatch.setattr(docx_writer, "write_markdown_report_docx", Mock(side_effect=OSError("Word unavailable")))
    result = generate_docx_reports(output_dir=tmp_path, result_review_result={"passed": True})
    assert all(item["passed"] is False for item in result.values())
    assert all((tmp_path / name).exists() for name in ("review.md", "result_review.md", "reproduction_report.md"))


def test_report_editor_cancellation_propagates_without_degraded_success(tmp_path, monkeypatch):
    from geng_agent import supervisor
    from geng_agent.progress import PipelineCancelled

    monkeypatch.setattr(supervisor, "supervised_call", lambda _node, operation, **kwargs: operation())
    runner = Mock(side_effect=PipelineCancelled("user stopped"))
    with pytest.raises(PipelineCancelled):
        run_supervised_report_editor(runner, arguments=_editor_arguments(tmp_path))
    runner.assert_called_once()
    assert not (tmp_path / "report_editor_error.json").exists()
