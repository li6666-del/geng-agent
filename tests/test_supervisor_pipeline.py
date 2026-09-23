from pathlib import Path
from types import SimpleNamespace

import pytest

from geng_agent.outputs import write_json
from geng_agent.pipeline_models import PipelineResult
from geng_agent.pipeline_supervision import run_supervised_pipeline
from geng_agent.progress import PipelineCancelled
from geng_agent.supervisor import RunSupervisor, StageBlocked
from geng_agent.supervisor_repairs import register_delivery_repairs, write_delivery_index


def case(tmp_path, monkeypatch):
    context = SimpleNamespace(output_dir=tmp_path, audit_dir=tmp_path / "audit", run_id="current",
                              options=SimpleNamespace(analysis_only=False, run_repro=True))
    supervisor = RunSupervisor(tmp_path, context.audit_dir, {"paper_sha256": "current"})
    calls = []

    def decide(node_id, **kwargs):
        calls.append((node_id, kwargs["context"]))
        if kwargs.get("trigger") == "tool_dispatch":
            from tests.supervisor_decisions import choose_tools
            return choose_tools(kwargs["context"])
        packet = kwargs["context"]
        action = "start" if packet.get("ready") else "finish" if node_id == "dispatch" else "block"
        return {"action": action, "next_node": packet.get("ready", [""])[0] if packet.get("ready") else "",
                "diagnosis": "offline controller decision", "status": "decided"}

    monkeypatch.setattr(supervisor, "_request", decide)
    return context, supervisor, calls


def result(root, *, status="complete"):
    return PipelineResult(output_dir=root, review_path=root / "review.md", repro_project_dir=root / "repro_project",
                          risk_report_path=root / "risk_report.json", delivery_status=status)


def test_execution_block_still_dispatches_editor_without_reusing_old_records(tmp_path, monkeypatch):
    context, supervisor, calls = case(tmp_path, monkeypatch)
    write_json(context.audit_dir / "03c_task_writers_records.json", {"tasks": [{"task_id": "a", "writer_completed": True}]})
    analysis = SimpleNamespace(tasks={"repro_tasks": [{"task_id": "a"}]})
    seen = []

    def execute(_analysis):
        raise StageBlocked("environment", {"diagnosis": "GPU unavailable"})

    def report(_analysis, execution):
        seen.append(execution)
        return result(tmp_path)

    delivered = run_supervised_pipeline(context=context, supervisor=supervisor, analyze=lambda: analysis,
        execute=execute, report=report, finish_analysis=lambda _: None)
    assert delivered.delivery_status == "partial"
    assert len(seen) == 1 and not seen[0].task_records[0]["writer_completed"]
    assert seen[0].runtime_result["engineering_failures"][0]["node_id"] == "environment"
    assert any("reports" in packet.get("ready", []) for _, packet in calls)


def test_scientific_failure_is_reportable_and_does_not_request_host_retry(tmp_path, monkeypatch):
    context, supervisor, calls = case(tmp_path, monkeypatch)
    execution = SimpleNamespace(runtime_result={"passed": True, "scientific_all_successful": False},
                                task_records=[], validation={"required_files_present": True})
    delivered = run_supervised_pipeline(context=context, supervisor=supervisor, analyze=lambda: object(),
        execute=lambda _: execution, report=lambda *_: result(tmp_path), finish_analysis=lambda _: None)
    assert delivered.delivery_status == "complete"
    assert not any(packet.get("failures") for _, packet in calls)


@pytest.mark.parametrize("failure", [StageBlocked("parse", {"diagnosis": "unreadable"}), RuntimeError("host conversion failed")])
def test_analysis_failure_finishes_with_explicit_blocked_state(tmp_path, monkeypatch, failure):
    context, supervisor, _ = case(tmp_path, monkeypatch)

    def analyze():
        raise failure

    def unavailable(*_):
        pytest.fail("cannot run science or author a report without an analysis contract")

    delivered = run_supervised_pipeline(context=context, supervisor=supervisor, analyze=analyze,
        execute=unavailable, report=unavailable, finish_analysis=unavailable)
    assert delivered.delivery_status == "blocked"
    assert not (tmp_path / "result_review.md").exists()


def test_editor_partial_status_is_not_lost(tmp_path, monkeypatch):
    context, supervisor, _ = case(tmp_path, monkeypatch)
    execution = SimpleNamespace(runtime_result={}, task_records=[], validation={})
    delivered = run_supervised_pipeline(context=context, supervisor=supervisor, analyze=lambda: object(),
        execute=lambda _: execution, report=lambda *_: result(tmp_path, status="partial"), finish_analysis=lambda _: None)
    assert delivered.delivery_status == "partial"


def test_user_cancel_never_enters_recovery(tmp_path, monkeypatch):
    context, supervisor, calls = case(tmp_path, monkeypatch)

    def analyze():
        raise PipelineCancelled("cancel")

    with pytest.raises(PipelineCancelled):
        run_supervised_pipeline(context=context, supervisor=supervisor, analyze=analyze,
            execute=lambda _: None, report=lambda *_: None, finish_analysis=lambda _: None)
    assert not calls  # Routine dispatch and cancellation do not call the moderator.


def test_delivery_index_does_not_publish_old_reports_after_early_block(tmp_path):
    (tmp_path / "result_review.md").write_text("old report", encoding="utf-8")
    (tmp_path / "repro_project").mkdir()
    index = write_delivery_index(tmp_path, {"run_id": "new", "delivery_status": "blocked"})
    assert index["files"] == [] and index["project_directory"] is None


def test_delivery_index_omits_stale_word_after_current_markdown_accepted(tmp_path):
    (tmp_path / "result_review.md").write_text("current", encoding="utf-8")
    (tmp_path / "result_review.docx").write_bytes(b"old version")
    index = write_delivery_index(tmp_path, {"reports_accepted": True, "accepted_docx_paths": []})
    assert [item["path"] for item in index["files"]] == ["result_review.md"]


def test_safe_self_repair_only_restores_accepted_asset_bytes(tmp_path, monkeypatch):
    import hashlib
    context, supervisor, _ = case(tmp_path, monkeypatch)
    workspace = context.audit_dir / "04a_task_reporters/01_a/session"
    image = workspace / "report_assets/a/figure.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"accepted bytes")
    records = [{"index": 1, "task_id": "a", "task_reporter": {"workspace": str(workspace),
        "asset_manifest": [{"path": "report_assets/a/figure.png", "sha256": hashlib.sha256(image.read_bytes()).hexdigest()}]}}]
    register_delivery_repairs(supervisor, context, records)
    repair = supervisor._repair_handlers["restore_report_assets"]
    # Handler registration carries description/schema separately from callable.
    if isinstance(repair, dict):
        repair = repair["handler"]
    repair({})
    assert (tmp_path / "report_assets/a/figure.png").read_bytes() == image.read_bytes()
    image.write_bytes(b"tampered")
    with pytest.raises(Exception, match="不能补造"):
        repair({})
