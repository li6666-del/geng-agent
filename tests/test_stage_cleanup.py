from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from geng_agent.stage_cleanup import _clear_stage_outputs


def _write(path: Path, content: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


class StageCleanupV2Tests(unittest.TestCase):
    def test_manifest_cleanup_preserves_foundation_and_removes_task_writer_audit(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            foundation_source = _write(root / "audit" / "03b_foundation_snapshot" / "src" / "channel.py")
            foundation_validation = _write(root / "audit" / "03b_foundation_validation.json")
            task_source = _write(root / "audit" / "03c_task_writer_sandboxes" / "01_task" / "tasks" / "task.py")
            task_status = _write(root / "audit" / "03c_task_writers_status.json")
            reporter = _write(root / "audit" / "04a_task_reporters" / "task" / "result.json")
            editor = _write(root / "audit" / "04b_report_editor_status.json")
            foundation_manifest = _write(root / "foundation_manifest.json")
            repro_manifest = _write(root / "repro_project_manifest.json")
            runtime = _write(root / "runtime_result.json")
            project_file = _write(root / "repro_project" / "src" / "channel.py")

            _clear_stage_outputs(root, "manifest")

            self.assertTrue(foundation_source.exists())
            self.assertTrue(foundation_validation.exists())
            self.assertTrue(foundation_manifest.exists())
            for stale in (task_source, task_status, reporter, editor, repro_manifest, runtime, project_file):
                self.assertFalse(stale.exists(), stale)

    def test_architecture_cleanup_still_removes_foundation_and_all_downstream_audit(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            paths = (
                _write(root / "scientific_architecture.json"),
                _write(root / "foundation_manifest.json"),
                _write(root / "audit" / "02f_design_scientific_architecture.json"),
                _write(root / "audit" / "03b_foundation_snapshot" / "src" / "channel.py"),
                _write(root / "audit" / "03c_task_writer_sandboxes" / "01_task" / "tasks" / "task.py"),
                _write(root / "audit" / "04a_task_reporters" / "task" / "result.json"),
            )

            _clear_stage_outputs(root, "scientific_architecture")

            for stale in paths:
                self.assertFalse(stale.exists(), stale)


if __name__ == "__main__":
    unittest.main()
