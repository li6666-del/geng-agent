from __future__ import annotations

import unittest
import copy
from pathlib import Path
from tempfile import TemporaryDirectory

from geng_agent.schemas import validate_stage
from geng_agent.stage_cleanup import _clear_stage_outputs
from geng_agent.task_evidence_backfill import (
    backfill_normalization_issues,
    collect_missing_fact_requests,
    cumulative_resolution_from_ledger,
    filter_actionable_requests,
    finalize_targeted_backfill,
    reconcile_final_tasks,
    summarize_backfill_resolution,
    update_search_ledger,
    validate_terminal_gap_assumptions,
    validate_targeted_backfill,
)
from geng_agent.tasks_normalize import finalize_repro_tasks


def _task(task_id: str, requests: list[dict]) -> dict:
    return {
        "task_id": task_id,
        "target": "Reproduce Fig. 4",
        "metric": "throughput",
        "metric_formula": "sum rate",
        "figure_or_claim": "Fig. 4",
        "expected_artifacts": ["results.csv", "plot.png", "summary.json"],
        "output_columns": ["snr_db", "rate"],
        "expected_trend": {
            "x_axis": "snr_db",
            "y_axis": "rate",
            "direction": "increasing",
            "reason": "higher SNR",
        },
        "comparison": {"baselines": [], "curve_groups": [], "tolerance": "qualitative"},
        "required_facts": [],
        "missing_fact_requests": requests,
        "assumptions": [],
        "risk_if_unreproducible": "claim cannot be checked",
    }


def _request(request_id: str, *, impact: str = "high") -> dict:
    return {
        "request_id": request_id,
        "type": "simulation_parameter",
        "name": "Fig. 4 transmit-power normalization",
        "why_needed": "sets the x axis",
        "impact": impact,
        "search_targets": ["Fig. 4 caption"],
    }


