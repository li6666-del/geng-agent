"""Scientific counterexamples for provisional acceptance and isolated reporting."""
import copy
import json
from pathlib import Path

from geng_agent.agentic_task_reporters import run_codex_task_reporter_workflow
from geng_agent.report_editor_assets import _build_task_packets
from geng_agent.report_editor_fallback import _render_fallback_review
from geng_agent.task_reporter_validation import normalize_reporter_observation_evidence
from geng_agent.verification_result import normalize_task_verification, writer_revision_allowed
from tests.test_agentic_task_reporters import _task, _record, _supported_raw


def _paper(tmp_path):
    path = tmp_path / "paper_evidence/source/paper.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("A is above B; gain at 20 dB is 1 mW. No central peak is claimed.", encoding="utf-8")
    return path.relative_to(tmp_path).as_posix()


def _basis(path, status="not_applicable", **extra):
    return {"status": status, "reason": "The cited paper defines a different target", "paper_evidence_files": [path], **extra}


def test_paper_can_retire_wrong_criterion_without_retiring_independent_failure(tmp_path):
    task = _task()
    task["scientific_acceptance"]["key_numeric_targets"] = []
    task["scientific_acceptance"]["core_conclusions"].append({"claim_id": "invented_peak"})
    raw = _supported_raw()
    raw["key_numeric_comparisons"] = []
    raw["core_conclusions"].append({"claim_id": "invented_peak", "status": "not_applicable", "basis_review": _basis(_paper(tmp_path))})
    accepted = normalize_task_verification(raw, "task_a", task=task, run_valid_hint=True, evidence_workspace=tmp_path)
    assert accepted["outcome"] == "reproduced"
    raw["core_conclusions"].append({"claim_id": "method_substitution", "status": "unsupported", "local_observation": "Output was projected onto the desired curve", "evidence_files": ["inputs/writer_output/source/task.py"]})
    raw["rerun_evidence"] = {"rerun_reason": "core_conclusion_failed", "contract_item_ids": ["method_substitution"],
        "paper_evidence_files": [_paper(tmp_path)], "causal_change": "Remove the projection", "change_targets": ["task.py:observable"], "predicted_effect": "Evaluate the defined observable"}
    failed = normalize_task_verification(raw, "task_a", task=task, run_valid_hint=True, evidence_workspace=tmp_path)
    assert failed["outcome"] == "not_reproduced"
    assert writer_revision_allowed(failed, "task_a")


def test_writer_or_designer_prose_cannot_authorize_basis_change(tmp_path):
    for path in ("inputs/writer_output/task_agent_result.json", "paper_evidence/task/evidence.json"):
        file = tmp_path / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("The target is irrelevant", encoding="utf-8")
        raw = _supported_raw()
        raw["core_conclusions"][0].update(status="not_applicable", basis_review=_basis(path, paper_evidence_verified=True))
        result = normalize_task_verification(raw, "task_a", task=_task(), run_valid_hint=True, evidence_workspace=tmp_path)
        assert result["outcome"] == "inconclusive_missing_information"
        assert result["core_conclusions"][0]["basis_review"]["paper_evidence_verified"] is False


def test_corrected_numeric_anchor_uses_original_paper_and_host_arithmetic(tmp_path):
    task = _task()
    task["scientific_acceptance"]["key_numeric_targets"][0].update(paper_magnitude=1000, unit="mW", metric="gain", regime="20 dB")
    raw = _supported_raw(local_magnitude=1)
    raw["key_numeric_comparisons"][0]["basis_review"] = _basis(_paper(tmp_path), "corrected", corrected_paper_magnitude=1, unit="mW", metric="gain", regime="20 dB")
    result = normalize_task_verification(raw, "task_a", task=task, run_valid_hint=True, evidence_workspace=tmp_path)
    assert result["key_numeric_comparisons"][0]["symmetric_ratio"] == 1
    assert result["outcome"] == "reproduced"


