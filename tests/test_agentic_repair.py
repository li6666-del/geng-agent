import json
import os
import sys
import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from geng_agent.agentic_repair import (
    build_agentic_repair_brief,
    collect_symptoms_by_task,
    run_agentic_science_repair,
)
from geng_agent.io_runtime import inject_io_runtime
from geng_agent.task_scripts import build_tasks_manifest, write_task_scaffolding


MISMATCH = {
    "task_id": "reproduce_fig_5",
    "paper_result_summary": "论文：经验和速率应低于两个上界",
    "local_result_summary": "本地：经验速率量级错误",
    "differences": ["量级差几个数量级"],
    "possible_causes": ["上界公式实现/归一化错"],
    "baseline_finding": "无法进行有效比较",
    "reproduction_logic_finding": "上界计算可疑",
}


def _make_project(temp: Path) -> tuple[Path, dict]:
    """A minimal per-task project with REAL harness-injected trusted files, so the
    restore-after-agent comparison is against canonical content."""
    proj = temp / "repro_project"
    (proj / "src").mkdir(parents=True)
    (proj / "src" / "__init__.py").write_text("", encoding="utf-8")
    (proj / "src" / "modulation.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (proj / "requirements.txt").write_text("numpy\n", encoding="utf-8")
    out = proj / "outputs" / "reproduce_fig_5"
    out.mkdir(parents=True)
    # an all-zero column -> the symptom detector must produce a lead for the brief
    out.joinpath("results.csv").write_text("x,sum_rate\n1,0\n2,0\n", encoding="utf-8")
    # two tasks: fig_5 is the offending one (editable), fig_4 passed review (protected)
    manifest = build_tasks_manifest(
        {"repro_tasks": [{"task_id": "reproduce_fig_5"}, {"task_id": "reproduce_fig_4"}]}
    )
    (proj / "tasks").mkdir()
    (proj / "tasks" / "reproduce_fig_5.py").write_text("def main(config_path=None):\n    return 0\n", encoding="utf-8")
    (proj / "tasks" / "reproduce_fig_4.py").write_text("def main(config_path=None):\n    return 4\n", encoding="utf-8")
    inject_io_runtime(proj)
    write_task_scaffolding(proj, manifest)
    return proj, manifest


def _write_mock_codex(temp: Path, body: str) -> str:
    """A fake codex CLI: a python script invoked as `<python> <script> exec --sandbox ... --cd
    <proj> <brief>`. Returns the GENG_CODEX_CMD-style command string (quoted, with spaces)."""
    script = temp / "mock_codex.py"
    script.write_text(
        textwrap.dedent(
            """
            import sys
            from pathlib import Path
            args = sys.argv[1:]
            proj = None
            for i, a in enumerate(args):
                if a == "--cd":
                    proj = Path(args[i + 1])
            assert proj is not None, "no --cd passed"
            """
        )
        + textwrap.dedent(body),
        encoding="utf-8",
    )
    return f'"{sys.executable}" "{script}"'


class BriefTests(unittest.TestCase):
    def test_brief_carries_diagnosis_symptoms_contract_and_scripts(self) -> None:
        brief = build_agentic_repair_brief(
            mismatches=[MISMATCH],
            symptoms_by_task={"reproduce_fig_5": ["列 ['sum_rate'] 整列≈0 → 查 SNR/归一化。"]},
            thesis_anchor="\n# 【论文思路·复现靶子】STAB > ZF\n",
            offending_scripts=["tasks/reproduce_fig_5.py"],
        )
        for needle in (
            "reproduce_fig_5",                      # the mismatch
            "整列≈0",                                # the numeric symptom lead
            "STAB > ZF",                            # the thesis anchor
            "python -m tasks.<task_id>",            # how to run/verify
            "绝不修改：src/_io.py",                  # the hard contract
            "tasks/reproduce_fig_5.py",             # the allowed task script
            "绝不硬凑/伪造数值",                     # honesty rule
        ):
            self.assertIn(needle, brief)

    def test_symptoms_collected_from_task_output_csv(self) -> None:
        with TemporaryDirectory() as temp_dir:
            proj, manifest = _make_project(Path(temp_dir))
            symptoms = collect_symptoms_by_task(proj, [MISMATCH], manifest)
            self.assertIn("reproduce_fig_5", symptoms)
            self.assertIn("整列≈0", " ".join(symptoms["reproduce_fig_5"]))


class AgenticRepairEffectTests(unittest.TestCase):
    def test_agent_edit_kept_and_trusted_files_restored(self) -> None:
        with TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            proj, manifest = _make_project(temp)
            io_before = (proj / "src" / "_io.py").read_text(encoding="utf-8")
            fig4_before = (proj / "tasks" / "reproduce_fig_4.py").read_text(encoding="utf-8")
            cmd = _write_mock_codex(
                temp,
                """
                mod = proj / "src" / "modulation.py"
                mod.write_text(mod.read_text(encoding="utf-8") + "\\n# fixed-by-mock\\n", encoding="utf-8")
                fig5 = proj / "tasks" / "reproduce_fig_5.py"
                fig5.write_text(fig5.read_text(encoding="utf-8") + "\\n# fig5-edit\\n", encoding="utf-8")
                (proj / "tasks" / "reproduce_fig_4.py").write_text("HIJACKED", encoding="utf-8")
                (proj / "src" / "_io.py").write_text("SABOTAGED", encoding="utf-8")
                (proj / "tasks_manifest.json").write_text("{}", encoding="utf-8")
                (proj / "requirements.txt").write_text("evil-package\\n", encoding="utf-8")
                print("mock codex done")
                """,
            )
            status = run_agentic_science_repair(
                repro_project_dir=proj,
                mismatches=[MISMATCH],
                tasks_manifest=manifest,
                thesis_anchor="",
                audit_dir=temp / "audit",
                timeout=60,
                codex_cmd=cmd,
            )
            self.assertTrue(status["ok"])
            self.assertEqual(status["returncode"], 0)
            # the agent's edits to ALLOWED files survive…
            self.assertIn("fixed-by-mock", (proj / "src" / "modulation.py").read_text(encoding="utf-8"))
            self.assertIn("fig5-edit", (proj / "tasks" / "reproduce_fig_5.py").read_text(encoding="utf-8"))
            # …but every protected file is back to canonical content — including the
            # NON-offending task script the contract forbade touching.
            self.assertEqual((proj / "tasks" / "reproduce_fig_4.py").read_text(encoding="utf-8"), fig4_before)
            self.assertEqual((proj / "src" / "_io.py").read_text(encoding="utf-8"), io_before)
            restored_manifest = json.loads((proj / "tasks_manifest.json").read_text(encoding="utf-8"))
            self.assertTrue(restored_manifest.get("tasks"))
            # canonical = what inject_io_runtime maintains (it appends matplotlib for _io);
            # the point is the agent's "evil-package" is GONE.
            requirements = (proj / "requirements.txt").read_text(encoding="utf-8")
            self.assertNotIn("evil-package", requirements)
            self.assertIn("numpy", requirements)
            self.assertEqual(
                sorted(status["touched_trusted"]),
                ["requirements.txt", "src/_io.py", "tasks/reproduce_fig_4.py", "tasks_manifest.json"],
            )
            # transcript + brief persisted for audit
            transcript = Path(status["transcript"]).read_text(encoding="utf-8")
            self.assertIn("mock codex done", transcript)
            brief = (temp / "audit" / "06_agentic_repair_codex_round_01_brief.md").read_text(encoding="utf-8")
            self.assertIn("整列≈0", brief)

    def test_missing_cli_reports_error_and_leaves_project_untouched(self) -> None:
        with TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            proj, manifest = _make_project(temp)
            before = (proj / "src" / "modulation.py").read_text(encoding="utf-8")
            status = run_agentic_science_repair(
                repro_project_dir=proj,
                mismatches=[MISMATCH],
                tasks_manifest=manifest,
                thesis_anchor="",
                audit_dir=temp / "audit",
                timeout=10,
                codex_cmd="definitely-not-a-real-cli-xyz",
            )
            self.assertFalse(status["ok"])
            self.assertIn("not found", status["error"])
            self.assertEqual((proj / "src" / "modulation.py").read_text(encoding="utf-8"), before)

    def test_timeout_is_reported_not_raised(self) -> None:
        with TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            proj, manifest = _make_project(temp)
            cmd = _write_mock_codex(temp, "import time\ntime.sleep(5)\n")
            status = run_agentic_science_repair(
                repro_project_dir=proj,
                mismatches=[MISMATCH],
                tasks_manifest=manifest,
                thesis_anchor="",
                audit_dir=temp / "audit",
                timeout=1,
                codex_cmd=cmd,
            )
            self.assertFalse(status["ok"])
            self.assertTrue(status["timed_out"])
            # trusted files still canonical after a timed-out session
            self.assertIn("def begin", (proj / "src" / "_io.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
