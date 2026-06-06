from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from geng_agent.pipeline import PipelineResult, ReviewPipeline, _apply_prompt_adjustment
from geng_agent.supervisor import SuperviseOptions, _act_with_pipeline
from tests.test_pipeline import FakeLLM


GUIDANCE_HEADER = "监督层补充指令"


class RecordingLLM(FakeLLM):
    def __init__(self) -> None:
        super().__init__()
        self.prompts: list[str] = []

    def complete(self, prompt: str, *, system: str | None = None, response_format: dict | None = None) -> str:
        self.prompts.append(prompt)
        return super().complete(prompt, system=system, response_format=response_format)


def write_paper(directory: Path) -> Path:
    paper = directory / "paper.md"
    paper.write_text("Simulation Results\nAWGN channel, BER vs SNR.", encoding="utf-8")
    return paper


class ApplyHelperTests(unittest.TestCase):
    def test_noop_without_adjustment(self) -> None:
        self.assertEqual(_apply_prompt_adjustment("BASE", None), "BASE")
        self.assertEqual(_apply_prompt_adjustment("BASE", "   "), "BASE")

    def test_appends_guidance_block(self) -> None:
        out = _apply_prompt_adjustment("BASE", "DO X")
        self.assertTrue(out.startswith("BASE"))
        self.assertIn(GUIDANCE_HEADER, out)
        self.assertIn("DO X", out)


class PipelineThreadingTests(unittest.TestCase):
    def test_run_threads_adjustment_into_facts_prompt_only(self) -> None:
        with TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            paper = write_paper(temp)
            llm = RecordingLLM()

            ReviewPipeline(client=llm).run(
                paper,
                temp / "case",
                run_repro=False,
                prompt_adjustments={"facts": "PRIORITIZE_CHANNEL_CODING_XYZ"},
            )

            facts_prompt = llm.prompts[0]
            self.assertIn("PRIORITIZE_CHANNEL_CODING_XYZ", facts_prompt)
            self.assertIn(GUIDANCE_HEADER, facts_prompt)
            # The tasks prompt (second LLM call) must not carry the facts-stage guidance.
            self.assertNotIn("PRIORITIZE_CHANNEL_CODING_XYZ", llm.prompts[1])

    def test_run_without_adjustment_leaves_prompt_clean(self) -> None:
        with TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            paper = write_paper(temp)
            llm = RecordingLLM()

            ReviewPipeline(client=llm).run(paper, temp / "case", run_repro=False)

            self.assertNotIn(GUIDANCE_HEADER, llm.prompts[0])

    def test_run_stage_threads_adjustment_on_regenerate(self) -> None:
        with TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            paper = write_paper(temp)
            case = temp / "case"
            pipeline = ReviewPipeline(client=RecordingLLM())
            pipeline.run(paper, case, run_repro=False)

            llm = RecordingLLM()
            pipeline.client = llm
            pipeline.run_stage(
                "facts",
                paper_path=paper,
                output_dir=case,
                run_repro=False,
                prompt_adjustments={"facts": "REDO_FACTS_TOKEN"},
            )

            self.assertTrue(any("REDO_FACTS_TOKEN" in prompt for prompt in llm.prompts))


class SupervisorForwardingTests(unittest.TestCase):
    def test_act_with_pipeline_forwards_prompt_adjustment(self) -> None:
        captured: dict = {}

        def _result() -> PipelineResult:
            return PipelineResult(
                output_dir=Path("o"),
                review_path=Path("o/review.md"),
                repro_project_dir=Path("o/repro_project"),
                risk_report_path=Path("o/risk_report.json"),
            )

        class FakePipeline:
            def run_stage(self, stage: str, **kwargs) -> PipelineResult:
                captured["stage"] = stage
                captured["kwargs"] = kwargs
                return _result()

            def run(self, **kwargs) -> PipelineResult:
                captured["kwargs"] = kwargs
                return _result()

        decision = {
            "action": "retry_stage",
            "target_stage": "engineering_facts",
            "reason": "retry with guidance",
            "evidence_paths": [],
            "risk_level": "low",
            "confidence": "low",
            "prompt_adjustment": "ADJUST_FACTS",
            "human_question": None,
        }

        _act_with_pipeline(
            decision=decision,
            pipeline=FakePipeline(),
            paper_path=Path("paper.md"),
            output_dir=Path("o"),
            options=SuperviseOptions(),
        )

        self.assertEqual(captured["stage"], "facts")
        self.assertEqual(captured["kwargs"]["prompt_adjustments"], {"facts": "ADJUST_FACTS"})

    def test_act_with_pipeline_no_adjustment_when_field_empty(self) -> None:
        captured: dict = {}

        class FakePipeline:
            def run_stage(self, stage: str, **kwargs) -> PipelineResult:
                captured["kwargs"] = kwargs
                return PipelineResult(
                    output_dir=Path("o"),
                    review_path=Path("o/review.md"),
                    repro_project_dir=Path("o/repro_project"),
                    risk_report_path=Path("o/risk_report.json"),
                )

            def run(self, **kwargs) -> PipelineResult:  # pragma: no cover - not expected
                captured["kwargs"] = kwargs
                raise AssertionError("run_stage should be used for a known target_stage")

        decision = {
            "action": "retry_stage",
            "target_stage": "repro_tasks",
            "reason": "retry",
            "evidence_paths": [],
            "risk_level": "low",
            "confidence": "low",
            "prompt_adjustment": None,
            "human_question": None,
        }

        _act_with_pipeline(
            decision=decision,
            pipeline=FakePipeline(),
            paper_path=Path("paper.md"),
            output_dir=Path("o"),
            options=SuperviseOptions(),
        )

        self.assertIsNone(captured["kwargs"]["prompt_adjustments"])


if __name__ == "__main__":
    unittest.main()
