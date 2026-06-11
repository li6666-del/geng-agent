import json
import sys
import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from geng_agent.agentic_project import (
    _feedback_from_results,
    _is_better_score,
    _score_candidate,
    build_writer_brief,
    run_codex_project_workflow,
)
from geng_agent.pipeline import ReviewPipeline
from geng_agent.prompts import PromptBook


class DummyLLM:
    def complete(self, *args, **kwargs):
        raise AssertionError("LLM repair/codegen must not be used by the Codex project backend")


class MinimalPipelineLLM:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, prompt: str, *, system=None, response_format=None) -> str:
        self.calls += 1
        if self.calls == 1:
            return json.dumps(
                {
                    "paper_domain": "communication",
                    "paper_repro_type": "signal_chain",
                    "engineering_facts": [
                        {
                            "type": "channel_model",
                            "name": "AWGN",
                            "value": {"snr_db": [0, 5]},
                            "source": {
                                "source_kind": "text",
                                "chunk_id": "text_c1",
                                "page": None,
                                "section": "Simulation",
                                "quote": "AWGN",
                                "figure_ref": "",
                            },
                            "confidence": "high",
                            "used_for_reproduction": True,
                        }
                    ],
                    "missing_information": [],
                }
            )
        if self.calls == 2:
            return json.dumps(
                {
                    "repro_tasks": [
                        {
                            "task_id": "reproduce_fig_1",
                            "target": "BER vs SNR",
                            "metric": "bit_error_rate",
                            "metric_formula": "bit_error_rate = bit_errors / total_bits",
                            "figure_or_claim": "Fig. 1",
                            "expected_artifacts": ["outputs/reproduce_fig_1/results.csv"],
                            "output_columns": ["snr_db", "bit_error_rate"],
                            "expected_trend": {
                                "x_axis": "snr_db",
                                "y_axis": "bit_error_rate",
                                "direction": "decreasing",
                                "reason": "higher SNR lowers BER",
                            },
                            "comparison": {"baselines": ["AWGN reference"], "curve_groups": ["sim"], "tolerance": "trend"},
                            "required_facts": [{"type": "channel_model", "name": "AWGN"}],
                            "assumptions": [],
                            "risk_if_unreproducible": "core curve unchecked",
                        }
                    ]
                }
            )
        raise AssertionError("Codex backend should not ask the LLM to write project code")


def _review(verdict: str, alignment: str, differences=None, causes=None) -> dict:
    dims = [
        "artifact_coverage",
        "reproduction_logic",
        "trend_shape",
        "metric_axis_scale",
        "baseline_comparison",
        "statistical_reliability",
        "conclusion_support",
    ]
    return {
        "task_id": "reproduce_fig_1",
        "local_result_credibility": "high",
        "paper_alignment": alignment,
        "scientific_verdict": verdict,
        "dimension_reviews": [
            {"dimension": dim, "rating": "strong", "finding": f"{dim} ok", "evidence": ["mock evidence"]}
            for dim in dims
        ],
        "paper_result_summary": "paper says curve should increase",
        "local_result_summary": "local curve increases",
        "differences": differences or [],
        "possible_causes": causes or [],
        "evidence": ["mock evidence"],
        "limitations": ["mock reviewer"],
        "confidence": "high",
    }


