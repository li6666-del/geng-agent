from __future__ import annotations

import unittest

from geng_agent.web.stages import build_stage_progress


class WebStagesTests(unittest.TestCase):
    def test_build_stage_progress_running(self) -> None:
        inspect = {
            "next_stage": "repro_tasks",
            "stages": [
                {"stage": "paper", "ok": True},
                {"stage": "engineering_facts", "ok": True},
                {"stage": "repro_tasks", "ok": False},
            ],
        }
        rows = build_stage_progress(inspect)
        by_id = {row["id"]: row for row in rows}
        self.assertEqual(by_id["paper"]["state"], "done")
        self.assertEqual(by_id["engineering_facts"]["state"], "done")
        self.assertEqual(by_id["repro_tasks"]["state"], "running")
        self.assertEqual(by_id["runtime"]["state"], "pending")


if __name__ == "__main__":
    unittest.main()