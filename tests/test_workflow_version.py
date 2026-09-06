from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from geng_agent.pipeline import (
    ReviewPipeline,
    UnsupportedWorkflowVersionError,
    _ensure_v2_workflow,
)


class V2WorkflowTests(unittest.TestCase):
    def test_new_case_writes_the_only_supported_workflow(self) -> None:
        with TemporaryDirectory() as temp:
            case = Path(temp) / "case"
            case.mkdir()

            _ensure_v2_workflow(case)

            marker = json.loads((case / "workflow.json").read_text(encoding="utf-8"))
            self.assertEqual(marker["workflow_version"], "2")
            self.assertEqual(
                marker["architecture_contract"],
                "scientific_architecture/advisory-1.1",
            )

    def test_existing_v2_case_remains_supported(self) -> None:
        with TemporaryDirectory() as temp:
            case = Path(temp) / "case"
            case.mkdir()
            (case / "workflow.json").write_text(
                json.dumps({"workflow_version": "2"}),
                encoding="utf-8",
            )

            _ensure_v2_workflow(case)

            marker = json.loads((case / "workflow.json").read_text(encoding="utf-8"))
            self.assertEqual(marker["workflow_version"], "2")

    def test_unsupported_marker_is_rejected_instead_of_rewritten(self) -> None:
        with TemporaryDirectory() as temp:
            case = Path(temp) / "case"
            case.mkdir()
            original = json.dumps({"workflow_version": "unsupported"})
            (case / "workflow.json").write_text(original, encoding="utf-8")

            with self.assertRaises(UnsupportedWorkflowVersionError):
                _ensure_v2_workflow(case)

            self.assertEqual((case / "workflow.json").read_text(encoding="utf-8"), original)

    def test_markerless_case_with_pipeline_artifacts_is_rejected(self) -> None:
        with TemporaryDirectory() as temp:
            case = Path(temp) / "case"
            case.mkdir()
            (case / "engineering_facts.json").write_text("{}\n", encoding="utf-8")

            with self.assertRaises(UnsupportedWorkflowVersionError):
                _ensure_v2_workflow(case)

            self.assertFalse((case / "workflow.json").exists())

    def test_stage_restart_rejects_unsupported_case_before_cleanup(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            case = root / "case"
            case.mkdir()
            (case / "workflow.json").write_text(
                json.dumps({"workflow_version": "unsupported"}),
                encoding="utf-8",
            )
            preserved = case / "runtime_result.json"
            preserved.write_text('{"completed": true}\n', encoding="utf-8")

            with self.assertRaises(UnsupportedWorkflowVersionError):
                ReviewPipeline().run_stage(
                    "facts",
                    paper_path=root / "paper.pdf",
                    output_dir=case,
                )

            self.assertTrue(preserved.is_file())


if __name__ == "__main__":
    unittest.main()