def _write_mock_writer(temp: Path) -> str:
    script = temp / "mock_writer.py"
    script.write_text(
        textwrap.dedent(
            r'''
            import sys
            from pathlib import Path

            args = sys.argv[1:]
            proj = Path(args[args.index("--cd") + 1])
            prompt = sys.stdin.read() if args and args[-1] == "-" else args[-1]
            log = Path(__file__).with_name("writer_prompts.txt")
            with log.open("a", encoding="utf-8") as handle:
                handle.write("---PROMPT---\n" + prompt + "\n")

            (proj / "src").mkdir(exist_ok=True)
            (proj / "tasks").mkdir(exist_ok=True)
            (proj / "README.md").write_text("mock codex project\n", encoding="utf-8")
            (proj / "requirements.txt").write_text("numpy\nmatplotlib\n", encoding="utf-8")
            (proj / "config.json").write_text('{"seed": 1}\n', encoding="utf-8")
            (proj / "config_smoke.json").write_text('{"seed": 1}\n', encoding="utf-8")
            (proj / "src" / "channel.py").write_text("def gain():\n    return 1.0\n", encoding="utf-8")
            (proj / "src" / "modulation.py").write_text("def method_name():\n    return 'mock'\n", encoding="utf-8")
            (proj / "src" / "metrics.py").write_text("def score(x):\n    return float(x)\n", encoding="utf-8")
            (proj / "src" / "simulation.py").write_text(
                "def run_curve():\n    return [(0, 0.1), (1, 0.3), (2, 0.5)]\n",
                encoding="utf-8",
            )
            (proj / "tasks" / "reproduce_fig_1.py").write_text(
                "\n".join([
                    "from __future__ import annotations",
                    "import json",
                    "import matplotlib.pyplot as plt",
                    "from src import _io",
                    "from src.simulation import run_curve",
                    "",
                    "def main(config_path=None) -> int:",
                    "    cfg_path = config_path or 'config_smoke.json'",
                    "    with open(cfg_path, 'r', encoding='utf-8') as handle:",
                    "        cfg = json.load(handle)",
                    "    task_id = 'reproduce_fig_1'",
                    "    _io.begin(task_id, cfg)",
                    "    rows = run_curve()",
                    "    _io.write_table(task_id, ['snr', 'rate'], rows)",
                    "    fig, ax = plt.subplots()",
                    "    ax.plot([x for x, _ in rows], [y for _, y in rows])",
                    "    ax.set_xlabel('snr')",
                    "    ax.set_ylabel('rate')",
                    "    _io.write_figure(task_id, 'fig1', fig)",
                    "    return _io.finish(task_id, metrics={'backend': 'cpu', 'points': len(rows)}, assumptions=['mock'])",
                    "",
                    "if __name__ == '__main__':",
                    "    raise SystemExit(main())",
                    "",
                ]),
                encoding="utf-8",
            )
            (proj / "src" / "_io.py").write_text("SABOTAGED\n", encoding="utf-8")
            (proj / "run_experiment.py").write_text("SABOTAGED\n", encoding="utf-8")
            (proj / "unexpected.py").write_text("print('bad')\n", encoding="utf-8")
            print("writer done")
            if Path(__file__).with_name("writer_exit_1.flag").exists():
                raise SystemExit(1)
            '''
        ),
        encoding="utf-8",
    )
    return f'"{sys.executable}" "{script}"'


def _write_failing_after_first_reviewer(temp: Path) -> str:
    script = temp / "mock_reviewer_fail_after_first.py"
    script.write_text(
        textwrap.dedent(
            r'''
            import json
            import sys
            from pathlib import Path

            args = sys.argv[1:]
            out = Path(args[args.index("--output-last-message") + 1])
            counter = Path(__file__).with_name("review_count_fail_after_first.txt")
            n = int(counter.read_text(encoding="utf-8")) if counter.exists() else 0
            counter.write_text(str(n + 1), encoding="utf-8")
            if n > 0:
                print("not json")
                raise SystemExit(1)
            dims = [
                "artifact_coverage",
                "reproduction_logic",
                "trend_shape",
                "metric_axis_scale",
                "baseline_comparison",
                "statistical_reliability",
                "conclusion_support",
            ]
            review = {
                "task_id": "reproduce_fig_1",
                "local_result_credibility": "high",
                "paper_alignment": "mismatch",
                "scientific_verdict": "does_not_support_paper_claim",
                "dimension_reviews": [
                    {"dimension": dim, "rating": "strong", "finding": dim + " ok", "evidence": ["mock evidence"]}
                    for dim in dims
                ],
                "paper_result_summary": "paper says curve should increase",
                "local_result_summary": "local curve decreases",
                "differences": ["first round has a real mismatch"],
                "possible_causes": ["mock modeling issue"],
                "evidence": ["mock evidence"],
                "limitations": ["mock reviewer"],
                "confidence": "high",
            }
            out.write_text(json.dumps(review, ensure_ascii=False), encoding="utf-8")
            print(json.dumps(review, ensure_ascii=False))
            '''
        ),
        encoding="utf-8",
    )
    return f'"{sys.executable}" "{script}"'


