import base64
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from geng_agent.outputs import (
    inspect_output_artifacts,
    resolve_inside,
    validate_repro_project,
    write_file_manifest,
    write_json,
    write_text,
)


PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="


class OutputTests(unittest.TestCase):
    def test_rejects_path_traversal(self) -> None:
        with TemporaryDirectory() as temp_dir:
            with self.assertRaises(ValueError):
                resolve_inside(Path(temp_dir), "../evil.py")

    def test_writes_and_validates_minimal_project(self) -> None:
        files = [
            ("README.md", "demo"),
            ("requirements.txt", "numpy\n"),
            ("config.json", "{}\n"),
            ("config_smoke.json", "{}\n"),
            ("run_experiment.py", "print('ok')\n"),
        ]
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_file_manifest(
                {"files": [{"path": path, "content": content} for path, content in files]},
                root,
            )

            validation = validate_repro_project(root)

            self.assertTrue(validation["required_files_present"])
            self.assertTrue(validation["python_compiles"])
            self.assertEqual(validation["required_files"], ["run_experiment.py"])
            self.assertTrue((root / "outputs").is_dir())

    def test_validate_repro_project_ignores_repair_log_candidates(self) -> None:
        files = [
            ("README.md", "demo"),
            ("requirements.txt", "numpy\n"),
            ("config.json", "{}\n"),
            ("config_smoke.json", "{}\n"),
            ("run_experiment.py", "print('ok')\n"),
        ]
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_file_manifest(
                {"files": [{"path": path, "content": content} for path, content in files]},
                root,
            )
            bad_candidate = root / "repair_logs" / "attempt_01_candidate" / "run_experiment.py"
            bad_candidate.parent.mkdir(parents=True)
            bad_candidate.write_text("x = \uff0c\n", encoding="utf-8")

            validation = validate_repro_project(root)

            self.assertTrue(validation["python_compiles"])
            self.assertEqual(validation["compile_errors"], [])

    def test_manifest_supports_content_lines_and_b64(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_file_manifest(
                {
                    "files": [
                        {"path": "README.md", "content_lines": ["hello", "world"]},
                        {"path": "data.txt", "content_b64": base64.b64encode("ok\n".encode()).decode()},
                    ]
                },
                root,
            )

            self.assertEqual((root / "README.md").read_text(encoding="utf-8"), "hello\nworld\n")
            self.assertEqual((root / "data.txt").read_text(encoding="utf-8"), "ok\n")

    def test_manifest_declares_task_entrypoint_without_fixed_science_modules(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "run_experiment.py").write_text("print('ok')\n", encoding="utf-8")
            (root / "tasks_manifest.json").write_text(
                '{"tasks":[{"script":"tasks/model.py"}]}',
                encoding="utf-8",
            )

            missing = validate_repro_project(root)
            self.assertFalse(missing["required_files_present"])
            self.assertEqual(missing["missing_files"], ["tasks/model.py"])

            task = root / "tasks" / "model.py"
            task.parent.mkdir()
            task.write_text("VALUE = 1\n", encoding="utf-8")
            valid = validate_repro_project(root)

            self.assertTrue(valid["required_files_present"])
            self.assertNotIn("src/channel.py", valid["required_files"])
            self.assertTrue(valid["local_imports_resolve"])

    def test_inspect_output_artifacts_rejects_fake_png_and_empty_summary(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            outputs = root / "outputs"
            outputs.mkdir()
            (outputs / "results.csv").write_text("x,y\n1,2\n", encoding="utf-8")
            (outputs / "plot.png").write_text("png placeholder", encoding="utf-8")
            (outputs / "summary.json").write_text("{}", encoding="utf-8")

            artifacts = inspect_output_artifacts(root)

            self.assertTrue(artifacts["has_csv"])
            self.assertFalse(artifacts["has_png"])
            self.assertFalse(artifacts["has_summary_json"])
            self.assertEqual(len(artifacts["invalid_files"]), 2)

    def test_inspect_output_artifacts_accepts_real_smoke_outputs(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            outputs = root / "outputs"
            outputs.mkdir()
            (outputs / "results.csv").write_text("x,y\n1,2\n", encoding="utf-8")
            (outputs / "plot.png").write_bytes(base64.b64decode(PNG_B64))
            (outputs / "summary.json").write_text('{"task_id":"smoke","metrics":{},"assumptions":[]}', encoding="utf-8")

            artifacts = inspect_output_artifacts(root)

            self.assertTrue(artifacts["has_csv"])
            self.assertTrue(artifacts["has_png"])
            self.assertTrue(artifacts["has_summary_json"])
            self.assertEqual(artifacts["invalid_files"], [])


    def test_manifest_declared_domain_artifacts_are_valid_without_csv_or_png(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            artifact = root / "outputs" / "task" / "checkpoint.pt"
            artifact.parent.mkdir(parents=True)
            artifact.write_bytes(b"scientific-checkpoint")

            result = inspect_output_artifacts(
                root,
                subdir="task",
                declared_artifacts=["outputs/checkpoint.pt", "outputs/missing.mat"],
            )

            self.assertTrue(result["has_artifacts"])
            self.assertEqual(result["declared_artifact_files"], ["checkpoint.pt"])
            self.assertEqual(result["missing_declared_artifacts"], ["missing.mat"])


    def test_json_and_text_writes_commit_with_same_directory_replace(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            json_path = root / "state.json"
            text_path = root / "notes.txt"
            real_replace = os.replace
            replacements: list[tuple[Path, Path]] = []

            def checked_replace(source, destination) -> None:
                source_path = Path(source)
                destination_path = Path(destination)
                self.assertEqual(source_path.parent, destination_path.parent)
                self.assertTrue(source_path.is_file())
                replacements.append((source_path, destination_path))
                real_replace(source_path, destination_path)

            with patch("geng_agent.outputs.os.replace", side_effect=checked_replace):
                write_json(json_path, {"ok": True})
                write_text(text_path, "line one\nline two\n")

            self.assertEqual(len(replacements), 2)
            self.assertEqual(json_path.read_text(encoding="utf-8"), '{\n  "ok": true\n}\n')
            self.assertEqual(text_path.read_text(encoding="utf-8"), "line one\nline two\n")

    def test_atomic_write_cleans_temporary_file_when_replace_fails(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            destination = root / "state.json"

            with patch("geng_agent.outputs.os.replace", side_effect=OSError("replace failed")):
                with self.assertRaisesRegex(OSError, "replace failed"):
                    write_json(destination, {"ok": False})

            self.assertFalse(destination.exists())
            self.assertEqual(list(root.glob(".state.json.*.tmp")), [])

if __name__ == "__main__":
    unittest.main()
