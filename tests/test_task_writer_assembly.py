from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from geng_agent.agentic_task_writers import (
    _merge_task_writer_deliveries,
    _task_writer_runtime_result,
)
from geng_agent.outputs import validate_repro_project


class TaskWriterAssemblyTests(unittest.TestCase):
    def test_transitive_task_helper_is_preserved(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sandbox = root / "sandbox"
            tasks = sandbox / "tasks"
            output = sandbox / "outputs" / "figure_6"
            tasks.mkdir(parents=True)
            output.mkdir(parents=True)
            (tasks / "figure_6.py").write_text(
                "from tasks import _figure_6_helper\n\n"
                "def main(config_path=None):\n"
                "    return _figure_6_helper.VALUE\n",
                encoding="utf-8",
            )
            (tasks / "_figure_6_helper.py").write_text("VALUE = 0\n", encoding="utf-8")
            (tasks / "lookup.json").write_text('{"scale": 1}\n', encoding="utf-8")
            (sandbox / "requirements.txt").write_text("numpy\n", encoding="utf-8")
            (sandbox / "config.json").write_text("{}\n", encoding="utf-8")
            (sandbox / "config_smoke.json").write_text("{}\n", encoding="utf-8")

            project = root / "project"
            expected = _merge_task_writer_deliveries(
                repro_project_dir=project,
                task_manifest={
                    "version": 1,
                    "tasks": [
                        {
                            "task_id": "figure_6",
                            "module": "figure_6",
                            "script": "tasks/figure_6.py",
                            "output_subdir": "figure_6",
                        }
                    ],
                },
                expected_paths={"tasks/figure_6.py"},
                task_records=[
                    {
                        "task_id": "figure_6",
                        "module": "figure_6",
                        "output_subdir": "figure_6",
                        "sandbox": str(sandbox),
                        "task_writer_status": "ready_for_review",
                    }
                ],
            )

            self.assertIn("tasks/_figure_6_helper.py", expected)
            self.assertTrue((project / "tasks" / "_figure_6_helper.py").is_file())
            self.assertIn("tasks/lookup.json", expected)
            self.assertEqual((project / "tasks" / "lookup.json").read_text(encoding="utf-8"), '{"scale": 1}\n')

    def test_missing_local_import_blocks_project_validation(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for rel, content in {
                "README.md": "demo\n",
                "requirements.txt": "numpy\n",
                "config.json": "{}\n",
                "config_smoke.json": "{}\n",
                "run_experiment.py": "print('ok')\n",
                "src/channel.py": "\n",
                "src/modulation.py": "\n",
                "src/metrics.py": "\n",
                "src/simulation.py": "\n",
                "tasks/__init__.py": "\n",
                "tasks/figure_6.py": "from tasks import missing_helper\n",
            }.items():
                path = root / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")

            validation = validate_repro_project(root)

            self.assertFalse(validation["local_imports_resolve"])
            self.assertIn("tasks.missing_helper", {item["module"] for item in validation["missing_local_imports"]})

    def test_relative_import_beyond_package_root_is_rejected(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for rel, content in {
                "README.md": "demo\n", "requirements.txt": "numpy\n", "config.json": "{}\n",
                "config_smoke.json": "{}\n", "run_experiment.py": "\n", "src/channel.py": "\n",
                "src/modulation.py": "\n", "src/metrics.py": "\n", "src/simulation.py": "\n",
                "tasks/__init__.py": "\n", "tasks/figure.py": "from .. import impossible\n",
            }.items():
                path = root / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")

            validation = validate_repro_project(root)

            self.assertFalse(validation["local_imports_resolve"])
            self.assertIn(
                "<relative-import-beyond-top-level>",
                {item["module"] for item in validation["missing_local_imports"]},
            )

    def test_different_helpers_at_same_path_fail_closed(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            records = []
            for task_id, value in (("fig_1", 1), ("fig_2", 2)):
                sandbox = root / task_id
                (sandbox / "tasks").mkdir(parents=True)
                (sandbox / "tasks" / f"{task_id}.py").write_text("from tasks import utils\n", encoding="utf-8")
                (sandbox / "tasks" / "utils.py").write_text(f"VALUE = {value}\n", encoding="utf-8")
                (sandbox / "requirements.txt").write_text("numpy\n", encoding="utf-8")
                records.append({"task_id": task_id, "module": task_id, "output_subdir": task_id, "sandbox": str(sandbox)})

            with self.assertRaisesRegex(RuntimeError, "source collision.*fig_1.*fig_2"):
                _merge_task_writer_deliveries(
                    repro_project_dir=root / "project",
                    task_manifest={"version": 1, "tasks": []},
                    expected_paths=set(),
                    task_records=records,
                )

    def test_runtime_pass_cannot_bypass_local_import_gate(self) -> None:
        record = {
            "task_id": "fig_1",
            "writer_completed": True,
            "task_writer_status": "ready_for_review",
            "artifacts": {},
        }
        result = _task_writer_runtime_result(
            task_records=[record],
            validation={
                "required_files_present": True,
                "python_compiles": True,
                "local_imports_resolve": False,
            },
            requirement_warnings=[],
            security_issues=[],
        )

        self.assertFalse(result["passed"])


if __name__ == "__main__":
    unittest.main()
