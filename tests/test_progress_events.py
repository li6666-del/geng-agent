from __future__ import annotations

import unittest

from geng_agent.progress import (
    CallbackProgressReporter,
    PHASES,
    PhaseProgressTracker,
    PipelineCancelled,
    phase_for_step,
)


class ProgressEventsTests(unittest.TestCase):
    def test_five_user_phases_cover_pipeline_steps(self) -> None:
        self.assertEqual(len(PHASES), 5)
        self.assertEqual(phase_for_step("paper"), "paper_analysis")
        self.assertEqual(phase_for_step("runtime"), "task_reproduction")
        self.assertEqual(phase_for_step("review"), "report_composition")
        self.assertEqual(phase_for_step("reports"), "report_delivery")

    def test_callback_payload_and_cooperative_cancel(self) -> None:
        events: list[dict] = []
        reporter = CallbackProgressReporter(events.append, cancelled=lambda: False)
        reporter.emit("phase.started", phase="paper_analysis", message="start")
        self.assertEqual(events[0]["phase"], "paper_analysis")

        cancelled = CallbackProgressReporter(events.append, cancelled=lambda: True)
        with self.assertRaises(PipelineCancelled):
            cancelled.check_cancelled()

    def test_tracker_moves_forward_through_current_pipeline_phases(self) -> None:
        events: list[dict] = []
        tracker = PhaseProgressTracker(CallbackProgressReporter(events.append))
        for step in ("start", "facts_initial", "tasks_preliminary", "facts", "generation", "task_reporters", "report_editor", "reports"):
            tracker.complete(step)
        tracker.finish()

        started = [event["phase"] for event in events if event["type"] == "phase.started"]
        self.assertEqual(started, [phase_id for phase_id, _label, _steps in PHASES])


if __name__ == "__main__":
    unittest.main()
