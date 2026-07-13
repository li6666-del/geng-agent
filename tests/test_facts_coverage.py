from __future__ import annotations

import unittest

from geng_agent.facts_coverage import (
    compute_fact_coverage,
    compute_task_coverage,
    enumerate_paper_anchors,
    facts_referenced_anchors,
    merge_engineering_facts,
)


def _chunk(text: str) -> dict:
    return {"chunk_id": "c1", "text": text, "page": 1, "section": "S"}


def _fact_claim(name: str) -> dict:
    return {
        "type": "figure_claim",
        "name": name,
        "value": {},
        "source": {
            "source_kind": "text",
            "chunk_id": "c1",
            "page": 1,
            "section": "R",
            "quote": name,
            "figure_ref": "",
        },
        "confidence": "high",
        "used_for_reproduction": True,
    }


class EnumerateAnchorsTests(unittest.TestCase):
    def test_finds_figures_tables_and_number_lists(self) -> None:
        anchors = enumerate_paper_anchors(
            [_chunk("Figs. 3 and 4, 5 compare curves; see Table II and Table 1.")]
        )
        self.assertEqual(anchors["figures"], ["3", "4", "5"])
        self.assertEqual(anchors["tables"], ["1", "II"])

    def test_subfigures_remain_distinct(self) -> None:
        anchors = enumerate_paper_anchors([_chunk("Fig. 7a and Fig. 7b show two regimes.")])
        self.assertEqual(anchors["figures"], ["7:a", "7:b"])

    def test_false_positive_tokens_are_ignored(self) -> None:
        anchors = enumerate_paper_anchors(
            [_chunk("Published Fig. 2020. The configuration and Table are described.")]
        )
        self.assertEqual(anchors, {"figures": [], "tables": []})


class CoverageTests(unittest.TestCase):
    def test_fact_coverage_reports_only_uncovered_anchors(self) -> None:
        chunks = [_chunk("Results in Fig. 3 and Fig. 7; parameters in Table I.")]
        facts = [
            {
                "name": "Fig.3 curve",
                "type": "figure_claim",
                "value": {},
                "source": {"figure_ref": "", "quote": "", "section": ""},
            }
        ]
        coverage = compute_fact_coverage(chunks, facts)
        self.assertEqual(coverage["uncovered_figures"], ["7"])
        self.assertEqual(coverage["uncovered_tables"], ["I"])

    def test_bare_number_does_not_cover_a_figure(self) -> None:
        references = facts_referenced_anchors(
            [
                {
                    "name": "threshold",
                    "type": "simulation_parameter",
                    "value": {"value": 7},
                    "source": {"figure_ref": "", "quote": "", "section": ""},
                }
            ]
        )
        self.assertEqual(references["figures"], set())

    def test_task_coverage_excludes_diagrams(self) -> None:
        facts = {
            "engineering_facts": [
                _fact_claim("Figure 4: Empirical CDF of UPA ZF sum rate"),
                _fact_claim("Figure 2: STAB system model with L=3"),
                _fact_claim("Figure 7: Average sum rate vs transmit power"),
            ]
        }
        tasks = {
            "repro_tasks": [
                {"task_id": "reproduce_fig_4", "figure_or_claim": "Fig. 4", "target": "CDF"}
            ]
        }
        coverage = compute_task_coverage(facts, tasks)
        self.assertEqual(coverage["experiment_figures"], ["4", "7"])
        self.assertEqual(coverage["uncovered_figures"], ["7"])


class MergeTests(unittest.TestCase):
    def _facts(self, items: list[dict]) -> dict:
        return {
            "paper_domain": "communication",
            "paper_repro_type": "other",
            "engineering_facts": items,
            "missing_information": [],
        }

    def test_fact_merge_is_semantic_and_idempotent(self) -> None:
        base = self._facts([{"type": "simulation_parameter", "name": "SNR range"}])
        addition = self._facts(
            [
                {"type": "simulation_parameter", "name": "snr  range"},
                {"type": "metric", "name": "BER"},
            ]
        )
        merged, first_delta = merge_engineering_facts(base, addition)
        merged_again, second_delta = merge_engineering_facts(merged, addition)
        self.assertEqual(first_delta, 1)
        self.assertEqual(second_delta, 0)
        self.assertEqual(len(merged_again["engineering_facts"]), 2)

if __name__ == "__main__":
    unittest.main()
