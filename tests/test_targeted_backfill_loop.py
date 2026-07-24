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


def _tasks(*, with_request: bool = True) -> dict:
    return {
        "repro_tasks": [
            {
                "task_id": "task_1",
                "missing_fact_requests": [_request()] if with_request else [],
                "required_facts": [],
                "assumptions": [],
            }
        ]
    }


def _resolved_result(request: dict, field: dict, fact_name: str) -> dict:
    fact = {
        "type": "simulation_parameter",
        "name": fact_name,
        "value": {field["field_id"]: "paper value"},
        "evidence_kind": "paper_explicit",
    }
    return {
        **_facts(),
        "engineering_facts": [fact],
        "request_resolutions": [
            {
                "request_id": request["request_id"],
                "field_results": [
                    {
                        "field_id": field["field_id"],
                        "status": "resolved_explicit",
                        "fact_refs": [{"type": fact["type"], "name": fact["name"]}],
                        "searched_locations": ["Fig. 4"],
                        "note": "found",
                    }
                ],
            }
        ],
    }


def _not_found_result(request: dict, field: dict) -> dict:
    return {
        **_facts(),
        "request_resolutions": [
            {
                "request_id": request["request_id"],
                "field_results": [
                    {
                        "field_id": field["field_id"],
                        "status": "not_found_in_paper",
                        "fact_refs": [],
                        "searched_locations": ["Fig. 4", "Simulation Setup"],
                        "note": "paper does not disclose the value",
                    }
                ],
            }
        ],
    }


def _set_handoff(
    document: dict,
    *,
    ready: bool,
    blocking_request_ids: list[str] | None = None,
    reason: str = "",
) -> None:
    document.setdefault("_meta", {})["backfill_handoff"] = {
        "ready_for_writer": ready,
        "blocking_request_ids": blocking_request_ids or [],
        "reason": reason,
    }


