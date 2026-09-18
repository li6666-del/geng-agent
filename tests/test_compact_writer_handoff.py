import json

from geng_agent.task_writer_inputs import build_writer_input, write_writer_input, unique_image_paths
from geng_agent.task_writer_prompts import _build_task_writer_brief
from geng_agent.task_writer_delivery import _collect_task_writer_delivery
from geng_agent.verification_result import normalize_task_verification, writer_delivery_issues


def test_all_explicit_facts_survive_without_twenty_fact_limit_and_conflicts_remain():
    facts = [{"fact_id": f"f{i}", "type": "parameter", "name": f"p{i}", "value": i} for i in range(35)]
    facts += [dict(facts[-1], value="conflicting value"), dict(facts[0])]
    tasks = [{"task_id": "a", "required_facts": [{"type": "parameter", "name": f"p{i}"} for i in range(35)]},
             {"task_id": "b", "required_fact_ids": ["f0", "f34"]}]
    packet = build_writer_input(members=[(i, t, {}) for i, t in enumerate(tasks)],
                                facts={"engineering_facts": facts}, experiment_index={}, bindings={})
    assert len(packet["engineering_facts"]) == 36
    assert len(packet["tasks"][0]["fact_indexes"]) == 36
    assert len(packet["tasks"][1]["fact_indexes"]) == 3
    assert {packet["engineering_facts"][i]["value"] for i in packet["tasks"][1]["fact_indexes"]} == {0, 34, "conflicting value"}


def test_no_semantic_fact_selection_and_missing_reference_is_visible():
    task = {"task_id": "t", "required_facts": [{"type": "parameter", "name": "noise power"}]}
    packet = build_writer_input(members=[(1, task, {})], facts={"engineering_facts": [
        {"type": "parameter", "name": "noise energy", "value": 7}]}, experiment_index={}, bindings={})
    assert packet["engineering_facts"] == []
    assert packet["tasks"][0]["unresolved_fact_references"] == task["required_facts"]
    assert packet["library"]["full_text"].endswith("paper_chunks.json")


def test_shared_components_once_and_unit_dependencies_preserved():
    tasks = [{"task_id": "producer"}, {"task_id": "consumer", "depends_on": ["producer"]}]
    component = {"component_id": "model", "execution": {"checkpoint_semantics": "same trained weights"}}
    unit = {"unit_id": "u", "task_ids": ["producer", "consumer"],
            "relationships": [{"producer_task_id": "producer", "consumer_task_ids": ["consumer"], "strength": "strong"}]}
    experiments = [{"task_id": t["task_id"], "protocol": f"complete {t['task_id']}"} for t in tasks]
    packet = build_writer_input(members=[(i, t, {}) for i, t in enumerate(tasks)], facts={},
                                experiment_index={"experiments": experiments},
                                bindings={t["task_id"]: {"components": [component]} for t in tasks}, unit=unit)
    assert packet["components"] == [component]
    assert all(t["binding"]["component_indexes"] == [0] for t in packet["tasks"])
    assert packet["experiments"] == experiments
    assert packet["execution_unit"]["relationships"] == unit["relationships"]
    assert packet["tasks"][1]["task"]["depends_on"] == ["producer"]


def test_material_files_retain_full_late_evidence_and_original_source(tmp_path):
    root = tmp_path / "paper_evidence"
    root.mkdir()
    original = root / "source.pdf"
    original.write_bytes(b"original paper bytes")
    (root / "index.json").write_text(json.dumps({"paper_source": {"relative_path": "paper_evidence/source.pdf"},
        "tasks": [{"task_id": "t", "task_evidence_json": "paper_evidence/t/evidence.json",
                   "task_context_markdown": "paper_evidence/t/context.md"}]}))
    chunks = [{"page": 99, "text": "x" * 25000 + " decisive late condition"}]
    write_writer_input(sandbox=tmp_path, members=[(1, {"task_id": "t"}, {})], facts={},
                       experiment_index={}, bindings={}, paper={"chunks": chunks}, case_runtime=None)
    assert json.loads((root / "paper_chunks.json").read_text())["chunks"] == chunks
    assert original.read_bytes() == b"original paper bytes"
    assert "writer_input" in json.loads((root / "t/evidence.json").read_text())
    assert "task" not in json.loads((root / "t/evidence.json").read_text())


def test_identical_images_attached_once_but_different_images_kept(tmp_path):
    paths = [tmp_path / f"{i}.png" for i in range(3)]
    for p, data in zip(paths, [b"same", b"same", b"different"]): p.write_bytes(data)
    assert unique_image_paths(paths) == [paths[0], paths[2]]
    assert all(p.exists() for p in paths)


