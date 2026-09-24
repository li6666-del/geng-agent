"""Offline checks: the host transports findings; it does not judge their science."""
import copy
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from geng_agent.verification_result import (
    aggregate_task_verifications, normalize_task_verification,
    partition_task_verification_issues, verification_scientifically_successful,
)


def test_host_keeps_raw_science_and_unstructured_causal_guidance():
    raw = {"task_id": "t", "outcome": "reproduced", "run_valid": True,
           "host_action": "rerun_writer", "decision_reason": "One implementation needs correction",
           "rerun_evidence": {"explanation": "The factor of two is missing in channel.py"},
           "verified_facts": ["This is an alternative fact representation"],
           "independent_comment": "Do not change the original experiment"}
    saved = copy.deepcopy(raw)
    result = normalize_task_verification(raw, "t", run_valid_hint=False,
        task={"scientific_acceptance": {"core_conclusions": [{"claim_id": "c1"}]}})
    assert raw == saved
    for key in ("outcome", "run_valid", "host_action", "rerun_evidence", "verified_facts", "independent_comment"):
        assert result[key] == raw[key]
    assert not partition_task_verification_issues(result, "t")[0]
    assert result["host_run_valid"] is False
    assert not verification_scientifically_successful(result)
    assert result["host_observations"]


@pytest.mark.parametrize("raw", [None, [], {}])
def test_absent_reporter_note_remains_a_handoff_failure(raw):
    result = normalize_task_verification(raw, "t")
    assert partition_task_verification_issues(result, "t")[0]


@pytest.mark.parametrize("raw", [{"task_id": "wrong", "host_action": "complete"},
    {"task_id": "t"}, {"task_id": "t", "host_action": "looks good"},
    {"task_id": "t", "host_action": {"next": "run"}}])
def test_reporter_echo_and_non_rerun_action_do_not_block_handoff(raw):
    result = normalize_task_verification(raw, "t")
    assert partition_task_verification_issues(result, "t")[0] == []
    assert result["task_id"] == result["assigned_task_id"] == "t"
    assert result["reported_task_id"] == raw.get("task_id")
    assert result["reporter_action"] == raw.get("host_action")
    assert result["host_action"] == "complete"


def test_wrong_task_echo_does_not_suppress_explicit_rerun():
    note = normalize_task_verification(
        {"task_id": "wrong", "host_action": "rerun_writer", "outcome": "not_reproduced"},
        "assigned",
    )
    assert note["task_id"] == "assigned"
    assert note["reported_task_id"] == "wrong"
    assert note["host_action"] == "rerun_writer"
    assert partition_task_verification_issues(note, "assigned")[0] == []


def test_positive_reporter_conclusion_and_full_run_observation_are_separate():
    note = normalize_task_verification(
        {"outcome": "reproduced", "decision_reason": "Reporter evidence supports the claim"},
        "assigned", run_valid_hint=False,
    )
    summary = aggregate_task_verifications([note])
    assert note["engineering_status"] == "execution_failed"
    assert summary["all_successful"] is True
    assert summary["all_full_runs_observed"] is False
    assert summary["tasks"][0]["outcome"] == "reproduced"


def test_arbitrary_scientific_expression_and_missing_schema_are_observations():
    raw = {"task_id": "t", "host_action": "complete", "outcome": "排序暂不确定，仍需更多样本",
           "comparison_summary": "An equivalent description"}
    note = normalize_task_verification(raw, "t", run_valid_hint=True)
    assert note["outcome"] == raw["outcome"]
    assert partition_task_verification_issues(note, "t")[0] == []


def test_alternative_observation_and_repair_representations_survive():
    raw = {"task_id": "t", "host_action": "rerun_writer", "core_conclusions": "排序仍不稳定",
        "key_numeric_comparisons": ["零误码不足以证明真实概率为零"],
        "rerun_evidence": "固定已有参数，提高样本量以缩小误码率区间。"}
    note = normalize_task_verification(raw, "t")
    for key in ("core_conclusions", "key_numeric_comparisons", "rerun_evidence"):
        assert note[key] == raw[key]
    assert not partition_task_verification_issues(note, "t")[0]


def test_unavailable_facts_stay_visible_to_supervisor(tmp_path):
    from geng_agent.task_reporter_validation import normalize_reporter_observation_evidence
    original = {"task_id": "t", "verified_facts": [
        {"source": "paper", "text": "N = 64", "evidence_files": ["missing.pdf"]},
        "Alternative narrative fact"]}
    result, warnings = normalize_reporter_observation_evidence(original, tmp_path)
    assert len(result["verified_facts"]) == 2
    assert result["verified_facts"][0]["text"] == "N = 64"
    assert result["verified_facts"][0]["host_evidence_available"] is False
    assert result["verified_facts"][1] == "Alternative narrative fact"
    assert warnings


def test_stopped_coordination_does_not_rewrite_pending_reporter_request():
    note = {"task_id": "t", "host_action": "rerun_writer", "outcome": "not_reproduced",
            "coordination_status": "stopped", "coordination_reason": "No budget"}
    result = aggregate_task_verifications([note])
    assert result["all_terminal"]
    assert result["tasks"][0] == note
    assert result["outcome_counts"] == {"not_reproduced": 1}
    assert "confidence" not in result and "verdict" not in result


