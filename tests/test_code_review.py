import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from geng_agent.code_review import filter_grounded_findings, run_code_faithfulness_review
from geng_agent.pipeline import ReviewPipeline
from geng_agent.prompts import PromptBook
from tests.test_pipeline import FakeLLM, schema_name

FACTS = {
    "paper_domain": "communication",
    "paper_repro_type": "signal_chain",
    "engineering_facts": [
        {
            "type": "modulation",
            "name": "16-QAM",
            "value": {"constellation": "(±(2m-1) ± (2m-1)j)"},
            "source": {"chunk_id": "c1", "page": 1, "section": "", "quote": "QAM constellation (±(2m-1) ± (2m-1)j)"},
            "confidence": "high",
            "used_for_reproduction": True,
        }
    ],
    "missing_information": [],
}

TASKS = {
    "repro_tasks": [
        {
            "task_id": "reproduce_ser_16qam",
            "target": "SER",
            "metric": "symbol_error_rate",
            "metric_formula": "Ps = ...",
            "figure_or_claim": "Fig. 5",
            "expected_artifacts": ["outputs/results.csv"],
            "output_columns": ["es_n0_db", "ser"],
            "expected_trend": {"x_axis": "es_n0_db", "y_axis": "ser", "direction": "decreasing", "reason": "r"},
            "comparison": {"baselines": ["analytical"], "curve_groups": ["sim"], "tolerance": "qualitative"},
            "required_facts": [{"type": "modulation", "name": "16-QAM"}],
            "assumptions": [],
            "risk_if_unreproducible": "core",
        }
    ]
}

BUGGY_MOD = (
    "import numpy as np\n\n\ndef _qam_unscaled(M):\n    m = int(np.sqrt(M))\n"
    "    coords = 2 * np.arange(1, m + 1) - 1\n"
    "    re, im = np.meshgrid(coords, coords, indexing='ij')\n    return (re + 1j * im).ravel()\n"
)
FIXED_MOD = (
    "import numpy as np\n\n\ndef _qam_unscaled(M):\n    m = int(np.sqrt(M))\n"
    "    coords = 2 * np.arange(m) - (m - 1)\n"
    "    re, im = np.meshgrid(coords, coords, indexing='ij')\n    return (re + 1j * im).ravel()\n"
)


def write_project(repro_project_dir: Path, modulation_src: str) -> None:
    (repro_project_dir / "src").mkdir(parents=True, exist_ok=True)
    (repro_project_dir / "src" / "modulation.py").write_text(modulation_src, encoding="utf-8")
    (repro_project_dir / "run_experiment.py").write_text("print('ok')\n", encoding="utf-8")


def _blocking_finding() -> dict:
    return {
        "spec_kind": "fact",
        "spec_ref": "16-QAM",
        "evidence_spec": "(±(2m-1) ± (2m-1)j)",
        "code_location": "src/modulation.py:6",
        "evidence_code": "coords = 2 * np.arange(1, m + 1) - 1",
        "severity": "blocking",
        "issue": "constellation uses all-positive levels, not symmetric",
        "suggested_fix": "coords = 2*np.arange(m) - (m-1)",
    }


class ReviseLLM:
    """Round 1: blocking finding; revise -> RepairManifest; round 2: pass."""

    def __init__(self) -> None:
        self.review_calls = 0
        self.stages: list = []

    def complete(self, prompt: str, *, system=None, response_format=None) -> str:
        stage = schema_name(response_format)
        self.stages.append(stage)
        if stage == "code_faithfulness_review":
            self.review_calls += 1
            if self.review_calls == 1:
                return json.dumps({"verdict": "revise", "findings": [_blocking_finding()], "unverifiable": [], "note": "qam constellation wrong"})
            return json.dumps({"verdict": "pass", "findings": [], "unverifiable": [], "note": "looks faithful now"})
        if stage == "repair_manifest":
            return json.dumps({
                "reason": "fix QAM constellation to symmetric levels",
                "touched_files": ["src/modulation.py"],
                "scientific_changes": ["constellation now symmetric / zero-mean"],
                "files": [{"path": "src/modulation.py", "content_lines": FIXED_MOD.splitlines()}],
            })
        raise AssertionError(f"unexpected stage {stage}")


