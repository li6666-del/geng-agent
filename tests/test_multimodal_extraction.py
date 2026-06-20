from __future__ import annotations

import unittest
from pathlib import Path

from geng_agent.documents import load_paper
from geng_agent.llm import LLMImage
from geng_agent.pipeline import ReviewPipeline

SAMPLE_PDF = Path(__file__).resolve().parents[1] / "sample_papers" / "rayleigh_error_probability_2406.16548.pdf"
ONE_IMAGE = [LLMImage(label="paper_page:1", mime_type="image/png", data_b64="QQ==")]


class TextOnlyClient:
    """A client that only implements complete() (no multimodal support)."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def complete(self, prompt, *, system=None, response_format=None) -> str:
        self.calls.append("complete")
        return '{"ok": true}'


class MultimodalClient(TextOnlyClient):
    def __init__(self, fail: bool = False) -> None:
        super().__init__()
        self.fail = fail

    def complete_multimodal(self, prompt, *, images, system=None, response_format=None) -> str:
        self.calls.append("multimodal")
        if self.fail:
            raise RuntimeError("multimodal endpoint boom")
        return '{"ok": true}'


class CompleteMaybeMultimodalTests(unittest.TestCase):
    def _call(self, client, images):
        pipeline = ReviewPipeline(client=client)
        pipeline._complete_maybe_multimodal("prompt", schema_stage="engineering_facts", images=images)
        return client.calls

    def test_uses_multimodal_when_images_and_supported(self) -> None:
        self.assertEqual(self._call(MultimodalClient(), ONE_IMAGE), ["multimodal"])

    def test_text_only_when_no_images(self) -> None:
        self.assertEqual(self._call(MultimodalClient(), []), ["complete"])

    def test_text_only_when_client_has_no_multimodal(self) -> None:
        self.assertEqual(self._call(TextOnlyClient(), ONE_IMAGE), ["complete"])

    def test_falls_back_to_text_when_multimodal_raises(self) -> None:
        self.assertEqual(self._call(MultimodalClient(fail=True), ONE_IMAGE), ["multimodal", "complete"])


class RenderPaperImagesTests(unittest.TestCase):
    def test_empty_for_non_pdf(self) -> None:
        pipeline = ReviewPipeline(client=MultimodalClient())
        self.assertEqual(pipeline._render_paper_images(paper_path=Path("x.md"), paper={"format": "md"}), [])

    def test_empty_when_client_text_only(self) -> None:
        pipeline = ReviewPipeline(client=TextOnlyClient())
        self.assertEqual(pipeline._render_paper_images(paper_path=SAMPLE_PDF, paper={"format": "pdf"}), [])

    def test_renders_without_client_for_codex_backend(self) -> None:
        if not SAMPLE_PDF.exists():
            self.skipTest("sample PDF not available")
        try:
            import fitz  # noqa: F401
            from PIL import Image  # noqa: F401
        except ImportError:
            self.skipTest("pymupdf/Pillow not installed")
        images = ReviewPipeline(client=None)._render_paper_images(
            paper_path=SAMPLE_PDF, paper={"format": "pdf"}
        )
        self.assertGreaterEqual(len(images), 1)
        self.assertEqual(images[0].mime_type, "image/png")

    def test_real_pdf_renders_images(self) -> None:
        if not SAMPLE_PDF.exists():
            self.skipTest("sample PDF not available")
        try:
            import fitz  # noqa: F401
            from PIL import Image  # noqa: F401
        except ImportError:
            self.skipTest("pymupdf/Pillow not installed")
        images = ReviewPipeline(client=MultimodalClient())._render_paper_images(
            paper_path=SAMPLE_PDF, paper={"format": "pdf"}
        )
        self.assertGreaterEqual(len(images), 1)
        self.assertEqual(images[0].mime_type, "image/png")
        self.assertTrue(images[0].data_b64)


class FitzChunkingTests(unittest.TestCase):
    def test_load_pdf_chunks_with_pages(self) -> None:
        if not SAMPLE_PDF.exists():
            self.skipTest("sample PDF not available")
        try:
            import fitz  # noqa: F401
        except ImportError:
            self.skipTest("pymupdf not installed")
        paper = load_paper(SAMPLE_PDF)
        self.assertEqual(paper["format"], "pdf")
        self.assertGreaterEqual(paper["chunk_count"], 1)
        self.assertTrue(all(isinstance(chunk["page"], int) for chunk in paper["chunks"]))


if __name__ == "__main__":
    unittest.main()
