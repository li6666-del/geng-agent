from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from geng_agent.pipeline import _select_workflow_version


class WorkflowVersionTests(unittest.TestCase):
    def test_legacy_resume_is_pinned_to_v1_and_no_resume_rebuilds_as_v2(self) -> None:
        with TemporaryDirectory() as temp:
            case = Path(temp) / "case"
            case.mkdir()
            (case / "engineering_facts.json").write_text("{}\n", encoding="utf-8")

            self.assertEqual(_select_workflow_version(case, resume=True), "1")
            marker = json.loads((case / "workflow.json").read_text(encoding="utf-8"))
            self.assertEqual(marker["workflow_version"], "1")

            (case / "engineering_facts.json").unlink()
            self.assertEqual(_select_workflow_version(case, resume=True), "1")
            self.assertEqual(_select_workflow_version(case, resume=False), "2")
            marker = json.loads((case / "workflow.json").read_text(encoding="utf-8"))
            self.assertEqual(marker["workflow_version"], "2")
            self.assertEqual(marker["architecture_contract"], "scientific_architecture/1.1")


if __name__ == "__main__":
    unittest.main()
