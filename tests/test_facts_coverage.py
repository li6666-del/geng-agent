from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from geng_agent.facts_coverage import (
    compute_fact_coverage,
    enumerate_paper_anchors,
    facts_referenced_anchors,
    merge_engineering_facts,
)
from geng_agent.pipeline import ReviewPipeline


def _chunk(text: str) -> dict:
    return {"chunk_id": "c1", "text": text, "page": 1, "section": "S"}


class EnumerateAnchorsTests(unittest.TestCase):
    def test_finds_figures_and_tables(self) -> None:
        anchors = enumerate_paper_anchors([_chunk("As shown in Fig. 7 and Figure 3, see Table II and Table 1.")])
        self.assertEqual(anchors["figures"], ["3", "7"])
        self.assertEqual(anchors["tables"], ["1", "II"])

    def test_figure_number_list_is_expanded(self) -> None:
        anchors = enumerate_paper_anchors([_chunk("Figs. 3 and 4, 5 compare the curves.")])
        self.assertEqual(anchors["figures"], ["3", "4", "5"])

    def test_subfigure_letter_collapses_to_number(self) -> None:
        anchors = enumerate_paper_anchors([_chunk("Fig. 7a and Fig. 7b show two regimes.")])
        self.assertEqual(anchors["figures"], ["7"])

    def test_year_like_number_is_not_a_figure(self) -> None:
        # "Fig. 2020" must not register figure 20 / 2 (two-digit cap + word boundary).
        anchors = enumerate_paper_anchors([_chunk("Published Fig. 2020 reference.")])
        self.assertEqual(anchors["figures"], [])

    def test_table_keyword_does_not_swallow_words(self) -> None:
        # "Table is shown" must NOT become Table I (roman branch is case-sensitive).
        anchors = enumerate_paper_anchors([_chunk("The Table is shown below and configuration matters.")])
        self.assertEqual(anchors["tables"], [])
        self.assertEqual(anchors["figures"], [])

    def test_configuration_is_not_a_figure(self) -> None:
        anchors = enumerate_paper_anchors([_chunk("The configuration uses 3 antennas.")])
        self.assertEqual(anchors["figures"], [])


class FactsReferencedTests(unittest.TestCase):
    def test_anchor_read_from_multiple_fact_fields(self) -> None:
        facts = [
            {"name": "Fig.7 sum-rate", "type": "figure_claim", "value": {}, "source": {"figure_ref": "", "quote": "", "section": ""}},
            {"name": "axis", "type": "figure_claim", "value": {}, "source": {"figure_ref": "Figure 3 BER vs SNR", "quote": "", "section": ""}},
            {"name": "cell", "type": "metric", "value": {"note": "Table II row 2"}, "source": {"figure_ref": "", "quote": "", "section": ""}},
        ]
        ref = facts_referenced_anchors(facts)
        self.assertEqual(ref["figures"], {"7", "3"})
        self.assertEqual(ref["tables"], {"II"})

    def test_bare_number_does_not_cover_anchor(self) -> None:
        # a value of "7" with no fig/table keyword must not count as covering figure 7
        facts = [{"name": "threshold", "type": "simulation_parameter", "value": {"v": 7}, "source": {"figure_ref": "", "quote": "", "section": ""}}]
        ref = facts_referenced_anchors(facts)
        self.assertEqual(ref["figures"], set())


class CoverageTests(unittest.TestCase):
    def test_uncovered_anchor_is_reported(self) -> None:
        chunks = [_chunk("Results in Fig. 3 and Fig. 7; parameters in Table I.")]
        facts = [{"name": "Fig.3 curve", "type": "figure_claim", "value": {}, "source": {"figure_ref": "", "quote": "", "section": ""}}]
        cov = compute_fact_coverage(chunks, facts)
        self.assertEqual(cov["paper_figures"], ["3", "7"])
        self.assertEqual(cov["covered_figures"], ["3"])
        self.assertEqual(cov["uncovered_figures"], ["7"])
        self.assertEqual(cov["uncovered_tables"], ["I"])
        self.assertFalse(cov["fully_covered"])

    def test_fully_covered(self) -> None:
        chunks = [_chunk("See Fig. 1.")]
        facts = [{"name": "Fig.1 plot", "type": "figure_claim", "value": {}, "source": {"figure_ref": "", "quote": "", "section": ""}}]
        cov = compute_fact_coverage(chunks, facts)
        self.assertTrue(cov["fully_covered"])
        self.assertEqual(cov["uncovered_figures"], [])


