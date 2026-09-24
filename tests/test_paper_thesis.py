import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from geng_agent.consolidated_analysis import load_paper_understanding
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

    def test_missing_central_claim_remains_visible_for_supervisor_review(self) -> None:
        doc = {key: value for key, value in GOOD_THESIS.items() if key != "central_claim"}
        self.assertEqual(validate_stage("paper_thesis", doc), [])

    def test_blank_mechanism_is_not_a_transport_failure(self) -> None:
        # Scientific sufficiency is not established by a nonblank string.
        doc = {**GOOD_THESIS, "mechanism": "   "}
        self.assertEqual(validate_stage("paper_thesis", doc), [])


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
        self.assertIn("理论界", prompt)
        self.assertIn("不得把决定结论的条件", prompt)
        self.assertIn("不要强迫总排序", prompt)


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
        self.assertIn("不代表都属于本任务验收目标", anchor)
        self.assertIn("additional_observations", anchor)
        self.assertNotIn("优先级最高", anchor)
        self.assertNotIn("does_not_support_paper_claim", anchor)
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
    def _run_stage(self, out: Path, client: _ThesisFake):
        context = SimpleNamespace(output_dir=out, audit_dir=out / "audit",
            options=SimpleNamespace(resume=False, analysis_backend="llm"))
        return load_paper_understanding(ReviewPipeline(client=client), context,
            paper={"source_sha256": "fixture"}, paper_context="original paper text",
            paper_images=[], valid_chunk_ids=set(), valid_pages=set())

    def test_combined_understanding_keeps_thesis_and_facts_from_one_reading(self) -> None:
        facts = {"engineering_facts": [{"name": "paper condition", "value": "dense/high Doppler"}]}
        client = _ThesisFake(json.dumps({"facts": facts, "paper_thesis": GOOD_THESIS}))
        with TemporaryDirectory() as directory:
            out = Path(directory)
            doc = self._run_stage(out, client)
            self.assertEqual(doc["paper_thesis"], GOOD_THESIS)
            self.assertEqual(doc["facts"], facts)
            persisted = json.loads((out / "paper_understanding.json").read_text(encoding="utf-8"))
            self.assertEqual(persisted["paper_thesis"]["central_claim"], GOOD_THESIS["central_claim"])
            self.assertEqual(len(client.calls), 1)

    def test_unreadable_understanding_is_preserved_for_next_stage(self) -> None:
        client = _ThesisFake("this is not json at all")
        with TemporaryDirectory() as directory:
            out = Path(directory)
            result = self._run_stage(out, client)
            self.assertEqual(len(client.calls), 1)
            self.assertEqual(result["facts"]["_raw_handoff_text"], client.raw)
            self.assertTrue((out / "paper_understanding.json").exists())
            candidates = list((out / "audit/api_prompt_inputs").glob("*/raw.txt"))
            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0].read_text(encoding="utf-8"), client.raw)


if __name__ == "__main__":
    unittest.main()
