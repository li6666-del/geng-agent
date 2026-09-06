from __future__ import annotations

from pathlib import Path
import json
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from geng_agent.agentic_task_writers import (
    _classify_task_writer_security_issues,
    _merge_task_writer_deliveries,
    _task_writer_runtime_result,
)
from geng_agent.outputs import validate_repro_project


class TaskWriterAssemblyTests(unittest.TestCase):
    def test_old_unconsumed_foundation_from_cached_unit_cannot_overwrite_current_snapshot(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            sandbox = root / "cached_writer"
            project = root / "project"
            (sandbox / "src").mkdir(parents=True)
            (sandbox / "src" / "other_component.py").write_text("VERSION = 'old'\n", encoding="utf-8")
            (sandbox / "src" / "private_science.py").write_text("VALUE = 3\n", encoding="utf-8")
            (sandbox / "foundation_manifest.json").write_text(json.dumps({"frozen_files": [
                {"path": "src/other_component.py", "sha256": "old"}]}), encoding="utf-8")
            (sandbox / "requirements.txt").write_text("", encoding="utf-8")
            def install(target, foundation):
                (target / "src").mkdir(parents=True, exist_ok=True)
                (target / "src" / "other_component.py").write_text("VERSION = 'new'\n", encoding="utf-8")
                (target / "foundation_manifest.json").write_text(json.dumps({"frozen_files": [
                    {"path": "src/other_component.py", "sha256": "new"}]}), encoding="utf-8")
                return {"src/other_component.py", "foundation_manifest.json"}
            with patch("geng_agent.task_writer_packaging.install_foundation_snapshot", side_effect=install):
                _merge_task_writer_deliveries(repro_project_dir=project, task_manifest={"tasks": []},
                    expected_paths=set(), task_records=[{"task_id": "a", "sandbox": str(sandbox)}], foundation={})
            self.assertEqual((project / "src" / "other_component.py").read_text(), "VERSION = 'new'\n")
            self.assertEqual((project / "src" / "private_science.py").read_text(), "VALUE = 3\n")

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

    def test_security_findings_are_strict_without_foundation(self) -> None:
        issues = _classify_task_writer_security_issues(
            [
                {
                    "file": "tasks/fig_1.py",
                    "line": "3",
                    "message": "forbidden dynamic builtin: getattr",
                }
            ],
            foundation=None,
        )

        self.assertEqual(issues[0]["category"], "ordinary_reflection")
        self.assertEqual(issues[0]["severity"], "error")

    def test_only_approved_findings_in_foundation_owned_files_are_warnings(self) -> None:
        foundation = {
            "manifest": {
                "files": [
                    {"path": "src/backend.py"},
                    {"path": "src/config.py"},
                ]
            }
        }
        issues = _classify_task_writer_security_issues(
            [
                {
                    "file": "src/backend.py",
                    "line": "1",
                    "message": "forbidden import: importlib",
                },
                {
                    "file": "src/config.py",
                    "line": "2",
                    "message": "forbidden environment access: os.getenv",
                },
                {
                    "file": "src/backend.py",
                    "line": "3",
                    "message": "forbidden dynamic builtin: getattr",
                },
                {
                    "file": "src/backend.py",
                    "line": "4",
                    "message": "dangerous dynamic import: forbidden module target 'subprocess'",
                },
                {
                    "file": "src/config.py",
                    "line": "5",
                    "message": "absolute path literal is forbidden: /tmp/output",
                },
            ],
            foundation=foundation,
            foundation_integrity_issues=[],
        )

        self.assertEqual(
            [item["severity"] for item in issues],
            ["warning", "warning", "warning", "error", "error"],
        )
        self.assertEqual(
            [item["category"] for item in issues],
            [
                "importlib_usage",
                "environment_access",
                "ordinary_reflection",
                "dangerous_dynamic_import",
                "security_violation",
            ],
        )

    def test_task_owned_approved_category_remains_strict_with_foundation(self) -> None:
        issues = _classify_task_writer_security_issues(
            [
                {
                    "file": "tasks/fig_1.py",
                    "line": "7",
                    "message": "forbidden import: importlib",
                }
            ],
            foundation={"manifest": {"files": [{"path": "src/backend.py"}]}},
            foundation_integrity_issues=[],
        )

        self.assertEqual(issues[0]["category"], "importlib_usage")
        self.assertEqual(issues[0]["severity"], "error")

    def test_foundation_security_advisory_stays_error_when_integrity_fails(self) -> None:
        violation = {
            "file": "src/backend.py",
            "message": "frozen foundation file was modified",
        }
        issues = _classify_task_writer_security_issues(
            [
                {
                    "file": "src/backend.py",
                    "line": "1",
                    "message": "forbidden import: importlib",
                }
            ],
            foundation={"manifest": {"files": [{"path": "src/backend.py"}]}},
            foundation_integrity_issues=[violation],
        )

        self.assertEqual(issues[0]["category"], "importlib_usage")
        self.assertEqual(issues[0]["severity"], "error")

    def test_runtime_blocks_any_error_but_not_security_warnings(self) -> None:
        record = {
            "task_id": "fig_1",
            "writer_completed": True,
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
        self.assertFalse(error_result["passed"])

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

    def test_runtime_blocks_and_exposes_foundation_integrity_violations(self) -> None:
        violation = {
            "file": "src/backend.py",
            "message": "frozen foundation file was modified",
        }
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
                "foundation_integrity_checked": True,
                "foundation_integrity_ok": False,
                "foundation_violations": [violation],
            },
            requirement_warnings=[],
            security_issues=[],
        )

        self.assertFalse(result["passed"])
        self.assertEqual(result["foundation_integrity_violations"], [violation])
        self.assertEqual(result["validation"]["foundation_violations"], [violation])


if __name__ == "__main__":
    unittest.main()