class MergeTests(unittest.TestCase):
    def _doc(self, facts, missing=None) -> dict:
        return {"paper_domain": "communication", "paper_repro_type": "other", "engineering_facts": facts, "missing_information": missing or []}

    def test_dedup_by_type_and_name(self) -> None:
        base = self._doc([{"type": "simulation_parameter", "name": "SNR range"}])
        addition = self._doc([
            {"type": "simulation_parameter", "name": "snr  range"},  # same after normalization -> drop
            {"type": "metric", "name": "BER"},                        # new -> keep
        ])
        merged, added = merge_engineering_facts(base, addition)
        self.assertEqual(added, 1)
        self.assertEqual(len(merged["engineering_facts"]), 2)
        names = {f["name"] for f in merged["engineering_facts"]}
        self.assertEqual(names, {"SNR range", "BER"})

    def test_idempotent_second_merge_adds_zero(self) -> None:
        base = self._doc([{"type": "metric", "name": "BER"}])
        addition = self._doc([{"type": "channel_model", "name": "Rayleigh"}])
        merged, added1 = merge_engineering_facts(base, addition)
        merged2, added2 = merge_engineering_facts(merged, addition)
        self.assertEqual(added1, 1)
        self.assertEqual(added2, 0)
        self.assertEqual(len(merged2["engineering_facts"]), 2)

    def test_missing_information_merged_by_name(self) -> None:
        base = self._doc([], missing=[{"name": "seed", "why_needed": "x", "impact": "high"}])
        addition = self._doc([], missing=[
            {"name": "seed", "why_needed": "dup", "impact": "low"},      # dup name -> drop
            {"name": "code length", "why_needed": "y", "impact": "high"},  # new -> keep
        ])
        merged, _ = merge_engineering_facts(base, addition)
        names = [m["name"] for m in merged["missing_information"]]
        self.assertEqual(names, ["seed", "code length"])

    def test_base_facts_preserved_and_not_mutated(self) -> None:
        base_facts = [{"type": "metric", "name": "BER"}]
        base = self._doc(base_facts)
        merge_engineering_facts(base, self._doc([{"type": "metric", "name": "SER"}]))
        # original list object must be untouched
        self.assertEqual(len(base_facts), 1)


class _GapLLM:
    """Returns one NEW fact on the first gap pass, then nothing -> loop must terminate."""

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, prompt: str, *, system=None, response_format=None) -> str:
        self.calls += 1
        if self.calls == 1:
            return json.dumps({
                "paper_domain": "communication",
                "paper_repro_type": "other",
                "engineering_facts": [{
                    "type": "simulation_parameter",
                    "name": "alpha_ST_threshold",
                    "value": {"v": 0.5},
                    "source": {"source_kind": "text", "chunk_id": "c1", "page": 1,
                               "section": "S", "quote": "alpha_ST=0.5 used for Fig. 7", "figure_ref": ""},
                    "confidence": "high",
                    "used_for_reproduction": True,
                }],
                "missing_information": [],
            })
        return json.dumps({
            "paper_domain": "communication", "paper_repro_type": "other",
            "engineering_facts": [], "missing_information": [],
        })


class GapFinderIntegrationTests(unittest.TestCase):
    def test_gap_finder_adds_missing_fact_merges_and_terminates(self) -> None:
        base = {
            "paper_domain": "communication",
            "paper_repro_type": "other",
            "engineering_facts": [{
                "type": "figure_claim", "name": "Fig.7 sum-rate", "value": {},
                "source": {"source_kind": "text", "chunk_id": "c1", "page": 1,
                           "section": "S", "quote": "Fig. 7 shows sum-rate", "figure_ref": ""},
                "confidence": "high", "used_for_reproduction": True,
            }],
            "missing_information": [],
        }
        paper = {"chunks": [{"chunk_id": "c1", "text": "Fig. 7 sum-rate vs power with threshold alpha_ST.", "page": 1, "section": "S"}]}

        with TemporaryDirectory() as d:
            out_dir = Path(d)
            (out_dir / "audit").mkdir()
            client = _GapLLM()
            pipe = ReviewPipeline(client=client)
            result = pipe._augment_facts_with_gap_finder(
                facts=base, paper=paper, paper_context="ctx", paper_images=[],
                valid_chunk_ids={"c1"}, valid_pages=set(),
                output_dir=out_dir, audit_dir=out_dir / "audit",
                resume=False, max_attempts=2, max_rounds=2,
            )

            names = {f["name"] for f in result["engineering_facts"]}
            self.assertIn("alpha_ST_threshold", names)   # gap fact added
            self.assertIn("Fig.7 sum-rate", names)       # base fact preserved
            # round 1 adds 1, round 2 returns nothing -> dry -> stop (exactly 2 gap calls)
            self.assertEqual(client.calls, 2)
            written = json.loads((out_dir / "engineering_facts.json").read_text(encoding="utf-8"))
            self.assertEqual(written["_meta"]["gap_finder"]["round_1_added"], 1)

    def test_zero_rounds_is_a_noop(self) -> None:
        base = {"paper_domain": "communication", "paper_repro_type": "other",
                "engineering_facts": [{"type": "metric", "name": "BER"}], "missing_information": []}
        client = _GapLLM()
        pipe = ReviewPipeline(client=client)
        with TemporaryDirectory() as d:
            out = pipe._augment_facts_with_gap_finder(
                facts=base, paper={"chunks": []}, paper_context="", paper_images=[],
                valid_chunk_ids=set(), valid_pages=set(),
                output_dir=Path(d), audit_dir=Path(d), resume=False, max_attempts=1, max_rounds=0,
            )
        self.assertEqual(client.calls, 0)
        self.assertEqual(out, base)


if __name__ == "__main__":
    unittest.main()
