"""Risk output is an attributed record, not another scientific reviewer."""
import copy

from geng_agent.risk_report import build_risk_report


def test_unperformed_compile_and_missing_optional_contract_do_not_create_risk():
    result = build_risk_report({}, {"repro_tasks": [{"task_id": "t", "target": "解释排序"}]},
        {"required_files_present": True, "python_compiles": None, "host_validation_skipped": True},
        runtime_result={"enabled": True, "passed": True}, paper_format="pdf")
    assert result["findings"] == []
    assert result["risk_dimensions"] == {}
    assert result["risk_level"] is result["scientific_risk_level"] is None
    assert result["validation"]["python_compiles"] is None


def test_actual_compile_failure_remains_recorded_without_fidelity_judgment():
    validation = {"python_compiles": False, "compile_errors": [{"file": "task.py", "error": "SyntaxError"}]}
    result = build_risk_report({}, {}, validation)
    assert result["findings"] == [{"type": "generated_code_compile_error",
        "message": "已有编译记录报告失败。", "compile_errors": validation["compile_errors"]}]
    assert result["risk_level"] is None


def test_original_scientific_notes_and_declared_gaps_are_retained_without_scoring():
    facts = {"missing_information": [{"name": "samples", "impact": "high"}]}
    tasks = {"repro_tasks": [{"task_id": "t", "assumptions": [{"value": "N=100", "risk": "high"}],
        "missing_fact_requests": [{"name": "samples", "description": "Alternative wording"}]}]}
    review = {"all_terminal": True, "tasks": [{"task_id": "t", "outcome": "not_reproduced",
        "handoff_accepted": False, "decision_reason": "The implementation is incorrect"}]}
    before = copy.deepcopy((facts, tasks, review))
    result = build_risk_report(facts, tasks, {}, result_review_result=review)
    assert result["result_review"] == review
    assert result["analysis_records"]["missing_information"] == facts["missing_information"]
    assert result["task_evidence_gap_count"] == result["assumptions_count"] == 1
    assert result["findings"] == []
    assert (facts, tasks, review) == before
    assert result["scientific_risk_level"] is None
