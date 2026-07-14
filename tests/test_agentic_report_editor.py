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


def _editor_command(root: Path, name: str, body: str) -> str:
    script = root / name
    script.write_text(
        "from pathlib import Path\nroot = Path.cwd()\n" + textwrap.dedent(body),
        encoding="utf-8",
    )
    return f'"{sys.executable}" "{script}"'


def _workflow_inputs(output: Path) -> dict:
    assets = output / "report_assets" / "task_1"
    assets.mkdir(parents=True, exist_ok=True)
    for name in ("local_result.png", "paper_target.png"):
        (assets / name).write_bytes(base64.b64decode(PNG_B64))
    return {
        "paper": {"title": "paper"},
        "facts": {"engineering_facts": []},
        "tasks": {"repro_tasks": [{"task_id": "task_1", "figure_or_claim": "Fig. 1"}]},
        "paper_thesis": {},
        "runtime_result": {"passed": True},
        "risk_report": {},
        "task_records": [{"task_id": "task_1", "result_json": {"summary": "done", "execution_summary": {}}}],
        "task_verifications": [{
            "task_id": "task_1", "verdict": "accepted", "revision_target": "none",
            "comparison_summary": "match", "differences": [], "non_material_differences": [],
            "evidence_files": ["paper.png"], "feedback": [], "confidence": "high",
            "local_assets": ["report_assets/task_1/local_result.png"],
            "paper_assets": ["report_assets/task_1/paper_target.png"],
        }],
        "output_dir": output,
        "audit_dir": output / "audit",
        "timeout": 30,
        "resume": False,
    }


