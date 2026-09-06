import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from geng_agent.project_portability import (
    ProjectPortabilityError,
    build_source_inventory,
    validate_repro_project_portability,
)


def _write_portable_project(root: Path) -> None:
    tasks = root / "tasks"
    configs = root / "configs"
    tasks.mkdir(parents=True)
    configs.mkdir(parents=True)
    (root / "run_experiment.py").write_text(
        "from tasks.demo import main\nraise SystemExit(main('configs/demo_smoke.json'))\n",
        encoding="utf-8",
    )
    (tasks / "__init__.py").write_text("", encoding="utf-8")
    (tasks / "demo.py").write_text("def main(config_path=None):\n    return 0\n", encoding="utf-8")
    (configs / "demo.json").write_text('{"run_profile":"full"}\n', encoding="utf-8")
    (configs / "demo_smoke.json").write_text(
        '{"run_profile":"smoke","smoke":true}\n',
        encoding="utf-8",
    )
    (root / "tasks_manifest.json").write_text(
        json.dumps(
            {
                "version": 1,
                "tasks": [
                    {
                        "task_id": "demo",
                        "script": "tasks/demo.py",
                        "config_full": "configs/demo.json",
                        "config_smoke": "configs/demo_smoke.json",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    _refresh_inventory(root)


def _refresh_inventory(root: Path) -> None:
    (root / "source_inventory.json").write_text(
        json.dumps(build_source_inventory(root), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


class ProjectPortabilityTests(unittest.TestCase):
    def test_portable_project_has_recursive_content_inventory(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"
            root.mkdir()
            _write_portable_project(root)
            cache = root / "tasks" / "__pycache__"
            cache.mkdir()
            (cache / "demo.cpython-311.pyc").write_bytes(b"bytecode")

            inventory = build_source_inventory(root)
            result = validate_repro_project_portability(root)

            paths = [item["path"] for item in inventory["files"]]
            self.assertEqual(paths, sorted(paths))
            self.assertIn("tasks/demo.py", paths)
            self.assertNotIn("source_inventory.json", paths)
            self.assertNotIn("tasks/__pycache__/demo.cpython-311.pyc", paths)
            self.assertRegex(inventory["inventory_sha256"], r"^[0-9a-f]{64}$")
            for item in inventory["files"]:
                self.assertEqual(set(item), {"path", "sha256", "size"})
                self.assertRegex(item["sha256"], r"^[0-9a-f]{64}$")
                self.assertGreaterEqual(item["size"], 0)
            self.assertTrue(result["portable"])
            self.assertEqual(result["issues"], [])
            self.assertEqual(result["warnings"], [])

    def test_source_inventory_is_required_and_checked_against_package_content(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"
            root.mkdir()
            _write_portable_project(root)
            (root / "tasks" / "demo.py").write_text(
                "def main(config_path=None):\n    return 7\n",
                encoding="utf-8",
            )

            result = validate_repro_project_portability(root, raise_on_error=False)

            self.assertFalse(result["portable"])
            self.assertTrue(
                any(item["code"] == "source_inventory_content_mismatch" for item in result["issues"])
            )

            _refresh_inventory(root)
            self.assertTrue(validate_repro_project_portability(root)["portable"])

            (root / "source_inventory.json").unlink()
            missing = validate_repro_project_portability(root, raise_on_error=False)
            self.assertTrue(any(item["code"] == "source_inventory_missing" for item in missing["issues"]))

    def test_source_inventory_excludes_only_its_own_bytes_from_hash(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"
            root.mkdir()
            _write_portable_project(root)
            inventory_path = root / "source_inventory.json"
            document = json.loads(inventory_path.read_text(encoding="utf-8"))
            inventory_path.write_text(json.dumps(document, indent=4) + "\n\n", encoding="utf-8")

            result = validate_repro_project_portability(root)

            self.assertTrue(result["portable"])
            self.assertNotIn(
                "source_inventory.json",
                [item["path"] for item in result["inventory"]["files"]],
            )

    def test_absolute_case_path_in_config_is_blocking(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"
            root.mkdir()
            _write_portable_project(root)
            (root / "configs" / "demo.json").write_text(
                json.dumps({"dataset_path": "/root/geng-agent-cases/case-17/data/train.npy"}),
                encoding="utf-8",
            )
            _refresh_inventory(root)

            with self.assertRaises(ProjectPortabilityError) as raised:
                validate_repro_project_portability(root)

            self.assertTrue(any(item["code"] == "absolute_case_path" for item in raised.exception.issues))

    def test_parent_reference_in_manifest_is_blocking(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"
            root.mkdir()
            _write_portable_project(root)
            manifest = json.loads((root / "tasks_manifest.json").read_text(encoding="utf-8"))
            manifest["tasks"][0]["script"] = "../outside.py"
            (root / "tasks_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            _refresh_inventory(root)

            result = validate_repro_project_portability(root, raise_on_error=False)

            self.assertFalse(result["portable"])
            self.assertTrue(any(item["code"] == "manifest_path_escape" for item in result["issues"]))

    def test_in_project_parent_composition_is_a_warning_not_a_blocker(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"
            root.mkdir()
            _write_portable_project(root)
            (root / "tasks" / "paths.py").write_text(
                "from pathlib import Path\n"
                'CONFIG_DIR = Path(__file__).resolve().parent / ".." / "configs"\n',
                encoding="utf-8",
            )
            scripts = root / "scripts"
            scripts.mkdir()
            (scripts / "run.sh").write_text(
                'ROOT="$(cd "$(dirname "$0")/../scripts" && pwd)"\n',
                encoding="utf-8",
            )
            _refresh_inventory(root)

            result = validate_repro_project_portability(root)

            self.assertTrue(result["portable"])
            warning_codes = {item["code"] for item in result["warnings"]}
            self.assertIn("python_path_literal_warning", warning_codes)
            self.assertIn("script_parent_path_warning", warning_codes)

    def test_missing_manifest_file_is_blocking(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"
            root.mkdir()
            _write_portable_project(root)
            (root / "tasks" / "demo.py").unlink()
            _refresh_inventory(root)

            with self.assertRaises(ProjectPortabilityError) as raised:
                validate_repro_project_portability(root)

            missing = [item for item in raised.exception.issues if item["code"] == "manifest_reference_missing"]
            self.assertEqual(len(missing), 1)
            self.assertEqual(missing[0]["reference"], "tasks/demo.py")

    def test_windows_drive_and_unc_paths_block_on_any_host_platform(self) -> None:
        references = (
            r"C:\geng-agent-cases\case-9\train.npy",
            r"\\server.example\share\case-9\train.npy",
            "//server.example/share/case-9/train.npy",
        )
        for reference in references:
            with self.subTest(reference=reference), TemporaryDirectory() as temporary:
                root = Path(temporary) / "project"
                root.mkdir()
                _write_portable_project(root)
                (root / "configs" / "demo.json").write_text(
                    json.dumps({"dataset_path": reference}),
                    encoding="utf-8",
                )
                _refresh_inventory(root)

                result = validate_repro_project_portability(root, raise_on_error=False)

                self.assertFalse(result["portable"])
                self.assertTrue(any(item["code"] == "absolute_case_path" for item in result["issues"]))

    def test_non_execution_python_path_literal_is_only_a_warning(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"
            root.mkdir()
            _write_portable_project(root)
            (root / "tasks" / "messages.py").write_text(
                'MESSAGE = "example failure: /root/geng-agent-cases/case-2/output.json"\n',
                encoding="utf-8",
            )
            tests = root / "tests"
            tests.mkdir()
            (tests / "test_example.py").write_text(
                'DATA_PATH = "C:\\\\fixtures\\\\paper.npy"\n',
                encoding="utf-8",
            )
            _refresh_inventory(root)

            result = validate_repro_project_portability(root)

            self.assertTrue(result["portable"])
            self.assertGreaterEqual(
                sum(item["code"] == "python_path_literal_warning" for item in result["warnings"]),
                2,
            )

    def test_execution_python_path_literal_remains_blocking(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"
            root.mkdir()
            _write_portable_project(root)
            (root / "tasks" / "runtime.py").write_text(
                'DATA_PATH = "/root/geng-agent-cases/case-2/data.npy"\n'
                "def load():\n"
                "    return open(DATA_PATH, 'rb')\n",
                encoding="utf-8",
            )
            _refresh_inventory(root)

            result = validate_repro_project_portability(root, raise_on_error=False)

            self.assertFalse(result["portable"])
            self.assertTrue(any(item["code"] == "absolute_case_path" for item in result["issues"]))

    def test_relocated_smoke_uses_isolated_minimal_environment(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"
            root.mkdir()
            _write_portable_project(root)
            (root / "smoke_environment.py").write_text(
                "import os\n"
                "from pathlib import Path\n"
                "assert 'DATA_ROOT' not in os.environ\n"
                "assert 'PYTHONPATH' not in os.environ\n"
                "assert 'UNRELATED_HOST_VALUE' not in os.environ\n"
                "home = Path(os.environ['HOME']).resolve()\n"
                "assert home.is_dir()\n"
                 "assert Path(os.environ['HF_HOME']).is_relative_to(home)\n"
                 "assert Path(os.environ['TORCH_HOME']).is_relative_to(home)\n"
                 "assert Path(os.environ['TORCHINDUCTOR_CACHE_DIR']).is_relative_to(home)\n"
                 "assert Path(os.environ['MPLCONFIGDIR']).is_relative_to(home)\n"
                 "assert Path(os.environ['TMPDIR']).is_relative_to(home)\n"
                 "assert os.environ['LNAME'] == 'geng-case-runtime'\n"
                 "assert os.environ['LOGNAME'] == 'geng-case-runtime'\n"
                 "assert os.environ['USER'] == 'geng-case-runtime'\n"
                 "assert os.environ['USERNAME'] == 'geng-case-runtime'\n"
                 "print('isolated-environment-ok')\n",
                encoding="utf-8",
            )
            _refresh_inventory(root)

            with patch.dict(
                os.environ,
                {
                    "DATA_ROOT": "/host/data",
                    "HF_HOME": "/host/huggingface",
                    "HOME": "/host/home",
                    "PYTHONPATH": "/host/python",
                     "TORCH_HOME": "/host/torch",
                     "UNRELATED_HOST_VALUE": "must-not-leak",
                     "USER": "host-user",
                     "USERNAME": "host-username",
                },
                clear=False,
            ):
                result = validate_repro_project_portability(
                    root,
                    run_smoke=True,
                    smoke_command=["{python}", "smoke_environment.py"],
                )

            self.assertTrue(result["portable"])
            self.assertEqual(result["smoke"]["status"], "passed")
            self.assertIn("isolated-environment-ok", result["smoke"]["stdout_tail"])

    def test_failed_relocated_smoke_preserves_aggregate_task_summary(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"
            root.mkdir()
            _write_portable_project(root)
            (root / "smoke_failure.py").write_text(
                "import json\n"
                "from pathlib import Path\n"
                "output = Path('outputs')\n"
                "output.mkdir(parents=True, exist_ok=True)\n"
                "summary = {'tasks': {'demo': {'status': 'error', "
                "'error': 'ValueError: broken smoke'}}}\n"
                "(output / 'summary.json').write_text(json.dumps(summary), encoding='utf-8')\n"
                "raise SystemExit(1)\n",
                encoding="utf-8",
            )
            _refresh_inventory(root)

            result = validate_repro_project_portability(
                root,
                run_smoke=True,
                smoke_command=["{python}", "smoke_failure.py"],
                raise_on_error=False,
            )

            self.assertFalse(result["portable"])
            self.assertEqual(result["smoke"]["returncode"], 1)
            self.assertEqual(
                result["smoke"]["aggregate_summary"]["tasks"]["demo"]["error"],
                "ValueError: broken smoke",
            )

    def test_inventory_and_relocation_share_the_same_ignore_policy(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"
            root.mkdir()
            _write_portable_project(root)
            for ignored in ("node_modules", ".tox"):
                directory = root / ignored
                directory.mkdir()
                (directory / "host.py").write_text(
                    'DATA_PATH = "/root/geng-agent-cases/ignored/data.npy"\n',
                    encoding="utf-8",
                )
            (root / "smoke_ignored_directories.py").write_text(
                "from pathlib import Path\n"
                "assert not Path('node_modules').exists()\n"
                "assert not Path('.tox').exists()\n",
                encoding="utf-8",
            )
            _refresh_inventory(root)

            inventory = build_source_inventory(root)
            result = validate_repro_project_portability(
                root,
                run_smoke=True,
                smoke_command=["{python}", "smoke_ignored_directories.py"],
            )

            paths = {item["path"] for item in inventory["files"]}
            self.assertFalse(any(path.startswith("node_modules/") for path in paths))
            self.assertFalse(any(path.startswith(".tox/") for path in paths))
            self.assertTrue(result["portable"])

    def test_smoke_timeout_is_inconclusive_not_a_portability_blocker(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"
            root.mkdir()
            _write_portable_project(root)
            (root / "smoke_timeout.py").write_text("import time\ntime.sleep(2)\n", encoding="utf-8")
            _refresh_inventory(root)

            result = validate_repro_project_portability(
                root,
                run_smoke=True,
                smoke_command=["{python}", "smoke_timeout.py"],
                smoke_timeout_s=0.01,
            )

            self.assertTrue(result["portable"])
            self.assertEqual(result["smoke"]["status"], "inconclusive")
            self.assertEqual(result["smoke"]["infrastructure_reason"], "timeout")
            self.assertTrue(
                any(item["code"] == "smoke_timeout_inconclusive" for item in result["warnings"])
            )

    def test_relocation_copy_failure_is_inconclusive_not_nonportable(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"
            root.mkdir()
            _write_portable_project(root)

            with patch(
                "geng_agent.portability_smoke.shutil.copytree",
                side_effect=OSError("temporary disk full"),
            ):
                result = validate_repro_project_portability(
                    root,
                    run_smoke=True,
                    smoke_command=["{python}", "run_experiment.py", "--smoke"],
                )

            self.assertTrue(result["portable"])
            self.assertEqual(result["smoke"]["status"], "inconclusive")
            self.assertEqual(result["smoke"]["infrastructure_reason"], "relocation_copy_failed")
            self.assertTrue(
                any(item["code"] == "relocation_copy_inconclusive" for item in result["warnings"])
            )


if __name__ == "__main__":
    unittest.main()
