import contextlib
import io
import unittest

from geng_agent.cli import build_parser


class CliDefaultsTests(unittest.TestCase):
    def test_review_keeps_codex_moderator_defaults(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["review", "paper.pdf", "--out", "case"])
        self.assertEqual(args.codex_agent_rounds, 3)
        self.assertIsNone(args.codex_agent_timeout)

    def test_review_help_no_longer_exposes_legacy_third_round_switches(self) -> None:
        parser = build_parser()
        stdout = io.StringIO()
        with self.assertRaises(SystemExit) as raised, contextlib.redirect_stdout(stdout):
            parser.parse_args(["review", "--help"])

        self.assertEqual(raised.exception.code, 0)
        help_text = stdout.getvalue()
        self.assertIn("--codex-agent-rounds", help_text)
        self.assertIn("--codex-agent-timeout", help_text)
        self.assertNotIn("--project-backend", help_text)
        self.assertNotIn("--generation-model", help_text)
        self.assertNotIn("--per-task-layout", help_text)
        self.assertNotIn("--science-repair-backend", help_text)


if __name__ == "__main__":
    unittest.main()
