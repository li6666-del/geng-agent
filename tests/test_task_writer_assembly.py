from __future__ import annotations

from pathlib import Path
import json
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from geng_agent.agentic_task_writers import _merge_task_writer_deliveries, _task_writer_runtime_result
from geng_agent.outputs import validate_repro_project


class TaskWriterAssemblyTests(unittest.TestCase):
    def test_package_preserves_paper_runtime_inputs_without_role_packets(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            sandbox = root / "writer"
            inputs = {
                "paper_evidence/source/paper.pdf": b"original-paper-bytes",
                "paper_evidence/full_paper_pages/paper_page_003.png": b"original-page-bytes",
            }
            audit_only = {
                "paper_evidence/writer_input.json",
                "paper_evidence/analysis_artifacts/engineering_facts.json",
                "paper_evidence/full_paper_pages/index.json",
                "paper_evidence/source/.env",
            }
            for relative, contents in {**inputs, **{p: b"audit-only" for p in audit_only}}.items():
                path = sandbox / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(contents)
            project = root / "project"
            expected = _merge_task_writer_deliveries(
                repro_project_dir=project, task_manifest={"tasks": []}, expected_paths=set(),
                task_records=[{"task_id": "figure", "module": "figure", "sandbox": str(sandbox)}],
            )
            # A relocated consumer must find exactly the bytes used by the Writer.
            for relative, contents in inputs.items():
                self.assertIn(relative, expected)
                self.assertEqual((project / relative).read_bytes(), contents)
            for relative in audit_only:
                self.assertFalse((project / relative).exists())

    def test_documentation_only_package_collision_preserves_both_descriptions(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            records = []
            for owner in ("noise", "ber"):
                sandbox = root / owner
                package = sandbox / "src" / "tasks"
                package.mkdir(parents=True)
                (package / "__init__.py").write_text(f'"""{owner} task description."""\n', encoding="utf-8")
                (package / f"{owner}.py").write_text("VALUE = 1\n", encoding="utf-8")
                records.append({"task_id": owner, "module": owner, "sandbox": str(sandbox)})
            project = root / "project"
            expected = _merge_task_writer_deliveries(repro_project_dir=project,
                task_manifest={"tasks": []}, expected_paths=set(), task_records=records)
            self.assertIn("src/tasks/ber.py", expected)
            for owner in ("noise", "ber"):
                note = f"task_notes/package_descriptions/{owner}/src/tasks/__init__.py.txt"
                self.assertIn(note, expected)
                self.assertIn(owner + " task description", (project / note).read_text(encoding="utf-8"))

    def test_package_initializer_with_executable_code_still_conflicts(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            records = []
            for owner, value in (("noise", 1), ("ber", 2)):
                sandbox = root / owner
                package = sandbox / "src" / "tasks"
                package.mkdir(parents=True)
                (package / "__init__.py").write_text(f"VALUE = {value}\n", encoding="utf-8")
                records.append({"task_id": owner, "module": owner, "sandbox": str(sandbox)})
            with self.assertRaisesRegex(RuntimeError, "package collision"):
                _merge_task_writer_deliveries(repro_project_dir=root / "project",
                    task_manifest={"tasks": []}, expected_paths=set(), task_records=records)

    def test_nested_src_tasks_package_is_preserved(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            sandbox = root / "writer"
            package = sandbox / "src" / "tasks"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text("", encoding="utf-8")
            (package / "ber.py").write_text("VALUE = 0.125\n", encoding="utf-8")
            (sandbox / "tasks").mkdir()
            (sandbox / "tasks" / "figure.py").write_text("from src.tasks.ber import VALUE\n", encoding="utf-8")
            project = root / "project"
            expected = _merge_task_writer_deliveries(
                repro_project_dir=project, task_manifest={"tasks": []}, expected_paths=set(),
                task_records=[{"task_id": "figure", "module": "figure", "sandbox": str(sandbox)}],
            )
            self.assertIn("src/tasks/__init__.py", expected)
            self.assertIn("src/tasks/ber.py", expected)
            self.assertEqual((project / "src/tasks/ber.py").read_text(encoding="utf-8"), "VALUE = 0.125\n")


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
            self.assertEqual((project / "task_requirements" / "figure_6.txt").read_text(encoding="utf-8"), "numpy\n")
            manifest = json.loads((project / "reproducibility_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["requirements_by_execution_unit"]["figure_6"],
                             "task_requirements/figure_6.txt")

    def test_missing_local_import_is_reported_as_advisory(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for rel, content in {
                "README.md": "demo\n",
                "requirements.txt": "numpy\n",
                "config.json": "{}\n",
                "config_smoke.json": "{}\n",
                "run_experiment.py": "print('ok')\n",
                "tasks/__init__.py": "\n",
                "tasks/figure_6.py": "from tasks import missing_helper\n",
            }.items():
                path = root / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")

            validation = validate_repro_project(root)

            self.assertTrue(validation["local_imports_resolve"])
            self.assertFalse(validation["static_local_imports_resolve"])
            self.assertIn("tasks.missing_helper", {item["module"] for item in validation["missing_local_imports"]})

    def test_relative_import_beyond_package_root_is_reported_as_advisory(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for rel, content in {
                "README.md": "demo\n", "requirements.txt": "numpy\n", "config.json": "{}\n",
                "config_smoke.json": "{}\n", "run_experiment.py": "\n",
                "tasks/__init__.py": "\n", "tasks/figure.py": "from .. import impossible\n",
            }.items():
                path = root / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")

            validation = validate_repro_project(root)

            self.assertTrue(validation["local_imports_resolve"])
            self.assertFalse(validation["static_local_imports_resolve"])
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

            with self.assertRaisesRegex(RuntimeError, "package collision.*fig_1.*fig_2"):
                _merge_task_writer_deliveries(
                    repro_project_dir=root / "project",
                    task_manifest={"version": 1, "tasks": []},
                    expected_paths=set(),
                    task_records=records,
                )

    def test_runtime_pass_overrides_static_local_import_advisory(self) -> None:
        record = {
            "task_id": "fig_1",
            "writer_completed": True,
            "host_execution": {"passed": True},
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

        self.assertTrue(result["passed"])

    pass  # Retired shared-code/compound execution policy.

    pass  # Retired shared-code/compound execution policy.

    pass  # Retired shared-code/compound execution policy.

    pass  # Retired shared-code/compound execution policy.

    def test_runtime_preserves_observed_full_despite_static_findings(self) -> None:
        record = {
            "task_id": "fig_1",
            "writer_completed": True,
            "host_execution": {"passed": True},
            "task_writer_status": "ready_for_review",
            "artifacts": {},
        }
        base = {
            "task_records": [record],
            "validation": {
                "required_files_present": True,
                "python_compiles": True,
                "local_imports_resolve": False,
            },
            "requirement_warnings": [],
        }

        warning_result = _task_writer_runtime_result(
            **base,
            security_issues=[
                {
                    "file": "src/backend.py",
                    "message": "forbidden import: importlib",
                    "category": "importlib_usage",
                    "severity": "warning",
                }
            ],
        )
        error_result = _task_writer_runtime_result(
            **base,
            security_issues=[
                {
                    "file": "tasks/fig_1.py",
                    "message": "forbidden import: importlib",
                    "category": "importlib_usage",
                    "severity": "error",
                }
            ],
        )

        self.assertTrue(warning_result["passed"])
        self.assertTrue(error_result["passed"])

    def test_runtime_blocks_unresolved_dependency_issue(self) -> None:
        result = _task_writer_runtime_result(
            task_records=[
                {
                    "task_id": "fig_1",
                    "writer_completed": True,
                    "task_writer_status": "ready_for_review",
                    "artifacts": {},
                }
            ],
            validation={
                "required_files_present": True,
                "python_compiles": True,
                "foundation_integrity_ok": True,
            },
            requirement_warnings=[],
            requirement_issues=[
                {
                    "file": "requirements.txt",
                    "message": "dependency is absent from the active case lock",
                }
            ],
            security_issues=[],
        )

        self.assertFalse(result["passed"])
        self.assertEqual(len(result["requirements_issues"]), 1)



if __name__ == "__main__":
    unittest.main()
