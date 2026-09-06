from __future__ import annotations

from geng_agent.execution_plan import compile_execution_plan
from geng_agent.scientific_architecture import (
    partition_scientific_architecture_issues,
    validate_scientific_architecture,
)


def _documents(task_ids: list[str], relationships: list[dict]):
    tasks = {
        "schema_version": "2.0",
        "execution_relationships": relationships,
        "repro_tasks": [{"task_id": task_id} for task_id in task_ids],
    }
    experiments = {
        "experiments": [
            {"task_id": task_id, "experiment_id": f"exp_{index}"}
            for index, task_id in enumerate(task_ids, start=1)
        ]
    }
    return tasks, experiments


def _architecture(task_ids: list[str]) -> dict:
    basis = {
        "status": "unresolved",
        "evidence_facts": [],
        "assumption_refs": [],
        "note": "",
    }
    return {
        "schema_version": "1.0",
        "workflow_version": "2",
        "quantities": [
            {
                "id": "shared_state",
                "scope": "consistency_group",
                "basis": basis,
            }
        ],
        "components": [
            {
                "id": "shared_system",
                "module": "src/shared_system.py",
                "inputs": [],
                "outputs": ["shared_state"],
                "parameters": [],
                "depends_on": [],
                "basis": basis,
            }
        ],
        "consistency_groups": [
            {
                "id": "all_tasks",
                "task_ids": task_ids,
                "shared_quantity_ids": ["shared_state"],
            }
        ],
        "bindings": [
            {
                "task_id": task_id,
                "experiment_id": f"exp_{index}",
                "consistency_group": "all_tasks",
                "components": ["shared_system"],
                "overrides": {},
                "outputs": ["shared_state"],
            }
            for index, task_id in enumerate(task_ids, start=1)
        ],
        "invariants": [],
    }


def test_strong_relationship_without_covering_group_is_advisory() -> None:
    relationship = {
        "relationship_id": "checkpoint_flow",
        "kind": "checkpoint_flow",
        "strength": "strong",
        "task_ids": ["train", "evaluate"],
        "producer_task_id": "train",
        "consumer_task_ids": ["evaluate"],
        "artifact_ids": ["checkpoint"],
    }
    tasks, experiments = _documents(["train", "evaluate"], [relationship])
    architecture = _architecture(["train", "evaluate"])
    architecture["consistency_groups"] = [
        {"id": "train_only", "task_ids": ["train"], "shared_quantity_ids": ["shared_state"]},
        {"id": "eval_only", "task_ids": ["evaluate"], "shared_quantity_ids": ["shared_state"]},
    ]
    architecture["bindings"][0]["consistency_group"] = "train_only"
    architecture["bindings"][1]["consistency_group"] = "eval_only"

    blockers, warnings = partition_scientific_architecture_issues(
        architecture,
        facts={"engineering_facts": []},
        tasks=tasks,
        experiment_index=experiments,
        execution_plan=compile_execution_plan(tasks),
    )

    assert not any(
        issue.path == "$.execution_relationships[0].task_ids"
        for issue in blockers
    )
    assert any(
        issue.path == "$.execution_relationships[0].task_ids"
        for issue in warnings
    )


def test_overlapping_weak_groups_do_not_require_multiple_binding_pointers() -> None:
    relationships = [
        {
            "relationship_id": "shared_channel",
            "kind": "shared_definition",
            "strength": "weak",
            "task_ids": ["task_a", "task_b"],
            "producer_task_id": None,
            "consumer_task_ids": [],
            "artifact_ids": [],
        },
        {
            "relationship_id": "shared_metric",
            "kind": "shared_definition",
            "strength": "weak",
            "task_ids": ["task_b", "task_c"],
            "producer_task_id": None,
            "consumer_task_ids": [],
            "artifact_ids": [],
        },
    ]
    tasks, experiments = _documents(["task_a", "task_b", "task_c"], relationships)
    architecture = _architecture(["task_a", "task_b", "task_c"])
    architecture["consistency_groups"] = [
        {"id": "channel", "task_ids": ["task_a", "task_b"], "shared_quantity_ids": ["shared_state"]},
        {"id": "metric", "task_ids": ["task_b", "task_c"], "shared_quantity_ids": ["shared_state"]},
    ]
    architecture["bindings"][0]["consistency_group"] = "channel"
    architecture["bindings"][1]["consistency_group"] = "channel"
    architecture["bindings"][2]["consistency_group"] = "metric"

    issues = validate_scientific_architecture(
        architecture,
        facts={"engineering_facts": []},
        tasks=tasks,
        experiment_index=experiments,
        execution_plan=compile_execution_plan(tasks),
    )

    assert not any(issue.path.startswith("$.execution_relationships[") for issue in issues)


def test_same_unit_weak_relationship_architecture_gap_is_advisory() -> None:
    relationships = [
        {
            "relationship_id": "strong_ab",
            "kind": "same_run_outputs",
            "strength": "strong",
            "task_ids": ["task_a", "task_b"],
            "producer_task_id": None,
            "consumer_task_ids": [],
            "artifact_ids": [],
        },
        {
            "relationship_id": "weak_ab",
            "kind": "shared_definition",
            "strength": "weak",
            "task_ids": ["task_a", "task_b"],
            "producer_task_id": None,
            "consumer_task_ids": [],
            "artifact_ids": [],
        },
    ]
    tasks, experiments = _documents(["task_a", "task_b"], relationships)
    architecture = _architecture(["task_a", "task_b"])
    architecture["consistency_groups"] = [
        {
            "id": "task_a_only",
            "task_ids": ["task_a"],
            "shared_quantity_ids": ["shared_state"],
        },
        {
            "id": "task_b_only",
            "task_ids": ["task_b"],
            "shared_quantity_ids": ["shared_state"],
        },
    ]
    architecture["bindings"][0]["consistency_group"] = "task_a_only"
    architecture["bindings"][1]["consistency_group"] = "task_b_only"

    blockers, warnings = partition_scientific_architecture_issues(
        architecture,
        facts={"engineering_facts": []},
        tasks=tasks,
        experiment_index=experiments,
        execution_plan=compile_execution_plan(tasks),
    )

    assert not any(
        issue.path.startswith("$.execution_relationships[") for issue in blockers
    )
    assert {
        issue.path
        for issue in warnings
        if issue.path.startswith("$.execution_relationships[")
    } == {
        "$.execution_relationships[0].task_ids",
        "$.execution_relationships[1].task_ids",
    }
