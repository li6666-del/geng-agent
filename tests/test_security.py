from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import os
import unittest

from geng_agent.security import (
    FORBIDDEN_BUILTINS,
    codex_safe_env,
    reconcile_whitelisted_requirements,
    requirement_name_for_import,
    split_requirement_issues,
    static_scan_repro_project,
    validate_requirements,
)


def scan_source(source: str) -> list[dict[str, str]]:
    with TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        (root / "run_experiment.py").write_text(source, encoding="utf-8")
        return static_scan_repro_project(root)


def messages(issues: list[dict[str, str]]) -> list[str]:
    return [issue["message"] for issue in issues]


class StaticScanDynamicBuiltinTests(unittest.TestCase):
    def test_flags_every_forbidden_dynamic_builtin(self) -> None:
        for name in FORBIDDEN_BUILTINS:
            with self.subTest(builtin=name):
                issues = scan_source(f"x = {name}()\n")
                self.assertIn(f"forbidden dynamic builtin: {name}", messages(issues))

    def test_flags_getattr_indirection_bypass(self) -> None:
        issues = scan_source("import os\ngetattr(os, 'sys' + 'tem')('echo hi')\n")
        self.assertIn("forbidden dynamic builtin: getattr", messages(issues))

    def test_flags_dunder_import_bypass(self) -> None:
        issues = scan_source("__import__('sock' + 'et')\n")
        self.assertIn("forbidden dynamic builtin: __import__", messages(issues))

    def test_flags_eval_of_string(self) -> None:
        issues = scan_source("eval(\"__import__('os').system('echo hi')\")\n")
        self.assertIn("forbidden dynamic builtin: eval", messages(issues))

    def test_flags_importlib_import(self) -> None:
        issues = scan_source("import importlib\nimportlib.import_module('socket')\n")
        self.assertIn("forbidden import: importlib", messages(issues))

    def test_flags_getattribute_reflection_bypass(self) -> None:
        # getattr is blocked, but __getattribute__ fetches os.system just the same.
        issues = scan_source("import os\nos.__getattribute__('sys' + 'tem')('echo hi')\n")
        self.assertIn("forbidden reflection attribute: __getattribute__", messages(issues))

    def test_flags_subclasses_escape_chain(self) -> None:
        issues = scan_source("p = ().__class__.__bases__[0].__subclasses__()\n")
        flagged = messages(issues)
        self.assertIn("forbidden reflection attribute: __bases__", flagged)
        self.assertIn("forbidden reflection attribute: __subclasses__", flagged)

    def test_flags_os_exec_and_startfile_family(self) -> None:
        for call in ("os.execvp('cmd', ['cmd'])", "os.startfile('calc.exe')", "os.posix_spawn('x', [], {})"):
            with self.subTest(call=call):
                issues = scan_source(f"import os\n{call}\n")
                self.assertTrue(
                    any(m.startswith("forbidden call: os.") for m in messages(issues)),
                    msg=f"{call} was not blocked",
                )

    def test_flags_asyncio_pty_winreg_imports(self) -> None:
        for module in ("asyncio", "pty", "winreg"):
            with self.subTest(module=module):
                issues = scan_source(f"import {module}\n")
                self.assertIn(f"forbidden import: {module}", messages(issues))

    def test_does_not_flag_benign_dunder_or_class_access(self) -> None:
        # __class__/__name__/__dict__ see legitimate use and must stay clean.
        source = (
            "import numpy as np\n"
            "arr = np.array([1, 2, 3])\n"
            "name = arr.__class__.__name__\n"
        )
        self.assertEqual(scan_source(source), [])

    def test_records_file_and_line(self) -> None:
        issues = scan_source("x = 1\ny = eval('2')\n")
        flagged = [issue for issue in issues if issue["message"] == "forbidden dynamic builtin: eval"]
        self.assertEqual(len(flagged), 1)
        self.assertEqual(flagged[0]["file"], "run_experiment.py")
        self.assertEqual(flagged[0]["line"], "2")

    def test_does_not_flag_dotted_or_legitimate_calls(self) -> None:
        # re.compile is an attribute call, not the bare compile builtin; numerical
        # code and relative-path file I/O must stay clean (no false positives).
        source = (
            "import re\n"
            "import numpy as np\n"
            "pattern = re.compile('ab')\n"
            "arr = np.array([1, 2, 3])\n"
            "with open('outputs/results.csv', 'w', encoding='utf-8') as fh:\n"
            "    fh.write('x\\n')\n"
        )
        self.assertEqual(scan_source(source), [])