class GroundingTests(unittest.TestCase):
    def test_grounded_kept_hallucinated_dropped(self) -> None:
        with TemporaryDirectory() as d:
            proj = Path(d) / "repro_project"
            write_project(proj, BUGGY_MOD)
            findings = [
                _blocking_finding(),  # both excerpts really present
                {
                    "spec_kind": "fact", "spec_ref": "ghost",
                    "evidence_spec": "a spec sentence that does not exist anywhere qqq",
                    "code_location": "x.py:1",
                    "evidence_code": "def totally_made_up_symbol_zzz():",
                    "severity": "blocking", "issue": "made up", "suggested_fix": "n",
                },
            ]
            kept, dropped = filter_grounded_findings(findings, FACTS, TASKS, proj)
            self.assertEqual([f["spec_ref"] for f in kept], ["16-QAM"])
            self.assertEqual(len(dropped), 1)


class ReviewLoopTests(unittest.TestCase):
    def test_blocking_triggers_revise_then_pass(self) -> None:
        with TemporaryDirectory() as d:
            root = Path(d)
            proj = root / "repro_project"
            audit = root / "audit"
            audit.mkdir(parents=True)
            write_project(proj, BUGGY_MOD)
            llm = ReviseLLM()

            result = run_code_faithfulness_review(
                client=llm, prompt_book=PromptBook(), repro_project_dir=proj, audit_dir=audit,
                facts=FACTS, tasks=TASKS, paper_context="paper", system_message="sys", max_revise_attempts=1,
            )

            self.assertTrue(result["passed"])
            self.assertTrue(result["revised"])
            fixed = (proj / "src" / "modulation.py").read_text(encoding="utf-8")
            self.assertIn("np.arange(m)", fixed)
            self.assertNotIn("np.arange(1, m + 1)", fixed)
            self.assertIn("repair_manifest", llm.stages)

    def test_hallucinated_blocking_does_not_revise(self) -> None:
        class HallucinateLLM:
            def __init__(self) -> None:
                self.stages: list = []

            def complete(self, prompt: str, *, system=None, response_format=None) -> str:
                self.stages.append(schema_name(response_format))
                return json.dumps({"verdict": "revise", "findings": [{
                    "spec_kind": "fact", "spec_ref": "ghost",
                    "evidence_spec": "nonexistent spec text qqq", "code_location": "x.py:1",
                    "evidence_code": "nonexistent code text qqq", "severity": "blocking",
                    "issue": "made up", "suggested_fix": "none"}], "unverifiable": [], "note": "n"})

        with TemporaryDirectory() as d:
            root = Path(d)
            proj = root / "repro_project"
            audit = root / "audit"
            audit.mkdir(parents=True)
            write_project(proj, BUGGY_MOD)
            llm = HallucinateLLM()

            result = run_code_faithfulness_review(
                client=llm, prompt_book=PromptBook(), repro_project_dir=proj, audit_dir=audit,
                facts=FACTS, tasks=TASKS, paper_context="p", system_message="s", max_revise_attempts=1,
            )

            self.assertTrue(result["passed"])  # ungrounded blocking dropped -> nothing to fix
            self.assertNotIn("repair_manifest", llm.stages)  # never revised

    def test_unresolved_blocking_does_not_hard_block(self) -> None:
        class AlwaysBlockLLM:
            def complete(self, prompt: str, *, system=None, response_format=None) -> str:
                stage = schema_name(response_format)
                if stage == "code_faithfulness_review":
                    return json.dumps({"verdict": "revise", "findings": [_blocking_finding()], "unverifiable": [], "note": "still wrong"})
                # revise returns a manifest that does NOT actually fix it
                return json.dumps({"reason": "noop", "touched_files": ["src/modulation.py"], "scientific_changes": [],
                                   "files": [{"path": "src/modulation.py", "content_lines": BUGGY_MOD.splitlines()}]})

        with TemporaryDirectory() as d:
            root = Path(d)
            proj = root / "repro_project"
            audit = root / "audit"
            audit.mkdir(parents=True)
            write_project(proj, BUGGY_MOD)

            result = run_code_faithfulness_review(
                client=AlwaysBlockLLM(), prompt_book=PromptBook(), repro_project_dir=proj, audit_dir=audit,
                facts=FACTS, tasks=TASKS, paper_context="p", system_message="s", max_revise_attempts=1,
            )

            self.assertFalse(result["passed"])  # still blocking after budget
            self.assertTrue(result["unresolved_findings"])  # recorded, not raised
            self.assertTrue((proj / "src" / "modulation.py").exists())  # code kept, not wiped