def _write_mock_reviewer(temp: Path) -> str:
    script = temp / "mock_reviewer.py"
    script.write_text(
        textwrap.dedent(
            r'''
            import json
            import sys
            from pathlib import Path

            args = sys.argv[1:]
            out = Path(args[args.index("--output-last-message") + 1])
            counter = Path(__file__).with_name("review_count.txt")
            n = int(counter.read_text(encoding="utf-8")) if counter.exists() else 0
            counter.write_text(str(n + 1), encoding="utf-8")
            verdict = "does_not_support_paper_claim" if n == 0 else "supports_paper_claim"
            alignment = "mismatch" if n == 0 else "match"
            differences = ["paper ordering is not matched yet"] if n == 0 else []
            causes = ["mock modeling issue"] if n == 0 else []
            dims = [
                "artifact_coverage",
                "reproduction_logic",
                "trend_shape",
                "metric_axis_scale",
                "baseline_comparison",
                "statistical_reliability",
                "conclusion_support",
            ]
            review = {
                "task_id": "reproduce_fig_1",
                "local_result_credibility": "high",
                "paper_alignment": alignment,
                "scientific_verdict": verdict,
                "dimension_reviews": [
                    {"dimension": dim, "rating": "strong", "finding": dim + " ok", "evidence": ["mock evidence"]}
                    for dim in dims
                ],
                "paper_result_summary": "paper says curve should increase",
                "local_result_summary": "local curve increases",
                "differences": differences,
                "possible_causes": causes,
                "evidence": ["mock evidence"],
                "limitations": ["mock reviewer"],
                "confidence": "high",
            }
            out.write_text(json.dumps(review, ensure_ascii=False), encoding="utf-8")
            print(json.dumps(review, ensure_ascii=False))
            '''
        ),
        encoding="utf-8",
    )
    return f'"{sys.executable}" "{script}"'


