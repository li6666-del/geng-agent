from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from geng_agent.paper_memory import (
    build_paper_memory,
    load_or_build_paper_memory,
    write_memory_manifest,
)
from geng_agent.schemas import validate_stage


class PaperMemoryTests(unittest.TestCase):
    def test_builds_subfigure_and_cross_reference_entities(self) -> None:
        with TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "paper.md"
            source.write_text("paper", encoding="utf-8")
            paper = {
                "format": "md",
                "chunks": [
                    {
                        "chunk_id": "c1",
                        "page": 9,
                        "section": "Results",
                        "text": "Fig. 9(a) shows BER; Fig. 9(b) uses Eq. (31) and Table II.",
                    }
                ],
            }

            memory = build_paper_memory(paper, source)

            ids = {item["entity_id"] for item in memory["entities"]}
            self.assertIn("fig:9", ids)
            self.assertIn("fig:9:a", ids)
            self.assertIn("fig:9:b", ids)
            self.assertIn("equation:31", ids)
            self.assertIn("table:II", ids)
            self.assertEqual(validate_stage("paper_memory", memory), [])
            self.assertTrue(any(item["relation"] == "contains" for item in memory["cross_references"]))

    def test_cache_rebuilds_when_source_changes(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "paper.md"
            source.write_text("v1", encoding="utf-8")
            paper = {"format": "md", "chunks": [{"chunk_id": "c1", "text": "Fig. 1 BER"}]}
            first = load_or_build_paper_memory(paper=paper, source_path=source, output_dir=root, resume=True)
            source.write_text("v2", encoding="utf-8")
            second = load_or_build_paper_memory(paper=paper, source_path=source, output_dir=root, resume=True)
            self.assertNotEqual(first["source"]["sha256"], second["source"]["sha256"])

    def test_memory_manifest_hash_changes_with_artifact(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            artifact = root / "facts.json"
            artifact.write_text("{}", encoding="utf-8")
            first = write_memory_manifest(root, {"facts": artifact})
            artifact.write_text('{"changed": true}', encoding="utf-8")
            second = write_memory_manifest(root, {"facts": artifact})
            self.assertNotEqual(first["snapshot_hash"], second["snapshot_hash"])


if __name__ == "__main__":
    unittest.main()
