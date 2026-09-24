from copy import deepcopy
from unittest.mock import patch

from geng_agent.agentic_report_editor import _build_report_editor_brief
from geng_agent.report_language import CHINESE_REPORT_RULES
from geng_agent.task_reporter_context import _build_task_reporter_brief, _task_reporter_input_hash
from geng_agent.verification_result import normalize_task_verification
from tests.test_agentic_task_reporters import _record, _supported_raw, _task


def test_report_language_policy_covers_normal_and_repair_workers():
    for repair in (False, True):
        brief = _build_task_reporter_brief(task_id="t1", report_asset_dir="report_assets/t1",
                                          include_all_paper_pages=False, repair=repair)
        assert CHINESE_REPORT_RULES in brief
        assert "简体中文" in brief
        editor = _build_report_editor_brief(task_count=1, repair_targets=["result_review.md"] if repair else [])
        assert CHINESE_REPORT_RULES in editor
    raw = _supported_raw()
    raw["report_title"] = "AWGN 信道下的 BER 对比"
    normalized = normalize_task_verification(raw, "task_a", task=_task(), run_valid_hint=True)
    assert normalized["report_title"] == raw["report_title"]




def test_chinese_policy_invalidates_old_reporter_cache_without_changing_writer_files(tmp_path):
    paper = tmp_path / "paper.md"
    paper.write_text("Fig. 1", encoding="utf-8")
    record = _record(tmp_path)
    kwargs = dict(task=_task(), task_record=record, paper_path=paper, facts={}, experiment_index={},
                  paper_thesis={}, figure_candidates=[])
    current = _task_reporter_input_hash(**kwargs)
    with patch("geng_agent.task_reporter_context.TASK_REPORTER_PROMPT_VERSION", "isolated_task_reporter_v12_report_explanation"):
        previous = _task_reporter_input_hash(**kwargs)
    assert current != previous
