import ast
import json
import importlib.util
import os
import site
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
import subprocess
import sys
import threading
import time
import textwrap
import unittest
from unittest.mock import patch

from geng_agent.codex_runner import (
    DEFAULT_CODEX_TIMEOUT_SECONDS,
    DEFAULT_GENG_CODEX_MODEL,
    _FOUNDATION_UNITTEST_GUARD,
    _clear_ephemeral_capability_cache,
    _foundation_unittest_guard_config,
    resolve_codex_timeout,
    run_codex_subprocess,
    run_python_unittest_subprocess,
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
        timeout: float | None = 30,
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
                timeout=timeout,
                command_override=command_override,
                output_schema=output_schema,
                image_paths=image_paths,
            )
        return status, calls, writes

    def test_default_command_is_ephemeral_and_prompt_stays_on_stdin(self) -> None:
        self.assertEqual(DEFAULT_GENG_CODEX_MODEL, "gpt-5.6-sol")
        self.assertEqual(DEFAULT_CODEX_TIMEOUT_SECONDS, 1800.0)

        status, calls, writes = self._invoke()

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0][-2:], ["exec", "--help"])
        command, kwargs = calls[1]
        self.assertEqual(command[command.index("exec") + 1], "--ephemeral")
        self.assertEqual(command.count("--ephemeral"), 1)
        self.assertEqual(kwargs["input"], "test prompt")
        self.assertEqual(status["model"], DEFAULT_GENG_CODEX_MODEL)
        self.assertEqual(status["timeout_s"], 30.0)
        self.assertEqual(status["session_persistence"], "ephemeral")
        self.assertTrue(status["ephemeral_capability"]["supported"])
        self.assertFalse(status["ephemeral_capability"]["cached"])
        self.assertEqual(writes[-1][1]["session_persistence"], "ephemeral")

    def test_none_timeout_resolves_to_the_central_30_minute_limit(self) -> None:
        status, calls, _ = self._invoke(timeout=None)
        _, kwargs = next(item for item in calls if "--ephemeral" in item[0])
        self.assertEqual(kwargs["timeout"], 1800.0)
        self.assertEqual(status["timeout_s"], 1800.0)

    def test_non_finite_timeout_is_rejected(self) -> None:
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                resolve_codex_timeout(value)

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
        self.assertEqual(command[command.index("--config") + 1], 'model_reasoning_effort="xhigh"')
        self.assertEqual(kwargs["input"], "test prompt")
        self.assertTrue(kwargs["capture_output"])
        self.assertEqual(status["reasoning_effort"], "xhigh")

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
            "analysis": "xhigh",
            "foundation_writer": "xhigh",
            "task_writer": "xhigh",
            "task_reporter": "xhigh",
            "report_editor": "xhigh",
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

    def test_generated_unittest_uses_isolated_guarded_case_environment(self) -> None:
        captured: dict = {}

        def fake_run(command, **kwargs):
            captured["command"] = list(command)
            captured.update(kwargs)
            return _completed(list(command))

        with TemporaryDirectory() as temp, patch(
            "geng_agent.codex_runner.build_safe_env",
            return_value={"PATH": "safe-path", "PYTHONIOENCODING": "utf-8"},
        ), patch("geng_agent.codex_runner.subprocess.run", side_effect=fake_run):
            work_dir = Path(temp)
            result = run_python_unittest_subprocess(work_dir=work_dir)

        env = captured["env"]
        command = captured["command"]
        guard_config = json.loads(command[-1])
        self.assertTrue(result["passed"])
        self.assertEqual(command[:4], [sys.executable, "-I", "-B", "-c"])
        self.assertEqual(command[4], _FOUNDATION_UNITTEST_GUARD)
        self.assertIn("sys.addaudithook(_audit)", command[4])
        self.assertIn('event.startswith("socket.")', command[4])
        self.assertIn('event == "subprocess.Popen"', command[4])
        self.assertIn("def _require_allowed_read", command[4])
        self.assertIn("follows a case symlink outside the sandbox", command[4])
        self.assertIn('event == "os.chdir"', command[4])
        self.assertIn("def _guarded_stat", command[4])
        self.assertIn("builtins.eval = _guarded_eval", command[4])
        self.assertIn("builtins.exec = _guarded_exec", command[4])
        self.assertIn("builtins.compile = _guarded_compile", command[4])
        self.assertIn("builtins.__import__ = _guarded_import", command[4])
        self.assertIn("os.stat = _guarded_stat", command[4])
        self.assertIn("not _is_import_statement(caller)", command[4])
        self.assertIn('event in {"compile", "exec"}', command[4])
        self.assertIn('code_filename.startswith("<")', command[4])
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("GENG_LLM_API_KEY", env)
        self.assertTrue(Path(env["HOME"]).is_relative_to(work_dir))
        self.assertTrue(Path(env["XDG_CACHE_HOME"]).is_relative_to(work_dir))
        self.assertTrue(Path(env["TMPDIR"]).is_relative_to(work_dir))
        self.assertEqual(Path(guard_config["work_dir"]), work_dir.resolve())
        self.assertEqual(guard_config["start_dir"], "tests")
        self.assertTrue(all(Path(path).is_absolute() for path in guard_config["sensitive_roots"]))
        self.assertIn(str(work_dir.resolve()), guard_config["trusted_read_roots"])
        self.assertEqual(captured["cwd"], work_dir.resolve())

    def test_runtime_guard_allows_authorized_features_and_blocks_boundaries(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            work_dir = root / "case"
            tests_dir = work_dir / "tests"
            tests_dir.mkdir(parents=True)
            outside = root / "outside.txt"
            outside.write_text("host-secret", encoding="utf-8")
            scientific_modules = tuple(
                name for name in ("numpy", "matplotlib", "torch")
                if importlib.util.find_spec(name) is not None
            )
            source = textwrap.dedent(
                f"""\
                import glob
                import importlib
                import json
                import math
                import os
                import unittest

                OUTSIDE = {str(outside)!r}
                OUTSIDE_PARENT = {str(root)!r}
                SCIENTIFIC_MODULES = {scientific_modules!r}


                class GuardBehaviorTest(unittest.TestCase):
                    def test_authorized_foundation_features(self):
                        loaded = importlib.import_module("fractions")
                        self.assertEqual(loaded.Fraction(1, 2), loaded.Fraction(2, 4))
                        self.assertEqual(json.loads("1"), 1)
                        self.assertEqual(math.floor(1.5), 1)
                        os.environ["FOUNDATION_TEST_FLAG"] = "yes"
                        self.assertEqual(os.environ.get("FOUNDATION_TEST_FLAG"), "yes")
                        self.assertEqual(os.getenv("FOUNDATION_TEST_FLAG"), "yes")

                        class Box:
                            pass

                        box = Box()
                        setattr(box, "value", 3)
                        self.assertEqual(getattr(box, "value"), 3)
                        self.assertEqual(vars(box)["value"], 3)
                        self.assertIn("GuardBehaviorTest", globals())
                        delattr(box, "value")
                        self.assertFalse(hasattr(box, "value"))

                    def test_installed_scientific_dependencies_import(self):
                        imported = []
                        for module_name in SCIENTIFIC_MODULES:
                            with self.subTest(module=module_name):
                                module = importlib.import_module(module_name)
                                imported.append(module.__name__)
                        self.assertEqual(imported, list(SCIENTIFIC_MODULES))

                    def test_external_file_and_directory_access_is_blocked(self):
                        actions = (
                            lambda: open(OUTSIDE, encoding="utf-8").read(),
                            lambda: os.listdir(OUTSIDE_PARENT),
                            lambda: list(os.scandir(OUTSIDE_PARENT)),
                            lambda: glob.glob(os.path.join(OUTSIDE_PARENT, "*")),
                            lambda: os.stat(OUTSIDE),
                            lambda: os.chdir(OUTSIDE_PARENT),
                        )
                        for action in actions:
                            with self.subTest(action=action):
                                with self.assertRaises(PermissionError):
                                    action()

                    def test_dynamic_execution_direct_and_alias_calls_are_blocked(self):
                        eval_alias = eval
                        exec_alias = exec
                        compile_alias = compile
                        import_alias = __import__
                        actions = (
                            lambda: eval("1 + 1"),
                            lambda: eval_alias("1 + 1"),
                            lambda: exec("value = 1"),
                            lambda: exec_alias("value = 1"),
                            lambda: compile("1 + 1", "<case>", "eval"),
                            lambda: compile_alias("1 + 1", "<case>", "eval"),
                            lambda: __import__("decimal"),
                            lambda: import_alias("decimal"),
                        )
                        for action in actions:
                            with self.subTest(action=action):
                                with self.assertRaises(PermissionError):
                                    action()
                """
            )
            (tests_dir / "test_guard_behavior.py").write_text(source, encoding="utf-8")

            result = run_python_unittest_subprocess(work_dir=work_dir, timeout=30)

        self.assertTrue(
            result["passed"],
            msg=f"guard behavior suite failed:\n{result.get('stdout')}\n{result.get('stderr')}",
        )

    def test_runtime_guard_blocks_case_symlink_escape(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            work_dir = root / "case"
            tests_dir = work_dir / "tests"
            tests_dir.mkdir(parents=True)
            outside = root / "outside.txt"
            outside.write_text("host-secret", encoding="utf-8")
            escape = work_dir / "escape.txt"
            try:
                escape.symlink_to(outside)
            except (NotImplementedError, OSError) as exc:
                self.skipTest(f"symlink creation is unavailable: {exc}")
            source = textwrap.dedent(
                f"""\
                import unittest

                ESCAPE = {str(escape)!r}



                class SymlinkEscapeTest(unittest.TestCase):
                    def test_symlink_read_is_blocked(self):
                        with self.assertRaises(PermissionError):
                            open(ESCAPE, encoding="utf-8").read()
                """
            )
            (tests_dir / "test_symlink_escape.py").write_text(source, encoding="utf-8")

            result = run_python_unittest_subprocess(work_dir=work_dir, timeout=30)

        self.assertTrue(
            result["passed"],
            msg=f"symlink guard suite failed:\n{result.get('stdout')}\n{result.get('stderr')}",
        )

    def test_guard_config_contains_only_paths_and_discovery_name(self) -> None:
        with TemporaryDirectory() as temp:
            work_dir = Path(temp)
            config = _foundation_unittest_guard_config(work_dir=work_dir, start_dir="tests")

        self.assertEqual(
            set(config),
            {"work_dir", "start_dir", "sensitive_roots", "trusted_read_roots"},
        )
        self.assertEqual(config["work_dir"], str(work_dir.resolve()))
        self.assertEqual(config["start_dir"], "tests")
        self.assertTrue(config["sensitive_roots"])
        self.assertTrue(
            all(
                isinstance(path, str) and Path(path).is_absolute()
                for key in ("sensitive_roots", "trusted_read_roots")
                for path in config[key]
            )
        )
        self.assertIn(str(Path(sys.prefix).resolve()), config["trusted_read_roots"])
        self.assertIn(str(Path(sys.base_prefix).resolve()), config["trusted_read_roots"])
        for package_root in site.getsitepackages():
            self.assertIn(str(Path(package_root).resolve()), config["trusted_read_roots"])
        if os.name == "nt" and os.environ.get("SystemRoot"):
            self.assertIn(
                str(Path(os.environ["SystemRoot"]).resolve()),
                config["trusted_read_roots"],
            )
        elif Path("/usr/lib").exists():
            self.assertIn(str(Path("/usr/lib").absolute()), config["trusted_read_roots"])


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
