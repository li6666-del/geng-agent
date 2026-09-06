import unittest

from geng_agent.facts_coverage import compute_task_coverage
from geng_agent.semantic_merge import (
    canonical_figure_ref,
    semantic_merge_engineering_facts,
    semantic_merge_repro_tasks,
)


def fact(name: str, value: dict) -> dict:
    return {"type": "simulation_parameter", "name": name, "value": value, "confidence": "medium"}


class SemanticMergeTests(unittest.TestCase):
    def test_subfigures_are_distinct_canonical_targets(self) -> None:
        self.assertEqual(canonical_figure_ref("Fig. 9(a)"), "fig:9:a")
        self.assertEqual(canonical_figure_ref("Fig. 9(b)"), "fig:9:b")
        self.assertEqual(canonical_figure_ref("Fig. 2026 is not a valid figure reference"), "")

    def test_fact_enrichment_and_conflict_are_idempotent(self) -> None:
        base = {"engineering_facts": [fact("Fig. 3 SNR", {"method": "MAMP"})]}
        addition = {"engineering_facts": [fact("Fig. 3 SNR", {"method": "MAMP", "snr_db": [0, 5]})]}
        merged, delta = semantic_merge_engineering_facts(base, addition)
        self.assertEqual(delta, 1)
        self.assertEqual(merged["engineering_facts"][0]["value"]["snr_db"], [0, 5])
        merged_again, delta_again = semantic_merge_engineering_facts(merged, addition)
        self.assertEqual(delta_again, 0)
        self.assertEqual(len(merged_again["engineering_facts"]), 1)

    def test_conflicting_value_does_not_inherit_candidate_evidence_grade(self) -> None:
        base_fact = fact("Fig. 3 SNR", {"snr_db": [0, 5]})
        base_fact.update(
            confidence="low",
            evidence_kind="visual_estimate",
            derivation="read approximately from the plot",
            source={"chunk_id": None, "page": 3},
        )
        candidate_fact = fact("Fig. 3 SNR", {"snr_db": [0, 10]})
        candidate_fact.update(
            confidence="high",
            evidence_kind="paper_explicit",
            derivation="copied from the caption",
            source={"chunk_id": "text_c7", "page": 3},
        )

        merged, delta = semantic_merge_engineering_facts(
            {"engineering_facts": [base_fact]},
            {"engineering_facts": [candidate_fact]},
        )

        self.assertEqual(delta, 1)
        retained = merged["engineering_facts"][0]
        self.assertEqual(retained["value"]["snr_db"], [0, 5])
        self.assertEqual(retained["confidence"], "low")
        self.assertEqual(retained["evidence_kind"], "visual_estimate")
        self.assertEqual(retained["derivation"], "read approximately from the plot")
        conflict = merged["_meta"]["semantic_merge"]["fact_conflicts"][0]
        self.assertEqual(conflict["candidate_source"], candidate_fact["source"])

    def test_same_task_id_different_target_records_conflict_without_duplicate(self) -> None:
        base = {"repro_tasks": [{"task_id": "fig9", "figure_or_claim": "Fig. 9(a)"}]}
        addition = {"repro_tasks": [{"task_id": "fig9", "figure_or_claim": "Fig. 9(b)"}]}
        merged, delta = semantic_merge_repro_tasks(base, addition)
        self.assertEqual(len(merged["repro_tasks"]), 1)
        self.assertEqual(delta, 1)
        self.assertEqual(len(merged["_meta"]["semantic_merge"]["task_conflicts"]), 1)

    def test_task_merge_preserves_soft_handoff_metadata(self) -> None:
        addition = {
            "repro_tasks": [
                {"task_id": "fig4", "figure_or_claim": "Fig. 4"}
            ],
            "backfill_handoff": {
                "ready_for_writer": False,
                "blocking_request_ids": ["fig4_setup"],
                "reason": "setup changes the experiment",
                "inferred": False,
            },
        }

        merged, _ = semantic_merge_repro_tasks(
            {"repro_tasks": []}, addition
        )

        self.assertEqual(
            merged["backfill_handoff"],
            addition["backfill_handoff"],
        )
        self.assertNotIn("backfill_handoff", merged.get("_meta", {}))

    def test_merge_does_not_copy_untrusted_unrelated_metadata(self) -> None:
        facts, _ = semantic_merge_engineering_facts(
            {"engineering_facts": []},
            {"engineering_facts": [], "_meta": {"untrusted": True}},
        )
        tasks, _ = semantic_merge_repro_tasks(
            {"repro_tasks": []},
            {"repro_tasks": [], "_meta": {"untrusted": True}},
        )

        self.assertNotIn("untrusted", facts.get("_meta", {}))
        self.assertNotIn("untrusted", tasks.get("_meta", {}))

    def test_task_merge_replaces_acceptance_as_one_snapshot(self) -> None:
        old_contract = {
            "contract_version": "1.0",
            "core_conclusions": [{"claim_id": "old", "statement": "old"}],
            "key_numeric_targets": [{"target_id": "stale"}],
            "information_gaps": [],
        }
        refined_contract = {
            "contract_version": "1.0",
            "core_conclusions": [{"claim_id": "final", "statement": "final"}],
            "key_numeric_targets": [],
            "information_gaps": [{"gap_id": "known_gap"}],
        }

        merged, delta = semantic_merge_repro_tasks(
            {
                "repro_tasks": [
                    {
                        "task_id": "fig4",
                        "figure_or_claim": "Fig. 4",
                        "scientific_acceptance": old_contract,
                    }
                ]
            },
            {
                "repro_tasks": [
                    {
                        "task_id": "fig4",
                        "figure_or_claim": "Fig. 4",
                        "scientific_acceptance": refined_contract,
                    }
                ]
            },
        )

        self.assertEqual(delta, 1)
        self.assertEqual(
            merged["repro_tasks"][0]["scientific_acceptance"], refined_contract
        )
        self.assertEqual(merged["_meta"]["semantic_merge"]["merge_version"], 4)
    def test_task_coverage_does_not_use_fig9a_to_cover_fig9b(self) -> None:
        facts = {"engineering_facts": [
            {"type": "figure_claim", "name": "Fig. 9(a) BER vs SNR", "value": {}},
            {"type": "figure_claim", "name": "Fig. 9(b) BER vs SNR", "value": {}},
        ]}
        tasks = {"repro_tasks": [{"task_id": "fig9a", "figure_or_claim": "Fig. 9(a)", "target": "BER"}]}
        coverage = compute_task_coverage(facts, tasks)
        self.assertEqual(coverage["uncovered_figures"], ["9:b"])

if __name__ == "__main__":
    unittest.main()
