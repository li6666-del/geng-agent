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


def test_shared_deterministic_science_leaves_distinct_goals_independent():
    task_ids = ["accuracy", "interval_ranking", "tail_limit", "order_sweep"]
    plan = compile_execution_plan({
        "repro_tasks": [_task(task_id) for task_id in task_ids],
        "execution_relationships": [_relationship(task_ids, rationale="Same frozen evaluator and grid")],
    })

    assert [unit["task_ids"] for unit in plan["execution_units"]] == [[task_id] for task_id in task_ids]
    assert len(plan["weak_consistency_groups"]) == 1
    assert len(plan["weak_consistency_groups"][0]["execution_unit_ids"]) == 4


def test_real_state_flow_and_paired_samples_survive_independent_definitions():
    tasks = ["train", "evaluate_ber", "evaluate_similarity", "paired_a", "paired_b", "analytic_bound"]
    plan = compile_execution_plan({
        "repro_tasks": [_task(task_id) for task_id in tasks],
        "execution_relationships": [
            _relationship(tasks[:3], strength="strong", kind="checkpoint_flow",
                          producer_task_id="train", consumer_task_ids=tasks[1:3],
                          artifact_ids=["selected_checkpoint"]),
            _relationship(tasks[3:5], strength="strong", kind="shared_random_realization",
                          artifact_ids=["paired_channel_samples"]),
            _relationship(["evaluate_ber", "paired_a", "analytic_bound"]),
        ],
    })

    assert [unit["task_ids"] for unit in plan["execution_units"]] == [tasks[:3], tasks[3:5], tasks[5:]]
    dependencies = plan["execution_units"][0]["dependencies"]
    assert {(item["producer_task_id"], item["consumer_task_id"], item["artifact_id"])
            for item in dependencies} == {
        ("train", "evaluate_ber", "selected_checkpoint"),
        ("train", "evaluate_similarity", "selected_checkpoint"),
    }


def test_host_neither_invents_dependencies_nor_downgrades_explicit_strong():
    task_ids = ["checkpoint_analysis", "checkpoint_ranking"]
    document = {"repro_tasks": [_task(task_id) for task_id in task_ids]}
    assert compile_execution_plan(document)["execution_unit_count"] == 2
    # Even a questionable explanation is not authorization for host semantic judgment.
    document["execution_relationships"] = [_relationship(
        task_ids, strength="strong", kind="same_run_outputs",
        rationale="Both cite one figure and recompute deterministic values",
    )]
    assert compile_execution_plan(document)["execution_unit_count"] == 1


def test_actual_combined_planner_receives_independence_and_state_protection(monkeypatch, tmp_path):
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
    assert "independent Writer by default" in prompt
    assert "deterministic recalculation" in prompt
    assert "Foundation supplies the same implementation" in prompt
    assert "Uncertainty is not a reason to guess strong" in prompt
    assert "Preserve every scientifically necessary dependency" in prompt
    assert "does not reinterpret prose to weaken them" in prompt
    # These architecture rules are injected by the real combined entry point.
    assert "same-run CSVs merely to agree numerically" in prompt
    assert "unnecessary draft edge is not an immutable scientific fact" in prompt
