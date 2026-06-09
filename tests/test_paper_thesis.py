import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from geng_agent.pipeline import ReviewPipeline, _thesis_anchor_text
from geng_agent.prompts import PromptBook
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


class ThesisAnchorTests(unittest.TestCase):
    def test_anchor_carries_claim_mechanism_and_ordering(self) -> None:
        anchor = _thesis_anchor_text(GOOD_THESIS)
        self.assertIn("复现靶子", anchor)
        self.assertIn(GOOD_THESIS["central_claim"], anchor)
        self.assertIn(GOOD_THESIS["mechanism"], anchor)
        self.assertIn("STAB > ZF > MRT", anchor)          # the checkable ordering
        self.assertIn("用户密集、高多普勒、高发射功率", anchor)  # its regime
        self.assertIn("方法排序反", anchor)                # all-reversed is a failure signal

    def test_anchor_is_empty_without_thesis(self) -> None:
        # None / blank thesis -> no anchor -> codegen prompts stay byte-for-byte unchanged.
        self.assertEqual(_thesis_anchor_text(None), "")
        self.assertEqual(_thesis_anchor_text({"central_claim": "", "mechanism": "  "}), "")

    def test_anchor_without_comparisons_still_carries_mechanism(self) -> None:
        anchor = _thesis_anchor_text({**GOOD_THESIS, "comparisons": []})
        self.assertIn(GOOD_THESIS["mechanism"], anchor)
        self.assertNotIn("论文断言的方法排序", anchor)  # no ordering block when no comparisons


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
