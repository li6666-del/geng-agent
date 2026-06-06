from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest

from geng_agent.outputs import write_file_manifest
from geng_agent.openhands_repair import normalize_openhands_model
from geng_agent.prompts import PromptBook
from geng_agent.runner import choose_repro_command, run_repro_once, run_repro_with_repair
from geng_agent.security import build_safe_env


PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="


class RepairLLM:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def complete(self, prompt: str, *, system: str | None = None, response_format: dict | None = None) -> str:
        self.calls.append(prompt)
        return json.dumps(
            {
                "reason": "Create required smoke artifacts after runtime failure.",
                "touched_files": ["run_experiment.py"],
                "scientific_changes": [],
                "files": [
                    {
                        "path": "run_experiment.py",
                        "content": (
                            "import base64\n"
                            f"PNG_B64 = '{PNG_B64}'\n"
                            "from pathlib import Path\n"
                            "Path('outputs').mkdir(exist_ok=True)\n"
                            "Path('outputs/results.csv').write_text('x,y\\n1,2\\n', encoding='utf-8')\n"
                            "Path('outputs/plot.png').write_bytes(base64.b64decode(PNG_B64))\n"
                            "Path('outputs/summary.json').write_text('{\"task_id\":\"smoke\",\"metrics\":{},\"assumptions\":[]}', encoding='utf-8')\n"
                        ),
                    }
                ]
            }
        )


class FailingRepairLLM:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, prompt: str, *, system: str | None = None, response_format: dict | None = None) -> str:
        self.calls += 1
        raise RuntimeError("repair manifest timed out")


def base_repro_files(run_experiment: str) -> list[tuple[str, str]]:
    return [
        ("README.md", "demo"),
        ("requirements.txt", ""),
        ("config.json", "{}"),
        ("config_smoke.json", "{}"),
        ("run_experiment.py", run_experiment),
        ("src/channel.py", ""),
        ("src/modulation.py", ""),
        ("src/metrics.py", ""),
        ("src/simulation.py", ""),
    ]


def write_valid_experiment(path: Path) -> None:
    path.write_text(
        "import base64\n"
        f"PNG_B64 = '{PNG_B64}'\n"
        "from pathlib import Path\n"
        "Path('outputs').mkdir(exist_ok=True)\n"
        "Path('outputs/results.csv').write_text('x,y\\n1,2\\n', encoding='utf-8')\n"
        "Path('outputs/plot.png').write_bytes(base64.b64decode(PNG_B64))\n"
        "Path('outputs/summary.json').write_text('{\"task_id\":\"smoke\",\"metrics\":{},\"assumptions\":[]}', encoding='utf-8')\n",
        encoding="utf-8",
    )


