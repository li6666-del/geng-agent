from __future__ import annotations

import unittest

from geng_agent.web.stages import build_stage_progress


class WebStagesTests(unittest.TestCase):
    def test_build_stage_progress_folds_internal_steps_into_five_phases(self) -> None:
        inspect = {
            "next_stage": "repro_tasks",
            "stages": [
                {"stage": "paper", "ok": True},
                {"stage": "engineering_facts", "ok": True},
                {"stage": "repro_tasks", "ok": False},
            ],
        }
        rows = build_stage_progress(inspect)
        self.assertEqual(len(rows), 5)
        by_id = {row["id"]: row for row in rows}
        self.assertEqual(by_id["paper_analysis"]["state"], "success")
        self.assertEqual(by_id["repro_design"]["state"], "running")
        self.assertEqual(by_id["execution"]["state"], "waiting")


if __name__ == "__main__":
    unittest.main()
