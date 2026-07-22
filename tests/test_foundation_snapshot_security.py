from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from geng_agent.agentic_foundation import (
    _foundation_project_files,
    _load_cached_foundation,
    _write_foundation_manifest,
    install_foundation_snapshot,
    restore_foundation_snapshot,
    validate_foundation_bundle,
)
from geng_agent.foundation_snapshot import (
    foundation_snapshot_hash,
    is_foundation_frozen_path,
    validate_foundation_manifest,
)
from geng_agent.outputs import write_json


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _manifest(files: list[dict[str, object]], *, input_hash: str = "a" * 64) -> dict[str, object]:
    canonical = [dict(item) for item in files]
    return {
        "schema_version": "1.0",
        "workflow_version": "2",
        "contract_version": "1",
        "input_hash": input_hash,
        "analysis_snapshot_hash": "b" * 64,
        "snapshot_hash": foundation_snapshot_hash(canonical),
        "files": canonical,
        "frozen_files": [
            dict(item)
            for item in canonical
            if is_foundation_frozen_path(str(item["path"]))
        ],
        "required_modules": ["src/channel.py"],
        "validation": {"tests_passed": True, "local_imports_resolve": True},
    }


def _fixture(root: Path) -> tuple[Path, dict[str, object]]:
    snapshot = root / "snapshot"
    source = snapshot / "src" / "channel.py"
    contract_test = snapshot / "tests" / "test_channel.py"
    source.parent.mkdir(parents=True)
    contract_test.parent.mkdir(parents=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")
    contract_test.write_text("import unittest\n", encoding="utf-8")
    files: list[dict[str, object]] = [
        {"path": "src/channel.py", "sha256": _sha(source), "bytes": source.stat().st_size},
        {
            "path": "tests/test_channel.py",
            "sha256": _sha(contract_test),
            "bytes": contract_test.stat().st_size,
        },
    ]
    return snapshot, _manifest(files)


def _foundation(snapshot: Path, manifest: dict[str, object]) -> dict[str, object]:
    return {
        "snapshot_dir": str(snapshot),
        "snapshot_hash": manifest["snapshot_hash"],
        "manifest": manifest,
    }


class FoundationSnapshotSecurityTests(unittest.TestCase):
    def test_unsafe_manifest_paths_are_rejected_before_copy(self) -> None:
        bad_paths = [
            "../outside.py",
            "/absolute.py",
            "C:/outside.py",
            "src\\escape.py",
            "tasks/figure4.py",
            "src/data.bin",
            "configs/figure4.yaml",
        ]
        with TemporaryDirectory() as temp:
            root = Path(temp)
            snapshot, manifest = _fixture(root)
            victim = root / "outside.py"
            victim.write_text("safe\n", encoding="utf-8")

            for bad_path in bad_paths:
                with self.subTest(path=bad_path):
                    poisoned = copy.deepcopy(manifest)
                    poisoned["files"][0]["path"] = bad_path
                    with self.assertRaises(RuntimeError):
                        install_foundation_snapshot(
                            root / "project",
                            _foundation(snapshot, poisoned),
                        )
                    self.assertEqual(victim.read_text(encoding="utf-8"), "safe\n")

    def test_snapshot_content_tampering_is_rejected(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            snapshot, manifest = _fixture(root)
            (snapshot / "src" / "channel.py").write_text("VALUE = 2\n", encoding="utf-8")

            with self.assertRaises(RuntimeError):
                install_foundation_snapshot(root / "project", _foundation(snapshot, manifest))

    def test_manifest_cannot_shrink_the_frozen_subset(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            snapshot, manifest = _fixture(root)
            poisoned = copy.deepcopy(manifest)
            poisoned["frozen_files"] = []

            issues = validate_foundation_manifest(poisoned)

            self.assertTrue(
                any(item["path"] == "$.frozen_files" for item in issues),
                issues,
            )
            with self.assertRaises(RuntimeError):
                install_foundation_snapshot(root / "project", _foundation(snapshot, poisoned))

    def test_case_colliding_paths_are_rejected(self) -> None:
        files: list[dict[str, object]] = [
            {"path": "src/channel.py", "sha256": "a" * 64, "bytes": 1},
            {"path": "src/CHANNEL.py", "sha256": "b" * 64, "bytes": 1},
        ]
        manifest = _manifest(files)

        issues = validate_foundation_manifest(manifest)

        self.assertTrue(
            any("case-colliding" in item["message"] for item in issues),
            issues,
        )

    def test_cached_foundation_revalidates_manifest_and_snapshot(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            snapshot, manifest = _fixture(root)
            manifest_path = root / "foundation_manifest.json"
            poisoned = copy.deepcopy(manifest)
            poisoned["files"][0]["path"] = "../outside.py"
            write_json(manifest_path, poisoned)

            cached = _load_cached_foundation(
                manifest_path=manifest_path,
                snapshot_dir=snapshot,
                expected_input_hash="a" * 64,
            )

            self.assertIsNone(cached)

    def test_unowned_foundation_output_is_rejected(self) -> None:
        with TemporaryDirectory() as temp:
            sandbox = Path(temp)
            payload = sandbox / "src" / "payload.bin"
            payload.parent.mkdir(parents=True)
            payload.write_bytes(b"payload")

            with self.assertRaises(ValueError):
                _foundation_project_files(sandbox)

    def test_foundation_files_use_manifest_posix_sort_order(self) -> None:
        with TemporaryDirectory() as temp:
            sandbox = Path(temp)
            paths = [
                sandbox / "README.foundation.md",
                sandbox / "configs" / "foundation.yaml",
                sandbox / "requirements.txt",
                sandbox / "src" / "channel.py",
            ]
            for path in paths:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("x\n", encoding="utf-8")

            relative_paths = [
                path.relative_to(sandbox).as_posix()
                for path in _foundation_project_files(sandbox)
            ]

            self.assertEqual(relative_paths, sorted(relative_paths))

    def test_outer_snapshot_hash_mismatch_is_rejected(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            snapshot, manifest = _fixture(root)
            bundle = _foundation(snapshot, manifest)
            bundle["snapshot_hash"] = "c" * 64

            issues = validate_foundation_bundle(bundle)

            self.assertTrue(any("outer Foundation snapshot hash" in item["message"] for item in issues), issues)
            with self.assertRaises(RuntimeError):
                install_foundation_snapshot(root / "project", bundle)

    def test_required_modules_are_src_only_and_match_expected_architecture(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            _, manifest = _fixture(root)
            poisoned = copy.deepcopy(manifest)
            poisoned["required_modules"] = ["tests/test_channel.py"]

            type_issues = validate_foundation_manifest(poisoned)
            architecture_issues = validate_foundation_manifest(
                manifest,
                expected_required_modules={"src/channel.py", "src/receiver.py"},
            )

            self.assertTrue(any("under src/" in item["message"] for item in type_issues), type_issues)
            self.assertTrue(
                any("current scientific architecture" in item["message"] for item in architecture_issues),
                architecture_issues,
            )

    def test_atomic_manifest_write_does_not_overwrite_hardlink_victim(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            victim = root / "victim.json"
            manifest_path = root / "foundation_manifest.json"
            original = '{"safe": true}\n'
            victim.write_text(original, encoding="utf-8")
            try:
                os.link(victim, manifest_path)
            except OSError as exc:
                self.skipTest(f"hardlink creation is unavailable: {exc}")

            _write_foundation_manifest(manifest_path, {"new": True})

            self.assertEqual(victim.read_text(encoding="utf-8"), original)
            self.assertEqual(json.loads(manifest_path.read_text(encoding="utf-8")), {"new": True})

    def test_restore_rejects_linked_project_root_before_deleting_victim(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            snapshot, manifest = _fixture(root)
            external = root / "external"
            rogue = external / "src" / "rogue.pyd"
            rogue.parent.mkdir(parents=True)
            rogue.write_bytes(b"safe")
            linked_project = root / "linked_project"
            try:
                os.symlink(external, linked_project, target_is_directory=True)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"directory symlink creation is unavailable: {exc}")

            with self.assertRaises(RuntimeError):
                restore_foundation_snapshot(linked_project, _foundation(snapshot, manifest))
            self.assertEqual(rogue.read_bytes(), b"safe")

    def test_install_does_not_follow_destination_symlink(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            snapshot, manifest = _fixture(root)
            target = root / "project"
            destination = target / "src" / "channel.py"
            destination.parent.mkdir(parents=True)
            victim = root / "victim.py"
            victim.write_text("safe\n", encoding="utf-8")
            try:
                os.symlink(victim, destination)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlink creation is unavailable: {exc}")

            with self.assertRaises((RuntimeError, ValueError)):
                install_foundation_snapshot(target, _foundation(snapshot, manifest))
            self.assertEqual(victim.read_text(encoding="utf-8"), "safe\n")


if __name__ == "__main__":
    unittest.main()