class ReconcileRequirementsTests(unittest.TestCase):
    def _project(self, tmp: str, *, requirements: str, sim_source: str) -> Path:
        root = Path(tmp)
        (root / "requirements.txt").write_text(requirements, encoding="utf-8")
        (root / "src").mkdir(exist_ok=True)
        (root / "src" / "__init__.py").write_text("", encoding="utf-8")
        (root / "src" / "simulation.py").write_text(sim_source, encoding="utf-8")
        return root

    def test_adds_undeclared_whitelisted_installed_import(self) -> None:
        with TemporaryDirectory() as tmp:
            root = self._project(
                tmp, requirements="numpy\n", sim_source="import numpy as np\nimport scipy.linalg\n"
            )
            added = reconcile_whitelisted_requirements(root)
            self.assertEqual(added, ["scipy"])
            text = (root / "requirements.txt").read_text(encoding="utf-8")
            self.assertIn("scipy", text)
            self.assertIn("numpy", text)

    def test_matplotlib_namespace_import_maps_to_matplotlib_requirement(self) -> None:
        self.assertEqual(requirement_name_for_import("mpl_toolkits"), "matplotlib")
        with TemporaryDirectory() as tmp:
            root = self._project(
                tmp,
                requirements="matplotlib\n",
                sim_source="from mpl_toolkits.axes_grid1.inset_locator import inset_axes\n",
            )

            def fake_find_spec(name: str):
                return object() if name in {"matplotlib", "mpl_toolkits"} else None

            with patch("geng_agent.security.importlib.util.find_spec", side_effect=fake_find_spec):
                self.assertEqual(validate_requirements(root), [])

    def test_reconcile_adds_matplotlib_for_mpl_toolkits_namespace_import(self) -> None:
        with TemporaryDirectory() as tmp:
            root = self._project(
                tmp,
                requirements="",
                sim_source="from mpl_toolkits.axes_grid1.inset_locator import inset_axes\n",
            )
            with patch("geng_agent.security.importlib.util.find_spec", return_value=object()):
                self.assertEqual(reconcile_whitelisted_requirements(root), ["matplotlib"])
            self.assertIn("matplotlib", (root / "requirements.txt").read_text(encoding="utf-8"))

    def test_split_requirement_issues_downgrades_installed_whitelisted_missing_declaration(self) -> None:
        issue = {
            "file": "tasks/demo.py",
            "line": "1",
            "message": "third-party import is not declared in requirements.txt: scipy.linalg (expected package scipy)",
        }
        with patch("geng_agent.security.importlib.util.find_spec", return_value=object()):
            blocking, warnings = split_requirement_issues([issue])
        self.assertEqual(blocking, [])
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]["severity"], "warning")

    def test_split_requirement_issues_keeps_unknown_package_blocking(self) -> None:
        issue = {
            "file": "tasks/demo.py",
            "line": "1",
            "message": "third-party import is not declared in requirements.txt: yaml (expected package yaml)",
        }
        blocking, warnings = split_requirement_issues([issue])
        self.assertEqual(warnings, [])
        self.assertEqual(len(blocking), 1)
        self.assertEqual(blocking[0]["severity"], "error")

    def test_does_not_add_non_whitelisted_import(self) -> None:
        with TemporaryDirectory() as tmp:
            root = self._project(
                tmp, requirements="numpy\n", sim_source="import numpy as np\nimport yaml\n"
            )
            self.assertEqual(reconcile_whitelisted_requirements(root), [])
            self.assertNotIn("yaml", (root / "requirements.txt").read_text(encoding="utf-8"))

    def test_noop_when_all_declared(self) -> None:
        with TemporaryDirectory() as tmp:
            root = self._project(
                tmp, requirements="numpy\nscipy\n", sim_source="import numpy as np\nimport scipy.linalg\n"
            )
            self.assertEqual(reconcile_whitelisted_requirements(root), [])

    def test_trusted_backend_torch_call_requires_requirement(self) -> None:
        with TemporaryDirectory() as tmp:
            root = self._project(
                tmp,
                requirements="numpy\n",
                sim_source="from src import _backend\n\ndef f():\n    return _backend.torch()\n",
            )
            issues = validate_requirements(root)
            self.assertIn(
                "trusted torch backend is used but requirements.txt does not declare torch",
                messages(issues),
            )

    def test_reconcile_adds_torch_for_trusted_backend_when_installed(self) -> None:
        with TemporaryDirectory() as tmp:
            root = self._project(
                tmp,
                requirements="numpy\n",
                sim_source="from src import _backend\n\ndef f():\n    return _backend.torch()\n",
            )
            with patch("geng_agent.security.importlib.util.find_spec", return_value=object()):
                self.assertEqual(reconcile_whitelisted_requirements(root), ["torch"])
            self.assertIn("torch", (root / "requirements.txt").read_text(encoding="utf-8"))