def test_apply_only_records_metadata_and_never_refreezes_execution_inputs(tmp_path):
    from geng_agent.task_writer_results import apply_verified_result
    project = tmp_path / "repro_project"
    project.mkdir()
    config = project / "config.json"
    config.write_text('{"N":64}', encoding="utf-8")
    manifest = tmp_path / "repro_project_manifest.json"
    manifest.write_text('{"legacy":"no freeze contract"}', encoding="utf-8")
    (tmp_path / "runtime_result.json").write_text('{"passed":true,"delivery_status":"partial"}', encoding="utf-8")
    before = config.read_bytes(), manifest.read_bytes()
    notes = aggregate_task_verifications([{"task_id": "t", "host_action": "rerun_writer", "outcome": "not_reproduced"}])
    records = [{"task_id": "t", "task_writer_status": "ready_for_review"}]
    result = apply_verified_result(task_records=records, verification_result=notes, output_dir=tmp_path,
        audit_dir=tmp_path / "audit", repro_project_dir=project)
    assert result["passed"] and result["delivery_status"] == "partial"
    assert records[0]["task_writer_status"] == "ready_for_review"
    assert records[0]["verification_result"]["host_action"] == "rerun_writer"
    assert (config.read_bytes(), manifest.read_bytes()) == before


def test_editor_receives_acceptance_and_coordination_without_filtering_note():
    from geng_agent.report_editor_assets import _build_task_packets
    verification = {"task_id": "t", "outcome": "reproduced", "host_action": "rerun_writer",
        "handoff_accepted": False, "coordination_status": "stopped", "novel_explanation": "An unaccepted candidate"}
    packet = _build_task_packets(facts={}, tasks={"repro_tasks": [{"task_id": "t"}]},
        task_records=[{"task_id": "t"}], task_verifications=[verification])[0]
    assert packet["verification"] == verification


def test_unaccepted_wrong_task_note_is_linked_by_host_assignment():
    from geng_agent.report_editor_assets import _build_task_packets
    verification = {"task_id": "wrong", "assigned_task_id": "t", "outcome": "reproduced",
        "host_action": "complete", "handoff_accepted": False, "coordination_status": "stopped"}
    packet = _build_task_packets(facts={}, tasks={"repro_tasks": [{"task_id": "t"}]},
        task_records=[{"task_id": "t"}], task_verifications=[verification])[0]
    assert packet["task_id"] == "t"
    assert packet["verification"] == verification
    assert packet["verification"]["handoff_accepted"] is False
    assert verification["task_id"] == "wrong"


def test_two_reports_without_navigation_are_delivered_and_cacheable(tmp_path, monkeypatch):
    from geng_agent import agentic_report_editor as editor
    from geng_agent.report_editor_workspace import _report_outputs_fingerprint
    output = tmp_path / "case"
    output.mkdir()
    def owner(**kwargs):
        import runpy
        from tests.test_agentic_report_editor import _FAKE_LAYOUT
        for name in ("reproduction_report.md", "result_review.md"):
            (kwargs["work_dir"] / name).write_text("# 智能体生成的正文\n保留实际限制。", encoding="utf-8")
        script = kwargs["work_dir"] / "report_layout.py"
        script.write_text(_FAKE_LAYOUT, encoding="utf-8")
        runpy.run_path(str(script))
        return {"ok": True, "role": "report_editor"}
    model = Mock(side_effect=owner)
    monkeypatch.setattr(editor, "run_codex_subprocess", model)
    arguments = dict(paper={}, facts={}, tasks={"repro_tasks": []}, paper_thesis=None,
        runtime_result={}, risk_report={}, task_records=[], task_verifications=[],
        output_dir=output, audit_dir=output / "audit", resume=False)
    result = editor.run_codex_report_editor_workflow(**arguments)
    assert result["ok"] and not (output / "review.md").exists()
    assert result["host_observations"] == ["review.md", "review.docx"]
    assert _report_outputs_fingerprint(output)
    cached = editor.run_codex_report_editor_workflow(**{**arguments, "resume": True})
    assert cached["cached"] and model.call_count == 1


def test_navigation_docx_absence_does_not_fail_two_main_reports(tmp_path, monkeypatch):
    from docx import Document
    from geng_agent.pipeline_report_delivery import inspect_editor_word_reports
    for name in ("reproduction_report.md", "result_review.md"):
        (tmp_path / name).write_text("# Report", encoding="utf-8")
    for name in ("reproduction_report.docx", "result_review.docx"):
        document = Document()
        document.add_paragraph("智能体生成的正文")
        document.save(tmp_path / name)
    result = inspect_editor_word_reports(output_dir=tmp_path, result_review_result={"passed": True})
    assert result["review_docx"]["passed"] is None
    assert result["reproduction_report_docx"]["passed"]
    assert result["result_review_docx"]["passed"]