class AgenticProjectWorkflowTests(unittest.TestCase):
    def test_partial_review_feedback_is_actionable_for_next_writer_round(self) -> None:
        review_doc = {
            "experiment_reviews": [
                _review(
                    "partially_supports_paper_claim",
                    "partial_match",
                    differences=["Fig. 5 should use a log y-axis"],
                    causes=["plotting uses a linear axis"],
                )
            ]
        }
        feedback = _feedback_from_results(
            {"passed": True, "coverage": "1/1"},
            review_doc,
            {"enabled": True, "passed": True},
        )
        self.assertEqual(len(feedback), 1)
        self.assertEqual(feedback[0]["type"], "paper_alignment_gap")
        self.assertEqual(feedback[0]["scientific_verdict"], "partially_supports_paper_claim")
        self.assertIn("Fig. 5 should use a log y-axis", feedback[0]["differences"])

        brief = build_writer_brief(
            facts={"engineering_facts": []},
            tasks={"repro_tasks": [{"task_id": "reproduce_fig_1"}]},
            experiment_index={"experiments": []},
            paper_context_json="paper context",
            paper_thesis=None,
            task_manifest={
                "version": 1,
                "tasks": [{"task_id": "reproduce_fig_1", "module": "reproduce_fig_1", "script": "tasks/reproduce_fig_1.py"}],
            },
            expected_paths={"README.md", "tasks/reproduce_fig_1.py"},
            feedback=feedback,
            round_no=2,
            max_rounds=3,
        )
        self.assertIn("Mandatory moderator feedback", brief)
        self.assertIn("supports_paper_claim", brief)
        self.assertIn("Fig. 5 should use a log y-axis", brief)
        self.assertNotIn("No reviewer/runtime feedback yet.", brief)

    def test_partial_reviews_count_as_scientific_gap_for_best_round(self) -> None:
        partial_doc = {
            "experiment_reviews": [
                _review("partially_supports_paper_claim", "partial_match", differences=["still partial"])
            ]
        }
        support_doc = {"experiment_reviews": [_review("supports_paper_claim", "match")]}

        common_runtime = {"passed": True, "coverage": "1/1"}
        common_status = {"enabled": True, "passed": True}
        common_writer = {"ok": True}
        common_validation = {"required_files_present": True, "python_compiles": True}

        partial_score = _score_candidate(
            common_runtime, partial_doc, common_status, common_writer, common_validation, []
        )
        support_score = _score_candidate(
            common_runtime, support_doc, common_status, common_writer, common_validation, []
        )

        self.assertEqual(partial_score["partial_count"], 1)
        self.assertEqual(partial_score["scientific_gap_count"], 1)
        self.assertEqual(support_score["support_count"], 1)
        self.assertEqual(support_score["scientific_gap_count"], 0)
        self.assertTrue(_is_better_score(support_score, partial_score))

    def test_codex_writer_reviewer_loop_restores_trusted_files_and_feeds_back_review(self) -> None:
        with TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            paper = temp / "paper.md"
            paper.write_text("Figure 1 shows rate increasing with SNR.", encoding="utf-8")
            out = temp / "case"
            audit = out / "audit"
            writer_cmd = _write_mock_writer(temp)
            reviewer_cmd = _write_mock_reviewer(temp)

            import os

            old_writer = os.environ.get("GENG_CODEX_WRITER_CMD")
            old_reviewer = os.environ.get("GENG_CODEX_REVIEWER_CMD")
            os.environ["GENG_CODEX_WRITER_CMD"] = writer_cmd
            os.environ["GENG_CODEX_REVIEWER_CMD"] = reviewer_cmd
            try:
                result = run_codex_project_workflow(
                    facts={"engineering_facts": []},
                    tasks={"repro_tasks": [{"task_id": "reproduce_fig_1", "expected_artifacts": ["fig1.png"]}]},
                    experiment_index={"experiments": []},
                    paper={"format": "markdown", "chunks": []},
                    paper_path=paper,
                    paper_context_json="Figure 1 shows rate increasing with SNR.",
                    paper_thesis={"central_claim": "rate increases", "mechanism": "mock", "comparisons": []},
                    output_dir=out,
                    audit_dir=audit,
                    repro_project_dir=out / "repro_project",
                    client=DummyLLM(),
                    prompt_book=PromptBook(),
                    system_message="system",
                    run_repro=True,
                    result_review=True,
                    rounds=2,
                    timeout=30,
                    run_timeout=30,
                    resume=False,
                )
            finally:
                if old_writer is None:
                    os.environ.pop("GENG_CODEX_WRITER_CMD", None)
                else:
                    os.environ["GENG_CODEX_WRITER_CMD"] = old_writer
                if old_reviewer is None:
                    os.environ.pop("GENG_CODEX_REVIEWER_CMD", None)
                else:
                    os.environ["GENG_CODEX_REVIEWER_CMD"] = old_reviewer

            self.assertTrue(result["runtime_result"]["passed"])
            self.assertTrue(result["result_review_result"]["passed"])
            self.assertEqual(result["status"]["best_round"], 2)
            self.assertIn("def begin", (out / "repro_project" / "src" / "_io.py").read_text(encoding="utf-8"))
            self.assertIn("Auto-generated run-all dispatcher", (out / "repro_project" / "run_experiment.py").read_text(encoding="utf-8"))
            self.assertFalse((out / "repro_project" / "unexpected.py").exists())
            self.assertTrue((out / "result_review.json").exists())
            round2_brief = (audit / "03c_agentic_project_round_02_writer_brief.md").read_text(encoding="utf-8")
            self.assertIn("paper ordering is not matched yet", round2_brief)
            self.assertIn("mock modeling issue", round2_brief)

    def test_failed_reviewer_round_cannot_win_best_round_or_leave_stale_error(self) -> None:
        with TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            paper = temp / "paper.md"
            paper.write_text("Figure 1 shows rate increasing with SNR.", encoding="utf-8")
            out = temp / "case"
            audit = out / "audit"
            writer_cmd = _write_mock_writer(temp)
            reviewer_cmd = _write_failing_after_first_reviewer(temp)

            import os

            old_writer = os.environ.get("GENG_CODEX_WRITER_CMD")
            old_reviewer = os.environ.get("GENG_CODEX_REVIEWER_CMD")
            os.environ["GENG_CODEX_WRITER_CMD"] = writer_cmd
            os.environ["GENG_CODEX_REVIEWER_CMD"] = reviewer_cmd
            try:
                result = run_codex_project_workflow(
                    facts={"engineering_facts": []},
                    tasks={"repro_tasks": [{"task_id": "reproduce_fig_1", "expected_artifacts": ["fig1.png"]}]},
                    experiment_index={"experiments": []},
                    paper={"format": "markdown", "chunks": []},
                    paper_path=paper,
                    paper_context_json="Figure 1 shows rate increasing with SNR.",
                    paper_thesis={"central_claim": "rate increases", "mechanism": "mock", "comparisons": []},
                    output_dir=out,
                    audit_dir=audit,
                    repro_project_dir=out / "repro_project",
                    client=DummyLLM(),
                    prompt_book=PromptBook(),
                    system_message="system",
                    run_repro=True,
                    result_review=True,
                    rounds=2,
                    timeout=30,
                    run_timeout=30,
                    resume=False,
                )
            finally:
                if old_writer is None:
                    os.environ.pop("GENG_CODEX_WRITER_CMD", None)
                else:
                    os.environ["GENG_CODEX_WRITER_CMD"] = old_writer
                if old_reviewer is None:
                    os.environ.pop("GENG_CODEX_REVIEWER_CMD", None)
                else:
                    os.environ["GENG_CODEX_REVIEWER_CMD"] = old_reviewer

            self.assertEqual(result["status"]["best_round"], 1)
            self.assertTrue((out / "result_review.json").exists())
            self.assertFalse((out / "result_review_error.json").exists())
            final_review = json.loads((out / "result_review.json").read_text(encoding="utf-8"))
            self.assertEqual(final_review["experiment_reviews"][0]["scientific_verdict"], "does_not_support_paper_claim")

    def test_writer_brief_includes_dependency_snapshot_and_backend_runtime_api(self) -> None:
        with TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            paper = temp / "paper.md"
            paper.write_text("Figure 1 shows rate increasing with SNR.", encoding="utf-8")
            out = temp / "case"
            audit = out / "audit"
            writer_cmd = _write_mock_writer(temp)
            reviewer_cmd = _write_mock_reviewer(temp)

            import os

            old_writer = os.environ.get("GENG_CODEX_WRITER_CMD")
            old_reviewer = os.environ.get("GENG_CODEX_REVIEWER_CMD")
            os.environ["GENG_CODEX_WRITER_CMD"] = writer_cmd
            os.environ["GENG_CODEX_REVIEWER_CMD"] = reviewer_cmd
            try:
                run_codex_project_workflow(
                    facts={"engineering_facts": []},
                    tasks={"repro_tasks": [{"task_id": "reproduce_fig_1", "expected_artifacts": ["fig1.png"]}]},
                    experiment_index={"experiments": []},
                    paper={"format": "markdown", "chunks": []},
                    paper_path=paper,
                    paper_context_json="Figure 1 shows rate increasing with SNR.",
                    paper_thesis=None,
                    output_dir=out,
                    audit_dir=audit,
                    repro_project_dir=out / "repro_project",
                    client=DummyLLM(),
                    prompt_book=PromptBook(),
                    system_message="system",
                    run_repro=True,
                    result_review=True,
                    rounds=1,
                    timeout=30,
                    run_timeout=30,
                    resume=False,
                )
            finally:
                if old_writer is None:
                    os.environ.pop("GENG_CODEX_WRITER_CMD", None)
                else:
                    os.environ["GENG_CODEX_WRITER_CMD"] = old_writer
                if old_reviewer is None:
                    os.environ.pop("GENG_CODEX_REVIEWER_CMD", None)
                else:
                    os.environ["GENG_CODEX_REVIEWER_CMD"] = old_reviewer

            brief = (audit / "03c_agentic_project_round_01_writer_brief.md").read_text(encoding="utf-8")
            self.assertIn("Dependency policy snapshot", brief)
            self.assertIn("torch", brief)
            self.assertIn("src/_backend.py", brief)
            self.assertIn("_backend.select_backend", brief)

    def test_writer_brief_truncates_large_paper_context(self) -> None:
        brief = build_writer_brief(
            facts={"engineering_facts": []},
            tasks={"repro_tasks": [{"task_id": "t1"}]},
            experiment_index={"experiments": []},
            paper_context_json="x" * 60_000,
            paper_thesis=None,
            task_manifest={"version": 1, "tasks": [{"task_id": "t1", "module": "t1", "script": "tasks/t1.py"}]},
            expected_paths={"README.md", "tasks/t1.py"},
            feedback=[],
            round_no=1,
            max_rounds=3,
        )

        self.assertIn("paper_context_json truncated by geng-agent", brief)
        self.assertLess(len(brief), 60_000)

    def test_full_pipeline_codex_backend_generates_final_reports_with_fake_agents(self) -> None:
        with TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            paper = temp / "paper.md"
            paper.write_text("Simulation Results\nAWGN channel, BER vs SNR. Fig. 1 decreases.", encoding="utf-8")
            writer_cmd = _write_mock_writer(temp)
            reviewer_cmd = _write_mock_reviewer(temp)

            import os

            old_writer = os.environ.get("GENG_CODEX_WRITER_CMD")
            old_reviewer = os.environ.get("GENG_CODEX_REVIEWER_CMD")
            os.environ["GENG_CODEX_WRITER_CMD"] = writer_cmd
            os.environ["GENG_CODEX_REVIEWER_CMD"] = reviewer_cmd
            try:
                result = ReviewPipeline(client=MinimalPipelineLLM()).run(
                    paper,
                    temp / "case",
                    run_repro=True,
                    run_timeout=30,
                    repair_attempts=0,
                    result_review=True,
                    resume=False,
                    facts_gap_rounds=0,
                    tasks_gap_rounds=0,
                    project_backend="codex",
                    codex_agent_rounds=2,
                    codex_agent_timeout=30,
                )
            finally:
                if old_writer is None:
                    os.environ.pop("GENG_CODEX_WRITER_CMD", None)
                else:
                    os.environ["GENG_CODEX_WRITER_CMD"] = old_writer
                if old_reviewer is None:
                    os.environ.pop("GENG_CODEX_REVIEWER_CMD", None)
                else:
                    os.environ["GENG_CODEX_REVIEWER_CMD"] = old_reviewer

            self.assertTrue(result.runtime_passed)
            self.assertTrue(result.result_review_passed)
            self.assertIsNotNone(result.result_review_path)
            self.assertIsNotNone(result.result_review_docx_path)
            generated = json.loads((result.output_dir / "generated_files.json").read_text(encoding="utf-8"))
            self.assertTrue(generated["result_review"]["passed"])
            self.assertEqual(generated["docx_generation"]["result_review_docx"]["passed"], True)

    def test_writer_transport_failure_after_edits_is_explicitly_annotated(self) -> None:
        with TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            (temp / "writer_exit_1.flag").write_text("fail after edits", encoding="utf-8")
            paper = temp / "paper.md"
            paper.write_text("Figure 1 shows rate increasing with SNR.", encoding="utf-8")
            out = temp / "case"
            audit = out / "audit"
            writer_cmd = _write_mock_writer(temp)
            reviewer_cmd = _write_mock_reviewer(temp)

            import os

            old_writer = os.environ.get("GENG_CODEX_WRITER_CMD")
            old_reviewer = os.environ.get("GENG_CODEX_REVIEWER_CMD")
            os.environ["GENG_CODEX_WRITER_CMD"] = writer_cmd
            os.environ["GENG_CODEX_REVIEWER_CMD"] = reviewer_cmd
            try:
                result = run_codex_project_workflow(
                    facts={"engineering_facts": []},
                    tasks={"repro_tasks": [{"task_id": "reproduce_fig_1", "expected_artifacts": ["fig1.png"]}]},
                    experiment_index={"experiments": []},
                    paper={"format": "markdown", "chunks": []},
                    paper_path=paper,
                    paper_context_json="Figure 1 shows rate increasing with SNR.",
                    paper_thesis=None,
                    output_dir=out,
                    audit_dir=audit,
                    repro_project_dir=out / "repro_project",
                    client=DummyLLM(),
                    prompt_book=PromptBook(),
                    system_message="system",
                    run_repro=True,
                    result_review=True,
                    rounds=1,
                    timeout=30,
                    run_timeout=30,
                    resume=False,
                )
            finally:
                if old_writer is None:
                    os.environ.pop("GENG_CODEX_WRITER_CMD", None)
                else:
                    os.environ["GENG_CODEX_WRITER_CMD"] = old_writer
                if old_reviewer is None:
                    os.environ.pop("GENG_CODEX_REVIEWER_CMD", None)
                else:
                    os.environ["GENG_CODEX_REVIEWER_CMD"] = old_reviewer

            writer = result["status"]["rounds"][0]["writer"]
            self.assertFalse(writer["ok"])
            self.assertTrue(writer["transport_failed_after_edits"])
            self.assertTrue((audit / "03c_agentic_project_round_01_writer_post_run.json").exists())


if __name__ == "__main__":
    unittest.main()
