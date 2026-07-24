from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from geng_agent.pipeline import _select_workflow_version


class WorkflowVersionTests(unittest.TestCase):
    def test_every_run_uses_v2_without_legacy_case_compatibility(self) -> None:
        with TemporaryDirectory() as temp:
            case = Path(temp) / "case"
            case.mkdir()
            (case / "engineering_facts.json").write_text("{}\n", encoding="utf-8")
            (case / "workflow.json").write_text(
                json.dumps({"workflow_version": "1"}),
                encoding="utf-8",
            )

            self.assertEqual(_select_workflow_version(case, resume=True), "2")
            marker = json.loads((case / "workflow.json").read_text(encoding="utf-8"))
            self.assertEqual(marker["workflow_version"], "2")
            self.assertEqual(
                marker["architecture_contract"],
                "scientific_architecture/advisory-1.1",
            )
            self.assertFalse(marker["legacy_case_compatibility"])

            (case / "engineering_facts.json").unlink()
            self.assertEqual(_select_workflow_version(case, resume=True), "2")
            self.assertEqual(_select_workflow_version(case, resume=False), "2")
if __name__ == "__main__":
    unittest.main()
