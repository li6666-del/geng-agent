from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from geng_agent import pdffigures2
from geng_agent.pdffigures2 import build_pdffigures2_evidence, select_pdffigures2_crop_for_task


class PDFFigures2EvidenceTests(unittest.TestCase):
    def test_missing_command_writes_disabled_index(self) -> None:
        with TemporaryDirectory() as temp_dir, patch("geng_agent.pdffigures2.get_config_value", return_value=None):
            root = Path(temp_dir)
            paper = root / "paper.pdf"
            paper.write_bytes(b"%PDF-1.4\n%%EOF\n")
            evidence_root = root / "paper_evidence"

            result = build_pdffigures2_evidence(paper_path=paper, evidence_root=evidence_root)

            self.assertFalse(result["enabled"])
            self.assertFalse(result["ok"])
            index_path = evidence_root / "pdffigures2" / "paper_figures.json"
            self.assertTrue(index_path.exists())
            written = json.loads(index_path.read_text(encoding="utf-8"))
            self.assertEqual(written["reason"], "GENG_PDFFIGURES2_CMD is not set")

    def test_missing_pdffigures2_index_returns_no_crop(self) -> None:
        with TemporaryDirectory() as temp_dir:
            page = Path(temp_dir) / "paper_evidence" / "01_task" / "paper_page_1.png"
            page.parent.mkdir(parents=True)
            page.write_bytes(b"not a real png")

            crop = select_pdffigures2_crop_for_task(
                source_page_image=page,
                figure_ref={"number": "1", "subfigure": ""},
                target_path=page.with_name("paper_page_1_crop.png"),
            )

            self.assertIsNone(crop)

    def test_template_command_runs_without_shell_and_preserves_placeholder_arguments(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input with spaces"
            data_dir = root / "data"
            image_dir = root / "images"
            stats = root / "stats file.json"
            for directory in (input_dir, data_dir, image_dir):
                directory.mkdir(parents=True)
            paper = input_dir / "paper.pdf"
            paper.write_bytes(b"%PDF-1.4\n%%EOF\n")
            completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
            command = '"C:\\Program Files\\Java\\bin\\java.exe" -jar "C:\\tools\\pdffigures2.jar" "{input_dir}" -s "{stats}"'

            with patch("geng_agent.pdffigures2.subprocess.run", return_value=completed) as run:
                pdffigures2._run_pdffigures2_command(
                    command=command,
                    paper_path=paper,
                    input_dir=input_dir,
                    data_dir=data_dir,
                    image_dir=image_dir,
                    stats_path=stats,
                )

            args = run.call_args.args[0]
            kwargs = run.call_args.kwargs
            self.assertEqual(args[0], "C:\\Program Files\\Java\\bin\\java.exe")
            self.assertEqual(args[3], str(input_dir))
            self.assertEqual(args[5], str(stats))
            self.assertFalse(kwargs["shell"])

    def test_build_reuses_pdffigures2_run_cache_for_same_paper(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paper = root / "paper.pdf"
            paper.write_bytes(b"%PDF-1.4\n%%EOF\n")
            pdffigures2._PDFFIGURES2_RUN_CACHE.clear()

            def fake_config(name: str) -> str | None:
                return "fake-pdffigures2" if name == "GENG_PDFFIGURES2_CMD" else None

            with (
                patch("geng_agent.pdffigures2.get_config_value", side_effect=fake_config),
                patch("geng_agent.pdffigures2._run_pdffigures2_command", return_value={"returncode": 0, "stdout": "[]", "stderr": ""}) as run,
                patch("geng_agent.pdffigures2._load_pdffigures2_json_documents", return_value=[]),
                patch("geng_agent.pdffigures2._normalize_pdffigures2_figures", return_value=[]),
            ):
                first = build_pdffigures2_evidence(paper_path=paper, evidence_root=root / "one")
                second = build_pdffigures2_evidence(paper_path=paper, evidence_root=root / "two")

            self.assertEqual(run.call_count, 1)
            self.assertFalse(first["cache_hit"])
            self.assertTrue(second["cache_hit"])

    def test_non_ok_pdffigures2_index_is_not_used_for_crop(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            page = root / "paper_evidence" / "01_task" / "paper_page_1.png"
            index = root / "paper_evidence" / "pdffigures2" / "paper_figures.json"
            page.parent.mkdir(parents=True)
            index.parent.mkdir(parents=True)
            page.write_bytes(b"not a real png")
            index.write_text(
                json.dumps(
                    {
                        "ok": False,
                        "figures": [
                            {
                                "figure_number": "1",
                                "page": 1,
                                "page_index": 0,
                                "figure_box": [0, 0, 10, 10],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            crop = select_pdffigures2_crop_for_task(
                source_page_image=page,
                figure_ref={"number": "1", "subfigure": ""},
                target_path=page.with_name("paper_page_1_crop.png"),
            )

            self.assertIsNone(crop)


if __name__ == "__main__":
    unittest.main()
