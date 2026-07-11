from __future__ import annotations

import inspect
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

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


class RiskDimensionTests(unittest.TestCase):
    def test_result_alignment_level_tracks_runtime_and_review(self) -> None:
        self.assertEqual(_result_alignment_level(True, True, True, True, []), "low")
        self.assertEqual(_result_alignment_level(True, False, True, True, []), "high")

    def test_clean_run_keeps_low_dimensions(self) -> None:
        dims = build_risk_dimensions(
            missing=[],
            assumptions=[],
            validation={"required_files_present": True, "python_compiles": True},
            runtime_result={"enabled": True, "passed": True},
            scientific_check={},
            tasks={},
            result_review_result={"enabled": True, "passed": True},
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
        )

        self.assertEqual(dims["runtime_reliability"]["level"], "low")
        self.assertEqual(dims["security_isolation"]["level"], "medium")
        self.assertIn("requirements_warnings=1", dims["security_isolation"]["evidence"])


class UsageRollupTests(unittest.TestCase):
    def test_cumulative_and_by_model_rollup(self) -> None:
        main = _UsageClient("main", [{"model": "main", "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}])
        pipe = ReviewPipeline(main)

        cum = pipe._cumulative_usage()
        self.assertEqual(cum["llm_calls"], 1)
        self.assertEqual(cum["total_tokens"], 15)

        by_model = pipe._usage_by_model()
        self.assertEqual(by_model["main"]["total_tokens"], 15)

    def test_pipeline_accepts_only_one_analysis_client(self) -> None:
        self.assertEqual(list(inspect.signature(ReviewPipeline).parameters), ["client", "prompt_book"])


if __name__ == "__main__":
    unittest.main()
