"""Task goals bound review; the host transports, rather than infers, relevance."""
import copy
import json

import pytest

from geng_agent.agentic_task_reporters import run_codex_task_reporter_workflow
from geng_agent.agentic_report_editor import _build_report_editor_brief
from geng_agent.report_editor_assets import _build_task_packets
from geng_agent.schemas import validate_stage
from geng_agent.task_reporter_context import _build_task_reporter_brief
from geng_agent.task_reporter_validation import normalize_reporter_observation_evidence
from geng_agent.verification_result import (
    aggregate_task_verifications,
    normalize_task_verification,
    verification_scientifically_successful,
    task_verification_issues,
)
from tests.test_agentic_task_reporters import _record, _supported_raw, _task


def _additional(path="inputs/writer_output/outputs/results.csv"):
    return {"observation_id": "other_local_order", "observation": "同图另一局部区间的排序未复现",
            "scope_reason": "该排序不属于本任务总体精度目标，也不影响指定测量",
            "evidence_files": [path]}


@pytest.mark.parametrize("missing_evidence", [False, True])
def test_out_of_scope_finding_survives_reporter_to_editor_without_changing_task(tmp_path, monkeypatch, missing_evidence):
    record = _record(tmp_path)
    task = _task()
    paper = tmp_path / "paper.txt"
    paper.write_text("Assigned ordering A > B. A different local interval is separate.", encoding="utf-8")
    raw = _supported_raw()
    raw["additional_observations"] = [_additional("missing.csv") if missing_evidence else _additional()]

    def worker(**kwargs):
        (kwargs["work_dir"] / "task_verification_result.json").write_text(json.dumps(raw), encoding="utf-8")
        return {"ok": True, "role": "task_reporter"}

    monkeypatch.setattr("geng_agent.agentic_task_reporters.run_codex_subprocess", worker)
    result = run_codex_task_reporter_workflow(index=1, task=task, task_record=record,
        paper={"chunks": [{"text": "A > B"}]}, paper_path=paper, facts={}, experiment_index={},
        paper_thesis=None, paper_images=[], output_dir=tmp_path / "case", audit_dir=tmp_path / "case/audit", resume=False)
    assert result["ok"], result
    # Exercise the stored normalized handoff, not just a prompt string.
    verified = result["task_verification"]
    assert verification_scientifically_successful(verified)
    assert verified["host_action"] == "complete"
    observation = verified["additional_observations"][0]
    assert observation["observation"] == raw["additional_observations"][0]["observation"]
    assert observation["evidence_files_available"] is not missing_evidence
    assert not verified["handoff_issues"]
    summary = aggregate_task_verifications([verified])
    assert summary["all_successful"]
    assert summary["tasks"][0] == verified
    packet = _build_task_packets(facts={}, tasks={"repro_tasks": [task]}, task_records=[record],
                                 task_verifications=summary["tasks"])[0]
    assert packet["terminal_outcome"] == "reproduced"
    assert packet["verification"]["additional_observations"] == [observation]
    assert packet["verification"]["provenance_base"] == verified["provenance_base"]
    if not missing_evidence:
        reference = tmp_path / "case" / packet["verification"]["provenance_base"] / observation["evidence_files"][0]
        assert reference.is_file()


def test_out_of_scope_request_is_preserved_for_supervisor_scope_review():
    raw = _supported_raw()
    raw.update(host_action="rerun_writer", additional_observations=[_additional()],
        rerun_evidence={"rerun_reason": "core_conclusion_failed", "contract_item_ids": ["other_local_order"],
            "paper_evidence_files": ["paper_evidence/source/paper.txt"], "causal_change": "Change a separate experiment",
            "change_targets": ["other_task.py"], "predicted_effect": "Fix another ordering"})
    result = normalize_task_verification(raw, "task_a", task=_task(), run_valid_hint=True)
    assert (not task_verification_issues(result, "task_a") and result.get("host_action") == "rerun_writer")
    assert result["engineering_status"] == "verified"
    assert result["outcome"] == "reproduced"  # Host does not replace the Agent's science.
    assert result["additional_observations"] == raw["additional_observations"]


def test_goal_relevant_algorithm_failure_without_designer_id_still_allows_causal_repair(tmp_path):
    source = tmp_path / "inputs/writer_output/source/model.py"
    source.parent.mkdir(parents=True)
    source.write_text("def estimate(y): return sorted(y)\n", encoding="utf-8")
    paper = tmp_path / "paper_evidence/source/paper.txt"
    paper.parent.mkdir(parents=True)
    paper.write_text("Compare raw measurements without reordering.", encoding="utf-8")
    raw = _supported_raw(outcome="not_reproduced")
    raw["evidence_files"] = [source.relative_to(tmp_path).as_posix()]
    raw["core_conclusions"].append({"claim_id": "algorithm_substitution", "status": "unsupported",
        "local_observation": "Implementation sorts measurements before testing the claimed ordering",
        "goal_relation": "This transformation invalidates the assigned A/B comparison",
        "evidence_files": raw["evidence_files"]})
    raw.update(host_action="rerun_writer", rerun_evidence={"rerun_reason": "core_conclusion_failed",
        "contract_item_ids": ["algorithm_substitution"], "paper_evidence_files": [paper.relative_to(tmp_path).as_posix()],
        "causal_change": "Remove output sorting", "change_targets": ["model.py:estimate"],
        "predicted_effect": "Measure the assigned ordering without selecting results"})
    # Point the fixture's assigned observation at the existing immutable source too.
    raw["core_conclusions"][0]["evidence_files"] = raw["evidence_files"]
    checked, _ = normalize_reporter_observation_evidence(raw, tmp_path)
    result = normalize_task_verification(checked, "task_a", task=_task(), run_valid_hint=True, evidence_workspace=tmp_path)
    assert result["outcome"] == "not_reproduced"
    assert (not task_verification_issues(result, "task_a") and result.get("host_action") == "rerun_writer")
    assert result["core_conclusions"][-1]["goal_relation"] == raw["core_conclusions"][-1]["goal_relation"]
    assert not task_verification_issues(result, "task_a")


def test_host_does_not_infer_scope_from_natural_language_or_rewrite_verdict():
    raw = _supported_raw(outcome="not_reproduced")
    raw["additional_observations"] = [_additional()]
    result = normalize_task_verification(raw, "task_a", task=_task(), run_valid_hint=True)
    assert result["outcome"] == "not_reproduced"
    assert result["decision_reason"] == raw["decision_reason"]
    assert result["core_conclusions"] == raw["core_conclusions"]
    legacy = copy.deepcopy(result)
    legacy.pop("additional_observations")
    assert "additional_observations" not in aggregate_task_verifications([legacy])["tasks"][0]


def test_agent_instructions_keep_scope_and_extra_findings_separate():
    prompt = _build_task_reporter_brief(task_id="task_a", report_asset_dir="report_assets/task_a", include_all_paper_pages=False)
    assert "incorrect formula that corrupts those assigned measurements is in scope" in prompt
    assert "They do not change `outcome`, `run_valid`, or `host_action`" in prompt
    assert "goal_relation" in prompt and "additional_observations" in prompt
    editor = _build_report_editor_brief(task_count=1)
    assert "不得把它们改写为本任务验收失败、通过依据或自动重跑要求" in editor