def test_incompatible_dimensions_cannot_create_numeric_rerun(tmp_path):
    for dimension, paper, local in (("unit", "mW", "MW"), ("metric", "BER", "NMSE"), ("regime", "20 dB", "30 dB")):
        task = _task()
        task["scientific_acceptance"]["key_numeric_targets"][0][dimension] = paper
        raw = _supported_raw(local_magnitude=100)
        raw["key_numeric_comparisons"][0]["local_" + dimension] = local
        result = normalize_task_verification(raw, "task_a", task=task, run_valid_hint=True, evidence_workspace=tmp_path)
        assert result["max_key_numeric_ratio"] is None
        assert result["outcome"] == "inconclusive_missing_information"
        assert result["host_action"] == "complete"


def test_not_applicable_numeric_target_does_not_demand_an_invented_value(tmp_path):
    raw = _supported_raw()
    raw["key_numeric_comparisons"][0].update(local_magnitude=None, basis_review=_basis(_paper(tmp_path)))
    result = normalize_task_verification(raw, "task_a", task=_task(), run_valid_hint=True, evidence_workspace=tmp_path)
    assert result["outcome"] == "reproduced"
    assert result["key_numeric_comparisons"][0]["comparison_status"] == "not_applicable"


def test_disputed_basis_and_duplicate_id_do_not_erase_failure(tmp_path):
    raw = _supported_raw()
    raw["core_conclusions"][0].update(status="unsupported", basis_review=_basis(_paper(tmp_path), "disputed"))
    raw["core_conclusions"].append({"claim_id": "claim_order", "status": "not_applicable", "basis_review": _basis(_paper(tmp_path))})
    result = normalize_task_verification(raw, "task_a", task=_task(), run_valid_hint=True, evidence_workspace=tmp_path)
    assert result["core_conclusions"][0]["status"] == "unsupported"
    assert result["outcome"] == "not_reproduced"


def test_legacy_supported_false_survives_a_basis_dispute(tmp_path):
    raw = _supported_raw()
    claim = raw["core_conclusions"][0]
    claim.pop("status")
    claim.update(supported=False, basis_review=_basis(_paper(tmp_path)))
    result = normalize_task_verification(raw, "task_a", task=_task(), run_valid_hint=True, evidence_workspace=tmp_path)
    assert result["core_conclusions"][0]["status"] == "unsupported"
    assert result["outcome"] == "not_reproduced"


def test_verified_report_facts_require_original_evidence_not_writer_account(tmp_path):
    source = tmp_path / "inputs/writer_output/source/model.py"
    source.parent.mkdir(parents=True)
    source.write_text("LAYERS=3", encoding="utf-8")
    account = tmp_path / "inputs/writer_account.json"
    account.write_text('{"layers": 99}', encoding="utf-8")
    raw = {"verified_facts": [
        {"category": "implementation", "source": "observed", "text": "3 layers", "evidence_files": ["inputs/writer_output/source/model.py"]},
        {"category": "implementation", "source": "observed", "text": "99 layers", "evidence_files": ["inputs/writer_account.json"]},
    ]}
    checked, warnings = normalize_reporter_observation_evidence(raw, tmp_path)
    assert [f["text"] for f in checked["verified_facts"]] == ["3 layers"]
    assert warnings


