from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from geng_agent.pipeline import (
    ReviewPipeline,
    _build_run_cost,
    _result_alignment_level,
    build_risk_dimensions,
    detect_nondeterminism_findings,
)


class _UsageClient:
    """Minimal LLM stand-in exposing a usage_log, like OpenAICompatibleClient."""

    def __init__(self, model: str, usage_log: list[dict]) -> None:
        self.model = model
        self.usage_log = usage_log

    def complete(self, prompt: str, *, system=None, response_format=None) -> str:  # pragma: no cover - unused
        return "{}"


class DetectNondeterminismTests(unittest.TestCase):
    def _project(self, tmp: str, run_experiment: str) -> Path:
        proj = Path(tmp)
        (proj / "src").mkdir(exist_ok=True)
        (proj / "run_experiment.py").write_text(run_experiment, encoding="utf-8")
        return proj

    def test_randomness_without_seed_is_flagged(self) -> None:
        with TemporaryDirectory() as tmp:
            proj = self._project(tmp, "import numpy as np\nx = np.random.normal(size=10)\n")
            findings = detect_nondeterminism_findings(proj)
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0]["type"], "nondeterministic_randomness")
            self.assertIn("run_experiment.py", findings[0]["files"])

    def test_default_rng_with_seed_is_not_flagged(self) -> None:
        with TemporaryDirectory() as tmp:
            proj = self._project(tmp, "import numpy as np\nrng = np.random.default_rng(42)\nx = rng.normal(size=10)\n")
            self.assertEqual(detect_nondeterminism_findings(proj), [])

    def test_random_seed_call_is_not_flagged(self) -> None:
        with TemporaryDirectory() as tmp:
            proj = self._project(tmp, "import random\nrandom.seed(0)\nv = random.random()\n")
            self.assertEqual(detect_nondeterminism_findings(proj), [])

    def test_no_randomness_is_not_flagged(self) -> None:
        with TemporaryDirectory() as tmp:
            proj = self._project(tmp, "x = 1 + 1\nprint(x)\n")
            self.assertEqual(detect_nondeterminism_findings(proj), [])


