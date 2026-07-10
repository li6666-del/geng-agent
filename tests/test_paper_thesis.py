import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from geng_agent.pipeline import ReviewPipeline
from geng_agent.prompts import PromptBook
from geng_agent.paper_evidence import (
    thesis_comparisons_for_task,
    thesis_ordering_anchor_for_task,
)
from geng_agent.schemas import validate_stage


GOOD_THESIS = {
    "central_claim": "在密集、高多普勒场景下，STAB 的平均和速率高于 ZF 与 MRT。",
    "proposed_method": "STAB（空时波束成形）",
    "mechanism": "空时（多普勒）维度让用户在等效信道里去相关，使 Ḡ 比纯空间 G 更良态，压过 1/L 预对数损失。",
    "comparisons": [
        {
            "claim_id": "stab_beats_zf_dense",
            "methods_best_to_worst": ["STAB", "ZF", "MRT"],
            "expected_ordering": "密集/高多普勒区 STAB > ZF > MRT",
            "metric": "average sum rate",
            "regime": "用户密集、高多普勒、高发射功率",
            "figure_ref": "Fig.4",
            "mechanism_note": "空时去相关带来条件数优势，抵消预对数惩罚",
        }
    ],
    "headline_shape": "和速率随发射功率单调上升；高功率区 STAB 曲线在最上方。",
    "caveats": ["用户稀疏或低多普勒时 STAB 的优势消失，甚至被 ZF 反超。"],
}


class PaperThesisSchemaTests(unittest.TestCase):
    def test_valid_thesis_passes(self) -> None:
        self.assertEqual(validate_stage("paper_thesis", GOOD_THESIS), [])

    def test_empty_comparisons_is_allowed(self) -> None:
        # A paper whose headline is not a head-to-head ordering still has a valid thesis.
        doc = {**GOOD_THESIS, "comparisons": []}
        self.assertEqual(validate_stage("paper_thesis", doc), [])

    def test_missing_central_claim_is_rejected(self) -> None:
        doc = {key: value for key, value in GOOD_THESIS.items() if key != "central_claim"}
        self.assertTrue(validate_stage("paper_thesis", doc))

    def test_blank_mechanism_is_rejected(self) -> None:
        # mechanism is the WHOLE point -- a blank one must not validate.
        doc = {**GOOD_THESIS, "mechanism": "   "}
        self.assertTrue(validate_stage("paper_thesis", doc))


class PaperThesisPromptTests(unittest.TestCase):
    def test_prompt_renders_and_targets_mechanism_and_ordering(self) -> None:
        prompt = PromptBook().render(
            "extract_paper_thesis.md",
            engineering_facts_json="{}",
            paper_chunks_json="[]",
        )
        # the prompt must steer toward the WHY (mechanism) and the checkable orderings, in Chinese
        for needle in (
            "核心思路",
            "机制",
            "methods_best_to_worst",
            "预期",
            "caveats",
            "必须用中文",
            "JSON object",
        ):
            self.assertIn(needle, prompt)
        # the mechanism must be prose, NOT a transcribed bound (that was the round-1 noise trap)
        self.assertIn("不要转写", prompt)


class ThesisOrderingMatchTests(unittest.TestCase):
    THESIS_FIG4 = {
        "comparisons": [
            {
                "claim_id": "c1",
                "methods_best_to_worst": ["STAB", "ZF"],
                "expected_ordering": "STAB > ZF",
                "metric": "sum rate",
                "regime": "用户密集",
                "figure_ref": "Fig.4",
                "mechanism_note": "条件数优势",
            }
        ]
    }

    def test_matches_task_by_figure_number(self) -> None:
        task = {"task_id": "reproduce_fig_4", "figure_or_claim": "Fig. 4 sum rate vs power",
                "metric": "spectral_efficiency", "target": "x", "output_columns": ["power", "sum_rate"]}
        matched = thesis_comparisons_for_task(self.THESIS_FIG4, task)
        self.assertEqual(len(matched), 1)
        anchor = thesis_ordering_anchor_for_task(self.THESIS_FIG4, task)
        self.assertIn("STAB > ZF", anchor)
        self.assertIn("baseline_comparison", anchor)
        self.assertIn("不要据此判 mismatch", anchor)  # the smoke-regime guard

    def test_no_match_for_different_figure(self) -> None:
        task = {"task_id": "reproduce_fig_9", "figure_or_claim": "Fig. 9 BER vs SNR",
                "metric": "bit_error_rate", "target": "x", "output_columns": ["snr_db", "ber"]}
        self.assertEqual(thesis_comparisons_for_task(self.THESIS_FIG4, task), [])
        self.assertEqual(thesis_ordering_anchor_for_task(self.THESIS_FIG4, task), "")

    def test_metric_word_fallback_when_comparison_has_no_figure(self) -> None:
        thesis = {"comparisons": [{
            "claim_id": "c2", "methods_best_to_worst": ["A", "B"], "expected_ordering": "A > B",
            "metric": "average sum rate", "regime": "", "figure_ref": "", "mechanism_note": "",
        }]}
        task = {"task_id": "t", "figure_or_claim": "throughput claim", "metric": "spectral_efficiency",
                "target": "sum rate study", "output_columns": ["power", "sum_rate"]}
        self.assertEqual(len(thesis_comparisons_for_task(thesis, task)), 1)

    def test_no_thesis_yields_no_match(self) -> None:
        task = {"task_id": "reproduce_fig_4", "figure_or_claim": "Fig. 4", "metric": "x",
                "target": "x", "output_columns": []}
        self.assertEqual(thesis_comparisons_for_task(None, task), [])
        self.assertEqual(thesis_ordering_anchor_for_task(None, task), "")


class _ThesisFake:
    """Returns a fixed raw string for the single thesis call; records prompts."""

    def __init__(self, raw: str) -> None:
        self.raw = raw
        self.calls: list[str] = []

    def complete(self, prompt: str, *, system=None, response_format=None) -> str:
        self.calls.append(prompt)
        return self.raw


class PaperThesisStageTests(unittest.TestCase):
    def _run_stage(self, raw: str):
        temp = TemporaryDirectory()
        out = Path(temp.name)
        (out / "audit").mkdir(parents=True, exist_ok=True)
        pipeline = ReviewPipeline(client=_ThesisFake(raw))
        doc = pipeline._load_or_create_paper_thesis(
            output_dir=out,
            audit_dir=out / "audit",
            facts={"paper_domain": "communication", "paper_repro_type": "other", "engineering_facts": [], "missing_information": []},
            paper_context="[]",
            paper_images=[],
            resume=False,
            max_attempts=1,
        )
        return doc, out, temp

    def test_returns_doc_and_persists_paper_thesis_json(self) -> None:
        doc, out, temp = self._run_stage(json.dumps(GOOD_THESIS))
        try:
            self.assertIsNotNone(doc)
            self.assertEqual(doc["proposed_method"], GOOD_THESIS["proposed_method"])
            persisted = json.loads((out / "paper_thesis.json").read_text(encoding="utf-8"))
            self.assertEqual(persisted["central_claim"], GOOD_THESIS["central_claim"])
        finally:
            temp.cleanup()

    def test_non_fatal_on_failure_returns_none_and_logs(self) -> None:
        # A thesis stage that never yields valid JSON must NOT sink the run -- it is advisory.
        doc, out, temp = self._run_stage("this is not json at all")
        try:
            self.assertIsNone(doc)
            self.assertFalse((out / "paper_thesis.json").exists())
            self.assertTrue((out / "audit" / "paper_thesis_error.json").exists())
        finally:
            temp.cleanup()


if __name__ == "__main__":
    unittest.main()