class TaskEvidenceBackfillTests(unittest.TestCase):
    def test_requests_are_deduplicated_across_tasks(self) -> None:
        tasks = {"repro_tasks": [_task("task_a", [_request("a")]), _task("task_b", [_request("b")])]}

        worklist = collect_missing_fact_requests(tasks)

        self.assertEqual(len(worklist), 1)
        self.assertEqual(worklist[0]["task_ids"], ["task_a", "task_b"])
        self.assertEqual(worklist[0]["source_request_ids"], ["a", "b"])

    def test_low_impact_request_does_not_trigger_backfill(self) -> None:
        tasks = {"repro_tasks": [_task("task_a", [_request("a", impact="low")])]}
        self.assertEqual(collect_missing_fact_requests(tasks, minimum_impact="medium"), [])

    def test_default_worklist_keeps_every_material_request(self) -> None:
        tasks = {"repro_tasks": [_task("task_a", [_request("a", impact="low")])]}
        self.assertEqual(len(collect_missing_fact_requests(tasks)), 1)

    def test_shared_request_uses_highest_impact_and_keeps_all_tasks(self) -> None:
        tasks = {
            "repro_tasks": [
                _task("task_a", [_request("a", impact="low")]),
                _task("task_b", [_request("b", impact="high")]),
            ]
        }
        worklist = collect_missing_fact_requests(tasks)
        self.assertEqual(worklist[0]["impact"], "high")
        self.assertEqual(worklist[0]["task_ids"], ["task_a", "task_b"])

    def test_unicode_request_names_remain_distinct(self) -> None:
        first = _request("a")
        first["name"] = "图四发射功率归一化"
        second = _request("b")
        second["name"] = "图四蒙特卡洛次数"
        tasks = {"repro_tasks": [_task("task_a", [first, second])]}

        worklist = collect_missing_fact_requests(tasks)

        self.assertEqual(len(worklist), 2)
        self.assertEqual(len({item["request_id"] for item in worklist}), 2)

    def test_fact_name_alone_does_not_resolve_a_request(self) -> None:
        requests = collect_missing_fact_requests(
            {"repro_tasks": [_task("task_a", [_request("a")])]}
        )
        facts = {
            "engineering_facts": [
                {
                    "type": "simulation_parameter",
                    "name": "Fig. 4 transmit-power normalization",
                }
            ]
        }

        summary = summarize_backfill_resolution(requests, facts)

        self.assertEqual(summary["resolved_count"], 0)
        self.assertEqual(summary["open_count"], 1)

    def test_field_level_evidence_resolves_a_request(self) -> None:
        requests = collect_missing_fact_requests(
            {"repro_tasks": [_task("task_a", [_request("a")])]}
        )
        facts = {
            "engineering_facts": [
                {
                    "type": "simulation_parameter",
                    "name": "Fig. 4 transmit-power normalization",
                    "evidence_kind": "paper_explicit",
                }
            ]
        }
        backfill = {
            "request_resolutions": [
                {
                    "request_id": requests[0]["request_id"],
                    "field_results": [
                        {
                            "field_id": "answer",
                            "status": "resolved_explicit",
                            "fact_refs": [
                                {
                                    "type": "simulation_parameter",
                                    "name": "Fig. 4 transmit-power normalization",
                                }
                            ],
                            "searched_locations": ["Fig. 4 caption"],
                            "note": "explicitly stated",
                        }
                    ],
                }
            ]
        }

        summary = summarize_backfill_resolution(requests, facts, backfill)

        self.assertEqual(summary["resolved_count"], 1)
        self.assertEqual(summary["unresolved_count"], 0)

    def test_not_found_is_terminal_but_not_resolved(self) -> None:
        requests = collect_missing_fact_requests(
            {"repro_tasks": [_task("task_a", [_request("a")])]}
        )
        backfill = {
            "request_resolutions": [
                {
                    "request_id": requests[0]["request_id"],
                    "field_results": [
                        {
                            "field_id": "answer",
                            "status": "not_found_in_paper",
                            "fact_refs": [],
                            "searched_locations": ["Fig. 4 caption"],
                            "note": "not disclosed",
                        }
                    ],
                }
            ]
        }
        summary = summarize_backfill_resolution(requests, {"engineering_facts": []}, backfill)
        ledger = update_search_ledger(
            None, round_index=1, requests=requests, resolution=summary
        )

        self.assertEqual(summary["terminal_unresolved_count"], 1)
        self.assertEqual(filter_actionable_requests(requests, ledger), [])
        cumulative = cumulative_resolution_from_ledger(
            requests, {"engineering_facts": []}, ledger
        )
        self.assertEqual(cumulative["terminal_unresolved_count"], 1)

    def test_selected_unresolved_field_can_be_searched_twice(self) -> None:
        requests = collect_missing_fact_requests(
            {"repro_tasks": [_task("task_a", [_request("a")])]}
        )
        backfill = {
            "request_resolutions": [
                {
                    "request_id": requests[0]["request_id"],
                    "field_results": [
                        {
                            "field_id": "answer",
                            "status": "not_found_in_paper",
                            "fact_refs": [],
                            "searched_locations": ["Fig. 4"],
                            "note": "not disclosed",
                        }
                    ],
                }
            ]
        }
        first = summarize_backfill_resolution(
            requests, {"engineering_facts": []}, backfill
        )
        ledger = update_search_ledger(
            None, round_index=1, requests=requests, resolution=first
        )

        self.assertEqual(filter_actionable_requests(requests, ledger), [])
        self.assertEqual(
            len(
                filter_actionable_requests(
                    requests, ledger, max_unresolved_attempts=2
                )
            ),
            1,
        )

        second = summarize_backfill_resolution(
            requests, {"engineering_facts": []}, backfill
        )
        ledger = update_search_ledger(
            ledger, round_index=2, requests=requests, resolution=second
        )
        self.assertEqual(
            filter_actionable_requests(
                requests, ledger, max_unresolved_attempts=2
            ),
            [],
        )

    def test_open_field_is_also_limited_to_two_searches(self) -> None:
        requests = collect_missing_fact_requests(
            {"repro_tasks": [_task("task_a", [_request("a")])]}
        )
        open_resolution = summarize_backfill_resolution(
            requests,
            {"engineering_facts": []},
            {"request_resolutions": []},
        )
        ledger = update_search_ledger(
            None, round_index=1, requests=requests, resolution=open_resolution
        )
        self.assertEqual(
            len(
                filter_actionable_requests(
                    requests, ledger, max_unresolved_attempts=2
                )
            ),
            1,
        )
        ledger = update_search_ledger(
            ledger, round_index=2, requests=requests, resolution=open_resolution
        )
        self.assertEqual(
            filter_actionable_requests(
                requests, ledger, max_unresolved_attempts=2
            ),
            [],
        )

    def test_partial_backfill_keeps_valid_fields_and_softens_science_issue(self) -> None:
        requests = collect_missing_fact_requests(
            {"repro_tasks": [_task("task_a", [_request("a")])]}
        )
        fact = {
            "type": "other",
            "name": "Fig. 4 visible layout",
            "value": {"mapping": "caption and axes"},
            "source": {
                "source_kind": "figure",
                "chunk_id": None,
                "page": 1,
                "section": "",
                "quote": "visible layout",
                "figure_ref": "Fig. 4",
            },
            "confidence": "medium",
            "used_for_reproduction": True,
            "evidence_kind": "visual_estimate",
            "derivation": None,
        }
        raw = {
            "paper_domain": "communication",
            "paper_repro_type": "signal_chain",
            "engineering_facts": [fact],
            "missing_information": [],
            "request_resolutions": [
                {
                    "request_id": requests[0]["request_id"],
                    "field_results": [
                        {
                            "field_id": "answer",
                            "status": "resolved_explicit",
                            "fact_refs": [
                                {"type": fact["type"], "name": fact["name"]}
                            ],
                            "searched_locations": ["Fig. 4"],
                            "note": "caption mapping is explicit",
                        },
                        {
                            "field_id": "invented_field",
                            "status": "resolved_explicit",
                            "fact_refs": [],
                            "searched_locations": [],
                            "note": "invalid field",
                        },
                    ],
                },
                {
                    "request_id": "invented_request",
                    "field_results": [
                        {
                            "field_id": "answer",
                            "status": "resolved_explicit",
                            "fact_refs": [],
                            "searched_locations": [],
                            "note": "invalid request",
                        }
                    ],
                },
            ],
        }

        normalized = finalize_targeted_backfill(
            raw,
            requests,
            {"engineering_facts": []},
            valid_chunk_ids=set(),
            valid_pages={1},
        )

        self.assertEqual(
            len(normalized["request_resolutions"][0]["field_results"]), 1
        )
        self.assertEqual(
            len(backfill_normalization_issues(normalized)), 2
        )
        self.assertEqual(
            validate_stage("targeted_fact_backfill", normalized), []
        )
        science_warnings = validate_targeted_backfill(
            normalized, requests, {"engineering_facts": []}
        )
        self.assertEqual(len(science_warnings), 1)
        self.assertIn("evidence kind", science_warnings[0].message)
    def test_task_normalization_preserves_structured_requests(self) -> None:
        document = {"repro_tasks": [_task("task_a", [_request("a")])]}
        finalized = finalize_repro_tasks(document, {"engineering_facts": []})
        self.assertEqual(
            finalized["repro_tasks"][0]["missing_fact_requests"][0]["request_id"], "a"
        )

    def test_final_reconciliation_preserves_unresolved_requests_and_task_set(self) -> None:
        draft = {"repro_tasks": [_task("task_a", [_request("a")])]}
        candidate = {
            "repro_tasks": [
                {**_task("task_a", []), "target": "refined target"},
                {"task_id": "invented_task"},
            ]
        }
        resolution = {
            "resolved": [],
            "unresolved": collect_missing_fact_requests(draft),
        }

        reconciled = reconcile_final_tasks(draft, candidate, resolution)

        self.assertEqual([item["task_id"] for item in reconciled["repro_tasks"]], ["task_a"])
        self.assertEqual(reconciled["repro_tasks"][0]["target"], "refined target")
        self.assertEqual(len(reconciled["repro_tasks"][0]["missing_fact_requests"]), 1)
        self.assertEqual(
            reconciled["_meta"]["task_set_reconciliation"]["discarded_candidate_task_ids"],
            ["invented_task"],
        )

    def test_final_reconciliation_accepts_new_task_with_material_paper_anchor(self) -> None:
        draft = {"repro_tasks": [_task("task_a", [])]}
        new_task = _task("new_fig_task", [])
        new_task["figure_or_claim"] = "Fig. 8"
        new_task["target"] = "Reproduce the additional Fig. 8 rate comparison"
        candidate = {
            "repro_tasks": [copy.deepcopy(draft["repro_tasks"][0]), new_task]
        }

        reconciled = reconcile_final_tasks(
            draft,
            candidate,
            {"resolved": [], "unresolved": []},
        )

        self.assertEqual(
            [item["task_id"] for item in reconciled["repro_tasks"]],
            ["task_a", "new_fig_task"],
        )
        metadata = reconciled["_meta"]["task_set_reconciliation"]
        self.assertEqual(metadata["added_candidate_task_ids"], ["new_fig_task"])
        self.assertEqual(metadata["discarded_candidate_task_ids"], [])

    def test_reconciliation_preserves_one_acceptance_snapshot_when_refresh_omits_it(self) -> None:
        contract = {
            "contract_version": "1.0",
            "core_conclusions": [
                {
                    "claim_id": "fig4_rate_trend",
                    "statement": "Rate increases with SNR.",
                    "kind": "trend",
                    "regime": "paper regime",
                    "paper_anchor": "Fig. 4",
                }
            ],
            "key_numeric_targets": [],
            "information_gaps": [],
        }
        draft_task = {**_task("task_a", []), "scientific_acceptance": contract}
        candidate_task = {**_task("task_a", []), "target": "refined target"}

        reconciled = reconcile_final_tasks(
            {"repro_tasks": [draft_task]},
            {"repro_tasks": [candidate_task]},
            {"resolved": [], "unresolved": []},
        )

        self.assertEqual(
            reconciled["repro_tasks"][0]["scientific_acceptance"], contract
        )
    def test_resolved_request_becomes_required_fact_and_is_removed(self) -> None:
        draft = {"repro_tasks": [_task("task_a", [_request("a")])]}
        requests = collect_missing_fact_requests(draft)
        facts = {
            "engineering_facts": [
                {
                    "type": "simulation_parameter",
                    "name": "Fig. 4 transmit-power normalization",
                }
            ]
        }
        backfill = {
            "request_resolutions": [
                {
                    "request_id": requests[0]["request_id"],
                    "field_results": [
                        {
                            "field_id": "answer",
                            "status": "resolved_explicit",
                            "fact_refs": [
                                {
                                    "type": "simulation_parameter",
                                    "name": "Fig. 4 transmit-power normalization",
                                }
                            ],
                            "searched_locations": ["Fig. 4 caption"],
                            "note": "explicitly stated",
                        }
                    ],
                }
            ]
        }
        resolution = summarize_backfill_resolution(requests, facts, backfill)

        reconciled = reconcile_final_tasks(draft, draft, resolution)

        task = reconciled["repro_tasks"][0]
        self.assertEqual(task["missing_fact_requests"], [])
        self.assertEqual(
            task["required_facts"],
            [
                {
                    "type": "simulation_parameter",
                    "name": "Fig. 4 transmit-power normalization",
                }
            ],
        )

    def test_validator_rejects_resolved_field_without_fact_ref(self) -> None:
        requests = collect_missing_fact_requests(
            {"repro_tasks": [_task("task_a", [_request("a")])]}
        )
        document = {
            "engineering_facts": [],
            "request_resolutions": [
                {
                    "request_id": requests[0]["request_id"],
                    "field_results": [
                        {
                            "field_id": "answer",
                            "status": "resolved_explicit",
                            "fact_refs": [],
                            "searched_locations": ["Fig. 4"],
                            "note": "claimed resolved",
                        }
                    ],
                }
            ],
        }

        issues = validate_targeted_backfill(
            document, requests, {"engineering_facts": []}
        )

        self.assertTrue(any("requires evidence" in issue.message for issue in issues))

    def test_validator_rejects_derived_fact_without_derivation_chain(self) -> None:
        requests = collect_missing_fact_requests(
            {"repro_tasks": [_task("task_a", [_request("a")])]}
        )
        fact = {
            "type": "simulation_parameter",
            "name": "Fig. 4 transmit-power normalization",
            "evidence_kind": "paper_derived",
        }
        document = {
            "engineering_facts": [fact],
            "request_resolutions": [
                {
                    "request_id": requests[0]["request_id"],
                    "field_results": [
                        {
                            "field_id": "answer",
                            "status": "resolved_derived",
                            "fact_refs": [
                                {"type": fact["type"], "name": fact["name"]}
                            ],
                            "searched_locations": ["Equation 12"],
                            "note": "derived from the stated normalization",
                        }
                    ],
                }
            ],
        }

        issues = validate_targeted_backfill(
            document, requests, {"engineering_facts": []}
        )

        self.assertTrue(any("derivation chain" in issue.message for issue in issues))

    def test_terminal_gap_requires_linked_sensitivity_assumption(self) -> None:
        request = collect_missing_fact_requests(
            {"repro_tasks": [_task("task_a", [_request("a")])]}
        )[0]
        resolution = {
            "terminal_unresolved": [
                {
                    **request,
                    "field_results": [
                        {"field_id": "answer", "status": "not_found_in_paper"}
                    ],
                }
            ]
        }
        tasks = {"repro_tasks": [_task("task_a", [_request("a")])]}

        issues = validate_terminal_gap_assumptions(tasks, resolution)
        self.assertTrue(issues)

        tasks["repro_tasks"][0]["assumptions"] = [
            {
                "request_id": request["request_id"],
                "field_ids": ["answer"],
                "sensitivity_check": "run 500 and 2000 trials",
            }
        ]
        self.assertEqual(validate_terminal_gap_assumptions(tasks, resolution), [])

    def test_task_restart_preserves_global_facts_and_clears_derived_evidence(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for name in (
                "engineering_facts_initial.json",
                "engineering_facts_backfill.json",
                "engineering_facts.json",
                "repro_tasks_preliminary.json",
                "repro_tasks.json",
            ):
                (root / name).write_text("{}", encoding="utf-8")

            _clear_stage_outputs(root, "tasks", preserve_audit=True)

            self.assertTrue((root / "engineering_facts_initial.json").exists())
            for name in (
                "engineering_facts_backfill.json",
                "engineering_facts.json",
                "repro_tasks_preliminary.json",
                "repro_tasks.json",
            ):
                self.assertFalse((root / name).exists(), name)


if __name__ == "__main__":
    unittest.main()
