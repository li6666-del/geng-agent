from __future__ import annotations

import copy
import unittest

from geng_agent.semantic_merge import semantic_merge_repro_tasks
from geng_agent.targeted_backfill_loop import run_targeted_backfill_loop
from geng_agent.task_evidence_backfill import reconcile_final_tasks


def _relationship(
    relationship_id: str,
    *,
    strength: str = "weak",
    task_ids: list[str] | None = None,
    rationale: str = "shared scientific definition",
) -> dict:
    return {
        "relationship_id": relationship_id,
        "kind": "shared_definition",
        "strength": strength,
        "task_ids": task_ids or ["task_1", "task_2"],
        "producer_task_id": None,
        "consumer_task_ids": [],
        "artifact_ids": [],
        "rationale": rationale,
    }


def _handoff(*, ready: bool, inferred: bool = False) -> dict:
    return {
        "ready_for_writer": ready,
        "blocking_request_ids": [] if ready else ["task_request"],
        "reason": "ready" if ready else "one material field remains open",
        "inferred": inferred,
    }


def _task(task_id: str, *, with_request: bool = False) -> dict:
    request = {
        "request_id": "task_request",
        "type": "simulation_parameter",
        "name": "shared evaluation protocol",
        "why_needed": "controls the scientific comparison",
        "impact": "high",
        "search_targets": ["evaluation protocol"],
        "required_fields": [
            {
                "field_id": "partition_rule",
                "description": "dataset partition rule",
                "affects": ["execution_relationships"],
            }
        ],
    }
    return {
        "task_id": task_id,
        "target": f"target for {task_id}",
        "figure_or_claim": f"claim for {task_id}",
        "required_facts": [],
        "missing_fact_requests": [request] if with_request else [],
        "assumptions": [],
    }


def _document(*, ready: bool, relationships: list[dict]) -> dict:
    return {
        "schema_version": "2.0",
        "backfill_handoff": _handoff(ready=ready),
        "execution_relationships": relationships,
        "repro_tasks": [_task("task_1"), _task("task_2")],
    }


class TaskRelationshipMergeTests(unittest.TestCase):
    def test_semantic_merge_refreshes_relationship_by_id_independent_of_task_dedup(self) -> None:
        base = _document(
            ready=False,
            relationships=[_relationship("shared_model", rationale="old rationale")],
        )
        addition = _document(
            ready=True,
            relationships=[
                _relationship(
                    "shared_model",
                    strength="strong",
                    rationale="the exact state must be shared",
                ),
                _relationship(
                    "unknown_task_reference",
                    task_ids=["task_2", "not_compiled_yet"],
                ),
                _relationship("invalid_singleton", task_ids=["task_1"]),
            ],
        )

        merged, _delta = semantic_merge_repro_tasks(base, addition)

        self.assertEqual(len(merged["repro_tasks"]), 2)
        self.assertEqual(
            [item["relationship_id"] for item in merged["execution_relationships"]],
            ["shared_model", "unknown_task_reference"],
        )
        self.assertEqual(merged["execution_relationships"][0]["strength"], "strong")
        self.assertEqual(
            merged["execution_relationships"][1]["task_ids"],
            ["task_2", "not_compiled_yet"],
        )
        self.assertTrue(merged["backfill_handoff"]["ready_for_writer"])
        self.assertNotIn("backfill_handoff", merged.get("_meta", {}))

    def test_reconciliation_preserves_omitted_relationships_and_refreshes_matching_ids(self) -> None:
        preliminary = _document(
            ready=False,
            relationships=[
                _relationship("keep_me"),
                _relationship("refresh_me", rationale="before refresh"),
            ],
        )
        candidate = _document(
            ready=True,
            relationships=[
                _relationship(
                    "refresh_me",
                    strength="strong",
                    rationale="after evidence refresh",
                )
            ],
        )

        reconciled = reconcile_final_tasks(preliminary, candidate, {"resolved": []})

        self.assertEqual(reconciled["schema_version"], "2.0")
        self.assertEqual(
            [item["relationship_id"] for item in reconciled["execution_relationships"]],
            ["keep_me", "refresh_me"],
        )
        self.assertEqual(reconciled["execution_relationships"][1]["strength"], "strong")
        self.assertTrue(reconciled["backfill_handoff"]["ready_for_writer"])
        self.assertNotIn("backfill_handoff", reconciled.get("_meta", {}))

    def test_backfill_refresh_keeps_relationships_and_writes_top_level_handoff(self) -> None:
        initial = _document(
            ready=False,
            relationships=[_relationship("keep_across_refresh")],
        )
        initial["repro_tasks"][0] = _task("task_1", with_request=True)

        def run_backfill(round_index, requests, facts, tasks, ledger):
            del round_index, facts, tasks, ledger
            request = requests[0]
            field = request["required_fields"][0]
            return {
                "paper_domain": "communication",
                "paper_repro_type": "other",
                "engineering_facts": [],
                "missing_information": [],
                "request_resolutions": [
                    {
                        "request_id": request["request_id"],
                        "field_results": [
                            {
                                "field_id": field["field_id"],
                                "status": "not_found_in_paper",
                                "fact_refs": [],
                                "searched_locations": ["evaluation protocol"],
                                "note": "not disclosed",
                            }
                        ],
                    }
                ],
            }

        def refresh_tasks(round_index, tasks, facts, resolution, ledger):
            del round_index, facts, resolution, ledger
            refreshed = copy.deepcopy(tasks)
            refreshed["backfill_handoff"] = _handoff(ready=True)
            refreshed["execution_relationships"] = []
            return refreshed

        result = run_targeted_backfill_loop(
            initial_facts={
                "paper_domain": "communication",
                "paper_repro_type": "other",
                "engineering_facts": [],
                "missing_information": [],
            },
            preliminary_tasks=initial,
            run_backfill=run_backfill,
            refresh_tasks=refresh_tasks,
            normalize_tasks=lambda tasks, facts: tasks,
            max_rounds=2,
        )

        self.assertEqual(result["round_count"], 1)
        self.assertEqual(
            [
                item["relationship_id"]
                for item in result["tasks"]["execution_relationships"]
            ],
            ["keep_across_refresh"],
        )
        self.assertTrue(result["tasks"]["backfill_handoff"]["ready_for_writer"])
        self.assertFalse(result["tasks"]["backfill_handoff"]["inferred"])
        self.assertNotIn("backfill_handoff", result["tasks"].get("_meta", {}))


if __name__ == "__main__":
    unittest.main()
