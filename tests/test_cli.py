import contextlib
import io
import unittest

from geng_agent.cli import build_parser


class CliDefaultsTests(unittest.TestCase):
    def test_review_keeps_codex_moderator_defaults(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["review", "paper.pdf", "--out", "case"])
        self.assertEqual(args.analysis_backend, "codex")
        self.assertIsNone(args.codex_analysis_timeout)
        self.assertIsNone(args.codex_agent_timeout)
        self.assertIsNone(args.codex_reporter_timeout)
        self.assertEqual(args.mineru_timeout, 1800.0)
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
        self.assertIn("Writer 自治迭代不设内部时间上限", help_text)
        self.assertIn("--analysis-backend", help_text)
        self.assertIn("--analysis-only", help_text)
        self.assertNotIn("--analysis-agent-width", help_text)
        self.assertIn("--codex-analysis-timeout", help_text)
        self.assertNotIn("--codex-agent-rounds", help_text)
        self.assertIn("--codex-agent-timeout", help_text)
        self.assertIn("--codex-reporter-timeout", help_text)
        self.assertIn("--mineru-timeout", help_text)
        self.assertNotIn("--no-result-review", help_text)
        self.assertIn("当前自治 Writer", help_text)
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

    def test_benchmark_accepts_multiple_case_directories(self) -> None:
        args = build_parser().parse_args(["benchmark", "case_a", "case_b", "--out", "report"])
        self.assertEqual([path.name for path in args.cases], ["case_a", "case_b"])
        self.assertEqual(args.out.name, "report")


if __name__ == "__main__":
    unittest.main()
