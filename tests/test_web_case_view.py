import json

from tests import web_test_env  # noqa: F401; configure before importing Web modules
from geng_agent.web.case_view import case_research_view


def write(root, name, value):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_finished_execution_and_report_files_never_imply_reproduction(tmp_path):
    write(tmp_path, "runtime_result.json", {"passed": True})
    write(tmp_path, "repro_tasks.json", {"repro_tasks": [{"task_id": "T1", "target": "Claim one"}, {"task_id": "T2"}]})
    (tmp_path / "result_review.md").write_text("report exists", encoding="utf-8")
    view = case_research_view(tmp_path)
    assert view["tasks"][0]["outcome"] is None
    assert view["editor_ok"] is None
    assert view["all_successful"] is None
    write(tmp_path, "verification_result.json", {"all_successful": False, "tasks": [
        {"task_id": "T1", "outcome": "not_reproduced", "decision_reason": "Numeric difference"},
        {"task_id": "orphan", "outcome": "reproduced", "engineering_status": "evidence_invalid"},
    ]})
    write(tmp_path, "audit/04b_report_editor_status.json", {"ok": False})
    view = case_research_view(tmp_path)
    assert [task["task_id"] for task in view["tasks"]] == ["T1", "T2", "orphan"]
    assert view["tasks"][0]["decision_reason"] == "Numeric difference"
    assert view["tasks"][1]["outcome"] is None
    assert view["tasks"][2]["outcome"] == "reproduced"
    assert view["tasks"][2]["engineering_status"] == "evidence_invalid"
    assert view["editor_ok"] is False


def test_partial_and_oversized_records_leave_unknowns_visible(tmp_path):
    (tmp_path / "verification_result.json").write_text('{"tasks":', encoding="utf-8")
    view = case_research_view(tmp_path)
    assert not view["verification_available"] and view["warnings"]
    (tmp_path / "verification_result.json").write_bytes(b" " * (8 * 1024 * 1024 + 1))
    view = case_research_view(tmp_path)
    assert not view["verification_available"] and view["warnings"]


def test_legacy_editor_status_is_displayed_without_inventing_an_agent_run(tmp_path):
    write(tmp_path, "risk_report.json", {"report_editor": {"ok": True, "mode": "program"}})
    view = case_research_view(tmp_path)
    assert view["editor_ok"] is True and view["editor_mode"] == "program"
    assert view["tasks"] == []


def test_report_preview_discloses_truncation(tmp_path):
    from geng_agent.web.artifacts import preview_artifact
    path = tmp_path / "result_review.md"
    path.write_text("a" * 100_001, encoding="utf-8")
    preview = preview_artifact(path, "markdown")
    assert len(preview["text"]) == 100_000
    assert preview["truncated"] is True