class PipelineIntegrationTests(unittest.TestCase):
    class ReviewPassFakeLLM(FakeLLM):
        def complete(self, prompt: str, *, system=None, response_format=None) -> str:
            if schema_name(response_format) == "code_faithfulness_review":
                return json.dumps({"verdict": "pass", "findings": [], "unverifiable": [], "note": "faithful"})
            return super().complete(prompt, system=system, response_format=response_format)

    def test_pipeline_runs_code_review_when_enabled(self) -> None:
        with TemporaryDirectory() as d:
            temp = Path(d)
            paper = temp / "paper.md"
            paper.write_text("Simulation Results\nAWGN channel, BER vs SNR.", encoding="utf-8")

            result = ReviewPipeline(client=self.ReviewPassFakeLLM()).run(paper, temp / "case", run_repro=False, code_review=True)

            cr_path = result.output_dir / "code_review.json"
            self.assertTrue(cr_path.exists())
            cr = json.loads(cr_path.read_text(encoding="utf-8"))
            self.assertTrue(cr["passed"])
            risk = json.loads((result.output_dir / "risk_report.json").read_text(encoding="utf-8"))
            self.assertIn("code_review", risk)

    def test_pipeline_skips_code_review_by_default(self) -> None:
        with TemporaryDirectory() as d:
            temp = Path(d)
            paper = temp / "paper.md"
            paper.write_text("Simulation Results\nAWGN channel, BER vs SNR.", encoding="utf-8")

            result = ReviewPipeline(client=FakeLLM()).run(paper, temp / "case", run_repro=False)

            self.assertFalse((result.output_dir / "code_review.json").exists())

    def test_separate_reviewer_client_handles_only_the_review(self) -> None:
        class ReviewerLLM:
            def __init__(self) -> None:
                self.stages: list = []

            def complete(self, prompt: str, *, system=None, response_format=None) -> str:
                self.stages.append(schema_name(response_format))
                return json.dumps({"verdict": "pass", "findings": [], "unverifiable": [], "note": "faithful"})

        with TemporaryDirectory() as d:
            temp = Path(d)
            paper = temp / "paper.md"
            paper.write_text("Simulation Results\nAWGN channel, BER vs SNR.", encoding="utf-8")
            main = FakeLLM()
            reviewer = ReviewerLLM()

            ReviewPipeline(client=main, code_review_client=reviewer).run(paper, temp / "case", run_repro=False, code_review=True)

            # The heterogeneous reviewer handled the reviews (and only reviews: per-file
            # faithfulness reviews during generation + the final whole-project review);
            # the main generator client never saw a code-review call.
            self.assertTrue(reviewer.stages)
            self.assertTrue(all(stage == "code_faithfulness_review" for stage in reviewer.stages))
            self.assertEqual(len(main.calls), 12)


class CodeReviewClientTests(unittest.TestCase):
    def test_explicit_model_builds_client(self) -> None:
        from geng_agent.config import build_code_review_client

        client = build_code_review_client(model="deepseek-v4-pro", api_key="k", base_url="https://api.deepseek.com")
        self.assertIsNotNone(client)
        self.assertEqual(client.model, "deepseek-v4-pro")
        self.assertEqual(client.base_url, "https://api.deepseek.com")

    def test_none_when_no_model_configured(self) -> None:
        from unittest.mock import patch

        from geng_agent import config

        with patch.object(config, "get_config_value", return_value=None):
            self.assertIsNone(config.build_code_review_client())

    def test_missing_key_raises(self) -> None:
        from unittest.mock import patch

        from geng_agent import config

        with patch.object(config, "get_config_value", return_value=None):
            with self.assertRaises(ValueError):
                config.build_code_review_client(model="deepseek-v4-pro", base_url="https://api.deepseek.com")


if __name__ == "__main__":
    unittest.main()