class TargetedBackfillLoopTests(unittest.TestCase):
    def test_no_initial_requests_skips_backfill(self) -> None:
        result = run_targeted_backfill_loop(
            initial_facts=_facts(),
            preliminary_tasks=_tasks(with_request=False),
            run_backfill=lambda *args: self.fail("backfill should not run"),
            refresh_tasks=lambda *args: self.fail("tasks should not refresh"),
            normalize_tasks=lambda tasks, facts: tasks,
        )

        self.assertEqual(result["round_count"], 0)
        self.assertEqual(result["stop_reason"], "no_initial_requests")

    def test_missing_preliminary_handoff_defaults_directly_to_writer(self) -> None:
        result = run_targeted_backfill_loop(
            initial_facts=_facts(),
            preliminary_tasks=_tasks(),
            run_backfill=lambda *args: self.fail("backfill should not run"),
            refresh_tasks=lambda *args: self.fail("tasks should not refresh"),
            normalize_tasks=lambda tasks, facts: tasks,
        )

        self.assertEqual(result["round_count"], 0)
        self.assertEqual(
            result["stop_reason"], "preliminary_task_handoff_default"
        )
        self.assertEqual(result["resolution"]["open_count"], 1)

    def test_task_expert_selected_blocker_causes_second_round(self) -> None:
        rounds: list[int] = []
        initial = _tasks()
        _set_handoff(
            initial,
            ready=False,
            blocking_request_ids=["task_request"],
            reason="normalization changes the implementation",
        )

        def run_backfill(round_index, requests, facts, tasks, ledger):
            rounds.append(round_index)
            request = requests[0]
            field = request["required_fields"][0]
            if round_index == 1:
                return _resolved_result(request, field, "Fig. 4 simulation setup")
            return _not_found_result(request, field)

        def refresh_tasks(round_index, tasks, facts, resolution, ledger):
            candidate = copy.deepcopy(tasks)
            if round_index == 1:
                candidate["repro_tasks"][0]["missing_fact_requests"][0][
                    "required_fields"
                ].append(
                    {
                        "field_id": "trial_count",
                        "description": "Monte Carlo trial count",
                        "affects": ["statistical_protocol"],
                    }
                )
                _set_handoff(
                    candidate,
                    ready=False,
                    blocking_request_ids=["task_request"],
                    reason="trial count changes the statistical protocol",
                )
            else:
                _set_handoff(
                    candidate,
                    ready=True,
                    reason="writer can choose and test a trial-count assumption",
                )
            return candidate

        result = run_targeted_backfill_loop(
            initial_facts=_facts(),
            preliminary_tasks=initial,
            run_backfill=run_backfill,
            refresh_tasks=refresh_tasks,
            normalize_tasks=lambda tasks, facts: tasks,
            max_rounds=3,
        )

        self.assertEqual(rounds, [1, 2])
        self.assertEqual(result["round_count"], 2)
        self.assertEqual(result["stop_reason"], "task_expert_handoff_ready")
        self.assertEqual(result["resolution"]["terminal_unresolved_count"], 1)
        self.assertEqual(
            result["round_summaries"][0]["handoff"][
                "resolved_blocking_request_ids"
            ],
            [result["known_requests"][0]["request_id"]],
        )

    def test_missing_refreshed_handoff_defaults_to_writer(self) -> None:
        initial = _tasks()
        _set_handoff(
            initial,
            ready=False,
            blocking_request_ids=["task_request"],
            reason="initially blocking",
        )

        def run_backfill(round_index, requests, facts, tasks, ledger):
            request = requests[0]
            return _not_found_result(request, request["required_fields"][0])

        result = run_targeted_backfill_loop(
            initial_facts=_facts(),
            preliminary_tasks=initial,
            run_backfill=run_backfill,
            refresh_tasks=lambda *args: _tasks(),
            normalize_tasks=lambda tasks, facts: tasks,
            max_rounds=3,
        )

        self.assertEqual(result["round_count"], 1)
        self.assertEqual(result["stop_reason"], "task_expert_handoff_default")
        self.assertTrue(result["final_handoff"]["ready_for_writer"])
        self.assertNotIn(
            "backfill_handoff", result["tasks"].get("_meta", {})
        )

    def test_unknown_preliminary_blocker_stops_without_empty_round(self) -> None:
        initial = _tasks()
        _set_handoff(
            initial,
            ready=False,
            blocking_request_ids=["does_not_exist"],
            reason="malformed advice",
        )
        result = run_targeted_backfill_loop(
            initial_facts=_facts(),
            preliminary_tasks=initial,
            run_backfill=lambda *args: self.fail("backfill should not run"),
            refresh_tasks=lambda *args: self.fail("tasks should not refresh"),
            normalize_tasks=lambda tasks, facts: tasks,
        )

        self.assertEqual(result["round_count"], 0)
        self.assertEqual(result["stop_reason"], "no_selected_blockers")
        self.assertEqual(
            result["final_handoff"]["ignored_blocking_request_ids"],
            ["does_not_exist"],
        )

    def test_same_unresolved_blocker_is_searched_at_most_twice(self) -> None:
        rounds: list[int] = []
        initial = _tasks()
        _set_handoff(
            initial,
            ready=False,
            blocking_request_ids=["task_request"],
            reason="the field changes the experiment",
        )

        def run_backfill(round_index, requests, facts, tasks, ledger):
            rounds.append(round_index)
            request = requests[0]
            return _not_found_result(
                request, request["required_fields"][0]
            )

        def refresh_tasks(round_index, tasks, facts, resolution, ledger):
            candidate = copy.deepcopy(tasks)
            _set_handoff(
                candidate,
                ready=False,
                blocking_request_ids=["task_request"],
                reason="the task expert still considers it blocking",
            )
            return candidate

        result = run_targeted_backfill_loop(
            initial_facts=_facts(),
            preliminary_tasks=initial,
            run_backfill=run_backfill,
            refresh_tasks=refresh_tasks,
            normalize_tasks=lambda tasks, facts: tasks,
            max_rounds=3,
        )

        self.assertEqual(rounds, [1, 2])
        self.assertEqual(result["round_count"], 2)
        self.assertEqual(result["stop_reason"], "selected_requests_exhausted")
        self.assertEqual(result["resolution"]["terminal_unresolved_count"], 1)

    def test_backfill_error_degrades_to_writer_with_existing_state(self) -> None:
        initial = _tasks()
        _set_handoff(
            initial,
            ready=False,
            blocking_request_ids=["task_request"],
            reason="worth searching but not worth stopping the workflow",
        )

        def fail_backfill(*args):
            raise RuntimeError("temporary model failure")

        result = run_targeted_backfill_loop(
            initial_facts=_facts(),
            preliminary_tasks=initial,
            run_backfill=fail_backfill,
            refresh_tasks=lambda *args: self.fail("refresh should not run"),
            normalize_tasks=lambda tasks, facts: tasks,
        )

        self.assertEqual(result["stop_reason"], "backfill_error")
        self.assertEqual(result["round_count"], 1)
        self.assertTrue(result["round_summaries"][0]["degraded_to_writer"])
        self.assertEqual(result["tasks"], initial)
        self.assertEqual(result["resolution"]["open_count"], 1)

    def test_refresh_error_keeps_new_facts_and_existing_tasks(self) -> None:
        initial = _tasks()
        _set_handoff(
            initial,
            ready=False,
            blocking_request_ids=["task_request"],
            reason="normalization affects implementation",
        )

        def run_backfill(round_index, requests, facts, tasks, ledger):
            request = requests[0]
            return _resolved_result(
                request,
                request["required_fields"][0],
                "recovered normalization",
            )

        def fail_refresh(*args):
            raise ValueError("malformed refresh response")

        result = run_targeted_backfill_loop(
            initial_facts=_facts(),
            preliminary_tasks=initial,
            run_backfill=run_backfill,
            refresh_tasks=fail_refresh,
            normalize_tasks=lambda tasks, facts: tasks,
        )

        self.assertEqual(result["stop_reason"], "task_refresh_error")
        self.assertEqual(result["round_count"], 1)
        self.assertTrue(result["round_summaries"][0]["new_facts_retained"])
        self.assertEqual(result["tasks"], initial)
        self.assertEqual(
            result["facts"]["engineering_facts"][0]["name"],
            "recovered normalization",
        )

    def test_three_round_safety_limit_preserves_new_open_field(self) -> None:
        initial = _tasks()
        _set_handoff(
            initial,
            ready=False,
            blocking_request_ids=["task_request"],
            reason="each discovered dependency changes implementation",
        )

        def run_backfill(round_index, requests, facts, tasks, ledger):
            request = requests[0]
            field = request["required_fields"][0]
            return _resolved_result(request, field, f"round {round_index} fact")

        def refresh_tasks(round_index, tasks, facts, resolution, ledger):
            candidate = copy.deepcopy(tasks)
            candidate["repro_tasks"][0]["missing_fact_requests"][0][
                "required_fields"
            ].append(
                {
                    "field_id": f"dependency_{round_index}",
                    "description": "new blocking dependency",
                    "affects": ["parameter_matrix"],
                }
            )
            _set_handoff(
                candidate,
                ready=False,
                blocking_request_ids=["task_request"],
                reason="new dependency still changes implementation",
            )
            return candidate

        result = run_targeted_backfill_loop(
            initial_facts=_facts(),
            preliminary_tasks=initial,
            run_backfill=run_backfill,
            refresh_tasks=refresh_tasks,
            normalize_tasks=lambda tasks, facts: tasks,
            max_rounds=3,
        )

        self.assertEqual(result["round_count"], 3)
        self.assertEqual(result["stop_reason"], "safety_limit_reached")
        self.assertEqual(result["resolution"]["open_count"], 1)


if __name__ == "__main__":
    unittest.main()