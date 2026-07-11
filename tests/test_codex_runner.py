from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from geng_agent.codex_runner import DEFAULT_GENG_CODEX_MODEL, run_codex_subprocess


class CodexRunnerModelTests(unittest.TestCase):
    def test_project_subprocess_defaults_to_gpt_5_5(self) -> None:
        self.assertEqual(DEFAULT_GENG_CODEX_MODEL, "gpt-5.5")
        with patch("geng_agent.codex_runner.shutil.which", return_value="codex"), patch(
            "geng_agent.codex_runner.subprocess.run"
        ) as run, patch("geng_agent.codex_runner.write_json"), patch("geng_agent.codex_runner.write_text"):
            run.return_value.returncode = 0
            run.return_value.stdout = ""
            run.return_value.stderr = ""
            status = run_codex_subprocess(
                role="test",
                work_dir=__import__("pathlib").Path("."),
                prompt="test",
                audit_dir=__import__("pathlib").Path("."),
                label="test_model",
                sandbox="read-only",
                timeout=1,
            )

        command = run.call_args.kwargs["args"] if "args" in run.call_args.kwargs else run.call_args.args[0]
        self.assertEqual(status["model"], DEFAULT_GENG_CODEX_MODEL)
        self.assertEqual(command[command.index("--model") + 1], DEFAULT_GENG_CODEX_MODEL)
        self.assertEqual(status["reasoning_effort"], None)

    def test_task_writer_overrides_global_reasoning_effort(self) -> None:
        with patch("geng_agent.codex_runner.shutil.which", return_value="codex"), patch(
            "geng_agent.codex_runner.subprocess.run"
        ) as run, patch("geng_agent.codex_runner.write_json"), patch("geng_agent.codex_runner.write_text"):
            run.return_value.returncode = 0
            run.return_value.stdout = ""
            run.return_value.stderr = ""
            status = run_codex_subprocess(
                role="task_writer",
                work_dir=__import__("pathlib").Path("."),
                prompt="test",
                audit_dir=__import__("pathlib").Path("."),
                label="test_reasoning",
                sandbox="workspace-write",
                timeout=1,
            )

        command = run.call_args.kwargs["args"] if "args" in run.call_args.kwargs else run.call_args.args[0]
        self.assertEqual(status["reasoning_effort"], "medium")
        self.assertEqual(command[command.index("--config") + 1], 'model_reasoning_effort="medium"')

    def test_task_writer_timeout_excludes_guarded_wait_and_full_phases(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            script = root / "fake_codex.py"
            timeout_state = root / "timeout_state.json"
            script.write_text(
                "import json, os, sys, time\n"
                "from pathlib import Path\n"
                "sys.stdin.read()\n"
                "state = Path(os.environ['TEST_TIMEOUT_STATE'])\n"
                "state.write_text(json.dumps({'phase':'full_run','heartbeat':time.time()}), encoding='utf-8')\n"
                "time.sleep(0.6)\n"
                "state.unlink(missing_ok=True)\n"
                "time.sleep(0.05)\n",
                encoding="utf-8",
            )
            status = run_codex_subprocess(
                role="task_writer",
                work_dir=root,
                prompt="test",
                audit_dir=root,
                label="paused_timeout",
                sandbox="workspace-write",
                timeout=0.25,
                command_override=f'"{sys.executable}" "{script}"',
                extra_env={"TEST_TIMEOUT_STATE": str(timeout_state)},
                timeout_state_path=timeout_state,
            )

        self.assertTrue(status["ok"], msg=status)
        self.assertFalse(status["timed_out"])
        self.assertGreaterEqual(float(status["excluded_duration_s"]), 0.4)
        self.assertLess(float(status["active_duration_s"]), 0.25)


if __name__ == "__main__":
    unittest.main()
