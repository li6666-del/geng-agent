from pathlib import Path
from tempfile import TemporaryDirectory
import re
import unittest
from unittest.mock import patch

from geng_agent.documents import load_paper, split_text
from geng_agent.facts_normalize import finalize_engineering_facts, select_valid_engineering_facts
from geng_agent.pipeline import build_risk_report
from geng_agent.review_markdown import render_review_markdown


class DocumentTests(unittest.TestCase):
    SAMPLE_PDF = Path(__file__).resolve().parents[1] / "sample_papers" / "rayleigh_error_probability_2406.16548.pdf"

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

    # --- new tests for review suggestion #2: chunking, chunk_id/page traceability, papers-with-figures sim, risk injection ---

    def test_load_text_paper_uses_text_chunk_ids_and_null_pages(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "paper.txt"
            path.write_text("Abstract\nThis is intro text.\n\nResults\nMore content here for chunking if long enough.", encoding="utf-8")
            paper = load_paper(path)
            self.assertEqual(paper["format"], "txt")
            self.assertGreaterEqual(paper["chunk_count"], 1)
            for chunk in paper["chunks"]:
                self.assertTrue(chunk["chunk_id"].startswith("text_c"))
                self.assertIsNone(chunk["page"])
                self.assertIn("section", chunk)  # may be None or str

    def test_load_pdf_paper_assigns_page_and_p_chunk_ids_when_available(self) -> None:
        if not self.SAMPLE_PDF.exists():
            self.skipTest("sample PDF not available")
        try:
            import fitz  # noqa: F401
        except ImportError:
            self.skipTest("pymupdf not installed")
        paper = load_paper(self.SAMPLE_PDF)
        self.assertEqual(paper["format"], "pdf")
        self.assertGreaterEqual(paper["chunk_count"], 1)
        chunk_ids = [c["chunk_id"] for c in paper["chunks"]]
        pages = [c["page"] for c in paper["chunks"]]
        # chunk_id format for PDF: p{page}_c{index}
        self.assertTrue(all(re.match(r"^p\d+_c\d+$", cid) for cid in chunk_ids))
        self.assertTrue(all(isinstance(p, int) and p >= 1 for p in pages))
        # pages should be non-decreasing, and cover at least first page
        self.assertIn(1, pages)
        # text is present and stripped
        self.assertTrue(all(len(c["text"]) > 0 for c in paper["chunks"]))

    def test_load_pdf_respects_max_pages(self) -> None:
        if not self.SAMPLE_PDF.exists():
            self.skipTest("sample PDF not available")
        try:
            import fitz  # noqa: F401
        except ImportError:
            self.skipTest("pymupdf not installed")
        full = load_paper(self.SAMPLE_PDF)
        limited = load_paper(self.SAMPLE_PDF, max_pages=1)
        self.assertEqual(limited["format"], "pdf")
        # limited should have chunks only from page 1
        limited_pages = {c["page"] for c in limited["chunks"]}
        self.assertEqual(limited_pages, {1})
        self.assertLessEqual(limited["chunk_count"], full["chunk_count"])
        # first chunk of limited should match first of full if any
        if full["chunks"] and limited["chunks"]:
            self.assertEqual(limited["chunks"][0]["page"], 1)

    def test_pdf_chunk_ids_traceable_and_unique(self) -> None:
        if not self.SAMPLE_PDF.exists():
            self.skipTest("sample PDF not available")
        try:
            import fitz  # noqa: F401
        except ImportError:
            self.skipTest("pymupdf not installed")
        paper = load_paper(self.SAMPLE_PDF, max_pages=2)
        chunk_ids = [c["chunk_id"] for c in paper["chunks"]]
        self.assertEqual(len(chunk_ids), len(set(chunk_ids)), "chunk_ids must be unique")
        # per-page chunks should be sequential starting at c1
        pages_to_chunks = {}
        for c in paper["chunks"]:
            p = c["page"]
            pages_to_chunks.setdefault(p, []).append(c["chunk_id"])
        for p, cids in pages_to_chunks.items():
            # extract the cN suffix
            suffixes = [int(cid.split("_c")[1]) for cid in cids]
            self.assertEqual(suffixes, list(range(1, len(suffixes) + 1)))

    def test_document_chunks_provide_traceability_for_facts_validation(self) -> None:
        """Simulate paper (PDF) load -> use its chunks to validate text facts (chunk_id) and figure pages.
        Directly addresses: papers with figures (figure facts use page), chunk_id/page traceability.
        Note: load_paper always text-only; figures compensated only via separate multimodal render + page set.
        """
        if not self.SAMPLE_PDF.exists():
            self.skipTest("sample PDF not available for figure page sim")
        try:
            import fitz  # noqa: F401
        except ImportError:
            self.skipTest("pymupdf not installed")
        paper = load_paper(self.SAMPLE_PDF, max_pages=3)
        valid_chunk_ids = {str(c["chunk_id"]) for c in paper["chunks"] if c.get("chunk_id")}
        # pages present in chunks can stand in for valid rendered pages for figure source test (in real flow, from _render_paper_images)
        valid_pages = {c["page"] for c in paper["chunks"] if isinstance(c.get("page"), int)}
        self.assertGreater(len(valid_chunk_ids), 0)
        self.assertGreater(len(valid_pages), 0)

        # good text fact using real chunk_id from document load
        good_text_fact = {
            "type": "channel_model",
            "name": "test",
            "value": {},
            "source": {"source_kind": "text", "chunk_id": next(iter(valid_chunk_ids)), "page": 1, "section": "", "quote": "x", "figure_ref": ""},
            "confidence": "high",
            "used_for_reproduction": True,
        }
        kept, dropped = select_valid_engineering_facts({"engineering_facts": [good_text_fact]}, valid_chunk_ids, valid_pages)
        self.assertEqual(len(kept), 1)
        self.assertEqual(len(dropped), 0)

        # bad chunk_id -> dropped (traceability enforced)
        bad_text_fact = dict(good_text_fact)
        bad_text_fact["source"] = dict(good_text_fact["source"], chunk_id="p99_c99")
        kept2, dropped2 = select_valid_engineering_facts({"engineering_facts": [bad_text_fact]}, valid_chunk_ids, valid_pages)
        self.assertEqual(len(kept2), 0)
        self.assertTrue(any("chunk_id" in d["reason"] for d in dropped2))

        # figure fact citing a page that exists in the document's chunk pages (simulates paper with figures where facts ref Fig on page X)
        fig_fact = {
            "type": "figure_claim",
            "name": "fig_on_real_page",
            "value": {},
            "source": {"source_kind": "figure", "chunk_id": None, "page": min(valid_pages), "section": "", "quote": "y", "figure_ref": "Fig.X"},
            "confidence": "medium",
            "used_for_reproduction": False,
        }
        kept_f, dropped_f = select_valid_engineering_facts({"engineering_facts": [fig_fact]}, valid_chunk_ids, valid_pages)
        self.assertEqual(len(kept_f), 1)
        self.assertEqual(len(dropped_f), 0)

        # finalize should keep them and produce clean doc (chunk/page traceability preserved)
        doc = finalize_engineering_facts(
            {"paper_repro_type": "signal_chain", "engineering_facts": [good_text_fact, fig_fact], "missing_information": []},
            valid_chunk_ids,
            valid_pages,
        )
        self.assertEqual(len(doc["engineering_facts"]), 2)
        self.assertTrue(any(f["source"]["source_kind"] == "figure" for f in doc["engineering_facts"]))

    def test_pypdf_fallback_path_when_fitz_unavailable(self) -> None:
        # Force no fitz, should still load PDF via pypdf if installed (or raise the known msg)
        if not self.SAMPLE_PDF.exists():
            self.skipTest("sample PDF not available")
        with patch.dict("sys.modules", {"fitz": None}):
            # remove cached fitz if present
            import sys
            if "fitz" in sys.modules:
                del sys.modules["fitz"]
            try:
                paper = load_paper(self.SAMPLE_PDF, max_pages=1)
                # if no pypdf either it would have raised inside, so if here, chunks came from pypdf fallback
                self.assertEqual(paper["format"], "pdf")
                self.assertGreaterEqual(paper["chunk_count"], 0)
                if paper["chunk_count"] > 0:
                    self.assertTrue(all(re.match(r"^p\d+_c\d+$", c["chunk_id"]) for c in paper["chunks"]))
            except RuntimeError as e:
                self.assertIn("pymupdf 或 pypdf", str(e))

    def test_build_risk_report_injects_pdf_images_lost_for_pdf_format(self) -> None:
        """Covers the recent auto-injection of image/figure loss limitation note into risk_report (for pdf text-chunk loading which drops images/figures)."""
        facts = {"engineering_facts": [], "missing_information": []}
        tasks = {"repro_tasks": []}
        validation = {"required_files_present": True, "python_compiles": True}
        risk = build_risk_report(facts, tasks, validation, paper_format="pdf")
        findings = risk.get("findings", [])
        lost = [f for f in findings if f.get("type") == "pdf_images_lost"]
        self.assertEqual(len(lost), 1, "pdf_images_lost must be auto-injected for pdf")
        self.assertIn("text chunks", lost[0]["message"])
        self.assertIn("图片、图表", lost[0]["message"])  # Chinese note about lost visuals/figures
        self.assertTrue(lost[0].get("always_injected_for_pdfs"))
        self.assertEqual(lost[0]["severity"], "high")

        # non-pdf should not inject it
        risk_md = build_risk_report(facts, tasks, validation, paper_format="md")
        self.assertFalse(any(f.get("type") == "pdf_images_lost" for f in risk_md.get("findings", [])))

    def test_dependency_warnings_are_reported_without_failing_runtime(self) -> None:
        facts = {"engineering_facts": [], "missing_information": []}
        tasks = {"repro_tasks": []}
        validation = {"required_files_present": True, "python_compiles": True}
        runtime_result = {
            "enabled": True,
            "passed": True,
            "requirements_warnings": [
                {
                    "file": "tasks/demo.py",
                    "line": "1",
                    "message": "third-party import is not declared in requirements.txt: scipy.linalg (expected package scipy)",
                }
            ],
        }

        risk = build_risk_report(facts, tasks, validation, runtime_result=runtime_result)

        self.assertTrue(any(item["type"] == "dependency_warnings" for item in risk["findings"]))
        self.assertIn("requirements_warnings=1", risk["risk_dimensions"]["security_isolation"]["evidence"])

        markdown = render_review_markdown(
            paper={"source_path": "paper.md"},
            facts=facts,
            tasks=tasks,
            risk_report=risk,
            validation=validation,
            runtime_result=runtime_result,
            result_review_result={"enabled": False, "passed": None, "reason": "not run"},
            repro_project_dir=Path("repro_project"),
        )
        self.assertIn("通过，有 1 条依赖告警", markdown)

    def test_unresolved_task_evidence_is_reported_without_pre_rating(self) -> None:
        facts = {"engineering_facts": [], "missing_information": []}
        tasks = {
            "repro_tasks": [
                {
                    "task_id": "fig_4",
                    "assumptions": [],
                    "missing_fact_requests": [
                        {
                            "request_id": "power_norm",
                            "type": "simulation_parameter",
                            "name": "power normalization",
                            "why_needed": "sets the x axis",
                            "impact": "high",
                            "search_targets": ["Fig. 4"],
                        }
                    ],
                }
            ]
        }
        validation = {"required_files_present": True, "python_compiles": True}

        risk = build_risk_report(facts, tasks, validation)

        self.assertEqual(risk["task_evidence_gap_count"], 1)
        self.assertTrue(any(item["type"] == "task_evidence_gaps" for item in risk["findings"]))

if __name__ == "__main__":
    unittest.main()
