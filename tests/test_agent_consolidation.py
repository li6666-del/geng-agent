from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from geng_agent.agentic_analysis import run_codex_json_stage
from geng_agent.pipeline import ReviewPipeline
from geng_agent.stage_cleanup import _clear_stage_outputs
from tests.test_pipeline import fact, fact_doc, task, task_doc, thesis_doc, understanding_doc


class AnalysisClient:
    """Deterministic transport fixture; never connects to a model or network."""
    model = "fixture"

    def __init__(self):
        self.calls = []

    def complete(self, prompt, **kwargs):
        self.calls.append(prompt)
        if "Role: paper understanding" in prompt:
            return json.dumps(understanding_doc(fact_doc(fact("figure_claim", "Fig. 4"))))
        assert "Role: experiment planner" in prompt
        assert thesis_doc()["central_claim"] in prompt
        tasks = task_doc(task("fig4", "Fig. 4"))
        tasks["backfill_handoff"] = {"ready_for_writer": True, "blocking_request_ids": [], "reason": "evidence ready"}
        return json.dumps({"tasks": tasks, "scientific_architecture": None})


def test_two_analysis_calls_resume_and_selective_invalidation(tmp_path):
    paper = tmp_path / "paper.md"
    paper.write_text("# Results\nFig. 4: BER decreases with SNR.", encoding="utf-8")
    output = tmp_path / "case"
    client = AnalysisClient()
    pipeline = ReviewPipeline(client=client)
    capabilities = {"fixture": "cpu"}
    mineru = {"ok": True, "figure_index": {"figures": [], "unmatched_visuals": []}}
    with (
        patch.object(pipeline, "_render_paper_images", return_value=[]),
        patch("geng_agent.pipeline.run_mineru_layout_stage", return_value=mineru),
        patch("geng_agent.preflight.architecture_capability_inventory", side_effect=lambda: deepcopy(capabilities)),
    ):
        def run():
            pipeline.run(paper, output, analysis_only=True, analysis_backend="llm", analysis_fallback=False)
            return json.loads((output / "analysis_result.json").read_text(encoding="utf-8"))

        first = run()
        assert len(client.calls) == first["analysis_stage_invocations"] == 2
        assert all((output / name).exists() for name in (
            "paper_understanding.json", "paper_thesis.json", "engineering_facts.json", "experiment_plan.json", "repro_tasks.json"))
        second = run()
        assert len(client.calls) == 2
        assert second["analysis_stage_invocations"] == 0
        # Host capabilities affect planning, never what the paper asserts.
        capabilities["fixture"] = "gpu"
        third = run()
        assert len(client.calls) == 3
        assert third["analysis_stage_invocations"] == 1
        # Explicit architecture restart invalidates the coupled plan only.
        _clear_stage_outputs(output, "scientific_architecture")
        run()
        assert len(client.calls) == 4
        paper.write_text("# Results\nFig. 4: revised paper conditions.", encoding="utf-8")
        run()
        assert len(client.calls) == 6


