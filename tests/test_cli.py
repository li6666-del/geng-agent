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
        self.assertEqual(args.facts_gap_rounds, 6)
        self.assertEqual(args.tasks_gap_rounds, 6)
        self.assertEqual(args.analysis_agent_width, 2)
        self.assertEqual(args.codex_agent_rounds, 5)
        self.assertIsNone(args.codex_agent_timeout)

    def test_review_help_no_longer_exposes_legacy_third_round_switches(self) -> None:
        parser = build_parser()
        stdout = io.StringIO()
        with self.assertRaises(SystemExit) as raised, contextlib.redirect_stdout(stdout):
            parser.parse_args(["review", "--help"])

        self.assertEqual(raised.exception.code, 0)
        help_text = stdout.getvalue()
        self.assertIn("单个 task writer 子进程", help_text)
        self.assertIn("--analysis-backend", help_text)
        self.assertIn("--analysis-agent-width", help_text)
        self.assertIn("--codex-analysis-timeout", help_text)
        self.assertIn("--codex-agent-rounds", help_text)
        self.assertIn("--codex-agent-timeout", help_text)
        self.assertIn("自治 task writer", help_text)
        self.assertNotIn("--codex-agent-stall-rounds", help_text)
        self.assertNotIn("--codex-agent-mode", help_text)
        self.assertNotIn("--project-backend", help_text)
        self.assertNotIn("--science-loop", help_text)
        self.assertNotIn("--no-template-fallback", help_text)
        self.assertIn("--no-analysis-fallback", help_text)
        self.assertNotIn("--generation-model", help_text)
        self.assertNotIn("--per-task-layout", help_text)
        self.assertNotIn("--science-repair-backend", help_text)

    def test_analysis_agent_width_rejects_unbounded_parallelism(self) -> None:
        parser = build_parser()
        stderr = io.StringIO()
        with self.assertRaises(SystemExit) as raised, contextlib.redirect_stderr(stderr):
            parser.parse_args(["review", "paper.pdf", "--out", "case", "--analysis-agent-width", "9"])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("analysis_agent_width must be between 1 and 8", stderr.getvalue())

    def test_benchmark_accepts_multiple_case_directories(self) -> None:
        args = build_parser().parse_args(["benchmark", "case_a", "case_b", "--out", "report"])
        self.assertEqual([path.name for path in args.cases], ["case_a", "case_b"])
        self.assertEqual(args.out.name, "report")


if __name__ == "__main__":
    unittest.main()
