from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from geng_agent.stage_cleanup import _clear_stage_outputs
from geng_agent.task_evidence_backfill import (
    collect_missing_fact_requests,
    reconcile_final_tasks,
    summarize_backfill_resolution,
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
        self.assertEqual(collect_missing_fact_requests(tasks), [])

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

    def test_resolution_uses_stable_fact_type_and_name(self) -> None:
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

        self.assertEqual(summary["resolved_count"], 1)
        self.assertEqual(summary["unresolved_count"], 0)

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
                _task("invented_task", []),
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
        resolution = summarize_backfill_resolution(requests, facts)

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
