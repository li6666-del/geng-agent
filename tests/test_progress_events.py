from __future__ import annotations

import unittest

from geng_agent.progress import CallbackProgressReporter, PHASES, PipelineCancelled, phase_for_step


class ProgressEventsTests(unittest.TestCase):
    def test_five_user_phases_cover_pipeline_steps(self) -> None:
        self.assertEqual(len(PHASES), 5)
        self.assertEqual(phase_for_step("paper"), "paper_analysis")
        self.assertEqual(phase_for_step("runtime"), "execution")
        self.assertEqual(phase_for_step("review"), "evidence_review")

    def test_callback_payload_and_cooperative_cancel(self) -> None:
        events: list[dict] = []
        reporter = CallbackProgressReporter(events.append, cancelled=lambda: False)
        reporter.emit("phase.started", phase="paper_analysis", message="start")
        self.assertEqual(events[0]["phase"], "paper_analysis")

        cancelled = CallbackProgressReporter(events.append, cancelled=lambda: True)
        with self.assertRaises(PipelineCancelled):
            cancelled.check_cancelled()


if __name__ == "__main__":
    unittest.main()