def _run_with_command(command: str, **kwargs) -> dict:
    old = os.environ.get("GENG_CODEX_REPORT_EDITOR_CMD")
    os.environ["GENG_CODEX_REPORT_EDITOR_CMD"] = command
    try:
        return run_codex_report_editor_workflow(**kwargs)
    finally:
        if old is None:
            os.environ.pop("GENG_CODEX_REPORT_EDITOR_CMD", None)
        else:
            os.environ["GENG_CODEX_REPORT_EDITOR_CMD"] = old


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

    def test_human_readable_task_headings_do_not_require_machine_task_ids(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "case"
            command = _editor_command(root, "human_titles.py", """
                (root / "review.md").write_text("# 总览\\n任务已经完成。", encoding="utf-8")
                (root / "reproduction_report.md").write_text("## 任务 1：Fig. 1\\n参数与假设。", encoding="utf-8")
                (root / "result_review.md").write_text("## 任务 1：Fig. 1\\n本地结果与论文一致。", encoding="utf-8")
            """)

            result = _run_with_command(command, **_workflow_inputs(output))

            self.assertTrue(result["ok"], result)
            self.assertEqual(result["coverage_issues"], [])
            self.assertEqual(result["completion_mode"], "passed_after_normalization")

    def test_common_markdown_delivery_errors_are_normalized_without_retry(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "case"
            command = _editor_command(root, "formatting.py", """
                payload = "\\ufeff```markdown\\r\\n# 总览\\r\\n完成。\\r\\n```"
                (root / "主报告.md").write_text(payload, encoding="utf-8")
                (root / "本地复现报告.md").write_text("## 任务 1\\n参数。", encoding="utf-8")
                (root / "论文对比报告.md").write_text(
                    "## 任务 1\\n![本地复现图](report_assets\\\\task_1\\\\local_result.png)",
                    encoding="utf-8",
                )
            """)

            result = _run_with_command(command, **_workflow_inputs(output))

            self.assertTrue(result["ok"], result)
            self.assertEqual(result["completion_mode"], "passed_after_normalization")
            self.assertTrue(any("renamed" in item for item in result["normalization_actions"]))
            review = (output / "review.md").read_bytes()
            self.assertFalse(review.startswith(b"\xef\xbb\xbf"))
            self.assertNotIn("```", review.decode("utf-8"))
            comparison = (output / "result_review.md").read_text(encoding="utf-8")
            self.assertIn("report_assets/task_1/local_result.png", comparison)

    def test_second_attempt_repairs_only_missing_report_and_preserves_drafts(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "case"
            first_command = _editor_command(root, "partial.py", """
                (root / "review.md").write_text("# 原始总览", encoding="utf-8")
                (root / "result_review.md").write_text("## 原始对比", encoding="utf-8")
            """)
            first = _run_with_command(first_command, **_workflow_inputs(output))
            self.assertFalse(first["ok"])
            self.assertTrue(first["retryable"])
            self.assertEqual(first["missing_outputs"], ["reproduction_report.md"])
            first_workspace = Path(first["workspace"])
            original_review = (first_workspace / "review.md").read_bytes()
            original_comparison = (first_workspace / "result_review.md").read_bytes()

            repair_command = _editor_command(root, "repair.py", """
                (root / "review.md").write_text("# 不应保留的重写", encoding="utf-8")
                (root / "result_review.md").write_text("## 不应保留的重写", encoding="utf-8")
                (root / "reproduction_report.md").write_text("## 任务 1\\n补齐参数。", encoding="utf-8")
            """)
            second = _run_with_command(
                repair_command,
                **_workflow_inputs(output),
                attempt_no=2,
                repair_context=first,
                allow_fallback=True,
            )

            self.assertTrue(second["ok"], second)
            self.assertEqual(second["completion_mode"], "passed_after_targeted_repair")
            self.assertEqual(second["repair_targets"], ["reproduction_report.md"])
            self.assertCountEqual(second["restored_files"], ["review.md", "result_review.md"])
            self.assertEqual((output / "review.md").read_bytes(), original_review)
            self.assertEqual((output / "result_review.md").read_bytes(), original_comparison)

    def test_second_attempt_uses_deterministic_fallback_when_codex_still_omits_files(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "case"
            empty_command = _editor_command(root, "empty.py", "pass\n")
            first = _run_with_command(empty_command, **_workflow_inputs(output))
            self.assertFalse(first["ok"])

            second = _run_with_command(
                empty_command,
                **_workflow_inputs(output),
                attempt_no=2,
                repair_context=first,
                allow_fallback=True,
            )

            self.assertTrue(second["ok"], second)
            self.assertEqual(second["completion_mode"], "degraded_fallback")
            self.assertTrue(second["degraded_report_generation"])
            self.assertCountEqual(second["fallback_files"], list(("review.md", "reproduction_report.md", "result_review.md")))
            for name in ("review.md", "reproduction_report.md", "result_review.md"):
                self.assertTrue((output / name).is_file())

    def test_complete_reports_survive_a_nonzero_editor_exit(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "case"
            command = _editor_command(root, "nonzero.py", """
                for name in ("review.md", "reproduction_report.md", "result_review.md"):
                    (root / name).write_text("## 完整报告", encoding="utf-8")
                raise SystemExit(3)
            """)

            result = _run_with_command(command, **_workflow_inputs(output))

            self.assertTrue(result["ok"], result)
            self.assertEqual(result["completion_mode"], "passed_with_process_warning")
            self.assertIsNotNone(result["process_warning"])

    def test_unsafe_report_shape_remains_a_hard_failure(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "case"
            command = _editor_command(root, "unsafe.py", """
                (root / "review.md").mkdir()
                (root / "reproduction_report.md").write_text("## 报告", encoding="utf-8")
                (root / "result_review.md").write_text("## 报告", encoding="utf-8")
            """)

            result = _run_with_command(command, **_workflow_inputs(output))

            self.assertFalse(result["ok"])
            self.assertFalse(result["retryable"])
            self.assertEqual(result["completion_mode"], "hard_failure")
            self.assertTrue(any("regular file" in issue for issue in result["hard_issues"]))


if __name__ == "__main__":
    unittest.main()
