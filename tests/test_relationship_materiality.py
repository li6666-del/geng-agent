from __future__ import annotations

from geng_agent.execution_plan import compile_execution_plan
from geng_agent.pipeline import (
    _execution_plan_requires_shared_science,
    _shared_foundation_is_material,
)
from geng_agent.scientific_architecture import (
    partition_scientific_architecture_issues,
)
from geng_agent.tasks_normalize import finalize_repro_tasks


def _task(task_id: str) -> dict[str, str]:
    return {
        "task_id": task_id,
        "target": f"Reproduce the scientific conclusion for {task_id}",
        "figure_or_claim": f"scientific claim for {task_id}",
    }


def _relationship(
    relationship_id: str,
    strength: str,
    left: str,
    right: str,
) -> dict[str, object]:
    return {
        "relationship_id": relationship_id,
        "kind": "shared_definition",
        "strength": strength,
        "task_ids": [left, right],
        "producer_task_id": None,
        "consumer_task_ids": [],
        "artifact_ids": [],
        "rationale": "the two tasks must use the same scientific definition",
    }


def _document(
    task_ids: list[str], relationships: list[dict[str, object]]
) -> dict[str, object]:
    return {
        "schema_version": "2.0",
        "repro_tasks": [_task(task_id) for task_id in task_ids],
        "execution_relationships": relationships,
    }


def _shared_architecture(
    task_ids: list[str],
    *,
    group_task_ids: list[str],
) -> tuple[dict[str, object], dict[str, object]]:
    basis = {
        "status": "unresolved",
        "evidence_facts": [],
        "assumption_refs": [],
        "note": "",
    }
    experiments = {
        "experiments": [
            {"task_id": task_id, "experiment_id": f"exp_{task_id}"}
            for task_id in task_ids
        ]
    }
    architecture = {
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
                "id": "shared_component",
                "module": "src/shared_component.py",
                "inputs": [],
                "outputs": ["shared_state"],
                "parameters": [],
                "depends_on": [],
                "basis": basis,
            }
        ],
        "consistency_groups": [
            {
                "id": "broad_group",
                "task_ids": group_task_ids,
                "shared_quantity_ids": ["shared_state"],
            }
        ],
        "bindings": [
            {
                "task_id": task_id,
                "experiment_id": f"exp_{task_id}",
                "consistency_group": "broad_group",
                "components": ["shared_component"],
                "overrides": {},
                "outputs": ["shared_state"],
            }
            for task_id in task_ids
        ],
        "invariants": [],
    }
    return architecture, experiments


def test_overlapping_weak_relationships_remain_two_nontransitive_groups() -> None:
    plan = compile_execution_plan(
        _document(
            ["task_a", "task_b", "task_c"],
            [
                _relationship("weak_ab", "weak", "task_a", "task_b"),
                _relationship("weak_bc", "weak", "task_b", "task_c"),
            ],
        )
    )

    groups = plan["weak_consistency_groups"]
    assert [group["task_ids"] for group in groups] == [
        ["task_a", "task_b"],
        ["task_b", "task_c"],
    ]
    assert [group["relationship_ids"] for group in groups] == [
        ["weak_ab"],
        ["weak_bc"],
    ]
    assert sum("task_b" in group["task_ids"] for group in groups) == 2
    assert not any(
        {"task_a", "task_c"}.issubset(set(group["task_ids"]))
        for group in groups
    )


def test_only_cross_unit_weak_relationships_make_foundation_material() -> None:
    strong_ab = _relationship("strong_ab", "strong", "task_a", "task_b")

    strong_only_plan = compile_execution_plan(
        _document(["task_a", "task_b"], [strong_ab])
    )
    same_unit_weak_plan = compile_execution_plan(
        _document(
            ["task_a", "task_b"],
            [
                strong_ab,
                _relationship("weak_ab", "weak", "task_a", "task_b"),
            ],
        )
    )
    cross_unit_weak_plan = compile_execution_plan(
        _document(
            ["task_a", "task_b", "task_c"],
            [
                strong_ab,
                _relationship("weak_bc", "weak", "task_b", "task_c"),
            ],
        )
    )

    assert strong_only_plan["execution_unit_count"] == 1
    assert not _execution_plan_requires_shared_science(strong_only_plan)
    assert not _shared_foundation_is_material(strong_only_plan, architecture={})

    same_unit_group = same_unit_weak_plan["weak_consistency_groups"][0]
    assert len(same_unit_group["execution_unit_ids"]) == 1
    assert not _execution_plan_requires_shared_science(same_unit_weak_plan)
    assert not _shared_foundation_is_material(same_unit_weak_plan, architecture={})

    cross_unit_group = cross_unit_weak_plan["weak_consistency_groups"][0]
    assert len(cross_unit_group["execution_unit_ids"]) == 2
    assert _execution_plan_requires_shared_science(cross_unit_weak_plan)
    assert _shared_foundation_is_material(cross_unit_weak_plan, architecture={})