class BuildRunCostTests(unittest.TestCase):
    def test_per_stage_diffs_and_totals(self) -> None:
        marks = [
            {"stage": "start", "elapsed_s": 0.0, "llm_calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            {"stage": "facts", "elapsed_s": 2.0, "llm_calls": 1, "prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
            {"stage": "tasks", "elapsed_s": 5.0, "llm_calls": 3, "prompt_tokens": 300, "completion_tokens": 120, "total_tokens": 420},
        ]
        cost = _build_run_cost(marks, total_wall_s=5.5, by_model={"m": {"llm_calls": 3}})

        self.assertEqual(cost["wall_clock_s"], 5.5)
        self.assertEqual(cost["totals"]["total_tokens"], 420)
        self.assertEqual(cost["totals"]["llm_calls"], 3)
        self.assertEqual(cost["by_model"], {"m": {"llm_calls": 3}})

        by_stage = {entry["stage"]: entry for entry in cost["by_stage"]}
        self.assertEqual(by_stage["facts"]["seconds"], 2.0)
        self.assertEqual(by_stage["facts"]["total_tokens"], 150)
        self.assertEqual(by_stage["tasks"]["seconds"], 3.0)
        self.assertEqual(by_stage["tasks"]["total_tokens"], 270)
        self.assertEqual(by_stage["tasks"]["llm_calls"], 2)

    def test_empty_marks_are_safe(self) -> None:
        cost = _build_run_cost([], total_wall_s=0.0, by_model={})
        self.assertEqual(cost["totals"]["total_tokens"], 0)
        self.assertEqual(cost["by_stage"], [])


class TemplateFallbackRiskTests(unittest.TestCase):
    def test_result_alignment_level_forces_high_on_template(self) -> None:
        # template fallback -> high regardless of an otherwise-clean run
        self.assertEqual(_result_alignment_level(True, True, True, True, [], True), "high")
        # the clean control path is low
        self.assertEqual(_result_alignment_level(True, True, True, True, []), "low")
        # runtime failure is still high
        self.assertEqual(_result_alignment_level(True, False, True, True, []), "high")

    def test_template_fallback_pushes_fidelity_and_alignment_high(self) -> None:
        dims = build_risk_dimensions(
            missing=[],
            assumptions=[],
            validation={"required_files_present": True, "python_compiles": True},
            runtime_result={"enabled": True, "passed": True},
            scientific_check={},
            tasks={},
            result_review_result={"enabled": False, "passed": None},
            manifest_meta={"template_fallback_used": True},
        )
        self.assertEqual(dims["implementation_fidelity"]["level"], "high")
        self.assertEqual(dims["result_alignment"]["level"], "high")

    def test_clean_run_keeps_low_dimensions(self) -> None:
        dims = build_risk_dimensions(
            missing=[],
            assumptions=[],
            validation={"required_files_present": True, "python_compiles": True},
            runtime_result={"enabled": True, "passed": True},
            scientific_check={},
            tasks={},
            result_review_result={"enabled": True, "passed": True},
            manifest_meta={},
        )
        self.assertEqual(dims["implementation_fidelity"]["level"], "low")
        self.assertEqual(dims["result_alignment"]["level"], "low")

    def test_dependency_warnings_are_visible_but_not_high_risk(self) -> None:
        dims = build_risk_dimensions(
            missing=[],
            assumptions=[],
            validation={"required_files_present": True, "python_compiles": True},
            runtime_result={
                "enabled": True,
                "passed": True,
                "requirements_warnings": [{"message": "missing declaration"}],
            },
            scientific_check={},
            tasks={},
            result_review_result={"enabled": True, "passed": True},
            manifest_meta={},
        )

        self.assertEqual(dims["runtime_reliability"]["level"], "low")
        self.assertEqual(dims["security_isolation"]["level"], "medium")
        self.assertIn("requirements_warnings=1", dims["security_isolation"]["evidence"])


class UsageRollupTests(unittest.TestCase):
    def test_cumulative_and_by_model_rollup(self) -> None:
        main = _UsageClient("main", [{"model": "main", "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}])
        secondary = _UsageClient(
            "sec",
            [
                {"model": "sec", "prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
                {"model": "sec"},  # provider omitted usage -> counts as a call, 0 tokens
            ],
        )
        pipe = ReviewPipeline(main, extraction_client_2=secondary)

        cum = pipe._cumulative_usage()
        self.assertEqual(cum["llm_calls"], 3)
        self.assertEqual(cum["total_tokens"], 25)

        by_model = pipe._usage_by_model()
        self.assertEqual(by_model["sec"]["llm_calls"], 2)
        self.assertEqual(by_model["sec"]["total_tokens"], 10)
        self.assertEqual(by_model["main"]["total_tokens"], 15)

    def test_same_client_for_secondary_extraction_is_not_double_counted(self) -> None:
        main = _UsageClient("main", [{"model": "main", "total_tokens": 15}])
        pipe = ReviewPipeline(main, extraction_client_2=main)
        self.assertEqual(pipe._cumulative_usage()["llm_calls"], 1)


class ResultReviewSkipsTemplateTests(unittest.TestCase):
    def test_result_review_skipped_when_template_fallback_used(self) -> None:
        pipe = ReviewPipeline(_UsageClient("m", []))
        result = pipe._run_result_review_if_ready(
            enabled=True,
            run_repro=True,
            runtime_result={"enabled": True, "passed": True, "template_fallback_used": True},
            template_fallback_used=True,
            paper_path=Path("paper.pdf"),
            paper={},
            facts={},
            tasks={},
            paper_context_json="[]",
            repro_project_dir=Path("."),
            output_dir=Path("."),
            audit_dir=Path("."),
            max_attempts=1,
            resume=False,
        )
        self.assertFalse(result["enabled"])
        self.assertIsNone(result["passed"])
        self.assertIn("template", result["reason"])


class ResultReviewPartialTests(unittest.TestCase):
    def test_runs_on_partial_output(self) -> None:
        # A not-fully-passing run that produced partial outputs should still get the
        # per-experiment result review (so one failed experiment doesn't negate the rest).
        pipe = ReviewPipeline(_UsageClient("m", []))
        runtime = {"enabled": True, "passed": False, "partial_success": {"has_partial_output": True}}
        stub = {"enabled": True, "passed": True, "mode": "stub"}
        with TemporaryDirectory() as tmp, patch("geng_agent.pipeline.run_result_review", return_value=stub) as mocked:
            result = pipe._run_result_review_if_ready(
                enabled=True,
                run_repro=True,
                runtime_result=runtime,
                template_fallback_used=False,
                paper_path=Path("paper.pdf"),
                paper={},
                facts={},
                tasks={},
                paper_context_json="[]",
                repro_project_dir=Path(tmp),
                output_dir=Path(tmp),
                audit_dir=Path(tmp) / "audit",
                max_attempts=1,
                resume=False,
            )
        self.assertTrue(mocked.called)
        self.assertEqual(result.get("mode"), "stub")

    def test_skipped_when_no_usable_output(self) -> None:
        pipe = ReviewPipeline(_UsageClient("m", []))
        result = pipe._run_result_review_if_ready(
            enabled=True,
            run_repro=True,
            runtime_result={"enabled": True, "passed": False},
            template_fallback_used=False,
            paper_path=Path("paper.pdf"),
            paper={},
            facts={},
            tasks={},
            paper_context_json="[]",
            repro_project_dir=Path("."),
            output_dir=Path("."),
            audit_dir=Path("."),
            max_attempts=1,
            resume=False,
        )
        self.assertFalse(result["enabled"])
        self.assertIn("no usable output", result["reason"])


if __name__ == "__main__":
    unittest.main()
