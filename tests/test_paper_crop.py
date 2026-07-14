import base64
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from geng_agent.paper_crop import _task_figure_target, finalize_paper_target


PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="


def _paper(path: Path) -> None:
    import fitz

    document = fitz.open()
    page = document.new_page(width=600, height=800)
    page.draw_rect(fitz.Rect(60, 80, 540, 520), color=(0, 0, 0), width=3)
    page.draw_line(fitz.Point(300, 80), fitz.Point(300, 520), color=(0, 0, 0), width=2)
    page.insert_text(fitz.Point(100, 130), "(a) complete left panel", fontsize=20)
    page.insert_text(fitz.Point(350, 130), "(b) complete right panel", fontsize=20)
    page.insert_text(fitz.Point(60, 560), "Figure 9: two-panel result", fontsize=18)
    document.save(path)
    document.close()


def _candidate() -> dict:
    return {
        "candidate_id": "fig-p1-001",
        "page": 1,
        "figure_number": "9",
        "bbox_norm": [0.08, 0.08, 0.92, 0.67],
    }


def _render_page(paper: Path, target: Path) -> None:
    import fitz

    document = fitz.open(str(paper))
    try:
        pixmap = document.load_page(0).get_pixmap(alpha=False)
        target.parent.mkdir(parents=True, exist_ok=True)
        pixmap.save(str(target))
    finally:
        document.close()


