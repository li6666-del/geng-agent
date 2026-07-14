import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from geng_agent.mineru_adapter import build_figure_index, task_figure_candidates


def _pdf(path: Path) -> None:
    import fitz

    document = fitz.open()
    page = document.new_page(width=600, height=800)
    page.draw_rect(fitz.Rect(60, 80, 540, 480), color=(0, 0, 0))
    document.save(path)
    document.close()


class MinerUAdapterTests(unittest.TestCase):
    def test_content_list_v2_builds_captioned_figure_candidate(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            raw = root / "raw"
            raw.mkdir()
            paper = root / "paper.pdf"
            _pdf(paper)
            payload = [[{
                "type": "image",
                "bbox": [100, 100, 900, 600],
                "content": {
                    "image_path": "images/example.png",
                    "image_caption": [{"type": "text", "content": "Fig. 9. BER results: (a) baseline; (b) proposed."}],
                },
            }]]
            (raw / "paper_content_list_v2.json").write_text(json.dumps(payload), encoding="utf-8")

            index = build_figure_index(
                raw_dir=raw,
                paper_path=paper,
                case_root=root,
                candidate_dir=root / "audit" / "00_mineru" / "candidates",
                paper_sha256="abc",
                backend="pipeline",
            )

            self.assertEqual(index["source_format"], "content_list_v2")
            self.assertEqual(len(index["figures"]), 1)
            figure = index["figures"][0]
            self.assertEqual(figure["figure_ref"], "Fig. 9")
            self.assertEqual(figure["page"], 1)
            self.assertEqual(figure["bbox_norm"], [0.1, 0.1, 0.9, 0.6])
            self.assertEqual(figure["subfigure_labels"], ["a", "b"])
            self.assertNotIn("image_path", figure["caption"])
            self.assertFalse(figure["caption"].startswith("text "))
            self.assertTrue((root / figure["asset_path"]).is_file())

            matches = task_figure_candidates(index, {"figure_or_claim": "Reproduce Fig. 9(a)"})
            self.assertEqual(len(matches), 1)
            self.assertEqual(matches[0]["target_subfigure"], "a")
            whole_figure = task_figure_candidates(index, {"figure_or_claim": "Fig. 9", "task_id": "task_a"})
            self.assertIsNone(whole_figure[0]["target_subfigure"])

    def test_merged_multi_figure_caption_is_ambiguous_not_bound_to_first_number(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            raw = root / "raw"
            raw.mkdir()
            paper = root / "paper.pdf"
            _pdf(paper)
            payload = [[{
                "type": "chart",
                "bbox": [500, 300, 920, 530],
                "content": {
                    "image_caption": [{
                        "type": "text",
                        "content": "Fig. 3: BER comparison. Fig. 4: PAPR comparison.",
                    }],
                },
            }]]
            (raw / "paper_content_list_v2.json").write_text(json.dumps(payload), encoding="utf-8")

            index = build_figure_index(
                raw_dir=raw,
                paper_path=paper,
                case_root=root,
                candidate_dir=root / "candidates",
                paper_sha256="abc",
                backend="pipeline",
            )

            self.assertEqual(index["figures"], [])
            self.assertEqual(len(index["unmatched_visuals"]), 1)
            candidate = index["unmatched_visuals"][0]
            self.assertIsNone(candidate["figure_number"])
            self.assertEqual(candidate["possible_figure_numbers"], ["3", "4"])
            self.assertEqual(candidate["identity_status"], "ambiguous_multi_figure_caption")
            matches = task_figure_candidates(index, {"figure_or_claim": "Fig. 3"})
            self.assertEqual([item["candidate_id"] for item in matches], [candidate["candidate_id"]])
            self.assertEqual(matches[0]["identity_status"], "ambiguous_multi_figure_caption")

    def test_middle_output_uses_page_geometry_and_caption_children(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            raw = root / "raw"
            raw.mkdir()
            paper = root / "paper.pdf"
            _pdf(paper)
            payload = {
                "pdf_info": [{
                    "page_idx": 0,
                    "page_size": [600, 800],
                    "para_blocks": [{
                        "type": "image",
                        "bbox": [60, 80, 540, 560],
                        "blocks": [
                            {"type": "image_body", "bbox": [60, 80, 540, 480], "lines": []},
                            {
                                "type": "image_caption",
                                "bbox": [60, 490, 540, 560],
                                "lines": [{"spans": [{"type": "text", "content": "Figure 4. Throughput comparison."}]}],
                            },
                        ],
                    }],
                }],
            }
            (raw / "paper_middle.json").write_text(json.dumps(payload), encoding="utf-8")

            index = build_figure_index(
                raw_dir=raw,
                paper_path=paper,
                case_root=root,
                candidate_dir=root / "candidates",
                paper_sha256="abc",
                backend=None,
            )

            figure = index["figures"][0]
            self.assertEqual(figure["figure_ref"], "Fig. 4")
            self.assertEqual(figure["bbox_norm"], [0.1, 0.1, 0.9, 0.6])
            self.assertEqual(figure["caption_bbox_norm"], [0.1, 0.6125, 0.9, 0.7])


if __name__ == "__main__":
    unittest.main()
