import json
import os
import sys
import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from geng_agent.mineru_runner import run_mineru_layout_stage


def _pdf(path: Path) -> None:
    import fitz

    document = fitz.open()
    document.new_page(width=600, height=800)
    document.save(path)
    document.close()


def _fake_mineru(root: Path) -> str:
    script = root / "fake_mineru.py"
    script.write_text(textwrap.dedent("""
        import json
        import sys
        from pathlib import Path

        args = sys.argv[1:]
        output = Path(args[args.index("-o") + 1])
        output.mkdir(parents=True, exist_ok=True)
        counter = Path(__file__).with_name("mineru_count.txt")
        value = int(counter.read_text() or "0") if counter.exists() else 0
        counter.write_text(str(value + 1))
        payload = [[{
            "type": "image",
            "bbox": [100, 100, 900, 600],
            "content": {"image_caption": [{"type": "text", "content": "Fig. 2. Capacity result."}]},
        }]]
        (output / "paper_content_list_v2.json").write_text(json.dumps(payload), encoding="utf-8")
    """), encoding="utf-8")
    return f'"{sys.executable}" "{script}"'


def _sleeping_mineru(root: Path) -> str:
    script = root / "sleeping_mineru.py"
    script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    return f'"{sys.executable}" "{script}"'


class MinerURunnerTests(unittest.TestCase):
    def test_fake_cli_runs_once_and_resume_uses_cache(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            paper = root / "paper.pdf"
            _pdf(paper)
            output = root / "case"
            command = _fake_mineru(root)
            old = os.environ.get("GENG_MINERU_CMD")
            os.environ["GENG_MINERU_CMD"] = command
            try:
                first = run_mineru_layout_stage(
                    paper_path=paper,
                    output_dir=output,
                    audit_dir=output / "audit",
                    resume=False,
                    timeout=30,
                )
                second = run_mineru_layout_stage(
                    paper_path=paper,
                    output_dir=output,
                    audit_dir=output / "audit",
                    resume=True,
                    timeout=30,
                )
            finally:
                if old is None:
                    os.environ.pop("GENG_MINERU_CMD", None)
                else:
                    os.environ["GENG_MINERU_CMD"] = old

            self.assertTrue(first["ok"], first)
            self.assertIn("--formula", first["command"])
            self.assertIn("--table", first["command"])
            self.assertEqual(first["figure_count"], 1)
            self.assertTrue(second["cached"])
            self.assertEqual((root / "mineru_count.txt").read_text(), "1")
            index = json.loads((output / "paper_figure_index.json").read_text(encoding="utf-8"))
            self.assertEqual(index["figures"][0]["figure_ref"], "Fig. 2")

    def test_missing_cached_candidate_forces_clean_rerun(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            paper = root / "paper.pdf"
            _pdf(paper)
            output = root / "case"
            old = os.environ.get("GENG_MINERU_CMD")
            os.environ["GENG_MINERU_CMD"] = _fake_mineru(root)
            try:
                first = run_mineru_layout_stage(
                    paper_path=paper,
                    output_dir=output,
                    audit_dir=output / "audit",
                    resume=False,
                    timeout=30,
                )
                candidate = output / first["figure_index"]["figures"][0]["asset_path"]
                candidate.unlink()
                second = run_mineru_layout_stage(
                    paper_path=paper,
                    output_dir=output,
                    audit_dir=output / "audit",
                    resume=True,
                    timeout=30,
                )
            finally:
                if old is None:
                    os.environ.pop("GENG_MINERU_CMD", None)
                else:
                    os.environ["GENG_MINERU_CMD"] = old

            self.assertFalse(second["cached"])
            self.assertEqual((root / "mineru_count.txt").read_text(), "2")

    def test_missing_cli_is_nonfatal_and_writes_empty_index(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            paper = root / "paper.pdf"
            _pdf(paper)
            output = root / "case"
            old = os.environ.get("GENG_MINERU_CMD")
            os.environ["GENG_MINERU_CMD"] = "definitely_missing_mineru_command"
            try:
                result = run_mineru_layout_stage(
                    paper_path=paper,
                    output_dir=output,
                    audit_dir=output / "audit",
                    resume=False,
                    timeout=30,
                )
            finally:
                if old is None:
                    os.environ.pop("GENG_MINERU_CMD", None)
                else:
                    os.environ["GENG_MINERU_CMD"] = old

            self.assertFalse(result["ok"])
            self.assertTrue(result["fallback_used"])
            self.assertEqual(result["error_kind"], "missing_cli")
            self.assertTrue((output / "paper_figure_index.json").is_file())

    def test_max_pages_is_forwarded_as_zero_based_end_page(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            paper = root / "paper.pdf"
            _pdf(paper)
            output = root / "case"
            old = os.environ.get("GENG_MINERU_CMD")
            os.environ["GENG_MINERU_CMD"] = _fake_mineru(root)
            try:
                result = run_mineru_layout_stage(
                    paper_path=paper,
                    output_dir=output,
                    audit_dir=output / "audit",
                    resume=False,
                    timeout=30,
                    max_pages=3,
                )
            finally:
                if old is None:
                    os.environ.pop("GENG_MINERU_CMD", None)
                else:
                    os.environ["GENG_MINERU_CMD"] = old

            end_index = result["command"].index("-e")
            self.assertEqual(result["command"][end_index + 1], "2")

    def test_timeout_is_fail_open_and_marks_run_failed(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            paper = root / "paper.pdf"
            _pdf(paper)
            output = root / "case"
            old = os.environ.get("GENG_MINERU_CMD")
            os.environ["GENG_MINERU_CMD"] = _sleeping_mineru(root)
            try:
                result = run_mineru_layout_stage(
                    paper_path=paper,
                    output_dir=output,
                    audit_dir=output / "audit",
                    resume=False,
                    timeout=0.2,
                )
            finally:
                if old is None:
                    os.environ.pop("GENG_MINERU_CMD", None)
                else:
                    os.environ["GENG_MINERU_CMD"] = old

            self.assertFalse(result["ok"])
            self.assertEqual(result["state"], "failed")
            self.assertTrue(result["timed_out"])
            self.assertEqual(result["error_kind"], "timeout")


if __name__ == "__main__":
    unittest.main()
