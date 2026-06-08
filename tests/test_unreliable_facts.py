from __future__ import annotations

import unittest

from geng_agent.facts_normalize import (
    drop_unreliable_facts,
    finalize_engineering_facts,
    unreliable_fact_reason,
)


def _fact(name, value, *, kind="text", section="", figref="", chunk_id="c1", page=1, ftype="metric"):
    return {
        "type": "figure_claim" if kind == "figure" else ftype,
        "name": name,
        "value": value,
        "source": {
            "source_kind": kind,
            "chunk_id": None if kind == "figure" else chunk_id,
            "page": page,
            "section": section,
            "quote": "q",
            "figure_ref": figref,
        },
        "confidence": "high",
        "used_for_reproduction": True,
    }


class UnreliableReasonTests(unittest.TestCase):
    def test_figure_point_value_read_dropped(self) -> None:
        f = _fact("Fig.7_pt", {"obs": "STAB+SDS 在 P=64 dBm 时约为 50 bits/s/Hz"}, kind="figure", figref="Fig. 7")
        self.assertEqual(unreliable_fact_reason(f), "figure_point_value_read")

    def test_arrow_mapping_point_read_dropped(self) -> None:
        f = _fact("Fig.5_pt", {"obs": "n=3 → 1.5"}, kind="figure", figref="Fig. 5")
        self.assertEqual(unreliable_fact_reason(f), "figure_point_value_read")

    def test_appendix_proof_dropped(self) -> None:
        f = _fact("bound66b", {"f": "<= M^p log2(...)"}, section="APPENDIX C PROOF OF LEMMA 3")
        self.assertEqual(unreliable_fact_reason(f), "appendix_or_proof_transcription")

    def test_snake_case_bound_formula_dropped(self) -> None:
        # \bbound\b would miss snake_case; the constant Cn the user flagged must be caught.
        f = _fact("ULA_cluster_eigenvalue_bound_Cn", {"formula": "Cn = M/(M+1) * ((n-1)!)^4"})
        self.assertEqual(unreliable_fact_reason(f), "transcribed_bound_formula")

    def test_name_vs_figref_mismatch_dropped(self) -> None:
        f = _fact("Fig.5_upa_zf_cdf_120km", {"d": "a Fig.4 curve"}, kind="figure", figref="Fig. 4 (R=120 km curve)")
        self.assertEqual(unreliable_fact_reason(f), "figure_number_name_ref_mismatch")

    def test_reliable_facts_survive(self) -> None:
        # load-bearing facts must NOT be dropped
        self.assertIsNone(unreliable_fact_reason(
            _fact("average_sum_rate", {"formula": "E[R]=K*log2(1+rho*M/tr(G^-1))"}, section="II. System Model")))
        self.assertIsNone(unreliable_fact_reason(_fact("仿真系统参数", {"fc": "1.9925 GHz", "K": 16})))
        # a structural figure read (ranges / curve counts / qualitative trend) is reliable, kept
        self.assertIsNone(unreliable_fact_reason(
            _fact("图7：和速率与功率", {"obs": "STAB+SDS 最佳；P 40-64 dBm；4 条曲线"}, kind="figure", figref="Fig. 7")))


class DropAndFinalizeTests(unittest.TestCase):
    def test_split_counts(self) -> None:
        facts = [
            _fact("keep1", {"fc": "1.9925 GHz"}),
            _fact("Fig.7_pt", {"obs": "约 50"}, kind="figure", figref="Fig. 7"),
            _fact("ULA_bound_Cn", {"formula": "Cn = M/(M+1)!"}),
        ]
        kept, dropped = drop_unreliable_facts(facts)
        self.assertEqual(len(kept), 1)
        self.assertEqual({d["reason"] for d in dropped}, {"figure_point_value_read", "transcribed_bound_formula"})

    def test_finalize_logs_unreliable_drops_and_keeps_reliable(self) -> None:
        doc = {
            "paper_domain": "communication",
            "paper_repro_type": "mimo_ofdm",
            "engineering_facts": [
                _fact("average_sum_rate", {"formula": "E[R]=K*log2(1+x)"}, section="II. System Model"),
                _fact("Fig.7_pt", {"obs": "约 50 bits/s/Hz"}, kind="figure", figref="Fig. 7"),
            ],
            "missing_information": [],
        }
        out = finalize_engineering_facts(doc, valid_chunk_ids={"c1"}, valid_pages={1})
        names = {f["name"] for f in out["engineering_facts"]}
        self.assertIn("average_sum_rate", names)
        self.assertNotIn("Fig.7_pt", names)
        self.assertEqual(out["_meta"]["unreliable_dropped_count"], 1)
        self.assertEqual(out["_meta"]["unreliable_dropped"][0]["reason"], "figure_point_value_read")


if __name__ == "__main__":
    unittest.main()
