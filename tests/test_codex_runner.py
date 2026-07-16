import ast
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import threading
import time
import unittest
from unittest.mock import patch

from geng_agent.codex_runner import (
    DEFAULT_GENG_CODEX_MODEL,
    _clear_ephemeral_capability_cache,
    run_codex_subprocess,
)


def _completed(command: list[str], *, stdout: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(command, returncode, stdout, "")


class CodexRunnerEphemeralTests(unittest.TestCase):
    def setUp(self) -> None:
        _clear_ephemeral_capability_cache()

    def _invoke(
        self,
        *,
        role: str = "test",
        prompt: str = "test prompt",
        sandbox: str = "read-only",
        command_override: str | None = None,
        help_text: str = "Usage: codex exec [OPTIONS]\n  --ephemeral",
        output_schema: Path | None = None,
        image_paths: list[Path] | None = None,
    ):
        calls: list[tuple[list[str], dict]] = []
        writes: list[tuple[Path, dict]] = []

        def fake_run(command, **kwargs):
            command = list(command)
            calls.append((command, kwargs))
            if command[-2:] == ["exec", "--help"]:
                return _completed(command, stdout=help_text)
            return _completed(command, stdout="worker stdout")

        with patch("geng_agent.codex_runner.shutil.which", return_value="C:\\tools\\codex.exe"), patch(
            "geng_agent.codex_runner.subprocess.run", side_effect=fake_run
        ), patch("geng_agent.codex_runner.get_config_value", return_value=None), patch(
            "geng_agent.codex_runner.write_json",
            side_effect=lambda path, data: writes.append((path, data)),
        ), patch("geng_agent.codex_runner.write_text"):
            status = run_codex_subprocess(
                role=role,
                work_dir=Path("case"),
                prompt=prompt,
                audit_dir=Path("case") / "audit",
                label=f"{role}_worker",
                sandbox=sandbox,
                timeout=30,
                command_override=command_override,
                output_schema=output_schema,
                image_paths=image_paths,
            )
        return status, calls, writes

    def test_default_command_is_ephemeral_and_prompt_stays_on_stdin(self) -> None:
        self.assertEqual(DEFAULT_GENG_CODEX_MODEL, "gpt-5.5")

        status, calls, writes = self._invoke()

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0][-2:], ["exec", "--help"])
        command, kwargs = calls[1]
        self.assertEqual(command[command.index("exec") + 1], "--ephemeral")
        self.assertEqual(command.count("--ephemeral"), 1)
        self.assertEqual(kwargs["input"], "test prompt")
        self.assertEqual(status["model"], DEFAULT_GENG_CODEX_MODEL)
        self.assertEqual(status["session_persistence"], "ephemeral")
        self.assertTrue(status["ephemeral_capability"]["supported"])
        self.assertFalse(status["ephemeral_capability"]["cached"])
        self.assertEqual(writes[-1][1]["session_persistence"], "ephemeral")

    def test_worker_options_and_project_evidence_outputs_are_preserved(self) -> None:
        schema = Path("schemas") / "result.schema.json"
        images = [Path("paper") / "page_1.png", Path("paper") / "page_2.png"]

        status, calls, _ = self._invoke(
            role="analysis",
            sandbox="workspace-write",
            output_schema=schema,
            image_paths=images,
        )

        command, kwargs = next(item for item in calls if "--ephemeral" in item[0])
        self.assertEqual(command[command.index("--sandbox") + 1], "workspace-write")
        self.assertEqual(command[command.index("--model") + 1], DEFAULT_GENG_CODEX_MODEL)
        self.assertEqual(command[command.index("--output-schema") + 1], str(schema))
        self.assertIn("--output-last-message", command)
        self.assertIn("--cd", command)
        self.assertEqual(command.count("--image"), 2)
        self.assertIn(str(images[0]), command)
        self.assertIn(str(images[1]), command)
        self.assertEqual(command[command.index("--config") + 1], 'model_reasoning_effort="high"')
        self.assertEqual(kwargs["input"], "test prompt")
        self.assertTrue(kwargs["capture_output"])
        self.assertEqual(status["reasoning_effort"], "high")

    def test_unsupported_cli_feature_fails_closed_without_worker_launch(self) -> None:
        status, calls, writes = self._invoke(help_text="Usage: codex exec [OPTIONS]")

        self.assertFalse(status["ok"])
        self.assertEqual(status["error_kind"], "unsupported_cli_feature")
        self.assertEqual(status["session_persistence"], "ephemeral")
        self.assertFalse(status["ephemeral_capability"]["supported"])
        self.assertIn("codex update", status["error"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(writes[-1][1]["error_kind"], "unsupported_cli_feature")

    def test_custom_command_arguments_place_ephemeral_inside_exec_subcommand(self) -> None:
        status, calls, _ = self._invoke(
            role="task_writer",
            command_override="codex --profile isolated --ephemeral",
        )

        self.assertTrue(status["ok"])
        self.assertEqual(
            calls[0][0],
            ["C:\\tools\\codex.exe", "--profile", "isolated", "exec", "--help"],
        )
        worker_command = calls[1][0]
        self.assertEqual(
            worker_command[:5],
            ["C:\\tools\\codex.exe", "--profile", "isolated", "exec", "--ephemeral"],
        )
        self.assertEqual(worker_command.count("--ephemeral"), 1)

    def test_all_one_shot_roles_are_ephemeral(self) -> None:
        expected_reasoning = {
            "analysis": "high",
            "task_writer": "medium",
            "task_reporter": "high",
            "report_editor": "medium",
        }

        for role, reasoning in expected_reasoning.items():
            with self.subTest(role=role):
                status, calls, _ = self._invoke(role=role)
                worker_command = next(command for command, _ in calls if "--ephemeral" in command)
                self.assertEqual(worker_command.count("--ephemeral"), 1)
                self.assertEqual(status["session_persistence"], "ephemeral")
                self.assertEqual(status["reasoning_effort"], reasoning)
                self.assertEqual(
                    worker_command[worker_command.index("--config") + 1],
                    f'model_reasoning_effort="{reasoning}"',
                )

    def test_capability_check_is_cached_for_repeated_workers(self) -> None:
        calls: list[list[str]] = []

        def fake_run(command, **kwargs):
            command = list(command)
            calls.append(command)
            return _completed(command, stdout="--ephemeral" if command[-2:] == ["exec", "--help"] else "")

        with patch("geng_agent.codex_runner.shutil.which", return_value="codex"), patch(
            "geng_agent.codex_runner.subprocess.run", side_effect=fake_run
        ), patch("geng_agent.codex_runner.get_config_value", return_value=None), patch(
            "geng_agent.codex_runner.write_json"
        ), patch("geng_agent.codex_runner.write_text"):
            statuses = [
                run_codex_subprocess(
                    role="task_writer",
                    work_dir=Path("."),
                    prompt=str(index),
                    audit_dir=Path("."),
                    label=f"writer_{index}",
                    sandbox="workspace-write",
                    timeout=1,
                )
                for index in range(2)
            ]

        self.assertEqual(sum(command[-2:] == ["exec", "--help"] for command in calls), 1)
        self.assertEqual(sum("--ephemeral" in command for command in calls), 2)
        self.assertFalse(statuses[0]["ephemeral_capability"]["cached"])
        self.assertTrue(statuses[1]["ephemeral_capability"]["cached"])

    def test_single_flight_probe_does_not_serialize_parallel_workers(self) -> None:
        lock = threading.Lock()
        help_calls = 0
        active_workers = 0
        peak_workers = 0

        def fake_run(command, **kwargs):
            nonlocal help_calls, active_workers, peak_workers
            command = list(command)
            if command[-2:] == ["exec", "--help"]:
                with lock:
                    help_calls += 1
                time.sleep(0.05)
                return _completed(command, stdout="--ephemeral")
            with lock:
                active_workers += 1
                peak_workers = max(peak_workers, active_workers)
            time.sleep(0.1)
            with lock:
                active_workers -= 1
            return _completed(command)

        with patch("geng_agent.codex_runner.shutil.which", return_value="codex"), patch(
            "geng_agent.codex_runner.subprocess.run", side_effect=fake_run
        ), patch("geng_agent.codex_runner.get_config_value", return_value=None), patch(
            "geng_agent.codex_runner.write_json"
        ), patch("geng_agent.codex_runner.write_text"):
            with ThreadPoolExecutor(max_workers=4) as executor:
                statuses = list(
                    executor.map(
                        lambda index: run_codex_subprocess(
                            role="task_writer",
                            work_dir=Path("."),
                            prompt=f"writer {index}",
                            audit_dir=Path("."),
                            label=f"writer_{index}",
                            sandbox="workspace-write",
                            timeout=1,
                        ),
                        range(4),
                    )
                )

        self.assertEqual(help_calls, 1)
        self.assertGreaterEqual(peak_workers, 2)
        self.assertTrue(all(status["ok"] for status in statuses))


class CodexRunnerBoundaryTests(unittest.TestCase):
    def test_no_direct_codex_cli_subprocess_bypass(self) -> None:
        source_root = Path(__file__).resolve().parents[1] / "geng_agent"
        offenders: list[str] = []
        subprocess_names = {
            "subprocess.run",
            "subprocess.Popen",
            "subprocess.call",
            "subprocess.check_call",
            "subprocess.check_output",
        }

        for source_path in source_root.rglob("*.py"):
            if source_path.name == "codex_runner.py":
                continue
            tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
            strings = {
                str(node.value)
                for node in ast.walk(tree)
                if isinstance(node, ast.Constant) and isinstance(node.value, str)
            }
            has_codex_command = any(
                value.strip().lower() in {"codex", "codex.exe"}
                or (value.startswith("GENG_CODEX") and value.endswith("_CMD"))
                for value in strings
            )
            if not has_codex_command:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and _qualified_name(node.func) in subprocess_names:
                    offenders.append(
                        f"{source_path.relative_to(source_root)}:{node.lineno}:{_qualified_name(node.func)}"
                    )

        self.assertEqual(offenders, [], msg=f"Codex CLI bypasses centralized runner: {offenders}")


def _qualified_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _qualified_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


if __name__ == "__main__":
    unittest.main()