def test_undeclared_shared_quantity_cannot_satisfy_relationship_contract() -> None:
    tasks = _document(
        ["task_a", "task_b"],
        [_relationship("weak_ab", "weak", "task_a", "task_b")],
    )
    experiments = {
        "experiments": [
            {"task_id": "task_a", "experiment_id": "exp_a"},
            {"task_id": "task_b", "experiment_id": "exp_b"},
        ]
    }
    basis = {
        "status": "unresolved",
        "evidence_facts": [],
        "assumption_refs": [],
        "note": "",
    }
    architecture = {
        "schema_version": "1.0",
        "workflow_version": "2",
        "quantities": [
            {
                "id": "declared_task_output",
                "scope": "task",
                "basis": basis,
            }
        ],
        "components": [
            {
                "id": "component_a",
                "module": "src/component_a.py",
                "inputs": [],
                "outputs": ["declared_task_output"],
                "parameters": [],
                "depends_on": [],
                "basis": basis,
            },
            {
                "id": "component_b",
                "module": "src/component_b.py",
                "inputs": [],
                "outputs": ["declared_task_output"],
                "parameters": [],
                "depends_on": [],
                "basis": basis,
            },
        ],
        "consistency_groups": [
            {
                "id": "weak_ab_group",
                "task_ids": ["task_a", "task_b"],
                "shared_quantity_ids": ["undeclared_shared_quantity"],
            }
        ],
        "bindings": [
            {
                "task_id": "task_a",
                "experiment_id": "exp_a",
                "consistency_group": "weak_ab_group",
                "components": ["component_a"],
                "overrides": {},
                "outputs": ["declared_task_output"],
            },
            {
                "task_id": "task_b",
                "experiment_id": "exp_b",
                "consistency_group": "weak_ab_group",
                "components": ["component_b"],
                "overrides": {},
                "outputs": ["declared_task_output"],
            },
        ],
        "invariants": [],
    }

    blockers, _warnings = partition_scientific_architecture_issues(
        architecture,
        facts={"engineering_facts": []},
        tasks=tasks,
        experiment_index=experiments,
        execution_plan=compile_execution_plan(tasks),
    )

    assert any(
        issue.path == "$.execution_relationships[0].shared_science"
        for issue in blockers
    )


def test_material_weak_relationship_requires_exact_architecture_membership() -> None:
    tasks = _document(
        ["task_a", "task_b", "task_c"],
        [_relationship("weak_ab", "weak", "task_a", "task_b")],
    )
    architecture, experiments = _shared_architecture(
        ["task_a", "task_b", "task_c"],
        group_task_ids=["task_a", "task_b", "task_c"],
    )

    blockers, _warnings = partition_scientific_architecture_issues(
        architecture,
        facts={"engineering_facts": []},
        tasks=tasks,
        experiment_index=experiments,
        execution_plan=compile_execution_plan(tasks),
    )

    assert any(
        issue.path == "$.execution_relationships[0].task_ids"
        for issue in blockers
    )


def test_missing_or_invalid_strength_normalizes_to_conservative_strong() -> None:
    normalized = finalize_repro_tasks(
        {
            "execution_relationships": [
                {
                    "relationship_id": "missing_strength",
                    "kind": "shared_definition",
                    "task_ids": ["task_a", "task_b"],
                },
                {
                    "relationship_id": "invalid_strength",
                    "kind": "shared_definition",
                    "strength": "uncertain",
                    "task_ids": ["task_b", "task_c"],
                },
            ],
            "repro_tasks": [_task(task_id) for task_id in ("task_a", "task_b", "task_c")],
        },
        {"engineering_facts": []},
    )

    assert [
        relationship["strength"]
        for relationship in normalized["execution_relationships"]
    ] == ["strong", "strong"]
    plan = compile_execution_plan(normalized)
    assert plan["execution_unit_count"] == 1
    assert plan["execution_units"][0]["task_ids"] == [
        "task_a",
        "task_b",
        "task_c",
    ]