class RunnerTests(unittest.TestCase):
    def test_safe_env_sets_matplotlib_config_dir(self) -> None:
        safe_env = build_safe_env()

        self.assertEqual(safe_env["MPLBACKEND"], "Agg")
        self.assertIn("MPLCONFIGDIR", safe_env)

    def test_openhands_model_uses_openai_prefix_for_compatible_base_url(self) -> None:
        self.assertEqual(normalize_openhands_model("MiniMax-M3", "https://api.minimaxi.com/v1"), "openai/MiniMax-M3")
        self.assertEqual(normalize_openhands_model("openai/MiniMax-M3", "https://api.minimaxi.com/v1"), "openai/MiniMax-M3")

    def test_choose_repro_command_uses_config_flag_when_supported(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "config_smoke.json").write_text("{}\n", encoding="utf-8")
            (root / "run_experiment.py").write_text("parser.add_argument('--config')\n", encoding="utf-8")

            command = choose_repro_command(root)

            self.assertEqual(command[-2:], ["--config", "config_smoke.json"])

    def test_runtime_failure_is_repaired_and_rerun(self) -> None:
        files = base_repro_files("raise ValueError('boom')\n")
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_file_manifest({"files": [{"path": p, "content": c} for p, c in files]}, root)
            llm = RepairLLM()

            result = run_repro_with_repair(
                repro_project_dir=root,
                client=llm,
                prompt_book=PromptBook(),
                system_message="system",
                max_repair_attempts=1,
                timeout_seconds=10,
            )

            self.assertTrue(result["passed"])
            self.assertEqual(result["repair_attempts_used"], 1)
            self.assertEqual(len(llm.calls), 1)
            self.assertTrue((root / "repair_logs" / "attempt_01_run.json").exists())
            self.assertTrue((root / "outputs" / "results.csv").exists())

    def test_runtime_runs_full_profile_after_smoke_passes(self) -> None:
        run_experiment = (
            "import base64, json, sys\n"
            "from pathlib import Path\n"
            f"PNG_B64 = '{PNG_B64}'\n"
            "cfg_name = sys.argv[-1] if len(sys.argv) > 1 else 'config.json'\n"
            "profile = 'smoke' if cfg_name == 'config_smoke.json' else 'full'\n"
            "Path('outputs').mkdir(exist_ok=True)\n"
            "Path('outputs/results.csv').write_text('profile,value\\n' + profile + ',1\\n', encoding='utf-8')\n"
            "Path('outputs/plot.png').write_bytes(base64.b64decode(PNG_B64))\n"
            "Path('outputs/summary.json').write_text(json.dumps({'task_id': profile, 'profile': profile}), encoding='utf-8')\n"
        )
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_file_manifest({"files": [{"path": p, "content": c} for p, c in base_repro_files(run_experiment)]}, root)

            result = run_repro_with_repair(
                repro_project_dir=root,
                client=RepairLLM(),
                prompt_book=PromptBook(),
                system_message="system",
                max_repair_attempts=0,
                timeout_seconds=10,
            )

            self.assertTrue(result["passed"])
            self.assertEqual(result["completed_profiles"], ["smoke", "full"])
            self.assertEqual(result["run_profile"], "full")
            self.assertEqual([attempt["run_profile"] for attempt in result["attempts"]], ["smoke", "full"])
            summary = json.loads((root / "outputs" / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["profile"], "full")
            self.assertTrue((root / "repair_logs" / "full_attempt_01_previous_outputs").exists())

    def test_hybrid_uses_openhands_after_llm_repair_failure(self) -> None:
        files = base_repro_files("raise ValueError('boom')\n")
        calls: list[Path] = []

        def fake_openhands(candidate_dir: Path, prompt: str, config: object) -> dict[str, object]:
            calls.append(candidate_dir)
            write_valid_experiment(candidate_dir / "run_experiment.py")
            (candidate_dir / "src" / "helper.py").write_text("VALUE = 1\n", encoding="utf-8")
            return {"ok": True, "mode": "fake"}

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_file_manifest({"files": [{"path": p, "content": c} for p, c in files]}, root)

            result = run_repro_with_repair(
                repro_project_dir=root,
                client=FailingRepairLLM(),
                prompt_book=PromptBook(),
                system_message="system",
                max_repair_attempts=1,
                timeout_seconds=10,
                repair_backend="hybrid",
                openhands_invoker=fake_openhands,
            )

            self.assertTrue(result["passed"])
            self.assertEqual(result["repair_backend"], "hybrid")
            self.assertEqual(len(calls), 1)
            self.assertTrue((root / "repair_logs" / "attempt_01_openhands_result.json").exists())
            self.assertIn("PNG_B64", (root / "run_experiment.py").read_text(encoding="utf-8"))
            self.assertTrue((root / "src" / "helper.py").exists())

    def test_hybrid_does_not_call_openhands_when_llm_repair_succeeds(self) -> None:
        def forbidden_openhands(candidate_dir: Path, prompt: str, config: object) -> dict[str, object]:
            raise AssertionError("OpenHands should not be called")

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_file_manifest({"files": [{"path": p, "content": c} for p, c in base_repro_files("raise ValueError('boom')\n")]}, root)

            result = run_repro_with_repair(
                repro_project_dir=root,
                client=RepairLLM(),
                prompt_book=PromptBook(),
                system_message="system",
                max_repair_attempts=1,
                timeout_seconds=10,
                repair_backend="hybrid",
                openhands_invoker=forbidden_openhands,
            )

            self.assertTrue(result["passed"])
            self.assertEqual(result["openhands_attempts"], [])

    def test_openhands_backend_records_adapter_error_without_crashing(self) -> None:
        def failing_openhands(candidate_dir: Path, prompt: str, config: object) -> dict[str, object]:
            raise RuntimeError("OpenHands SDK is not installed")

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_file_manifest({"files": [{"path": p, "content": c} for p, c in base_repro_files("raise ValueError('boom')\n")]}, root)

            result = run_repro_with_repair(
                repro_project_dir=root,
                client=RepairLLM(),
                prompt_book=PromptBook(),
                system_message="system",
                max_repair_attempts=1,
                timeout_seconds=10,
                repair_backend="openhands",
                openhands_invoker=failing_openhands,
            )

            self.assertFalse(result["passed"])
            self.assertEqual(result["repair_backend"], "openhands")
            self.assertIn("OpenHands SDK is not installed", result["openhands_attempts"][0]["agent_result"]["error"])
            self.assertTrue((root / "repair_logs" / "attempt_01_openhands_result.json").exists())

    def test_openhands_candidate_rejected_when_security_scan_fails(self) -> None:
        def unsafe_openhands(candidate_dir: Path, prompt: str, config: object) -> dict[str, object]:
            (candidate_dir / "run_experiment.py").write_text("import os\nprint(os.getenv('GENG_LLM_API_KEY'))\n", encoding="utf-8")
            return {"ok": True, "mode": "fake"}

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_file_manifest({"files": [{"path": p, "content": c} for p, c in base_repro_files("raise ValueError('boom')\n")]}, root)

            result = run_repro_with_repair(
                repro_project_dir=root,
                client=RepairLLM(),
                prompt_book=PromptBook(),
                system_message="system",
                max_repair_attempts=1,
                timeout_seconds=10,
                repair_backend="openhands",
                openhands_invoker=unsafe_openhands,
            )

            self.assertFalse(result["passed"])
            self.assertTrue(result["openhands_attempts"][0]["run_result"]["security_issues"])
            self.assertNotIn("os.getenv", (root / "run_experiment.py").read_text(encoding="utf-8"))

    def test_guard_refuses_env_access(self) -> None:
        files = base_repro_files("import os\nprint(os.getenv('GENG_LLM_API_KEY'))\n")
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_file_manifest({"files": [{"path": p, "content": c} for p, c in files]}, root)

            result = run_repro_once(root, timeout_seconds=10)

            self.assertFalse(result["passed"])
            self.assertTrue(result["blocked_by_security"])
            self.assertTrue(result["security_issues"])

    def test_guard_refuses_dynamic_exec_builtin(self) -> None:
        files = base_repro_files("eval('1 + 1')\n")
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_file_manifest({"files": [{"path": p, "content": c} for p, c in files]}, root)

            result = run_repro_once(root, timeout_seconds=10)

            self.assertFalse(result["passed"])
            self.assertTrue(result["blocked_by_security"])
            self.assertTrue(any("dynamic builtin" in issue["message"] for issue in result["security_issues"]))

    def test_guard_refuses_url_requirement(self) -> None:
        files = base_repro_files("print('ok')\n")
        files[1] = ("requirements.txt", "https://example.com/pkg.whl\n")
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_file_manifest({"files": [{"path": p, "content": c} for p, c in files]}, root)

            result = run_repro_once(root, timeout_seconds=10)

            self.assertFalse(result["passed"])
            self.assertTrue(result["blocked_by_security"])
            self.assertTrue(result["requirements_issues"])

    def test_guard_refuses_undeclared_third_party_import(self) -> None:
        files = base_repro_files("import numpy as np\nprint(np.array([1]))\n")
        files[1] = ("requirements.txt", "\n")
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_file_manifest({"files": [{"path": p, "content": c} for p, c in files]}, root)

            result = run_repro_once(root, timeout_seconds=10)

            self.assertFalse(result["passed"])
            self.assertTrue(result["blocked_by_security"])
            self.assertTrue(any("not declared" in issue["message"] for issue in result["requirements_issues"]))

    def test_guard_refuses_uninstalled_declared_dependency(self) -> None:
        import importlib.util as importlib_util
        from unittest.mock import patch

        real_find_spec = importlib_util.find_spec

        def fake_find_spec(name, *args, **kwargs):
            # Simulate a whitelisted package that is absent from the runner interpreter,
            # independent of whether reedsolo happens to be installed in the test env.
            if name == "reedsolo":
                return None
            return real_find_spec(name, *args, **kwargs)

        files = base_repro_files("import reedsolo\nprint('ok')\n")
        files[1] = ("requirements.txt", "reedsolo\n")
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_file_manifest({"files": [{"path": p, "content": c} for p, c in files]}, root)

            with patch("importlib.util.find_spec", side_effect=fake_find_spec):
                result = run_repro_once(root, timeout_seconds=10)

            self.assertFalse(result["passed"])
            self.assertTrue(result["blocked_by_security"])
            self.assertTrue(any("not installed" in issue["message"] for issue in result["requirements_issues"]))

    def test_guard_refuses_broad_try_except_dependency_fallback(self) -> None:
        files = base_repro_files(
            "try:\n"
            "    import numpy as np\n"
            "except Exception:\n"
            "    np = None\n"
            "print('ok')\n"
        )
        files[1] = ("requirements.txt", "numpy\n")
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_file_manifest({"files": [{"path": p, "content": c} for p, c in files]}, root)

            result = run_repro_once(root, timeout_seconds=10)

            self.assertFalse(result["passed"])
            self.assertTrue(result["blocked_by_security"])
            self.assertTrue(any("silently degrade" in issue["message"] for issue in result["requirements_issues"]))


if __name__ == "__main__":
    unittest.main()
