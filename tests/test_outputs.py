import base64
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from geng_agent.outputs import inspect_output_artifacts, resolve_inside, validate_repro_project, write_file_manifest


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
            ("src/channel.py", "\n"),
            ("src/modulation.py", "\n"),
            ("src/metrics.py", "\n"),
            ("src/simulation.py", "\n"),
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
            self.assertTrue((root / "outputs").is_dir())

    def test_validate_repro_project_ignores_repair_log_candidates(self) -> None:
        files = [
            ("README.md", "demo"),
            ("requirements.txt", "numpy\n"),
            ("config.json", "{}\n"),
            ("config_smoke.json", "{}\n"),
            ("run_experiment.py", "print('ok')\n"),
            ("src/channel.py", "\n"),
            ("src/modulation.py", "\n"),
            ("src/metrics.py", "\n"),
            ("src/simulation.py", "\n"),
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


if __name__ == "__main__":
    unittest.main()