def test_reporter_structure_recovery_sees_old_note_and_canonical_inputs(tmp_path, monkeypatch):
    record = _record(tmp_path)
    paper = tmp_path / "paper.txt"
    paper.write_text("A is above B", encoding="utf-8")
    seen = []
    def worker(**kwargs):
        workspace = kwargs["work_dir"]
        seen.append(kwargs)
        if len(seen) == 1:
            (workspace / "task_verification_result.json").write_text('{"core_conclusions": [', encoding="utf-8")
        else:
            recovery = json.loads((workspace / "inputs/reporter_repair.json").read_text(encoding="utf-8"))
            assert recovery["kind"] == "structure_recovery"
            assert "readable evidence note" in recovery["issues"][0]
            assert (workspace / "inputs/previous_reporter_note.txt").read_text(encoding="utf-8") == '{"core_conclusions": ['
            (workspace / "task_verification_result.json").write_text(json.dumps(_supported_raw()), encoding="utf-8")
        return {"ok": True, "role": "task_reporter"}
    monkeypatch.setattr("geng_agent.agentic_task_reporters.run_codex_subprocess", worker)
    arguments = dict(index=1, task=_task(), task_record=record, paper={"chunks": [{"text": "A is above B"}]}, paper_path=paper,
        facts={"engineering_facts": []}, experiment_index={}, paper_thesis=None, paper_images=[], output_dir=tmp_path / "case", audit_dir=tmp_path / "case/audit", resume=False)
    first = run_codex_task_reporter_workflow(**arguments)
    assert first["recovery_kind"] == "structure" and not first["ok"]
    second = run_codex_task_reporter_workflow(**arguments, repair_context=first)
    assert second["ok"]
    assert "Recover the existing evidence note" in seen[1]["prompt"]
    assert "All rendered paper pages are attached" not in seen[1]["prompt"]
    assert "Actual attached images: 0" in seen[1]["prompt"]
    workspace = Path(second["workspace"])
    manifest = json.loads((workspace / "inputs/attachment_manifest.json").read_text(encoding="utf-8"))
    assert manifest["attached_image_count"] == 0
    assert manifest["images"] == []
    assert manifest["original_paper_paths"]
    assert "not evidence that the paper omits" in manifest["limitation"]
    saved_prompt = (workspace.parent / (workspace.name + "_brief.md")).read_text(encoding="utf-8")
    assert saved_prompt == seen[1]["prompt"]
    report_input = json.loads((workspace / "inputs/task_report_input.json").read_text(encoding="utf-8"))
    assert "writer_result" not in report_input
    assert (workspace / report_input["writer_account_path"]).is_file()
    evidence = json.loads(next((workspace / "paper_evidence").glob("01_*/evidence.json")).read_text(encoding="utf-8"))
    assert "task" not in evidence and "facts" not in evidence
    assert "paper_context" in evidence


def test_editor_counts_and_facts_use_host_and_reporter_not_writer(tmp_path):
    task = _task()
    task["assumptions"] = [{"text": "unverified plan"}]
    receipt = {"observer": "orchestration_host", "run_id": "run-1", "task_id": "task_a", "mode": "full", "returncode": 0}
    record = {"task_id": "task_a", "host_execution": {"passed": True, "receipt": receipt},
        "result_json": {"summary": "Everything succeeded", "execution_summary": {"full_run_count": 99}, "parameter_resolution": [{"layers": 99}]}}
    verification = {"task_id": "task_a", "outcome": "not_reproduced", "run_valid": True,
        "verified_facts": [{"text": "3 layers", "source": "observed", "evidence_files": ["audit/source.py"]}]}
    packet = _build_task_packets(facts={}, tasks={"repro_tasks": [task]}, task_records=[record], task_verifications=[verification])[0]
    assert packet["terminal_outcome"] == "not_reproduced"
    assert packet["execution_summary"]["observed_full_attempt_count"] == 1
    assert packet["execution_summary"]["latest_valid_execution_count"] == 1
    from geng_agent.report_facts import terminal_fact_block
    table = terminal_fact_block([packet])
    assert "未复现" in table
    assert "末次执行与产物有效（0/1）" in table
    assert "科学有效完成" not in table
    assert "| task_a | 1 | 1 |" in table
    assert "structured_evidence" not in packet and "writer_summary" not in packet
    assert "assumptions" not in packet["task"]
    assert "99" not in json.dumps(packet)
    fallback = _render_fallback_review(paper={}, task_packets=[packet], risk_report={"reproducibility_verdict": "Unconditionally reproduced"})
    assert "Unconditionally reproduced" not in fallback


def test_editor_deduplicates_host_receipts_and_leaves_unknown_validity_unavailable(tmp_path):
    receipt = {"observer": "orchestration_host", "run_id": "same-run", "task_id": "task_a", "mode": "full", "returncode": 0}
    for folder in ("first", "copied"):
        path = tmp_path / "execution_runs" / folder / "execution_receipt.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(receipt), encoding="utf-8")
    record = {"task_id": "task_a", "writer_status": {"execution_audit_dir": str(tmp_path)}, "host_execution": {"passed": True, "receipt": receipt}}
    packet = _build_task_packets(facts={}, tasks={"repro_tasks": [_task()]}, task_records=[record], task_verifications=[{"task_id": "task_a"}])[0]
    assert packet["execution_summary"]["observed_full_attempt_count"] == 1
    assert packet["execution_summary"]["latest_valid_execution_count"] is None