def test_backfill_revision_cache_survives_resume_bookkeeping(tmp_path):
    from geng_agent.task_evidence_backfill import collect_missing_fact_requests
    initial = understanding_doc(fact_doc(fact("figure_claim", "Fig. 4")))
    draft = task("t1", "Fig. 4")
    draft["missing_fact_requests"] = [{"request_id": "normalization", "type": "simulation_parameter",
        "name": "power normalization", "why_needed": "defines the SNR", "impact": "high", "search_targets": ["Fig. 4"]}]
    preliminary = task_doc(draft)
    preliminary["backfill_handoff"] = {"ready_for_writer": False, "blocking_request_ids": ["normalization"], "reason": "need evidence"}
    request = collect_missing_fact_requests(preliminary)[0]
    backfill = {**fact_doc(fact("simulation_parameter", "power normalization")), "request_resolutions": [{
        "request_id": request["request_id"], "field_results": [{"field_id": "answer", "status": "resolved_explicit",
            "fact_refs": [{"type": "simulation_parameter", "name": "power normalization"}],
            "searched_locations": ["Fig. 4"], "note": "found in caption"}]}]}
    final_tasks = task_doc(task("t1", "Fig. 4"))
    final_tasks["backfill_handoff"] = {"ready_for_writer": True, "blocking_request_ids": [], "reason": "ready"}
    responses = [initial, {"tasks": preliminary, "scientific_architecture": None}, backfill,
                 {"tasks": final_tasks, "scientific_architecture": None}]
    prompts = []

    class Client:
        def complete(self, prompt, **kwargs):
            prompts.append(prompt)
            assert responses, "unchanged resume must reuse the entire backfill plan"
            return json.dumps(responses.pop(0))

    pipeline = ReviewPipeline(client=Client())
    paper = tmp_path / "paper.md"
    paper.write_text("# Results\nFig. 4 power normalization.", encoding="utf-8")
    output = tmp_path / "case"
    with patch("geng_agent.pipeline.run_mineru_layout_stage", return_value={"ok": True, "figure_index": {}}), \
         patch("geng_agent.preflight.architecture_capability_inventory", return_value={}):
        for _ in range(2):
            pipeline.run(paper, output, analysis_only=True, analysis_backend="llm", analysis_fallback=False)
    assert len(prompts) == 4
    result = json.loads((output / "analysis_result.json").read_text(encoding="utf-8"))
    assert result["analysis_stage_invocations"] == 0
    assert json.loads((output / "experiment_plan.json").read_text(encoding="utf-8"))["_meta"]["planning_complete"]


@pytest.mark.parametrize("broken", ["missing", "syntax"])
def test_combined_codex_repairs_one_document_in_same_workspace(tmp_path, broken):
    output = tmp_path / "case"
    audit = output / "audit"
    audit.mkdir(parents=True)
    facts = fact_doc(fact("figure_claim", "Fig. 4"))
    workspaces = []
    first_bytes = None

    def worker(**kwargs):
        nonlocal first_bytes
        workspace = kwargs["work_dir"]
        workspaces.append(workspace)
        assert workspace.is_relative_to(audit)
        assert workspace != output
        assert kwargs["sandbox"] == "workspace-write"
        if len(workspaces) == 1:
            (workspace / "facts.json").write_text(json.dumps(facts), encoding="utf-8")
            first_bytes = (workspace / "facts.json").read_bytes()
            (workspace / "limitations.json").write_text("[]", encoding="utf-8")
            if broken == "syntax":
                (workspace / "paper_thesis.json").write_text("{broken", encoding="utf-8")
        else:
            assert (workspace / "facts.json").read_bytes() == first_bytes
            assert "paper_thesis" in kwargs["prompt"]
            (workspace / "paper_thesis.json").write_text(json.dumps(thesis_doc()), encoding="utf-8")
        # Completion prose is not the large JSON transport.
        return {"ok": True}

    with patch("geng_agent.agentic_analysis.run_codex_subprocess", side_effect=worker):
        result = run_codex_json_stage(prompt="Read the paper", stage_label="01_understand_paper",
            schema_stage="paper_understanding", output_dir=output, audit_dir=audit, max_attempts=2)
    assert len(workspaces) == 2 and workspaces[0] == workspaces[1]
    assert result["facts"] == facts
    assert result["paper_thesis"] == thesis_doc()






def test_plan_architecture_repair_permissions_stay_field_specific():
    from geng_agent.analysis_repair import preserved_science_issues, scientific_correction_paths
    from geng_agent.consolidated_analysis import plan_repair_preservation
    from geng_agent.schemas import ValidationIssue
    before = {"scientific_architecture": {"quantities": [
        {"id": "q", "scope": "global", "unit": "W", "default": 2.0}], "bindings": []}}
    after = deepcopy(before)
    after["scientific_architecture"]["quantities"][0].update(scope="experiment", unit="dBm")
    allowed = scientific_correction_paths([ValidationIssue("$.scientific_architecture.quantities[0].scope", "scope conflict")], after, before)
    remaining = preserved_science_issues(plan_repair_preservation, before, after, allowed)
    assert any(issue.path.endswith(".unit") for issue in remaining)
    assert not any(issue.path.endswith(".scope") for issue in remaining)


