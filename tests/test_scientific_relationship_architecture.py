from copy import deepcopy
from geng_agent.schemas import validate_stage


def test_architecture_does_not_infer_relationship_semantics_from_shared_prose():
    document = {"tasks": {"repro_tasks": [{"task_id": "a"}, {"task_id": "b"}],
        "execution_relationships": [{"strength": "weak", "task_ids": ["a", "b"]}]},
        "scientific_architecture": {"components": [{"id": "shared", "description": "same paper definitions"}],
                                    "consistency_groups": [{"task_ids": ["a"]}]}}
    original = deepcopy(document)
    assert validate_stage("experiment_plan", document) == []
    assert document == original
