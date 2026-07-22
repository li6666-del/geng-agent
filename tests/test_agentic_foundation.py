from __future__ import annotations

import hashlib
import inspect
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from geng_agent.agentic_foundation import (
    _validate_foundation_delivery,
    foundation_violations,
    run_codex_foundation_writer_workflow,
    install_foundation_snapshot,
    restore_foundation_snapshot,
)
from geng_agent.foundation_snapshot import foundation_snapshot_hash


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class FoundationSnapshotTests(unittest.TestCase):
    def test_foundation_writer_default_session_timeout_is_30_minutes(self) -> None:
        timeout = inspect.signature(run_codex_foundation_writer_workflow).parameters["timeout"].default
        self.assertEqual(timeout, 1800.0)

    def test_metadata_debt_does_not_skip_host_validation(self) -> None:
        with TemporaryDirectory() as temp:
            sandbox = Path(temp)
            source = sandbox / "src" / "model.py"
            source.parent.mkdir(parents=True)
            source.write_text("VALUE = 1\n", encoding="utf-8")
            (sandbox / "orphan.py").write_text("VALUE = 2\n", encoding="utf-8")
            with patch(
                "geng_agent.agentic_foundation._required_foundation_modules",
                return_value={"src/model.py"},
            ), patch(
                "geng_agent.agentic_foundation._missing_local_imports",
                return_value=[],
            ), patch(
                "geng_agent.agentic_foundation.static_scan_repro_project",
                return_value=[],
            ), patch(
                "geng_agent.agentic_foundation._run_foundation_tests",
                return_value={"passed": True, "returncode": 0},
            ) as host_tests:
                issues, result = _validate_foundation_delivery(
                    sandbox=sandbox,
                    architecture={},
                    trusted_changed=[],
                )

            self.assertEqual(issues, [])
            self.assertTrue(result["passed"])
            host_tests.assert_called_once_with(sandbox)
            messages = [item["message"] for item in result["warnings"]]
            self.assertTrue(any("hand-off JSON" in message for message in messages))
            self.assertTrue(any("no Foundation contract test" in message for message in messages))
            self.assertTrue(any("outside Foundation ownership" in message for message in messages))
            self.assertTrue(any("requirements.txt is missing" in message for message in messages))

    def test_syntax_error_still_skips_host_validation(self) -> None:
        with TemporaryDirectory() as temp:
            sandbox = Path(temp)
            source = sandbox / "src" / "model.py"
            source.parent.mkdir(parents=True)
            source.write_text("def broken(:\n", encoding="utf-8")
            with patch(
                "geng_agent.agentic_foundation._required_foundation_modules",
                return_value={"src/model.py"},
            ), patch(
                "geng_agent.agentic_foundation._missing_local_imports",
                return_value=[],
            ), patch(
                "geng_agent.agentic_foundation.static_scan_repro_project",
                return_value=[],
            ), patch(
                "geng_agent.agentic_foundation._run_foundation_tests",
            ) as host_tests:
                issues, result = _validate_foundation_delivery(
                    sandbox=sandbox,
                    architecture={},
                    trusted_changed=[],
                )

            self.assertTrue(any("syntax error" in item["message"] for item in issues))
            self.assertTrue(result["skipped"])
            host_tests.assert_not_called()
    def test_snapshot_is_installed_frozen_and_restorable(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            snapshot = root / "snapshot"
            source = snapshot / "src" / "channel.py"
            test_file = snapshot / "tests" / "test_channel.py"
            source.parent.mkdir(parents=True)
            test_file.parent.mkdir(parents=True)
            source.write_text("VALUE = 1\n", encoding="utf-8")
            test_file.write_text("import unittest\n", encoding="utf-8")
            files = [
                {"path": "src/channel.py", "sha256": _sha(source), "bytes": source.stat().st_size},
                {"path": "tests/test_channel.py", "sha256": _sha(test_file), "bytes": test_file.stat().st_size},
            ]
            manifest = {
                "schema_version": "1.0",
                "workflow_version": "2",
                "contract_version": "1",
                "input_hash": "a" * 64,
                "analysis_snapshot_hash": "b" * 64,
                "snapshot_hash": foundation_snapshot_hash(files),
                "files": files,
                "frozen_files": files,
                "required_modules": ["src/channel.py"],
                "validation": {"tests_passed": True, "local_imports_resolve": True},
            }
            foundation = {
                "snapshot_dir": str(snapshot),
                "snapshot_hash": manifest["snapshot_hash"],
                "manifest": manifest,
            }
            project = root / "project"
            installed = install_foundation_snapshot(project, foundation)
            self.assertEqual(installed, {"src/channel.py", "tests/test_channel.py"})
            self.assertEqual(foundation_violations(project, foundation), [])

            (project / "src" / "channel.py").write_text("VALUE = 2\n", encoding="utf-8")
            (project / "src" / "shadow.py").write_text("VALUE = 3\n", encoding="utf-8")
            (project / "src" / "payload.pyd").write_bytes(b"binary")
            pyc = project / "src" / "__pycache__" / "rogue.pyc"
            pyc.parent.mkdir()
            pyc.write_bytes(b"bytecode")
            extra_test = project / "tests" / "extra.py"
            extra_test.write_text("VALUE = 4\n", encoding="utf-8")
            override = project / "configs" / "foundation_override.yaml"
            override.parent.mkdir()
            override.write_text("unsafe: true\n", encoding="utf-8")
            violations = foundation_violations(project, foundation)
            messages = [item["message"] for item in violations]
            files = {item["file"] for item in violations}
            self.assertIn("frozen foundation file was modified", messages)
            self.assertTrue(
                {"src/shadow.py", "src/payload.pyd", "src/__pycache__/rogue.pyc", "tests/extra.py", "configs/foundation_override.yaml"}
                <= files,
                violations,
            )

            restore_foundation_snapshot(project, foundation)
            self.assertEqual(foundation_violations(project, foundation), [])
            self.assertFalse((project / "src" / "shadow.py").exists())
            self.assertFalse((project / "src" / "payload.pyd").exists())
            self.assertFalse(pyc.exists())
            self.assertFalse(extra_test.exists())
            self.assertFalse(override.exists())


if __name__ == "__main__":
    unittest.main()