def test_singleton_packet_preserves_plan_relationships_and_missing_fact_ids(tmp_path):
    root = tmp_path / "paper_evidence" / "analysis_artifacts"
    root.mkdir(parents=True)
    unit = {"unit_id": "u", "mode": "singleton", "task_ids": ["t"],
            "relationships": [{"source_task_id": "upstream", "strength": "weak"}],
            "dependencies": ["upstream"], "artifact_ids": ["checkpoint"]}
    (root / "execution_plan.json").write_text(json.dumps({"execution_units": [unit]}))
    packet = write_writer_input(sandbox=tmp_path, members=[(1, {"task_id": "t", "required_fact_ids": ["missing"]}, {})],
        facts={}, experiment_index={}, bindings={}, paper={}, case_runtime=None)
    assert packet["execution_unit"] == unit
    assert packet["tasks"][0]["unresolved_fact_ids"] == ["missing"]


def test_writer_json_without_markdown_or_self_reported_counts_is_usable(tmp_path):
    note = {"task_id": "t", "status": "ready_for_review", "summary": "待独立核验",
            "execution_refs": [{"run_id": "host-run"}], "implementation_notes": {"method": "paper method"}}
    (tmp_path / "task_agent_result.json").write_text(json.dumps(note))
    record = _collect_task_writer_delivery(index=1, task={"task_id": "t"},
        manifest_entry={"task_id": "t", "module": "t", "output_subdir": "t"},
        sandbox=tmp_path, writer_status={"ok": True})
    assert record["writer_completed"] is True
    assert record["result_markdown_path"] is None
    assert record["delivery_blockers"] == []
    assert not any("execution_summary" in s or "full run" in s for s in writer_delivery_issues(note))
    # Usable Writer handoff is not a scientific success or an observed run.
    assert record["task_writer_status"] == "ready_for_review"
    assert record["host_execution"] is None


def test_reporter_single_decision_preserves_negative_outcome_without_old_prose():
    raw = {"schema_version": "3.0", "task_id": "t", "outcome": "not_reproduced",
           "host_action": "complete", "run_valid": True, "decision_reason": "原文端点反例成立",
           "core_conclusions": [{"claim_id": "c", "status": "unsupported", "local_observation": "发散"}],
           "remaining_uncertainties": ["作者权重约定未知"]}
    normalized = normalize_task_verification(raw, "t")
    assert normalized["outcome"] == "not_reproduced"
    assert normalized["host_action"] == "complete"
    assert normalized["remaining_uncertainties"] == raw["remaining_uncertainties"]
    legacy = normalize_task_verification({**raw, "comparison_summary": "旧比较", "report_explanation": "旧说明"}, "t")
    assert legacy["comparison_summary"] == "旧比较"
    assert legacy["report_explanation"] == "旧说明"


def test_editor_uses_host_timing_even_when_writer_claims_different_execution():
    from geng_agent.report_editor_assets import _host_report_execution
    record = {"task_id": "t", "result_json": {"execution_summary": {"full_run_count": 99, "full_durations_s": [1]}},
              "host_execution": {"passed": True, "receipt": {"observer": "orchestration_host", "task_id": "t",
                  "run_id": "observed", "mode": "full", "config": "configs/t.json", "returncode": 0,
                  "started_at": 10, "finished_at": 27.25, "environment_hash": "env"}}}
    summary = _host_report_execution(record, {"run_valid": True, "outcome": "not_reproduced"})
    assert summary["observed_full_attempt_count"] == 1
    assert summary["latest_valid_execution_count"] == 1
    assert summary["runs"] == [{"run_id": "observed", "mode": "full", "config": "configs/t.json",
                                "returncode": 0, "duration_s": 17.25, "environment_hash": "env"}]


def test_prompt_does_not_repeat_task_or_require_full_library_read():
    task = {"task_id": "t", "scientific_acceptance": {"sentinel": "COMPLETE_SCIENTIFIC_CONDITION"}}
    prompt = _build_task_writer_brief(index=1, task=task, manifest_entry={"module": "t"},
        facts={}, experiment_index={}, paper={}, paper_context_json="FULL_PREVIEW_SENTINEL",
        paper_thesis=None, run_repro=True)
    assert "COMPLETE_SCIENTIFIC_CONDITION" not in prompt
    assert "FULL_PREVIEW_SENTINEL" not in prompt
    assert "paper_evidence/writer_input.json" in prompt
    assert "Mandatory complete inputs" not in prompt
    assert "task_agent_result.md" not in prompt
    assert '"full_run_count"' not in prompt and '"last_returncode"' not in prompt
