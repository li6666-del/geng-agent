from __future__ import annotations

import copy
import unittest

from geng_agent.targeted_backfill_loop import run_targeted_backfill_loop


def _facts() -> dict:
    return {
        "paper_domain": "communication",
        "paper_repro_type": "signal_chain",
        "engineering_facts": [],
        "missing_information": [],
    }


def _request() -> dict:
    return {
        "request_id": "task_request",
        "type": "simulation_parameter",
        "name": "Fig. 4 simulation setup",
        "why_needed": "controls the implementation",
        "impact": "high",
        "search_targets": ["Fig. 4"],
        "required_fields": [
            {
                "field_id": "normalization",
                "description": "power normalization",
                "affects": ["formula_chain"],
            }
        ],
    }


def _tasks() -> dict:
    return {
        "repro_tasks": [
            {
                "task_id": "task_1",
                "missing_fact_requests": [_request()],
                "required_facts": [],
                "assumptions": [],
            }
        ]
    }


class TargetedBackfillLoopTests(unittest.TestCase):
    def test_new_dependency_causes_second_round_then_terminal_gap_stops(self) -> None:
        rounds: list[int] = []

        def run_backfill(round_index, requests, facts, tasks, ledger):
            rounds.append(round_index)
            request = requests[0]
            field = request["required_fields"][0]
            if round_index == 1:
                fact = {
                    "type": "simulation_parameter",
                    "name": "Fig. 4 simulation setup",
                    "value": {"normalization": "total power"},
                    "evidence_kind": "paper_explicit",
                }
                status = "resolved_explicit"
                refs = [{"type": fact["type"], "name": fact["name"]}]
            else:
                fact = None
                status = "not_found_in_paper"
                refs = []
            return {
                **_facts(),
                "engineering_facts": [fact] if fact else [],
                "request_resolutions": [
                    {
                        "request_id": request["request_id"],
                        "field_results": [
                            {
                                "field_id": field["field_id"],
                                "status": status,
                                "fact_refs": refs,
                                "searched_locations": ["Fig. 4", "Simulation Setup"],
                                "note": "round result",
                            }
                        ],
                    }
                ],
            }

        def refresh_tasks(round_index, tasks, facts, resolution, ledger):
            candidate = copy.deepcopy(tasks)
            request = candidate["repro_tasks"][0]["missing_fact_requests"][0]
            if round_index == 1:
                request["required_fields"].append(
                    {
                        "field_id": "trial_count",
                        "description": "Monte Carlo trial count",
                        "affects": ["statistical_protocol"],
                    }
                )
            else:
                candidate["repro_tasks"][0]["assumptions"].append(
                    {
                        "name": "trial count",
                        "default_value": 1000,
                        "reason": "not disclosed",
                        "risk": "medium",
                        "request_id": resolution["terminal_unresolved"][0]["request_id"],
                        "field_ids": ["trial_count"],
                        "sensitivity_check": "repeat with 500 and 2000 trials",
                    }
                )
            return candidate

        result = run_targeted_backfill_loop(
            initial_facts=_facts(),
            preliminary_tasks=_tasks(),
            run_backfill=run_backfill,
            refresh_tasks=refresh_tasks,
            normalize_tasks=lambda tasks, facts: tasks,
            max_rounds=6,
        )

        self.assertEqual(rounds, [1, 2])
        self.assertEqual(result["round_count"], 2)
        self.assertEqual(result["stop_reason"], "all_requests_terminal")
        self.assertEqual(result["resolution"]["terminal_unresolved_count"], 1)
        self.assertEqual(result["round_summaries"][0]["delta"]["new_request_field_count"], 1)
        self.assertEqual(result["round_summaries"][1]["delta"]["material_delta"], 0)
        self.assertEqual(
            result["tasks"]["repro_tasks"][0]["assumptions"][0]["field_ids"],
            ["trial_count"],
        )

    def test_max_rounds_preserves_newly_exposed_open_field(self) -> None:
        def run_backfill(round_index, requests, facts, tasks, ledger):
            request = requests[0]
            field = request["required_fields"][0]
            fact_name = f"round {round_index} fact"
            return {
                **_facts(),
                "engineering_facts": [
                    {
                        "type": "simulation_parameter",
                        "name": fact_name,
                        "value": {},
                        "evidence_kind": "paper_explicit",
                    }
                ],
                "request_resolutions": [
                    {
                        "request_id": request["request_id"],
                        "field_results": [
                            {
                                "field_id": field["field_id"],
                                "status": "resolved_explicit",
                                "fact_refs": [
                                    {"type": "simulation_parameter", "name": fact_name}
                                ],
                                "searched_locations": ["paper"],
                                "note": "found",
                            }
                        ],
                    }
                ],
            }

        def refresh_tasks(round_index, tasks, facts, resolution, ledger):
            candidate = copy.deepcopy(tasks)
            request = candidate["repro_tasks"][0]["missing_fact_requests"][0]
            request["required_fields"].append(
                {
                    "field_id": f"dependency_{round_index}",
                    "description": "new dependency",
                    "affects": ["parameter_matrix"],
                }
            )
            return candidate

        result = run_targeted_backfill_loop(
            initial_facts=_facts(),
            preliminary_tasks=_tasks(),
            run_backfill=run_backfill,
            refresh_tasks=refresh_tasks,
            normalize_tasks=lambda tasks, facts: tasks,
            max_rounds=2,
        )

        self.assertEqual(result["round_count"], 2)
        self.assertEqual(result["stop_reason"], "max_rounds_reached")
        self.assertEqual(result["resolution"]["open_count"], 1)


if __name__ == "__main__":
    unittest.main()
