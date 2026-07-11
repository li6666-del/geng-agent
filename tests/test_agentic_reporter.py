import base64
import os
import sys
import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from geng_agent.agentic_reporter import run_codex_reporter_workflow


PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="


def _fake_reporter(root: Path, *, complete: bool = True) -> str:
    script = root / ("fake_reporter_ok.py" if complete else "fake_reporter_incomplete.py")
    body = f"""
import base64
from pathlib import Path

root = Path.cwd()
assets = root / "report_assets" / "task_1"
assets.mkdir(parents=True, exist_ok=True)
(assets / "paper_target.png").write_bytes(base64.b64decode({PNG_B64!r}))
(assets / "local_result.png").write_bytes(base64.b64decode({PNG_B64!r}))
(root / "review.md").write_text("# 主审查报告\\n\\n总体结论。\\n", encoding="utf-8")
(root / "reproduction_report.md").write_text(
    "## 1. task_1\\n\\n### 关键参数与假设\\n\\n- SNR: 0--10 dB\\n",
    encoding="utf-8",
)
"""
    if complete:
        body += """
(root / "result_review.md").write_text(
    "## 1. task_1\\n\\n| 本地复现图 | 论文原图 |\\n|---|---|\\n"
    "| ![本地复现图](report_assets/task_1/local_result.png) | "
    "![论文原图：Fig. 1](report_assets/task_1/paper_target.png) |\\n\\n### 对比结论\\n\\n匹配。\\n",
    encoding="utf-8",
)
"""
    script.write_text(textwrap.dedent(body), encoding="utf-8")
    return f'"{sys.executable}" "{script}"'


class CodexReporterWorkflowTests(unittest.TestCase):
    def _inputs(self, root: Path) -> dict:
        paper = root / "paper.md"
        paper.write_text("Fig. 1 reports the target curve.", encoding="utf-8")
        output_dir = root / "case"
        repro = output_dir / "repro_project"
        task_output = repro / "outputs" / "task_1"
        task_output.mkdir(parents=True)
        (task_output / "curve.png").write_bytes(base64.b64decode(PNG_B64))
        (task_output / "results.csv").write_text("x,y\n0,1\n", encoding="utf-8")
        return {
            "paper": {"format": "markdown", "chunks": []},
            "paper_path": paper,
            "facts": {"engineering_facts": [], "missing_information": []},
            "tasks": {"repro_tasks": [{"task_id": "task_1", "figure_or_claim": "Fig. 1"}]},
            "experiment_index": {"experiments": []},
            "paper_thesis": {"central_claim": "target"},
            "paper_memory": None,
            "runtime_result": {"passed": True},
            "risk_report": {"risk_level": "low"},
            "task_records": [
                {
                    "task_id": "task_1",
                    "output_subdir": "task_1",
                    "task_writer_status": "matched",
                    "writer_completed": True,
                    "task_contract": {"seed": 1, "assumptions": []},
                    "result_json": {
                        "task_id": "task_1",
                        "status": "matched",
                        "summary": "趋势匹配",
                        "local_image_paths": ["outputs/task_1/curve.png"],
                    },
                }
            ],
            "output_dir": output_dir,
            "audit_dir": output_dir / "audit",
            "repro_project_dir": repro,
            "timeout": 30,
            "resume": False,
            "memory_snapshot_hash": "snapshot",
        }

    def test_single_reporter_generates_three_reports_and_paper_crop(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            inputs = self._inputs(root)
            old = os.environ.get("GENG_CODEX_REPORTER_CMD")
            os.environ["GENG_CODEX_REPORTER_CMD"] = _fake_reporter(root)
            try:
                result = run_codex_reporter_workflow(**inputs)
            finally:
                if old is None:
                    os.environ.pop("GENG_CODEX_REPORTER_CMD", None)
                else:
                    os.environ["GENG_CODEX_REPORTER_CMD"] = old

            self.assertTrue(result["ok"], result)
            self.assertEqual(result["codex_status"]["reasoning_effort"], "high")
            case = root / "case"
            for name in ("review.md", "reproduction_report.md", "result_review.md"):
                self.assertTrue((case / name).exists())
            self.assertTrue((case / "report_assets" / "task_1" / "paper_target.png").exists())
            review = (case / "result_review.md").read_text(encoding="utf-8")
            self.assertNotIn("附录", review)
            self.assertNotIn("Writer 自审原文", review)
            brief = (case / "audit" / "04_reporter_brief.md").read_text(encoding="utf-8")
            self.assertIn("only report agent", brief)
            self.assertIn("Do not include `附录`", brief)

            inputs["resume"] = True
            cached = run_codex_reporter_workflow(**inputs)
            self.assertTrue(cached["ok"])
            self.assertTrue(cached["cached"])

            (case / "report_assets" / "task_1" / "paper_target.png").write_bytes(b"changed")
            with patch(
                "geng_agent.agentic_reporter.run_codex_subprocess",
                return_value={"ok": False, "error": "forced rerun"},
            ):
                invalidated = run_codex_reporter_workflow(**inputs)
            self.assertFalse(invalidated["ok"])
            self.assertFalse(invalidated["cached"])

    def test_incomplete_reporter_delivery_is_explicit_failure(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            old = os.environ.get("GENG_CODEX_REPORTER_CMD")
            os.environ["GENG_CODEX_REPORTER_CMD"] = _fake_reporter(root, complete=False)
            try:
                result = run_codex_reporter_workflow(**self._inputs(root))
            finally:
                if old is None:
                    os.environ.pop("GENG_CODEX_REPORTER_CMD", None)
                else:
                    os.environ["GENG_CODEX_REPORTER_CMD"] = old

            self.assertFalse(result["ok"])
            self.assertIn("result_review.md", result["missing_outputs"])
            self.assertTrue((root / "case" / "reporter_error.json").exists())

    def test_evidence_preparation_failure_is_recorded_without_crashing_pipeline(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch(
                "geng_agent.agentic_reporter._write_paper_evidence_bundle",
                side_effect=OSError("disk unavailable"),
            ):
                result = run_codex_reporter_workflow(**self._inputs(root))

            self.assertFalse(result["ok"])
            self.assertEqual(result["codex_status"]["error_kind"], "reporter_preparation_failed")
            self.assertTrue((root / "case" / "reporter_error.json").exists())


if __name__ == "__main__":
    unittest.main()