class CodexSafeEnvTests(unittest.TestCase):
    def test_strips_geng_secrets_but_keeps_codex_creds(self) -> None:
        fake = {
            "GENG_LLM_API_KEY": "geng-secret",
            "GENG_LLM2_API_KEY": "geng-secret-2",
            "OPENAI_API_KEY": "codex-needs-this",
            "PATH": "/usr/bin",
        }
        with patch.dict("os.environ", fake, clear=True):
            env = codex_safe_env()
        # geng's own keys are not something codex needs -> drop them
        self.assertNotIn("GENG_LLM_API_KEY", env)
        self.assertNotIn("GENG_LLM2_API_KEY", env)
        # codex authenticates with its own creds and needs PATH -> these must survive
        self.assertEqual(env["OPENAI_API_KEY"], "codex-needs-this")
        self.assertEqual(env["PATH"], "/usr/bin")

    def test_geng_python_is_preferred_for_codex_shell_python(self) -> None:
        with TemporaryDirectory() as temp_dir:
            python_path = Path(temp_dir) / "miniconda3" / "envs" / "torch" / "python.exe"
            python_path.parent.mkdir(parents=True)
            python_path.write_text("", encoding="utf-8")
            python_dir = python_path.parent
            fake = {
                "GENG_PYTHON": f'"{python_path}"',
                "PATH": os.pathsep.join(["/usr/bin", str(python_dir)]),
            }
            with patch.dict("os.environ", fake, clear=True):
                env = codex_safe_env()

        path_parts = env["PATH"].split(os.pathsep)
        self.assertEqual(
            path_parts[:3],
            [
                str(python_dir),
                str(python_dir / "Scripts"),
                str(python_dir / "Library" / "bin"),
            ],
        )
        self.assertEqual(path_parts.count(str(python_dir)), 1)
        self.assertEqual(env["PYTHON"], str(python_path))
        self.assertEqual(env["GENG_PYTHON"], str(python_path))
        self.assertEqual(env["CONDA_PREFIX"], str(python_dir))

    def test_invalid_geng_python_falls_back_to_default_torch_env(self) -> None:
        with TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            default_python = home / "miniconda3" / "envs" / "torch" / ("python.exe" if os.name == "nt" else "bin/python")
            default_python.parent.mkdir(parents=True)
            default_python.write_text("", encoding="utf-8")
            fake = {
                "USERPROFILE": str(home),
                "GENG_PYTHON": str(home / "missing" / "python.exe"),
                "PATH": "/usr/bin",
            }
            with patch.dict("os.environ", fake, clear=True):
                env = codex_safe_env()

        self.assertEqual(env["GENG_PYTHON"], str(default_python))
        self.assertEqual(env["PYTHON"], str(default_python))

    def test_default_torch_env_is_used_for_codex_when_geng_python_is_absent(self) -> None:
        with TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            if os.name == "nt":
                python_path = home / "miniconda3" / "envs" / "torch" / "python.exe"
            else:
                python_path = home / "miniconda3" / "envs" / "torch" / "bin" / "python"
            python_path.parent.mkdir(parents=True)
            python_path.write_text("", encoding="utf-8")
            fake = {
                "USERPROFILE": str(home),
                "PATH": "/usr/bin",
            }
            with patch.dict("os.environ", fake, clear=True):
                env = codex_safe_env()

        self.assertEqual(env["GENG_PYTHON"], str(python_path))
        self.assertEqual(env["PYTHON"], str(python_path))
        self.assertTrue(env["PATH"].split(os.pathsep)[0].endswith(str(python_path.parent)))


if __name__ == "__main__":
    unittest.main()