def test_combined_documents_use_real_subprocess_working_directory(tmp_path):
    from tests.test_agentic_analysis import _write_analysis_script
    payload = understanding_doc(fact_doc(fact("figure_claim", "Fig. 4")))
    command = _write_analysis_script(tmp_path, f'''
        import json
        from pathlib import Path
        payload = json.loads({json.dumps(payload)!r})
        for field, name in {{"facts": "facts.json", "paper_thesis": "paper_thesis.json", "limitations": "limitations.json"}}.items():
            Path(name).write_text(json.dumps(payload[field]), encoding="utf-8")
        # No --output-last-message JSON; the host reads the separate files.
    ''')
    output = tmp_path / "case"
    with patch.dict("os.environ", {"GENG_CODEX_ANALYSIS_CMD": command}):
        result = run_codex_json_stage(prompt="fixture paper", stage_label="01_understand_paper",
            schema_stage="paper_understanding", output_dir=output, audit_dir=output / "audit", max_attempts=1)
    assert result["facts"] == payload["facts"]
    assert not (output / "facts.json").exists()
    assert len(list((output / "audit" / "01_understand_paper_documents").glob("*/facts.json"))) == 1


def test_coupled_planner_validates_actual_shared_dependencies(tmp_path):
    from types import SimpleNamespace
    from geng_agent.consolidated_analysis import load_experiment_plan
    from tests.test_pipeline import architecture_doc

    facts = fact_doc(fact("figure_claim", "Fig. 1"), fact("figure_claim", "Fig. 2"))
    tasks = task_doc(task("t1", "Fig. 1"), task("t2", "Fig. 2"))
    tasks["backfill_handoff"] = {"ready_for_writer": True, "blocking_request_ids": [], "reason": "ready"}
    tasks["execution_relationships"] = [{"relationship_id": "channel", "kind": "shared_definition", "strength": "weak",
        "task_ids": ["t1", "t2"], "producer_task_id": None, "consumer_task_ids": [], "artifact_ids": []}]
    responses = [{"tasks": tasks, "scientific_architecture": None},
                 {"tasks": tasks, "scientific_architecture": architecture_doc(tmp_path, tasks)}]
    prompts = []

    class Client:
        def complete(self, prompt, **kwargs):
            prompts.append(prompt)
            return json.dumps(responses.pop(0))

    context = SimpleNamespace(output_dir=tmp_path, audit_dir=tmp_path / "audit", options=SimpleNamespace(
        json_repair_attempts=1, resume=False, tasks_timeout=10, analysis_backend="llm", analysis_fallback=False))
    plan = load_experiment_plan(ReviewPipeline(client=Client()), context, facts=facts, paper_thesis=thesis_doc(),
        paper={}, paper_context="paper", paper_images=[], figure_index={}, host_capabilities={})
    assert len(prompts) == 2
    assert "Shared scientific dependencies require an architecture" in prompts[1]
    assert {item["task_id"] for item in plan["scientific_architecture"]["bindings"]} == {"t1", "t2"}


def test_existing_editor_is_default_and_program_override_is_retired(tmp_path):
    from tests.test_pipeline import _run_minimal_full_pipeline
    for mode in ("", "program"):
        case = tmp_path / (mode or "default")
        case.mkdir()
        _, output = _run_minimal_full_pipeline(case, report_mode=mode,
            report_editor_error=RuntimeError("editor was invoked"),
            verdict_candidate={"invalid_fixture_verdict": True})
        risk = json.loads((output / "risk_report.json").read_text(encoding="utf-8"))
        assert any("editor was invoked" in str(item) for item in risk["findings"])
        assert not (output / "audit/04b_program_report_status.json").exists()
