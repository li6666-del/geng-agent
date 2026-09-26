"""Scientific independence is explicit; prose never rewrites accepted dependencies."""

from types import SimpleNamespace

from geng_agent.consolidated_analysis import load_experiment_plan
from geng_agent.execution_plan import compile_execution_plan
from geng_agent.pipeline import ReviewPipeline


def _task(task_id):
    # Shared figure wording must not cause implicit co-location.
    return {"task_id": task_id, "figure_or_claim": "Fig. 1", "target": task_id}


def _relationship(task_ids, *, strength="weak", kind="shared_definition", **extra):
    return {"task_ids": task_ids, "strength": strength, "kind": kind, **extra}


def test_planner_merge_is_a_real_single_task():
    task = {**_task("joint"), "experiments": [
        {"experiment_id":"train", "goal":"train a checkpoint"},
        {"experiment_id":"ber", "goal":"evaluate the same checkpoint"}]}
    plan = compile_execution_plan({"repro_tasks":[task]})
    assert plan["execution_unit_count"] == 1
    assert plan["execution_units"][0]["task_ids"] == ["joint"]
    assert len(task["experiments"]) == 2


def test_explicit_file_dependency_remains_separate_task():
    consumer = {**_task("evaluate"), "depends_on":[{"task_id":"train","artifacts":["model.pt"]}]}
    plan = compile_execution_plan({"repro_tasks":[_task("train"),consumer]})
    assert plan["execution_unit_count"] == 2
    assert plan["execution_units"][1]["depends_on"] == consumer["depends_on"]


def test_legacy_relationships_do_not_override_final_tasks():
    document = {"repro_tasks":[_task("a"),_task("b")],
                "execution_relationships":[_relationship(["a","b"],strength="strong")]}
    assert compile_execution_plan(document)["execution_unit_count"] == 2


def test_actual_combined_planner_receives_balanced_boundaries_and_state_protection(monkeypatch, tmp_path):
    pipeline = ReviewPipeline()
    captured = {}

    def capture(**kwargs):
        captured.update(kwargs)
        return {}

    monkeypatch.setattr(pipeline, "_load_or_create_analysis_stage_json", capture)
    context = SimpleNamespace(
        output_dir=tmp_path, audit_dir=tmp_path / "audit",
        options=SimpleNamespace(json_repair_attempts=1, resume=False, analysis_fallback=False,
                                analysis_backend="codex", tasks_timeout=120),
    )
    load_experiment_plan(pipeline, context, facts={}, paper_thesis=None,
                         paper={"source_sha256": "fixture"}, paper_context="fixture",
                         paper_images=[], figure_index={}, host_capabilities={})

    prompt = captured["prompt"]
    assert "single Writer's workload" in prompt
    assert "independent review and repair" in prompt
    assert "brief rationale in Chinese" in prompt
    assert "Avoid unnecessary supplementary experiments" in prompt
    assert "producer\nhandoff follows its Writer/Reporter process" in prompt
    assert "Component reuse or compatible implementations must not silently merge tasks" in prompt
    assert "ONE task with ONE task_id" not in prompt
    assert "already merged compatible experiments" not in prompt
    assert "Preserve every" in prompt
    assert "original goal, condition, baseline" in prompt
    assert "Each final task receives one independent Reporter" in prompt
    assert "consumer task's `depends_on` list" in prompt
    assert "does not infer mergers" in prompt
