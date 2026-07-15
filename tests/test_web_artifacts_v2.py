from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests import web_test_env  # noqa: F401

from geng_agent.web.artifacts import LocalArtifactStore, UnsafeArtifactPath, artifact_kind, phase_for_path


class ArtifactStoreTests(unittest.TestCase):
    def test_resolve_accepts_case_file_and_blocks_escape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "review.md"
            target.write_text("ok", encoding="utf-8")
            store = LocalArtifactStore(root)
            self.assertEqual(store.resolve("review.md"), target.resolve())
            with self.assertRaises(UnsafeArtifactPath):
                store.resolve("../secret.txt", must_exist=False)
            with self.assertRaises(UnsafeArtifactPath):
                store.resolve(str(target.resolve()), must_exist=False)

    def test_artifact_classification_and_phase(self) -> None:
        self.assertEqual(artifact_kind(Path("plot.png")), "image")
        self.assertEqual(artifact_kind(Path("results.csv")), "csv")
        self.assertEqual(phase_for_path("engineering_facts_initial.json"), "paper_analysis")
        self.assertEqual(phase_for_path("engineering_facts.json"), "repro_design")
        self.assertEqual(phase_for_path("repro_project/outputs/figure.png"), "task_reproduction")
        self.assertEqual(phase_for_path("review.md"), "report_composition")
        self.assertEqual(phase_for_path("review.docx"), "report_delivery")

    def test_catalog_iteration_skips_bulk_audit_and_exports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "review.md").write_text("ok", encoding="utf-8")
            (root / "audit").mkdir()
            (root / "audit" / "transcript.txt").write_text("large trace", encoding="utf-8")
            (root / "exports").mkdir()
            (root / "exports" / "case.zip").write_bytes(b"zip")

            relative = {
                path.relative_to(root).as_posix()
                for path in LocalArtifactStore(root).iter_files()
            }
            self.assertEqual(relative, {"review.md"})


if __name__ == "__main__":
    unittest.main()