def test_reporter_cache_uses_actual_prompt_model_and_page_content(tmp_path, monkeypatch):
    from geng_agent import task_reporter_context as context
    from geng_agent.llm import LLMImage
    record = _record(tmp_path)
    paper = tmp_path / "paper.txt"
    paper.write_text("A is above B", encoding="utf-8")
    kwargs = dict(task=_task(), task_record=record, paper_path=paper, facts={}, experiment_index={}, paper_thesis={}, figure_candidates=[],
                  paper_images=[LLMImage("paper_page:1", "image/png", "YQ==")])
    before = context._task_reporter_input_hash(**kwargs)
    with monkeypatch.context() as scoped:
        original = context._build_task_reporter_brief
        scoped.setattr(context, "_build_task_reporter_brief", lambda **args: original(**args) + "\nActual policy changed")
        assert context._task_reporter_input_hash(**kwargs) != before
    kwargs["paper_images"] = [LLMImage("paper_page:1", "image/png", "Yg==")]
    assert context._task_reporter_input_hash(**kwargs) != before
    kwargs["paper_images"] = [LLMImage("paper_page:1", "image/png", "YQ==")]
    monkeypatch.setenv("GENG_CODEX_MODEL", "different-local-model")
    assert context._task_reporter_input_hash(**kwargs) != before


def test_editor_cache_ignores_removed_writer_and_risk_prose_but_tracks_facts(tmp_path):
    from geng_agent.agentic_report_editor import _editor_input_hash
    args = dict(output_dir=tmp_path, paper={"title": "Paper"}, paper_thesis={}, runtime_result={}, risk_report={}, task_packets=[])
    before = _editor_input_hash(**args)
    args["risk_report"] = {"reproducibility_verdict": "stale verdict"}
    args["paper_thesis"] = {"unused": "long narrative"}
    assert _editor_input_hash(**args) == before
    args["task_packets"] = [{"task_id": "task_a", "verification": {"verified_facts": [{"text": "3 verified layers"}]}}]
    assert _editor_input_hash(**args) != before


def test_reporter_cache_tracks_host_scientific_code_without_bumping_versions(tmp_path, monkeypatch):
    from geng_agent import task_reporter_context as context
    from geng_agent import task_reporter_validation as evidence
    from geng_agent import verification_result as verification
    record = _record(tmp_path)
    paper = tmp_path / "paper.txt"
    paper.write_text("A is above B", encoding="utf-8")
    kwargs = dict(task=_task(), task_record=record, paper_path=paper, facts={}, experiment_index={}, paper_thesis={}, figure_candidates=[])
    before = context._task_reporter_input_hash(**kwargs)
    version = context.TASK_REPORTER_PROMPT_VERSION
    def changed_outcome(**values):
        return "not_reproduced", "complete"
    with monkeypatch.context() as scoped:
        scoped.setattr(verification, "_derive_outcome", changed_outcome)
        assert context.TASK_REPORTER_PROMPT_VERSION == version
        assert context._task_reporter_input_hash(**kwargs) != before
    def changed_assets(**values):
        return {"local_assets": [], "paper_assets": []}, []
    with monkeypatch.context() as scoped:
        scoped.setattr(evidence, "_materialize_task_assets", changed_assets)
        assert context._task_reporter_input_hash(**kwargs) == before


def test_attachment_manifest_lists_actual_relative_paths_and_types(tmp_path):
    from geng_agent.task_reporter_context import _reporter_attachment_visibility
    paths = [tmp_path / "paper_evidence/full_paper_pages/paper_page_002.png",
             tmp_path / "inputs/writer_output/outputs/result.png"]
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"image bytes")
    manifest, note = _reporter_attachment_visibility(tmp_path, paths)
    assert manifest["attached_image_count"] == 2
    assert [item["kind"] for item in manifest["images"]] == ["paper_page", "local_result"]
    assert all(not Path(item["path"]).is_absolute() and item["sha256"] for item in manifest["images"])
    assert "Actual attached images: 2" in note
    assert "paper_page_002.png" in note and "local_result" in note
