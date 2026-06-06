from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from geng_agent.documents import load_paper, split_text


class DocumentTests(unittest.TestCase):
    def test_split_text_overlaps_long_input(self) -> None:
        text = "a" * 7000
        chunks = split_text(text, max_chars=3000, overlap=100)

        self.assertGreaterEqual(len(chunks), 3)
        self.assertLessEqual(len(chunks[0]), 3000)

    def test_load_markdown_paper(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "paper.md"
            path.write_text("# Simulation Results\nBER vs SNR is evaluated.", encoding="utf-8")

            paper = load_paper(path)

            self.assertEqual(paper["format"], "md")
            self.assertEqual(paper["chunk_count"], 1)
            self.assertIn("BER", paper["chunks"][0]["text"])


if __name__ == "__main__":
    unittest.main()
