from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import os
import unittest

from geng_agent.security import (
    FOUNDATION_STATIC_SECURITY_ADVISORY_CATEGORIES,
    FORBIDDEN_BUILTINS,
    codex_safe_env,
    reconcile_runtime_requirements,
    requirement_name_for_import,
    split_static_security_issues,
    split_requirement_issues,
    static_scan_repro_project,
    validate_requirements,
    _runtime_lock_is_trusted,
)


def scan_source(source: str) -> list[dict[str, str]]:
    with TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        (root / "run_experiment.py").write_text(source, encoding="utf-8")
        return static_scan_repro_project(root)


def messages(issues: list[dict[str, str]]) -> list[str]:
    return [issue["message"] for issue in issues]


def trusted_lock(requirements: list[dict]) -> dict:
    normalized_requirements = []
    for raw_item in requirements:
        item = dict(raw_item)
        if "resolution_source" not in item:
            item["resolution_source"] = (
                "not_applicable"
                if item.get("applicable") is False
                else "trusted_index"
            )
        normalized_requirements.append(item)
    artifacts = [
        {
            "distribution": item["distribution"],
            "version": item["installed_version"],
            "url": (
                "https://files.pythonhosted.org/packages/"
                f"{item['distribution']}-{item['installed_version']}.whl"
            ),
            "sha256": "d" * 64,
        }
        for item in normalized_requirements
        if (
            item.get("applicable") is not False
            and item.get("satisfied") is True
            and item.get("resolution_source") == "trusted_index"
        )
    ]
    return {
        "kind": "geng.case_environment.lock",
        "ready": True,
        "runtime_mode": "host_shared",
        "host_provenance": {
            "kind": "geng.host_shared_runtime",
            "runtime_mode": "host_shared",
            "selected_launcher": "/trusted/bin/python",
            "resolved_executable": "/trusted/bin/python3",
            "prefix": "/trusted",
            "mutex_identity_sha256": "e" * 64,
        },
        "source_policy": {
            "trusted": True,
            "host_runtime_verified": any(
                item.get("resolution_source") == "host_runtime"
                for item in normalized_requirements
            ),
            "artifact_report_verified": bool(artifacts),
            "binary_wheels_only": True,
            "artifact_evidence": {
                "plan_report_sha256": "b" * 64,
                "install_report_sha256": "c" * 64,
                "artifacts": artifacts,
            },
        },
        "index": {
            "fingerprint": "a" * 64,
            "artifact_hosts": ["files.pythonhosted.org", "pypi.org"],
        },
        "requirements": normalized_requirements,
    }

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

    def test_importlib_import_module_honors_keywords_and_relative_packages(self) -> None:
        safe_sources = (
            "import importlib\nimportlib.import_module(name='numpy')\n",
            "import importlib\nimportlib.import_module('num' + 'py')\n",
            "import importlib\nimportlib.import_module('.layers', package='src.models')\n",
            "import importlib\nimportlib.import_module(name='.layers', package='src.models')\n",
        )
        for source in safe_sources:
            with self.subTest(source=source):
                self.assertFalse(
                    any(
                        item.startswith("dangerous dynamic import:")
                        for item in messages(scan_source(source))
                    )
                )

        hard_sources = (
            "import importlib\nname = 'numpy'\nimportlib.import_module(name)\n",
            "import importlib\nimportlib.import_module(name='socket')\n",
            "import importlib\nimportlib.import_module('.layers')\n",
            (
                "import importlib\npackage = 'src.models'\n"
                "importlib.import_module('.layers', package=package)\n"
            ),
            "import importlib\nimportlib.import_module('.path', package='os')\n",
        )
        for source in hard_sources:
            with self.subTest(source=source):
                self.assertTrue(
                    any(
                        item.startswith("dangerous dynamic import:")
                        for item in messages(scan_source(source))
                    )
                )

    def test_assignment_aliases_preserve_module_security_identity(self) -> None:
        import_issues = scan_source(
            "import importlib\n"
            "lib = importlib\n"
            "lib.import_module(name='socket')\n"
        )
        self.assertIn(
            "dangerous dynamic import: forbidden module target 'socket'",
            messages(import_issues),
        )

        process_issues = scan_source(
            "import os\n"
            "op = os\n"
            "op.system('echo blocked')\n"
        )
        self.assertIn("forbidden call: os.system", messages(process_issues))

        loader_issues = scan_source(
            "import importlib\n"
            "machinery = importlib.machinery\n"
            "machinery.SourceFileLoader('x', '/outside/module.py')\n"
        )
        self.assertTrue(
            any(
                item.startswith("dangerous dynamic import: loader construction")
                for item in messages(loader_issues)
            ),
            loader_issues,
        )

    def test_dunder_builtins_is_normalized_for_direct_and_reflected_access(self) -> None:
        for capability in ("eval", "exec", "compile", "__import__"):
            with self.subTest(capability=capability, access="direct"):
                issues = scan_source(f"__builtins__.{capability}()\n")
                self.assertIn(
                    f"forbidden dynamic builtin: {capability}",
                    messages(issues),
                )
            with self.subTest(capability=capability, access="getattr"):
                issues = scan_source(
                    f"getattr(__builtins__, {capability!r})()\n"
                )
                self.assertTrue(
                    any(
                        item.startswith("dangerous reflection:")
                        for item in messages(issues)
                    ),
                    issues,
                )
            with self.subTest(capability=capability, access="vars"):
                issues = scan_source(f"vars(__builtins__)[{capability!r}]()\n")
                self.assertTrue(
                    any(
                        item.startswith("dangerous reflection:")
                        for item in messages(issues)
                    ),
                    issues,
                )

    def test_dangerous_getattr_requires_a_foldable_dangerous_attribute(self) -> None:
        for expression in ("'sys' + 'tem'", "f'system'"):
            with self.subTest(expression=expression):
                issues = scan_source(f"import os\ngetattr(os, {expression})\n")
                self.assertIn(
                    "dangerous reflection: sensitive module os attribute 'system'",
                    messages(issues),
                )

        for expression in ("attribute", "f'{attribute}'"):
            with self.subTest(expression=expression):
                issues = scan_source(
                    "import os\nattribute = 'system'\n"
                    f"getattr(os, {expression})\n"
                )
                self.assertFalse(
                    any(
                        item.startswith("dangerous reflection:")
                        for item in messages(issues)
                    )
                )
                self.assertIn("forbidden dynamic builtin: getattr", messages(issues))

    def test_literal_mapping_and_loader_reflection_capabilities_are_blocking(self) -> None:
        hard_sources = {
            "direct builtins": "__builtins__['eval']('1')\n",
            "vars builtins": (
                "import builtins\nvars(builtins)['eval']('1')\n"
            ),
            "globals builtins": (
                "globals()['__builtins__']['__import__']('socket')\n"
            ),
            "importlib dict": (
                "import importlib\n"
                "importlib.__dict__['import_module']('socket')\n"
            ),
            "vars os": "import os\nvars(os)['system']('echo blocked')\n",
            "getattr importlib util": (
                "import importlib\n"
                "getattr(importlib.util, 'spec_from_file_location')"
                "('x', '/outside/module.py')\n"
            ),
            "getattr machinery loader": (
                "import importlib\n"
                "getattr(importlib.machinery, 'SourceFileLoader')"
                "('x', '/outside/module.py')\n"
            ),
            "vars loader exec": (
                "spec = object()\nvars(spec.loader)['exec_module'](None)\n"
            ),
            "loader dict load": (
                "spec = object()\n"
                "spec.loader.__dict__['load_module']('x')\n"
            ),
            "getattr loader exec": (
                "spec = object()\ngetattr(spec.loader, 'exec_module')(None)\n"
            ),
            "getattr loader load": (
                "spec = object()\ngetattr(spec.loader, 'load_module')('x')\n"
            ),
        }
        for label, source in hard_sources.items():
            with self.subTest(label=label):
                self.assertTrue(
                    any(
                        item.startswith("dangerous reflection:")
                        for item in messages(scan_source(source))
                    )
                )

    def test_dynamic_mapping_and_loader_reflection_keys_remain_advisory(self) -> None:
        issues = scan_source(
            "import builtins\n"
            "import importlib\n"
            "import os\n"
            "key = '__import__'\n"
            "getattr(__builtins__, key)\n"
            "vars(__builtins__)[key]\n"
            "__builtins__[key]\n"
            "vars(builtins)[key]\n"
            "globals()['__builtins__'][key]\n"
            "importlib.__dict__[key]\n"
            "vars(os)[key]\n"
            "spec = object()\n"
            "getattr(importlib.machinery, key)\n"
            "vars(spec.loader)[key]\n"
            "spec.loader.__dict__[key]\n"
            "getattr(spec.loader, key)\n"
        )
        self.assertFalse(
            any(item.startswith("dangerous reflection:") for item in messages(issues)),
            issues,
        )
        self.assertIn("forbidden dynamic builtin: vars", messages(issues))
        self.assertIn("forbidden dynamic builtin: globals", messages(issues))
        self.assertIn("forbidden dynamic builtin: getattr", messages(issues))

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

    def test_environment_findings_are_foundation_advisories_but_strict_by_default(self) -> None:
        issues = scan_source(
            "import os\n"
            "key = 'OPENAI_' + 'API_KEY'\n"
            "secret = os.getenv('OPENAI_API_KEY')\n"
            "dynamic = os.environ[key]\n"
            "bulk = dict(os.environ)\n"
            "os.environ.update({'MODEL_SIZE': 'small'})\n"
        )

        strict_blocking, strict_warnings = split_static_security_issues(issues)
        self.assertEqual(strict_warnings, [])
        self.assertTrue(strict_blocking)
        self.assertTrue(all(item["severity"] == "error" for item in strict_blocking))

        foundation_blocking, foundation_warnings = split_static_security_issues(
            issues,
            advisory_categories=FOUNDATION_STATIC_SECURITY_ADVISORY_CATEGORIES,
        )
        self.assertEqual(foundation_blocking, [])
        self.assertTrue(foundation_warnings)
        self.assertTrue(
            all(
                item["category"] == "environment_access"
                and item["severity"] == "warning"
                for item in foundation_warnings
            ),
            foundation_warnings,
        )

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
    def test_host_runtime_requirement_uses_probe_and_provenance_not_artifact(self) -> None:
        lock = trusted_lock([{
            "requirement": "torch",
            "distribution": "torch",
            "import_names": ["torch"],
            "applicable": True,
            "installed_version": "2.11.0+cu128",
            "version_satisfied": True,
            "imports_ok": True,
            "satisfied": True,
            "resolution_source": "host_runtime",
        }])
        lock["source_policy"]["artifact_report_verified"] = False
        lock["source_policy"]["artifact_evidence"] = {}
        self.assertTrue(_runtime_lock_is_trusted(lock))

        lock["source_policy"]["host_runtime_verified"] = False
        self.assertFalse(_runtime_lock_is_trusted(lock))

    def test_trusted_index_requirement_still_requires_artifact_evidence(self) -> None:
        lock = trusted_lock([{
            "requirement": "transformers",
            "distribution": "transformers",
            "import_names": ["transformers"],
            "applicable": True,
            "installed_version": "5.0.0",
            "version_satisfied": True,
            "imports_ok": True,
            "satisfied": True,
        }])
        self.assertTrue(_runtime_lock_is_trusted(lock))
        lock["source_policy"]["artifact_evidence"]["artifacts"] = []
        self.assertFalse(_runtime_lock_is_trusted(lock))

    def _project(self, tmp: str, *, requirements: str, sim_source: str) -> Path:
        root = Path(tmp)
        (root / "requirements.txt").write_text(requirements, encoding="utf-8")
        (root / "src").mkdir(exist_ok=True)
        (root / "src" / "__init__.py").write_text("", encoding="utf-8")
        (root / "src" / "simulation.py").write_text(sim_source, encoding="utf-8")
        return root

    def test_adds_undeclared_installed_import(self) -> None:
        with TemporaryDirectory() as tmp:
            root = self._project(
                tmp, requirements="numpy\n", sim_source="import numpy as np\nimport scipy.linalg\n"
            )
            added = reconcile_runtime_requirements(root)
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
                self.assertEqual(reconcile_runtime_requirements(root), ["matplotlib"])
            self.assertIn("matplotlib", (root / "requirements.txt").read_text(encoding="utf-8"))

    def test_split_requirement_issues_downgrades_installed_missing_declaration(self) -> None:
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

    def test_split_requirement_issues_uses_case_lock_for_unknown_package(self) -> None:
        issue = {
            "file": "tasks/demo.py",
            "line": "1",
            "category": "dependency_declaration_missing",
            "package": "custom-runtime",
            "message": "third-party import is not declared in requirements.txt: custom_runtime (expected package custom-runtime)",
        }
        lock = trusted_lock([{
            "requirement": "custom-runtime>=1",
            "distribution": "custom-runtime",
            "import_names": ["custom_runtime"],
            "applicable": True,
            "installed_version": "1.2",
            "version_satisfied": True,
            "imports_ok": True,
            "satisfied": True,
        }])
        blocking, warnings = split_requirement_issues([issue], runtime_lock=lock)
        self.assertEqual(blocking, [])
        self.assertEqual(len(warnings), 1)
        blocking, warnings = split_requirement_issues(
            [issue], runtime_lock=trusted_lock([])
        )
        self.assertEqual(warnings, [])
        self.assertEqual(len(blocking), 1)
        self.assertEqual(blocking[0]["severity"], "error")

    def test_reconcile_adds_unknown_import_proven_by_case_lock(self) -> None:
        with TemporaryDirectory() as tmp:
            root = self._project(
                tmp, requirements="numpy\n", sim_source="import numpy as np\nimport custom_runtime\n"
            )
            lock = trusted_lock([{
                "requirement": "custom-runtime>=1",
                "distribution": "custom-runtime",
                "import_names": ["custom_runtime"],
                "applicable": True,
                "installed_version": "1.2",
                "version_satisfied": True,
                "imports_ok": True,
                "satisfied": True,
            }])
            self.assertEqual(
                reconcile_runtime_requirements(root, runtime_lock=lock),
                ["custom-runtime"],
            )
            self.assertIn("custom-runtime>=1", (root / "requirements.txt").read_text(encoding="utf-8"))

    def test_noop_when_all_declared(self) -> None:
        with TemporaryDirectory() as tmp:
            root = self._project(
                tmp, requirements="numpy\nscipy\n", sim_source="import numpy as np\nimport scipy.linalg\n"
            )
            self.assertEqual(reconcile_runtime_requirements(root), [])

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
                self.assertEqual(reconcile_runtime_requirements(root), ["torch"])
            self.assertIn("torch", (root / "requirements.txt").read_text(encoding="utf-8"))

    def test_unknown_distribution_is_not_rejected_by_name(self) -> None:
        with TemporaryDirectory() as tmp:
            root = self._project(
                tmp,
                requirements="custom-runtime>=1\n",
                sim_source="import custom_runtime\n",
            )
            with patch("geng_agent.security.importlib.util.find_spec", return_value=object()):
                issues = validate_requirements(root)
        self.assertEqual(issues, [])

    def test_explicit_case_lock_is_authoritative_and_constraint_bound(self) -> None:
        with TemporaryDirectory() as tmp:
            root = self._project(
                tmp,
                requirements="custom-runtime>=1\n",
                sim_source="import custom_runtime\n",
            )
            lock = trusted_lock([{
                "requirement": "custom-runtime>=1",
                "distribution": "custom-runtime",
                "import_names": ["custom_runtime"],
                "applicable": True,
                "installed_version": "1.2",
                "version_satisfied": True,
                "imports_ok": True,
                "satisfied": True,
            }])
            with patch("geng_agent.security.importlib.util.find_spec", return_value=None):
                self.assertEqual(validate_requirements(root, runtime_lock=lock), [])
            (root / "requirements.txt").write_text("custom-runtime>=999\n", encoding="utf-8")
            issues = validate_requirements(root, runtime_lock=lock)
        self.assertIn("dependency_lock_constraint_mismatch", {item["category"] for item in issues})

    def test_rejects_untrusted_requirement_sources_and_installer_options(self) -> None:
        samples = (
            "--extra-index-url https://evil.invalid/simple",
            "demo @ https://evil.invalid/a.whl",
            "../demo.whl",
        )
        for sample in samples:
            with self.subTest(sample=sample), TemporaryDirectory() as tmp:
                root = self._project(tmp, requirements=sample + "\n", sim_source="")
                issues = validate_requirements(root)
                self.assertIn("unsafe_requirement_syntax", {item["category"] for item in issues})


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
