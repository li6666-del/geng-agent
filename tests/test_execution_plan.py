from __future__ import annotations

import json
import unittest

from geng_agent.execution_plan import (
    ExecutionPlanError,
    compile_execution_plan,
    validate_execution_relationships,
)


def _task(task_id: str) -> dict[str, str]:
    return {"task_id": task_id}


class _PydanticLikeDocument:
    def __init__(self, value: dict) -> None:
        self.value = value

    def model_dump(self, *, mode: str = "python") -> dict:
        if mode != "python":
            raise AssertionError("compiler must request a Python-compatible dump")
        return self.value


class ExecutionPlanCompilerTests(unittest.TestCase):
    def _communication_document(self) -> dict:
        return {
            "repro_tasks": [
                _task("train_semantic_model"),
                _task("evaluate_checkpoint"),
                _task("generate_ber_curves"),
                _task("generate_similarity_curves"),
                _task("parse_protocol_table"),
            ],
            "execution_relationships": [
                {
                    "relationship_id": "checkpoint_evaluation",
                    "strength": "strong",
                    "task_ids": ["train_semantic_model", "evaluate_checkpoint"],
                    "producer_task_id": "train_semantic_model",
                    "consumer_task_ids": ["evaluate_checkpoint"],
                    "artifact_ids": ["semantic_model_checkpoint"],
                },
                {
                    "relationship_id": "shared_random_realization",
                    "strength": "strong",
                    "task_ids": [
                        "generate_ber_curves",
                        "generate_similarity_curves",
                    ],
                    "artifact_ids": ["shared_random_stream"],
                },
                {
                    "relationship_id": "shared_channel_definition",
                    "strength": "weak",
                    "task_ids": ["evaluate_checkpoint", "generate_ber_curves"],
                    "artifact_ids": ["channel_model_definition"],
                },
            ],
        }

    def test_compiles_five_logical_tasks_into_three_execution_units(self) -> None:
        document = self._communication_document()

        validate_execution_relationships(document)
        plan = compile_execution_plan(document)

        self.assertEqual(plan["schema_version"], "1.0")
        self.assertEqual(plan["logical_task_count"], 5)
        self.assertEqual(plan["execution_unit_count"], 3)
        self.assertEqual(
            [unit["task_ids"] for unit in plan["execution_units"]],
            [
                ["train_semantic_model", "evaluate_checkpoint"],
                ["generate_ber_curves", "generate_similarity_curves"],
                ["parse_protocol_table"],
            ],
        )
        self.assertEqual(
            [unit["mode"] for unit in plan["execution_units"]],
            ["compound", "compound", "singleton"],
        )

        training_unit = plan["execution_units"][0]
        checkpoint_dependency = next(
            item
            for item in training_unit["dependencies"]
            if item["artifact_id"] == "semantic_model_checkpoint"
        )
        self.assertEqual(
            (
                checkpoint_dependency["producer_task_id"],
                checkpoint_dependency["consumer_task_id"],
            ),
            ("train_semantic_model", "evaluate_checkpoint"),
        )
        self.assertIn("semantic_model_checkpoint", training_unit["artifact_ids"])

        weak_group = plan["weak_consistency_groups"][0]
        self.assertEqual(
            weak_group["task_ids"],
            ["evaluate_checkpoint", "generate_ber_curves"],
        )
        self.assertEqual(len(weak_group["execution_unit_ids"]), 2)
        self.assertNotEqual(
            plan["task_to_execution_unit"]["evaluate_checkpoint"],
            plan["task_to_execution_unit"]["generate_ber_curves"],
        )
        json.dumps(plan, ensure_ascii=False, sort_keys=True)

    def test_accepts_model_dump_and_is_independent_of_relationship_order(self) -> None:
        document = self._communication_document()
        reversed_document = {
            **document,
            "execution_relationships": list(
                reversed(document["execution_relationships"])
            ),
        }

        from_mapping = compile_execution_plan(document)
        from_model = compile_execution_plan(
            _PydanticLikeDocument(reversed_document)
        )

        self.assertEqual(from_model, from_mapping)

    def test_compound_task_ids_follow_strong_topology_with_original_order_ties(self) -> None:
        document = {
            "repro_tasks": [
                _task("evaluate_second"),
                _task("evaluate_first"),
                _task("train"),
            ],
            "execution_relationships": [
                {
                    "relationship_id": "train_to_second",
                    "strength": "strong",
                    "task_ids": ["train", "evaluate_second"],
                    "producer_task_id": "train",
                    "consumer_task_ids": ["evaluate_second"],
                    "artifact_ids": ["checkpoint_for_second"],
                },
                {
                    "relationship_id": "train_to_first",
                    "strength": "strong",
                    "task_ids": ["train", "evaluate_first"],
                    "producer_task_id": "train",
                    "consumer_task_ids": ["evaluate_first"],
                    "artifact_ids": ["checkpoint_for_first"],
                },
            ],
        }

        plan = compile_execution_plan(document)

        self.assertEqual(
            plan["execution_units"][0]["task_ids"],
            ["train", "evaluate_second", "evaluate_first"],
        )

    def test_one_producer_can_feed_two_consumers(self) -> None:
        document = {
            "repro_tasks": [
                _task("evaluate_ber"),
                _task("train"),
                _task("evaluate_similarity"),
            ],
            "execution_relationships": [
                {
                    "relationship_id": "one_checkpoint_two_evaluations",
                    "strength": "strong",
                    "task_ids": [
                        "train",
                        "evaluate_ber",
                        "evaluate_similarity",
                    ],
                    "producer_task_id": "train",
                    "consumer_task_ids": [
                        "evaluate_ber",
                        "evaluate_similarity",
                    ],
                    "artifact_ids": ["semantic_model_checkpoint"],
                }
            ],
        }

        plan = compile_execution_plan(document)

        unit = plan["execution_units"][0]
        self.assertEqual(
            unit["task_ids"],
            ["train", "evaluate_ber", "evaluate_similarity"],
        )
        relationship = unit["relationships"][0]
        self.assertEqual(
            relationship["consumer_task_ids"],
            ["evaluate_ber", "evaluate_similarity"],
        )
        self.assertEqual(
            {
                dependency["consumer_task_id"]
                for dependency in unit["dependencies"]
            },
            {"evaluate_ber", "evaluate_similarity"},
        )

    def test_unknown_task_reference_is_rejected(self) -> None:
        document = {
            "repro_tasks": [_task("known_a"), _task("known_b")],
            "execution_relationships": [
                {
                    "relationship_id": "unknown_member",
                    "strength": "strong",
                    "task_ids": ["known_a", "ghost_task"],
                }
            ],
        }

        with self.assertRaisesRegex(ExecutionPlanError, "unknown task"):
            compile_execution_plan(document)

    def test_dependency_endpoint_must_belong_to_relationship(self) -> None:
        document = {
            "repro_tasks": [_task("a"), _task("b"), _task("c")],
            "execution_relationships": [
                {
                    "relationship_id": "bad_endpoint",
                    "strength": "strong",
                    "task_ids": ["a", "b"],
                    "producer_task_id": "c",
                    "consumer_task_ids": ["b"],
                    "artifact_ids": ["checkpoint"],
                }
            ],
        }

        with self.assertRaisesRegex(
            ExecutionPlanError, "not included in relationship.task_ids"
        ):
            compile_execution_plan(document)

    def test_strong_dependency_cycle_is_rejected(self) -> None:
        document = {
            "repro_tasks": [_task("train"), _task("evaluate")],
            "execution_relationships": [
                {
                    "relationship_id": "train_to_eval",
                    "strength": "strong",
                    "task_ids": ["train", "evaluate"],
                    "producer_task_id": "train",
                    "consumer_task_ids": ["evaluate"],
                    "artifact_ids": ["checkpoint"],
                },
                {
                    "relationship_id": "eval_to_train",
                    "strength": "strong",
                    "task_ids": ["train", "evaluate"],
                    "producer_task_id": "evaluate",
                    "consumer_task_ids": ["train"],
                    "artifact_ids": ["calibration"],
                },
            ],
        }

        with self.assertRaisesRegex(ExecutionPlanError, "strong dependency cycle"):
            compile_execution_plan(document)

    def test_one_artifact_cannot_have_multiple_producers(self) -> None:
        document = {
            "repro_tasks": [_task("train_a"), _task("train_b"), _task("evaluate")],
            "execution_relationships": [
                {
                    "relationship_id": "producer_a",
                    "strength": "strong",
                    "task_ids": ["train_a", "evaluate"],
                    "producer_task_id": "train_a",
                    "consumer_task_ids": ["evaluate"],
                    "artifact_ids": ["shared_checkpoint"],
                },
                {
                    "relationship_id": "producer_b",
                    "strength": "strong",
                    "task_ids": ["train_b", "evaluate"],
                    "producer_task_id": "train_b",
                    "consumer_task_ids": ["evaluate"],
                    "artifact_ids": ["shared_checkpoint"],
                },
            ],
        }

        with self.assertRaisesRegex(ExecutionPlanError, "multiple producers"):
            compile_execution_plan(document)

    def test_disjoint_units_may_reuse_a_generic_artifact_label(self) -> None:
        document = {
            "repro_tasks": [
                _task("train_a"),
                _task("evaluate_a"),
                _task("train_b"),
                _task("evaluate_b"),
            ],
            "execution_relationships": [
                {
                    "relationship_id": "flow_a",
                    "strength": "strong",
                    "task_ids": ["train_a", "evaluate_a"],
                    "producer_task_id": "train_a",
                    "consumer_task_ids": ["evaluate_a"],
                    "artifact_ids": ["checkpoint"],
                },
                {
                    "relationship_id": "flow_b",
                    "strength": "strong",
                    "task_ids": ["train_b", "evaluate_b"],
                    "producer_task_id": "train_b",
                    "consumer_task_ids": ["evaluate_b"],
                    "artifact_ids": ["checkpoint"],
                },
            ],
        }

        plan = compile_execution_plan(document)

        self.assertEqual(plan["execution_unit_count"], 2)
        scoped_ids = {
            dependency["artifact_id"]
            for unit in plan["execution_units"]
            for dependency in unit["dependencies"]
        }
        self.assertEqual(len(scoped_ids), 2)
        self.assertNotIn("checkpoint", scoped_ids)
        self.assertEqual(
            {scope["source_artifact_id"] for scope in plan["artifact_scopes"]},
            {"checkpoint"},
        )
        self.assertEqual(len(plan["artifact_scopes"]), 2)


if __name__ == "__main__":
    unittest.main()
