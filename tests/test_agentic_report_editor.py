import base64
import json
import os
import sys
import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from geng_agent.agentic_report_editor import (
    REPORT_EDITOR_PROMPT_VERSION,
    run_codex_report_editor_workflow,
)


PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="


def _fake_editor(root: Path) -> str:
    script = root / "report_editor.py"
    script.write_text(textwrap.dedent("""
        import json
        import sys
        from pathlib import Path
        if sys.argv[1:] == ["exec", "--help"]:
            print("--ephemeral")
            raise SystemExit(0)
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
        "import sys\n"
        "if sys.argv[1:] == ['exec', '--help']:\n"
        "    print('--ephemeral')\n"
        "    raise SystemExit(0)\n"
        "from pathlib import Path\nroot = Path.cwd()\n"
        + textwrap.dedent(body),
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
            "task_id": "task_1", "outcome": "reproduced", "host_action": "complete",
            "comparison_summary": "match", "differences": [], "non_material_differences": [],
            "evidence_files": ["paper.png"], "feedback": [], "confidence": "high",
            "local_assets": ["report_assets/task_1/local_result.png"],
            "paper_assets": ["report_assets/task_1/paper_target.png"],
        }],
        "output_dir": output,
        "audit_dir": output / "audit",
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
    def test_editor_receives_terminal_packets_and_writes_reports(self) -> None:
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
                        "task_id": "task_1", "outcome": "reproduced", "host_action": "complete",
                        "comparison_summary": "match", "differences": [], "non_material_differences": [],
                        "evidence_files": ["paper.png"], "feedback": [], "confidence": "high",
                        "local_assets": ["report_assets/task_1/local_result.png"],
                        "paper_assets": ["report_assets/task_1/paper_target.png"],
                    }],
                    output_dir=output, audit_dir=output / "audit", resume=False,
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

    def test_full_run_count_is_only_attempt_count_and_iteration_records_are_available(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "case"
            inputs = _workflow_inputs(output)
            iterations = [
                {
                    "full_run_index": 1,
                    "scientific_reason": "initial_run",
                    "outcome": "invalid",
                },
                {
                    "full_run_index": 2,
                    "scientific_reason": "invalid_run",
                    "outcome": "supported",
                },
            ]
            writer_result = inputs["task_records"][0]["result_json"]
            writer_result["execution_summary"] = {"full_run_count": 2, "last_returncode": 0}
            writer_result["iteration_records"] = iterations

            result = _run_with_command(_fake_editor(root), **inputs)

            self.assertTrue(result["ok"], result)
            report_input = json.loads(
                (Path(result["workspace"]) / "inputs" / "report_editor_input.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(report_input["task_packets"][0]["iteration_records"], iterations)
            brief = (output / "audit" / "04b_report_editor_brief.md").read_text(encoding="utf-8")
            self.assertIn("is the number of full-run attempts", brief)
            self.assertIn("Never describe that field by itself as `有效完整运行次数`", brief)
            self.assertIn("return code 124", brief)
            self.assertEqual(
                REPORT_EDITOR_PROMPT_VERSION,
                "final_report_editor_v4_run_attempt_semantics",
            )

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

    def test_terminal_not_reproduced_task_without_images_is_reportable(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "case"
            inputs = _workflow_inputs(output)
            for path in (output / "report_assets").rglob("*"):
                if path.is_file():
                    path.unlink()
            inputs["task_verifications"] = [{
                "task_id": "task_1",
                "terminal_outcome": "not_reproduced",
                "comparison_summary": "valid run did not support the core conclusion",
                "evidence_files": ["outputs/task_1/results.csv"],
                "local_assets": [],
                "paper_assets": [],
            }]
            command = _fake_editor(root)

            result = _run_with_command(command, **inputs)

            self.assertTrue(result["ok"], result)
            report_input = json.loads(
                (Path(result["workspace"]) / "inputs" / "report_editor_input.json").read_text(encoding="utf-8")
            )
            packet = report_input["task_packets"][0]
            self.assertEqual(packet["terminal_outcome"], "not_reproduced")
            self.assertEqual(packet["local_assets"], [])
            self.assertEqual(packet["paper_assets"], [])

    def test_missing_declared_crop_becomes_warning_not_failure(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "case"
            inputs = _workflow_inputs(output)
            missing = output / "report_assets" / "task_1" / "paper_target.png"
            missing.unlink()
            command = _fake_editor(root)

            result = _run_with_command(command, **inputs)

            self.assertTrue(result["ok"], result)
            self.assertTrue(any("paper_asset" in item for item in result["asset_warnings"]))

    def test_asset_content_change_invalidates_editor_cache_even_with_same_metadata(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "case"
            command = _fake_editor(root)
            inputs = _workflow_inputs(output)
            first = _run_with_command(command, **inputs)
            self.assertTrue(first["ok"], first)

            cached_inputs = _workflow_inputs(output)
            cached_inputs["resume"] = True
            cached = _run_with_command(command, **cached_inputs)
            self.assertTrue(cached["cached"], cached)

            asset = output / "report_assets" / "task_1" / "local_result.png"
            stat = asset.stat()
            payload = bytearray(asset.read_bytes())
            payload[-1] ^= 1
            asset.write_bytes(bytes(payload))
            os.utime(asset, ns=(stat.st_atime_ns, stat.st_mtime_ns))

            refreshed_inputs = dict(cached_inputs)
            refreshed = _run_with_command(command, **refreshed_inputs)
            self.assertTrue(refreshed["ok"], refreshed)
            self.assertFalse(refreshed["cached"], refreshed)
            self.assertNotEqual(first["input_hash"], refreshed["input_hash"])

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

    def test_missing_report_gets_deterministic_fallback_without_retry(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "case"
            command = _editor_command(root, "partial.py", """
                (root / "review.md").write_text("# 原始总览", encoding="utf-8")
                (root / "result_review.md").write_text("## 原始对比", encoding="utf-8")
            """)
            result = _run_with_command(command, **_workflow_inputs(output))

            self.assertTrue(result["ok"], result)
            self.assertFalse(result["retryable"])
            self.assertEqual(result["completion_mode"], "degraded_fallback")
            self.assertEqual(result["missing_outputs"], [])
            self.assertEqual(result["fallback_files"], ["reproduction_report.md"])
            review = (output / "review.md").read_text(encoding="utf-8")
            self.assertTrue(review.endswith("# 原始总览\n"))
            self.assertIn("任务终态与核验记录", review)
            self.assertTrue((output / "reproduction_report.md").is_file())

    def test_empty_editor_output_gets_all_deterministic_reports(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "case"
            empty_command = _editor_command(root, "empty.py", "pass\n")
            result = _run_with_command(empty_command, **_workflow_inputs(output))

            self.assertTrue(result["ok"], result)
            self.assertFalse(result["retryable"])
            self.assertEqual(result["completion_mode"], "degraded_fallback")
            self.assertTrue(result["degraded_report_generation"])
            self.assertCountEqual(
                result["fallback_files"],
                ["review.md", "reproduction_report.md", "result_review.md"],
            )
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

    def test_reports_larger_than_two_megabytes_remain_deliverable(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "case"
            command = _editor_command(root, "large.py", """
                (root / "review.md").write_text("# 大报告\\n" + "x" * (2 * 1024 * 1024 + 1), encoding="utf-8")
                for name in ("reproduction_report.md", "result_review.md"):
                    (root / name).write_text("## 报告", encoding="utf-8")
            """)

            result = _run_with_command(command, **_workflow_inputs(output))

            self.assertTrue(result["ok"], result)
            self.assertEqual(result["fallback_files"], [])
            self.assertGreater((output / "review.md").stat().st_size, 2 * 1024 * 1024)

    def test_oversized_report_is_quarantined_and_replaced_with_fallback(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "case"
            command = _editor_command(root, "oversized.py", """
                (root / "review.md").write_text("# oversized\\n" + "x" * (128 * 1024), encoding="utf-8")
                for name in ("reproduction_report.md", "result_review.md"):
                    (root / name).write_text("## ??", encoding="utf-8")
            """)

            with patch("geng_agent.agentic_report_editor.REPORT_MARKDOWN_MAX_BYTES", 64 * 1024):
                result = _run_with_command(command, **_workflow_inputs(output))

            self.assertTrue(result["ok"], result)
            self.assertEqual(result["completion_mode"], "degraded_fallback")
            self.assertEqual(result["fallback_files"], ["review.md"])
            self.assertTrue(any("resource limit" in issue for issue in result["recovered_packaging_issues"]))
            discarded = Path(result["workspace"]) / "discarded_report_outputs" / "review.md"
            self.assertGreater(discarded.stat().st_size, 64 * 1024)
            self.assertLess((output / "review.md").stat().st_size, 64 * 1024)

    def test_unsafe_report_shape_is_quarantined_and_replaced(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "case"
            command = _editor_command(root, "unsafe.py", """
                (root / "review.md").mkdir()
                (root / "reproduction_report.md").write_text("## 报告", encoding="utf-8")
                (root / "result_review.md").write_text("## 报告", encoding="utf-8")
            """)

            result = _run_with_command(command, **_workflow_inputs(output))

            self.assertTrue(result["ok"], result)
            self.assertFalse(result["retryable"])
            self.assertEqual(result["completion_mode"], "degraded_fallback")
            self.assertEqual(result["hard_issues"], [])
            self.assertEqual(result["fallback_files"], ["review.md"])
            self.assertTrue(any("regular file" in issue for issue in result["recovered_packaging_issues"]))
            self.assertTrue((output / "review.md").is_file())
            discarded = Path(result["workspace"]) / "discarded_report_outputs" / "review.md"
            self.assertTrue(discarded.is_dir())


if __name__ == "__main__":
    unittest.main()