class PaperCropTests(unittest.TestCase):
    def test_subfigure_parser_does_not_consume_following_task_text(self) -> None:
        self.assertEqual(_task_figure_target({"figure_or_claim": "Fig. 1", "task_id": "task_a"}), ("1", None))
        self.assertEqual(_task_figure_target({"figure_or_claim": "Fig. 9(a)"}), ("9", "a"))
        self.assertEqual(_task_figure_target({"figure_or_claim": "Figure 9a"}), ("9", "a"))

    def test_python_crops_exact_subfigure_from_relative_bbox(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            paper = root / "paper.pdf"
            _paper(paper)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "paper_target_metadata.json").write_text(
                json.dumps({
                    "candidate_id": "fig-p1-001",
                    "source_page": 1,
                    "child_bbox_relative": [0, 0, 0.5, 1],
                    "visual_check": {
                        "target_identity_confirmed": True,
                        "panel_boundary_complete": True,
                        "axes_and_labels_complete": True,
                        "legend_and_annotations_complete": True,
                        "compared_against_parent": True,
                    },
                }),
                encoding="utf-8",
            )
            verification = {}
            result = finalize_paper_target(
                paper_path=paper,
                workspace=workspace,
                task={"figure_or_claim": "Fig. 9(a)"},
                task_id="fig9a",
                candidates=[_candidate()],
                verification=verification,
            )
            self.assertEqual(result["status"], "exact_subfigure", result)
            self.assertTrue(Path(result["output_path"]).is_file())
            self.assertEqual(verification["paper_assets"], ["report_assets/fig9a/paper_target.png"])

    def test_unreviewed_subfigure_bbox_falls_back_to_parent(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            paper = root / "paper.pdf"
            _paper(paper)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "paper_target_metadata.json").write_text(
                json.dumps({"candidate_id": "fig-p1-001", "child_bbox_relative": [0, 0, 0.5, 1]}),
                encoding="utf-8",
            )
            verification = {"remaining_uncertainties": []}
            result = finalize_paper_target(
                paper_path=paper,
                workspace=workspace,
                task={"figure_or_claim": "Fig. 9(a)"},
                task_id="fig9a",
                candidates=[_candidate()],
                verification=verification,
            )
            self.assertEqual(result["status"], "fallback_parent_figure", result)
            self.assertTrue(verification["remaining_uncertainties"])

    def test_missing_subfigure_bbox_falls_back_to_complete_parent(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            paper = root / "paper.pdf"
            _paper(paper)
            workspace = root / "workspace"
            workspace.mkdir()
            verification = {}
            result = finalize_paper_target(
                paper_path=paper,
                workspace=workspace,
                task={"figure_or_claim": "Fig. 9(a)"},
                task_id="fig9a",
                candidates=[_candidate()],
                verification=verification,
            )
            self.assertEqual(result["status"], "fallback_parent_figure", result)
            self.assertTrue(Path(result["output_path"]).is_file())

    def test_candidate_page_is_authoritative_over_reporter_metadata(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            paper = root / "paper.pdf"
            _paper(paper)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "paper_target_metadata.json").write_text(
                json.dumps({
                    "candidate_status": "accepted",
                    "candidate_id": "fig-p1-001",
                    "source_page": 99,
                    "visual_check": {
                        "target_identity_confirmed": True,
                        "figure_content_complete": True,
                        "panel_boundary_complete": True,
                        "axes_and_labels_complete": True,
                        "legend_and_annotations_complete": True,
                        "caption_complete": True,
                        "no_adjacent_content": True,
                        "compared_against_parent": True,
                    },
                }),
                encoding="utf-8",
            )
            result = finalize_paper_target(
                paper_path=paper,
                workspace=workspace,
                task={"figure_or_claim": "Fig. 9"},
                task_id="fig9",
                candidates=[_candidate()],
                verification={},
            )
            self.assertEqual(result["status"], "complete_figure", result)
            self.assertEqual(result["source_page"], 1)

    def test_incomplete_whole_figure_candidate_uses_verified_manual_crop(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            paper = root / "paper.pdf"
            _paper(paper)
            workspace = root / "workspace"
            page_image = workspace / "paper_evidence" / "full_paper_pages" / "paper_page_001.png"
            _render_page(paper, page_image)
            (workspace / "paper_target_metadata.json").write_text(
                json.dumps({
                    "candidate_status": "accepted",
                    "candidate_id": "fig-p1-001",
                    "source_page": 1,
                    "manual_crop": {
                        "source_image": "paper_evidence/full_paper_pages/paper_page_001.png",
                        "bbox_pixels": [60, 80, 540, 580],
                    },
                    "visual_check": {
                        "target_identity_confirmed": True,
                        "figure_content_complete": True,
                        "panel_boundary_complete": True,
                        "axes_and_labels_complete": True,
                        "legend_and_annotations_complete": True,
                        "caption_complete": False,
                        "no_adjacent_content": True,
                        "compared_against_parent": True,
                    },
                }),
                encoding="utf-8",
            )
            result = finalize_paper_target(
                paper_path=paper,
                workspace=workspace,
                task={"figure_or_claim": "Fig. 9"},
                task_id="fig9",
                candidates=[_candidate()],
                verification={},
            )
            self.assertEqual(result["status"], "verified_manual_page_crop", result)
            self.assertEqual(result["selection_reason"], "candidate_boundary_not_verified")
            self.assertEqual(result["rejected_candidate_id"], "fig-p1-001")

    def test_rejected_candidate_cannot_overwrite_nested_manual_page_crop(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            paper = root / "paper.pdf"
            _paper(paper)
            workspace = root / "workspace"
            page_image = workspace / "paper_evidence" / "full_paper_pages" / "paper_page_001.png"
            _render_page(paper, page_image)
            target = workspace / "report_assets" / "fig9" / "paper_target.png"
            target.parent.mkdir(parents=True)
            target.write_bytes(base64.b64decode(PNG_B64))
            wrong_candidate = {
                "candidate_id": "wrong-papr-panel",
                "page": 1,
                "figure_number": "9",
                "bbox_norm": [0.5, 0.1, 0.9, 0.65],
            }
            (workspace / "paper_target_metadata.json").write_text(
                json.dumps({
                    "candidate_status": "rejected_wrong_identity",
                    "candidate_id": "wrong-papr-panel",
                    "source_page": 1,
                    "manual_crop": {
                        "source_image": "paper_evidence/full_paper_pages/paper_page_001.png",
                        "bbox_pixels": [60, 80, 300, 520],
                    },
                    "visual_check": {
                        "target_identity_confirmed": True,
                        "mineru_candidate_identity_confirmed": False,
                    },
                }),
                encoding="utf-8",
            )

            result = finalize_paper_target(
                paper_path=paper,
                workspace=workspace,
                task={"figure_or_claim": "Fig. 9"},
                task_id="fig9",
                candidates=[wrong_candidate],
                verification={},
            )

            self.assertEqual(result["status"], "verified_manual_page_crop", result)
            self.assertEqual(result["source_mode"], "reporter_manual_page_crop")
            self.assertIsNone(result["candidate_id"])
            self.assertEqual(result["rejected_candidate_id"], "wrong-papr-panel")
            self.assertEqual(result["crop_bbox_norm"], [0.1, 0.1, 0.5, 0.65])
            self.assertTrue(result["output_sha256"])

    def test_false_identity_uses_legacy_flat_manual_crop_fields(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            paper = root / "paper.pdf"
            _paper(paper)
            workspace = root / "workspace"
            page_image = workspace / "paper_evidence" / "full_paper_pages" / "paper_page_001.png"
            _render_page(paper, page_image)
            (workspace / "paper_target_metadata.json").write_text(
                json.dumps({
                    "candidate_id": "wrong-papr-panel",
                    "source_page": 1,
                    "manual_crop_source": "paper_evidence/full_paper_pages/paper_page_001.png",
                    "manual_crop_pixel_bbox": [60, 80, 300, 520],
                    "visual_check": {"target_identity_confirmed": False},
                }),
                encoding="utf-8",
            )
            result = finalize_paper_target(
                paper_path=paper,
                workspace=workspace,
                task={"figure_or_claim": "Fig. 9"},
                task_id="fig9",
                candidates=[{
                    "candidate_id": "wrong-papr-panel",
                    "page": 1,
                    "figure_number": "9",
                    "bbox_norm": [0.5, 0.1, 0.9, 0.65],
                }],
                verification={},
            )
            self.assertEqual(result["status"], "verified_manual_page_crop", result)
            self.assertEqual(result["selection_reason"], "reporter_rejected_target_identity")
            self.assertEqual(result["crop_bbox_norm"], [0.1, 0.1, 0.5, 0.65])

    def test_ambiguous_same_figure_candidates_are_not_silently_selected(self) -> None:
        candidates = [_candidate(), {**_candidate(), "candidate_id": "fig-p2-002", "page": 2}]
        with TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            target = workspace / "report_assets" / "fig9" / "paper_target.png"
            target.parent.mkdir(parents=True)
            target.write_bytes(base64.b64decode(PNG_B64))
            result = finalize_paper_target(
                paper_path=root / "paper.pdf",
                workspace=workspace,
                task={"figure_or_claim": "Fig. 9"},
                task_id="fig9",
                candidates=candidates,
                verification={},
            )
            self.assertEqual(result["status"], "legacy_reporter_crop", result)

    def test_legacy_reporter_crop_remains_a_fail_open_fallback(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            target = workspace / "report_assets" / "task_a" / "paper_target.png"
            target.parent.mkdir(parents=True)
            target.write_bytes(base64.b64decode(PNG_B64))
            verification = {}
            result = finalize_paper_target(
                paper_path=root / "paper.md",
                workspace=workspace,
                task={"figure_or_claim": "Fig. 1", "task_id": "task_a"},
                task_id="task_a",
                candidates=[],
                verification=verification,
            )
            self.assertEqual(result["status"], "legacy_reporter_crop", result)
            self.assertEqual(verification["paper_assets"], ["report_assets/task_a/paper_target.png"])


if __name__ == "__main__":
    unittest.main()
