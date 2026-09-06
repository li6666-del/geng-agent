from __future__ import annotations

import unittest
from pathlib import Path

from geng_agent.schema_models import ReproTasksDocument
from geng_agent.schemas import validate_stage
from geng_agent.tasks_normalize import finalize_repro_tasks


def _task(task_id: str) -> dict:
    return {
        "task_id": task_id,
        "target": f"Reproduce the scientific conclusion for {task_id}",
        "figure_or_claim": f"scientific claim for {task_id}",
    }


class TaskExecutionRelationshipContractTests(unittest.TestCase):
    def test_legacy_document_gets_explicit_v2_defaults(self) -> None:
        normalized = finalize_repro_tasks(
            {"repro_tasks": [_task("task_a"), _task("task_b")]},
            {"engineering_facts": []},
        )

        self.assertEqual(normalized["schema_version"], "2.0")
        self.assertEqual(normalized["execution_relationships"], [])
        self.assertEqual(
            normalized["backfill_handoff"],
            {
                "ready_for_writer": True,
                "blocking_request_ids": [],
                "reason": (
                    "Host inferred a non-blocking Writer handoff because the Task "
                    "Designer did not provide a valid backfill_handoff."
                ),
                "inferred": True,
            },
        )
        self.assertEqual(validate_stage("repro_tasks", normalized), [])

    def test_relationship_contract_preserves_unknown_task_references_for_compiler(self) -> None:
        normalized = finalize_repro_tasks(
            {
                "schema_version": "2.0",
                "backfill_handoff": {
                    "ready_for_writer": True,
                    "blocking_request_ids": ["request_a", "request_a", ""],
                    "reason": "the atomic tasks are ready",
                    "inferred": False,
                },
                "execution_relationships": [
                    {
                        "relationship_id": "checkpoint_to_evaluation",
                        "kind": "checkpoint_flow",
                        "strength": "strong",
                        "task_ids": ["train", "evaluate", "unknown_future_task"],
                        "producer_task_id": "train",
                        "consumer_task_ids": ["evaluate"],
                        "artifact_ids": ["trained_checkpoint"],
                        "rationale": "evaluation must consume the exact trained checkpoint",
                    }
                ],
                "repro_tasks": [_task("train"), _task("evaluate")],
            },
            {"engineering_facts": []},
        )

        relationship = normalized["execution_relationships"][0]
        self.assertEqual(relationship["task_ids"][-1], "unknown_future_task")
        self.assertEqual(relationship["producer_task_id"], "train")
        self.assertEqual(relationship["consumer_task_ids"], ["evaluate"])
        self.assertEqual(relationship["artifact_ids"], ["trained_checkpoint"])
        self.assertFalse(normalized["backfill_handoff"]["inferred"])
        self.assertEqual(
            normalized["backfill_handoff"]["blocking_request_ids"], ["request_a"]
        )
        self.assertEqual(validate_stage("repro_tasks", normalized), [])
        ReproTasksDocument.model_validate(
            {key: value for key, value in normalized.items() if key != "_meta"}
        )

    def test_low_value_relationship_shape_debt_is_repaired_or_dropped(self) -> None:
        normalized = finalize_repro_tasks(
            {
                "backfill_handoff": "malformed",
                "execution_relationships": [
                    {
                        "relationship_id": "shared_definition",
                        "kind": "unrecognized_kind",
                        "strength": "uncertain",
                        "task_ids": ["task_a", "task_b", "task_b"],
                    },
                    {
                        "relationship_id": "one_task_is_not_a_relationship",
                        "kind": "shared_definition",
                        "strength": "weak",
                        "task_ids": ["task_a"],
                    },
                    "not an object",
                ],
                "repro_tasks": [_task("task_a"), _task("task_b")],
            },
            {"engineering_facts": []},
        )

        self.assertTrue(normalized["backfill_handoff"]["inferred"])
        self.assertEqual(len(normalized["execution_relationships"]), 1)
        relationship = normalized["execution_relationships"][0]
        self.assertEqual(relationship["kind"], "other")
        # An invalid strength is conservatively co-located so the host does not
        # accidentally split tasks that may require one shared run.
        self.assertEqual(relationship["strength"], "strong")
        self.assertEqual(relationship["task_ids"], ["task_a", "task_b"])
        self.assertIsNone(relationship["producer_task_id"])
        self.assertEqual(relationship["consumer_task_ids"], [])
        self.assertEqual(relationship["artifact_ids"], [])
        self.assertEqual(validate_stage("repro_tasks", normalized), [])

    def test_complete_weak_artifact_flow_is_conservatively_colocated(self) -> None:
        normalized = finalize_repro_tasks(
            {
                "execution_relationships": [
                    {
                        "relationship_id": "checkpoint_flow",
                        "kind": "checkpoint_flow",
                        "strength": "weak",
                        "task_ids": ["train", "evaluate"],
                        "producer_task_id": "train",
                        "consumer_task_ids": ["evaluate"],
                        "artifact_ids": ["trained_checkpoint"],
                    }
                ],
                "repro_tasks": [_task("train"), _task("evaluate")],
            },
            {"engineering_facts": []},
        )

        relationship = normalized["execution_relationships"][0]
        self.assertEqual(relationship["strength"], "strong")
        self.assertIn(
            "weak -> 'strong' for a complete producer/consumer artifact flow",
            "\n".join(normalized.get("_meta", {}).get("coercions", [])),
        )

    def test_prompts_define_scientific_not_paper_specific_relationships(self) -> None:
        prompt_dir = Path(__file__).parents[1] / "geng_agent" / "prompts"
        text = "\n".join(
            (prompt_dir / name).read_text(encoding="utf-8")
            for name in ("build_repro_tasks.md", "finalize_repro_tasks.md")
        )

        self.assertIn('"schema_version": "2.0"', text)
        self.assertIn("execution_relationships", text)
        self.assertIn("same_run_outputs|checkpoint_flow|shared_pretraining", text)
        self.assertIn("不得根据特定论文", text)
        self.assertIn("不确定时不得猜成 strong", text)


if __name__ == "__main__":
    unittest.main()
