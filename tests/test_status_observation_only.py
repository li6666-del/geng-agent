"""Reading status must never re-run acceptance or mutate a case."""
from pathlib import Path
from unittest.mock import Mock

from geng_agent.outputs import write_json
from geng_agent.status import inspect_stage
from geng_agent.runtime_status import _load_cached_result_review_status


def test_status_does_not_compile_or_import_an_accepted_project(tmp_path, monkeypatch):
    project = tmp_path / "repro_project"
    project.mkdir()
    source = project / "optional_diagnostic.py"
    source.write_text("not valid Python!", encoding="utf-8")
    monkeypatch.setattr("geng_agent.outputs.validate_repro_project", Mock(side_effect=AssertionError("status cannot execute checks")))
    before = {p.relative_to(tmp_path).as_posix(): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    result = inspect_stage(tmp_path, "repro_project", "repro_project", None)
    assert result["ok"] is True
    assert result["validation_repeated"] is False
    assert before == {p.relative_to(tmp_path).as_posix(): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert not list(tmp_path.rglob("__pycache__"))


def test_status_retains_stage_notes_without_requiring_old_full_schema(tmp_path, monkeypatch):
    write_json(tmp_path / "repro_tasks.json", {"repro_tasks": [{"task_id": "t", "goal": "inspect a small curve"}]})
    monkeypatch.setattr("geng_agent.schemas.validate_stage", Mock(side_effect=AssertionError("already reviewed")))
    result = inspect_stage(tmp_path, "repro_tasks", "repro_tasks.json", "repro_tasks")
    assert result["ok"] is True
    assert result["reason"] == "recorded"


def test_failed_runtime_is_a_recorded_stage_and_remains_visible(tmp_path):
    write_json(tmp_path / "runtime_result.json", {"passed": False, "delivery_status": "partial"})
    result = inspect_stage(tmp_path, "runtime", "runtime_result.json", None)
    assert result["ok"] is True
    assert result["passed"] is False
    assert result["delivery_status"] == "partial"


def test_report_status_does_not_require_a_retired_json_report_schema(tmp_path, monkeypatch):
    (tmp_path / "result_review.md").write_text("中文比较报告", encoding="utf-8")
    write_json(tmp_path / "result_review.json", {"editor_note": "An optional companion note"})
    monkeypatch.setattr("geng_agent.runtime_status.validate_stage", Mock(side_effect=AssertionError("retired report gate")))
    result = _load_cached_result_review_status(tmp_path)
    assert result["passed"] is True
