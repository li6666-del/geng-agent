import contextlib
import io
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from geng_agent.cli import build_parser


class CliDefaultsTests(unittest.TestCase):
    def test_exit_status_distinguishes_delivery_failure_from_unreproduced_science(self) -> None:
        from geng_agent.cli import main
        from geng_agent.pipeline_models import PipelineResult
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            for status, code, message in (
                ("complete", 0, "审查完成"),
                ("partial", 1, "已保留部分交付"),
                ("blocked", 1, "运行受阻"),
            ):
                with self.subTest(status=status):
                    result = PipelineResult(output_dir=root, review_path=root / "review.md",
                        repro_project_dir=root / "project", risk_report_path=root / "risk.json",
                        runtime_passed=False, result_review_passed=False,
                        delivery_status=status, supervision_path=root / "supervisor.json")
                    stdout = io.StringIO()
                    with patch("geng_agent.pipeline.ReviewPipeline.run", return_value=result), \
                         patch("geng_agent.cli._resolve_case_path_or_error", return_value=root), \
                         patch("geng_agent.cli._warn_if_environment_incomplete"), \
                         contextlib.redirect_stdout(stdout):
                        actual = main(["review", "paper.md", "--out", str(root)])
                    self.assertEqual(actual, code)
                    self.assertIn(message, stdout.getvalue())
                    self.assertIn("主持人运行记录", stdout.getvalue())
                    if status != "complete":
                        self.assertNotIn("审查完成", stdout.getvalue())

    def test_analysis_only_does_not_claim_completion_when_blocked(self) -> None:
        from geng_agent.cli import main
        from geng_agent.pipeline_models import PipelineResult
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = PipelineResult(output_dir=root, review_path=root / "review.md",
                repro_project_dir=root / "project", risk_report_path=root / "risk.json",
                delivery_status="blocked", supervision_path=root / "supervisor.json")
            stdout = io.StringIO()
            with patch("geng_agent.pipeline.ReviewPipeline.run", return_value=result), \
                 patch("geng_agent.cli._resolve_case_path_or_error", return_value=root), \
                 patch("geng_agent.cli._warn_if_environment_incomplete"), \
                 contextlib.redirect_stdout(stdout):
                code = main(["review", "paper.md", "--out", str(root), "--analysis-only"])
            self.assertEqual(code, 1)
            self.assertIn("分析尚未完成", stdout.getvalue())
            self.assertNotIn("前两阶段完成", stdout.getvalue())

    def test_review_keeps_codex_moderator_defaults(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["review", "paper.pdf", "--out", "case"])
        self.assertEqual(args.analysis_backend, "codex")
        self.assertFalse(hasattr(args, "project_timeout"))
        self.assertFalse(hasattr(args, "codex_analysis_timeout"))
        self.assertFalse(hasattr(args, "codex_agent_timeout"))
        self.assertFalse(hasattr(args, "codex_reporter_timeout"))
        self.assertEqual(args.mineru_timeout, 1800.0)
        self.assertEqual(args.json_repair_attempts, 1)
        self.assertFalse(args.analysis_only)
        self.assertFalse(hasattr(args, "no_result_review"))
        self.assertFalse(hasattr(args, "analysis_agent_width"))
        self.assertFalse(hasattr(args, "codex_agent_rounds"))

    def test_review_help_no_longer_exposes_legacy_third_round_switches(self) -> None:
        parser = build_parser()
        stdout = io.StringIO()
        with self.assertRaises(SystemExit) as raised, contextlib.redirect_stdout(stdout):
            parser.parse_args(["review", "--help"])

        self.assertEqual(raised.exception.code, 0)
        help_text = stdout.getvalue()
        self.assertIn("--analysis-backend", help_text)
        self.assertIn("--analysis-only", help_text)
        self.assertNotIn("--analysis-agent-width", help_text)
        self.assertNotIn("--project-timeout", help_text)
        self.assertNotIn("--codex-analysis-timeout", help_text)
        self.assertNotIn("--codex-agent-rounds", help_text)
        self.assertNotIn("--codex-agent-timeout", help_text)
        self.assertNotIn("--codex-reporter-timeout", help_text)
        self.assertIn("--timeout", help_text)
        self.assertIn("--tasks-timeout", help_text)
        self.assertIn("--run-timeout", help_text)
        self.assertIn("--mineru-timeout", help_text)
        self.assertNotIn("--no-result-review", help_text)
        self.assertNotIn("--codex-agent-stall-rounds", help_text)
        self.assertNotIn("--codex-agent-mode", help_text)
        self.assertNotIn("--project-backend", help_text)
        self.assertNotIn("--science-loop", help_text)
        self.assertNotIn("--no-template-fallback", help_text)
        self.assertIn("--no-analysis-fallback", help_text)
        self.assertNotIn("--generation-model", help_text)
        self.assertNotIn("--per-task-layout", help_text)
        self.assertNotIn("--science-repair-backend", help_text)

    def test_removed_analysis_width_option_is_rejected(self) -> None:
        parser = build_parser()
        stderr = io.StringIO()
        with self.assertRaises(SystemExit) as raised, contextlib.redirect_stderr(stderr):
            parser.parse_args(["review", "paper.pdf", "--out", "case", "--analysis-agent-width", "9"])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("unrecognized arguments", stderr.getvalue())

    def test_removed_codex_wall_clock_options_are_rejected(self) -> None:
        parser = build_parser()
        for option in (
            "--project-timeout",
            "--codex-analysis-timeout",
            "--codex-agent-timeout",
            "--codex-reporter-timeout",
        ):
            with self.subTest(option=option):
                stderr = io.StringIO()
                with self.assertRaises(SystemExit) as raised, contextlib.redirect_stderr(stderr):
                    parser.parse_args(["review", "paper.pdf", "--out", "case", option, "30"])
                self.assertEqual(raised.exception.code, 2)
                self.assertIn("unrecognized arguments", stderr.getvalue())

    def test_benchmark_accepts_multiple_case_directories(self) -> None:
        args = build_parser().parse_args(["benchmark", "case_a", "case_b", "--out", "report"])
        self.assertEqual([path.name for path in args.cases], ["case_a", "case_b"])
        self.assertEqual(args.out.name, "report")


if __name__ == "__main__":
    unittest.main()
