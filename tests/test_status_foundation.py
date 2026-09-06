from __future__ import annotations

import copy
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from geng_agent.foundation_snapshot import (
    foundation_snapshot_hash,
    is_foundation_frozen_path,
)
from geng_agent.outputs import write_json
from geng_agent.status import inspect_case_status


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_foundation_fixture(case: Path) -> tuple[Path, dict[str, object]]:
    snapshot = case / "audit" / "03b_foundation_snapshot"
    source = snapshot / "src" / "channel.py"
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")
    files: list[dict[str, object]] = [
        {"path": "src/channel.py", "sha256": _sha(source), "bytes": source.stat().st_size}
    ]
    manifest: dict[str, object] = {
        "schema_version": "1.0",
        "workflow_version": "2",
        "contract_version": "1",
        "input_hash": "a" * 64,
        "analysis_snapshot_hash": "b" * 64,
        "snapshot_hash": foundation_snapshot_hash(files),
        "files": files,
        "frozen_files": [
            dict(item)
            for item in files
            if is_foundation_frozen_path(str(item["path"]))
        ],
        "required_modules": ["src/channel.py"],
        "validation": {"tests_passed": True, "local_imports_resolve": True},
    }
    write_json(case / "foundation_manifest.json", manifest)
    return source, manifest


def _foundation_stage(case: Path) -> dict[str, object]:
    return next(
        item
        for item in inspect_case_status(case)["stages"]
        if item["stage"] == "foundation_manifest"
    )


class FoundationStatusTests(unittest.TestCase):
    def test_status_accepts_valid_foundation_snapshot(self) -> None:
        with TemporaryDirectory() as temp:
            case = Path(temp) / "case"
            case.mkdir()
            write_json(case / "workflow.json", {"workflow_version": "2"})
            _write_foundation_fixture(case)

            stage = _foundation_stage(case)

            self.assertTrue(stage["ok"], stage)
            self.assertEqual(stage["reason"], "valid")

    def test_status_detects_snapshot_content_tampering(self) -> None:
        with TemporaryDirectory() as temp:
            case = Path(temp) / "case"
            case.mkdir()
            write_json(case / "workflow.json", {"workflow_version": "2"})
            source, _ = _write_foundation_fixture(case)
            source.write_text("VALUE = 2\n", encoding="utf-8")

            stage = _foundation_stage(case)

            self.assertFalse(stage["ok"], stage)
            self.assertEqual(stage["reason"], "invalid foundation snapshot")
            self.assertTrue(stage["issues"])

    def test_status_rejects_traversal_manifest_without_reading_outside(self) -> None:
        with TemporaryDirectory() as temp:
            case = Path(temp) / "case"
            case.mkdir()
            write_json(case / "workflow.json", {"workflow_version": "2"})
            _, manifest = _write_foundation_fixture(case)
            victim = Path(temp) / "outside.py"
            victim.write_text("safe\n", encoding="utf-8")
            poisoned = copy.deepcopy(manifest)
            poisoned["files"][0]["path"] = "../outside.py"
            write_json(case / "foundation_manifest.json", poisoned)

            stage = _foundation_stage(case)

            self.assertFalse(stage["ok"], stage)
            self.assertEqual(victim.read_text(encoding="utf-8"), "safe\n")

    def test_status_reports_missing_v2_manifest(self) -> None:
        with TemporaryDirectory() as temp:
            case = Path(temp) / "case"
            case.mkdir()
            write_json(case / "workflow.json", {"workflow_version": "2"})

            stage = _foundation_stage(case)

            self.assertFalse(stage["ok"], stage)
            self.assertEqual(stage["reason"], "missing")

    def test_status_rejects_aggregate_hash_mismatch(self) -> None:
        with TemporaryDirectory() as temp:
            case = Path(temp) / "case"
            case.mkdir()
            write_json(case / "workflow.json", {"workflow_version": "2"})
            _, manifest = _write_foundation_fixture(case)
            manifest["snapshot_hash"] = "c" * 64
            write_json(case / "foundation_manifest.json", manifest)

            stage = _foundation_stage(case)

            self.assertFalse(stage["ok"], stage)
            self.assertTrue(any(item["path"] == "$.snapshot_hash" for item in stage["issues"]), stage)

    def test_status_rejects_missing_snapshot_file(self) -> None:
        with TemporaryDirectory() as temp:
            case = Path(temp) / "case"
            case.mkdir()
            write_json(case / "workflow.json", {"workflow_version": "2"})
            source, _ = _write_foundation_fixture(case)
            source.unlink()

            stage = _foundation_stage(case)

            self.assertFalse(stage["ok"], stage)
            self.assertTrue(stage["issues"])

    def test_markerless_foundation_case_requires_a_clean_rebuild(self) -> None:
        with TemporaryDirectory() as temp:
            case = Path(temp) / "case"
            case.mkdir()
            _write_foundation_fixture(case)

            status = inspect_case_status(case)

            self.assertFalse(status["supported"])
            self.assertEqual(status["resume_from"], "rebuild_case")
            self.assertEqual(status["stages"], [])

    def test_status_turns_snapshot_io_error_into_resumeable_stage(self) -> None:
        with TemporaryDirectory() as temp:
            case = Path(temp) / "case"
            case.mkdir()
            write_json(case / "workflow.json", {"workflow_version": "2"})
            _write_foundation_fixture(case)
            only_foundation = [("foundation_manifest", "foundation_manifest.json", None)]
            with (
                patch("geng_agent.status.STAGES", only_foundation),
                patch("geng_agent.status.validate_foundation_snapshot", side_effect=OSError("denied")),
                patch("geng_agent.status.latest_audit_items", side_effect=OSError("audit denied")),
            ):
                status = inspect_case_status(case)

            self.assertFalse(status["stages"][0]["ok"], status)
            self.assertEqual(status["next_stage"], "foundation_manifest")
            self.assertEqual(status["resume_from"], "03b_foundation_writer")
            self.assertEqual(status["latest_audit"], [])
            self.assertIn("cannot inspect Foundation snapshot", status["stages"][0]["issues"][0]["message"])

    def test_unsupported_workflow_requires_a_clean_rebuild(self) -> None:
        with TemporaryDirectory() as temp:
            case = Path(temp) / "case"
            case.mkdir()
            write_json(case / "workflow.json", {"workflow_version": "unsupported"})
            write_json(case / "foundation_manifest.json", {"broken": True})

            status = inspect_case_status(case)

            self.assertFalse(status["supported"])
            self.assertEqual(status["error_kind"], "unsupported_workflow_version")
            self.assertEqual(status["resume_from"], "rebuild_case")
            self.assertEqual(status["stages"], [])


if __name__ == "__main__":
    unittest.main()
