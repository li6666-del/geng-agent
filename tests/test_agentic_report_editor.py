import base64
import os
import sys
import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from geng_agent.agentic_report_editor import run_codex_report_editor_workflow


PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="


def _fake_editor(root: Path) -> str:
    script = root / "report_editor.py"
    script.write_text(textwrap.dedent("""
        import json
        from pathlib import Path
        root = Path.cwd()
        report_input = json.loads((root / "inputs" / "report_editor_input.json").read_text(encoding="utf-8"))
        ids = [packet["task_id"] for packet in report_input["task_packets"]]
        (root / "review.md").write_text("# 主审查报告\\n" + "\\n".join(ids), encoding="utf-8")
        (root / "reproduction_report.md").write_text("\\n".join(f"## {item}" for item in ids), encoding="utf-8")
        (root / "result_review.md").write_text("\\n".join(f"## {item}" for item in ids), encoding="utf-8")
    """), encoding="utf-8")
    return f'"{sys.executable}" "{script}"'


class FinalReportEditorTests(unittest.TestCase):
    def test_editor_receives_accepted_packets_and_writes_reports(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "case"
            assets = output / "report_assets" / "task_1"
            assets.mkdir(parents=True)
            for name in ("local_result.png", "paper_target.png"):
                (assets / name).write_bytes(base64.b64decode(PNG_B64))
            stale = output / "report_assets" / "stale_task"
            stale.mkdir(parents=True)
            (stale / "unrelated.png").write_bytes(base64.b64decode(PNG_B64))
            old = os.environ.get("GENG_CODEX_REPORT_EDITOR_CMD")
            os.environ["GENG_CODEX_REPORT_EDITOR_CMD"] = _fake_editor(root)
            try:
                result = run_codex_report_editor_workflow(
                    paper={"title": "paper"}, facts={"engineering_facts": []},
                    tasks={"repro_tasks": [{"task_id": "task_1", "figure_or_claim": "Fig. 1"}]},
                    paper_thesis={}, runtime_result={"passed": True}, risk_report={},
                    task_records=[{"task_id": "task_1", "result_json": {"summary": "done", "execution_summary": {}}}],
                    task_verifications=[{
                        "task_id": "task_1", "verdict": "accepted", "revision_target": "none",
                        "comparison_summary": "match", "differences": [], "non_material_differences": [],
                        "evidence_files": ["paper.png"], "feedback": [], "confidence": "high",
                        "local_assets": ["report_assets/task_1/local_result.png"],
                        "paper_assets": ["report_assets/task_1/paper_target.png"],
                    }],
                    output_dir=output, audit_dir=output / "audit", timeout=30, resume=False,
                )
            finally:
                if old is None: os.environ.pop("GENG_CODEX_REPORT_EDITOR_CMD", None)
                else: os.environ["GENG_CODEX_REPORT_EDITOR_CMD"] = old
            self.assertTrue(result["ok"], result)
            self.assertTrue((output / "result_review.md").is_file())
            brief = (output / "audit" / "04b_report_editor_brief.md").read_text(encoding="utf-8")
            self.assertIn("not a scientific reviewer", brief)
            editor_assets = Path(result["workspace"]) / "report_assets"
            self.assertTrue((editor_assets / "task_1" / "local_result.png").is_file())
            self.assertFalse((editor_assets / "stale_task").exists())


if __name__ == "__main__":
    unittest.main()
