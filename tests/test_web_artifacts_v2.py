from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

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
        self.assertEqual(phase_for_path("engineering_facts.json"), "paper_analysis")
        self.assertEqual(phase_for_path("repro_project/outputs/figure.png"), "project_build")
        self.assertEqual(phase_for_path("review.docx"), "evidence_review")


if __name__ == "__main__":
    unittest.main()
