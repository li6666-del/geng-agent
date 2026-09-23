from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from geng_agent.agentic_foundation import (
    _foundation_brief,
    _load_foundation_validation_record,
    _restore_trusted_runtime_atomically,
    _validate_foundation_delivery,
    foundation_violations,
    run_codex_foundation_writer_workflow,
    install_foundation_snapshot,
    restore_foundation_snapshot,
)
from geng_agent.foundation_snapshot import foundation_snapshot_hash
from geng_agent.foundation_snapshot_delivery import (
    _publish_foundation_snapshot,
    load_foundation_writer_delivery,
    persist_foundation_writer_delivery,
    restore_foundation_writer_delivery,
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _symlink_or_skip(test: unittest.TestCase, link: Path, target: Path) -> None:
    try:
        link.symlink_to(target)
    except (NotImplementedError, OSError) as exc:
        test.skipTest(f"symlinks are unavailable on this platform: {exc}")


def _hardlink_or_skip(test: unittest.TestCase, link: Path, target: Path) -> None:
    try:
        link.hardlink_to(target)
    except (NotImplementedError, OSError) as exc:
        test.skipTest(f"hardlinks are unavailable on this platform: {exc}")


class FoundationSnapshotTests(unittest.TestCase):
    def test_matching_failed_validation_reuses_completed_writer_delivery_for_host_recheck(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            output_dir = root / "output"
            audit_dir = root / "audit"
            sandbox = audit_dir / "03b_foundation_writer_sandbox"
            (sandbox / "src").mkdir(parents=True)
            (sandbox / "src" / "model.py").write_text("VALUE = 1\n", encoding="utf-8")
            output_dir.mkdir()
            (audit_dir / "03b_foundation_writer.json").write_text(
                json.dumps({"ok": True, "role": "foundation_writer"}),
                encoding="utf-8",
            )
            cached_issues = [{"file": "tests", "message": "Foundation contract tests failed or timed out"}]
            validation_record = {
                "ok": False,
                "input_hash": "current-input",
                "issues": cached_issues,
                "tests": {
                    "passed": False,
                    "returncode": None,
                    "spawn_error": "host process could not start",
                    "stderr": (
                        "C:/env/Lib/site-packages/torch/library.py: "
                        "PermissionError: Foundation runtime guard: host import blocked"
                    ),
                    "delivery_immutable": True,
                },
            }
            writer_delivery = {"trusted_changed": []}
            finalized = {"snapshot_hash": "revalidated"}

            with patch(
                "geng_agent.agentic_foundation._collect_writer_analysis_artifacts",
                return_value={"scientific_architecture.json": {}},
            ), patch(
                "geng_agent.agentic_foundation._missing_required_analysis_artifacts",
                return_value=[],
            ), patch(
                "geng_agent.agentic_foundation._analysis_snapshot_hash",
                return_value="a" * 64,
            ), patch(
                "geng_agent.agentic_foundation._foundation_input_hash",
                return_value="current-input",
            ), patch(
                "geng_agent.agentic_foundation._load_cached_foundation",
                return_value=None,
            ), patch(
                "geng_agent.agentic_foundation.load_foundation_writer_delivery",
                return_value=writer_delivery,
            ), patch(
                "geng_agent.agentic_foundation._load_foundation_validation_record",
                return_value=validation_record,
            ), patch(
                "geng_agent.agentic_foundation._required_foundation_modules",
                return_value={"src/model.py"},
            ), patch(
                "geng_agent.agentic_foundation.restore_foundation_writer_delivery",
            ), patch(
                "geng_agent.agentic_foundation._finalize_foundation_delivery",
                return_value=finalized,
            ) as finalize, patch(
                "geng_agent.agentic_foundation.run_codex_subprocess",
            ) as writer:
                result = run_codex_foundation_writer_workflow(
                    facts={},
                    tasks={},
                    experiment_index={},
                    scientific_architecture={},
                    paper={},
                    paper_path=root / "paper.pdf",
                    paper_images=[],
                    paper_thesis=None,
                    output_dir=output_dir,
                    audit_dir=audit_dir,
                    resume=True,
                )

            resume_record = json.loads(
                (audit_dir / "03b_foundation_writer_resume.json").read_text(encoding="utf-8")
            )

        self.assertEqual(result, finalized)
        writer.assert_not_called()
        finalize.assert_called_once()
        self.assertEqual(resume_record["source"], "cached_writer_delivery_host_revalidation")
        self.assertFalse(resume_record["writer_rerun"])



    def test_completed_writer_delivery_retries_freeze_without_rerunning_writer(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            output_dir = root / "output"
            audit_dir = root / "audit"
            output_dir.mkdir()
            audit_dir.mkdir()
            writer_delivery = {"trusted_changed": []}
            finalized = {"snapshot_hash": "freeze-retried"}

            with patch(
                "geng_agent.agentic_foundation._collect_writer_analysis_artifacts",
                return_value={"scientific_architecture.json": {}},
            ), patch(
                "geng_agent.agentic_foundation._missing_required_analysis_artifacts",
                return_value=[],
            ), patch(
                "geng_agent.agentic_foundation._analysis_snapshot_hash",
                return_value="a" * 64,
            ), patch(
                "geng_agent.agentic_foundation._foundation_input_hash",
                return_value="current-input",
            ), patch(
                "geng_agent.agentic_foundation._load_cached_foundation",
                return_value=None,
            ), patch(
                "geng_agent.agentic_foundation.load_foundation_writer_delivery",
                return_value=writer_delivery,
            ), patch(
                "geng_agent.agentic_foundation._load_foundation_validation_record",
                return_value={
                    "ok": True,
                    "input_hash": "current-input",
                    "issues": [],
                    "tests": {"passed": True, "delivery_immutable": True},
                },
            ), patch(
                "geng_agent.agentic_foundation._required_foundation_modules",
                return_value={"src/model.py"},
            ), patch(
                "geng_agent.agentic_foundation.restore_foundation_writer_delivery",
            ) as restore, patch(
                "geng_agent.agentic_foundation._finalize_foundation_delivery",
                return_value=finalized,
            ) as finalize, patch(
                "geng_agent.agentic_foundation.run_codex_subprocess",
            ) as writer:
                result = run_codex_foundation_writer_workflow(
                    facts={},
                    tasks={},
                    experiment_index={},
                    scientific_architecture={},
                    paper={},
                    paper_path=root / "paper.pdf",
                    paper_images=[],
                    paper_thesis=None,
                    output_dir=output_dir,
                    audit_dir=audit_dir,
                    resume=True,
                )

            resume_record = json.loads(
                (audit_dir / "03b_foundation_writer_resume.json").read_text(encoding="utf-8")
            )

        self.assertEqual(result, finalized)
        writer.assert_not_called()
        restore.assert_called_once()
        finalize.assert_called_once()
        self.assertEqual(resume_record["source"], "cached_writer_delivery_freeze_retry")
        self.assertFalse(resume_record["writer_rerun"])

    def test_matching_timed_out_validation_reuses_pristine_delivery_without_rerunning_writer(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            output_dir = root / "output"
            audit_dir = root / "audit"
            output_dir.mkdir()
            audit_dir.mkdir()
            validation_record = {
                "ok": False,
                "input_hash": "current-input",
                "issues": [
                    {
                        "file": "tests",
                        "message": "Foundation contract tests failed or timed out",
                    }
                ],
                "tests": {
                    "passed": False,
                    "timed_out": True,
                    "delivery_immutable": True,
                },
            }
            writer_delivery = {"trusted_changed": []}
            finalized = {"snapshot_hash": "revalidated-after-timeout"}

            with patch('geng_agent.agentic_foundation._collect_writer_analysis_artifacts', return_value={'scientific_architecture.json': {}}), patch('geng_agent.agentic_foundation._missing_required_analysis_artifacts', return_value=[]), patch('geng_agent.agentic_foundation._analysis_snapshot_hash', return_value='a' * 64), patch('geng_agent.agentic_foundation._foundation_input_hash', return_value='current-input'), patch('geng_agent.agentic_foundation._load_cached_foundation', return_value=None), patch('geng_agent.agentic_foundation.load_foundation_writer_delivery', return_value=writer_delivery), patch('geng_agent.agentic_foundation._load_foundation_validation_record', return_value=validation_record), patch('geng_agent.agentic_foundation._required_foundation_modules', return_value={'src/model.py'}), patch('geng_agent.agentic_foundation.restore_foundation_writer_delivery') as restore, patch('geng_agent.agentic_foundation._finalize_foundation_delivery', return_value=finalized) as finalize, patch('geng_agent.agentic_foundation.run_codex_subprocess') as writer:
                result = run_codex_foundation_writer_workflow(
                    facts={},
                    tasks={},
                    experiment_index={},
                    scientific_architecture={},
                    paper={},
                    paper_path=root / "paper.pdf",
                    paper_images=[],
                    paper_thesis=None,
                    output_dir=output_dir,
                    audit_dir=audit_dir,
                    resume=True,
                )

            resume_record = json.loads(
                (audit_dir / "03b_foundation_writer_resume.json").read_text(
                    encoding="utf-8"
                )
            )

        self.assertEqual(result, finalized)
        writer.assert_not_called()
        restore.assert_called_once()
        finalize.assert_called_once()
        self.assertEqual(
            resume_record["source"],
            "cached_writer_delivery_host_revalidation",
        )
        self.assertFalse(resume_record["writer_rerun"])


    def test_pristine_writer_delivery_restores_test_pollution_before_resume(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            sandbox = root / "sandbox"
            source = sandbox / "src" / "model.py"
            source.parent.mkdir(parents=True)
            source.write_text("VALUE = 1\n", encoding="utf-8")
            (sandbox / "requirements.txt").write_text("numpy\n", encoding="utf-8")
            (sandbox / "foundation_result.json").write_text(
                '{"status": "ready_for_tasks"}\n',
                encoding="utf-8",
            )
            delivery_dir = root / "deliveries" / ("a" * 64)

            receipt = persist_foundation_writer_delivery(
                sandbox=sandbox,
                delivery_dir=delivery_dir,
                input_hash="a" * 64,
                analysis_hash="b" * 64,
                environment_hash="host-runtime",
                required_modules={"src/model.py"},
                trusted_changed=[],
            )
            source.write_text("VALUE = 2\n", encoding="utf-8")
            (sandbox / "src" / "pollution.py").write_text("BAD = True\n", encoding="utf-8")

            loaded = load_foundation_writer_delivery(
                delivery_dir=delivery_dir,
                expected_input_hash="a" * 64,
                expected_required_modules={"src/model.py"},
            )
            self.assertIsNotNone(loaded)
            restore_foundation_writer_delivery(
                delivery_dir=delivery_dir,
                receipt=loaded or receipt,
                sandbox=sandbox,
            )

            self.assertEqual((sandbox / "src" / "model.py").read_text(encoding="utf-8"), "VALUE = 1\n")
            self.assertFalse((sandbox / "src" / "pollution.py").exists())
            self.assertTrue((sandbox / "src" / "_io.py").is_file())
            self.assertTrue((sandbox / "src" / "_backend.py").is_file())

    def test_snapshot_publication_restores_previous_directory_when_replace_fails(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            sandbox = root / "sandbox"
            source = sandbox / "src" / "model.py"
            source.parent.mkdir(parents=True)
            source.write_text("VALUE = 2\n", encoding="utf-8")
            snapshot = root / "snapshot"
            snapshot.mkdir()
            previous = snapshot / "previous.txt"
            previous.write_text("preserve me\n", encoding="utf-8")
            real_replace = os.replace
            replace_count = 0

            def fail_new_publication(source_path: object, target_path: object) -> None:
                nonlocal replace_count
                replace_count += 1
                if replace_count == 2:
                    raise OSError("publish failed")
                real_replace(source_path, target_path)

            with patch(
                "geng_agent.foundation_snapshot_delivery.os.replace",
                side_effect=fail_new_publication,
            ), self.assertRaisesRegex(OSError, "publish failed"):
                _publish_foundation_snapshot(sandbox=sandbox, snapshot_dir=snapshot)

            self.assertEqual(previous.read_text(encoding="utf-8"), "preserve me\n")
            self.assertFalse(snapshot.with_name(".snapshot.previous").exists())

    def test_successful_validation_record_is_reusable_for_freeze_retry(self) -> None:
        with TemporaryDirectory() as temp:
            path = Path(temp) / "03b_foundation_validation.json"
            path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "input_hash": "current-input",
                        "issues": [],
                        "tests": {"passed": True, "delivery_immutable": True},
                    }
                ),
                encoding="utf-8",
            )

            record = _load_foundation_validation_record(
                validation_path=path,
                expected_input_hash="current-input",
            )

        self.assertIsNotNone(record)
        self.assertTrue(record["ok"])






    def test_foundation_brief_treats_acceptance_bindings_as_output_interfaces_only(self) -> None:
        architecture = {
            "schema_version": "1.1",
            "components": [
                {
                    "id": "metric",
                    "kind": "metric",
                    "module": "src/metrics.py",
                    "callable": "bit_error_rate",
                    "execution": {},
                }
            ],
            "bindings": [
                {
                    "task_id": "fig_1",
                    "outputs": ["ber"],
                    "acceptance_bindings": [
                        {
                            "criterion_id": "fig_1.ber_decreases",
                            "criterion_kind": "core_conclusion",
                            "output_quantity_ids": ["ber"],
                        }
                    ],
                }
            ],
        }

        prompt = _foundation_brief(architecture)

        self.assertIn("fig_1.ber_decreases", prompt)
        self.assertIn("output-routing hints", prompt)
        self.assertIn("Do not decide whether a paper conclusion is supported", prompt)
        self.assertIn("Never add tests for paper-claim success", prompt)
        self.assertIn("pixel similarity", prompt)



    def test_unsafe_foundation_link_layout_skips_host_tests(self) -> None:
        with TemporaryDirectory() as temp:
            sandbox = Path(temp)
            source = sandbox / "src" / "model.py"
            source.parent.mkdir(parents=True)
            source.write_text("VALUE = 1\n", encoding="utf-8")
            with patch('geng_agent.agentic_foundation._foundation_project_files', side_effect=RuntimeError('Foundation output contains a link or reparse point: src/escape')), patch('geng_agent.agentic_foundation._run_foundation_tests') as host_tests:
                issues, result = _validate_foundation_delivery(
                    sandbox=sandbox,
                    architecture={},
                    trusted_changed=[],
                )

            self.assertTrue(result["skipped"])
            self.assertEqual(result["reason"], "unsafe Foundation filesystem layout")
            self.assertTrue(
                any(
                    "link or reparse point" in item["message"]
                    for item in issues
                )
            )
            host_tests.assert_not_called()

    def test_foundation_result_symlink_is_rejected_before_generated_content_reads(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            sandbox = root / "sandbox"
            sandbox.mkdir()
            evidence = sandbox / "paper_evidence"
            evidence.mkdir()
            (evidence / "host-owned.json").write_text("{}\n", encoding="utf-8")
            outside = root / "outside-result.json"
            outside.write_text('{"status": "ready_for_tasks"}\n', encoding="utf-8")
            _symlink_or_skip(self, sandbox / "foundation_result.json", outside)

            with patch('geng_agent.agentic_foundation._run_foundation_tests') as host_tests:
                issues, result = _validate_foundation_delivery(
                    sandbox=sandbox,
                    architecture={},
                    trusted_changed=[],
                )

            self.assertTrue(result["skipped"])
            self.assertEqual(result["reason"], "unsafe Foundation filesystem layout")
            self.assertTrue(any("foundation_result.json" in item["message"] for item in issues), issues)
            host_tests.assert_not_called()

    def test_foundation_source_symlink_is_rejected_before_execution_validation(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            sandbox = root / "sandbox"
            source_dir = sandbox / "src"
            source_dir.mkdir(parents=True)
            outside = root / "outside-model.py"
            outside.write_text("VALUE = 1\n", encoding="utf-8")
            _symlink_or_skip(self, source_dir / "model.py", outside)
            (sandbox / "foundation_result.json").write_text("{}\n", encoding="utf-8")

            with patch('geng_agent.agentic_foundation._run_foundation_tests') as host_tests:
                issues, result = _validate_foundation_delivery(
                    sandbox=sandbox,
                    architecture={},
                    trusted_changed=[],
                )

            self.assertTrue(result["skipped"])
            self.assertEqual(result["reason"], "unsafe Foundation filesystem layout")
            self.assertTrue(any("src/model.py" in item["message"] for item in issues), issues)
            host_tests.assert_not_called()

    def test_trusted_runtime_restore_refuses_symlink_without_touching_target(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            sandbox = root / "sandbox"
            source_dir = sandbox / "src"
            source_dir.mkdir(parents=True)
            outside = root / "outside-runtime.py"
            outside.write_text("DO_NOT_CHANGE = True\n", encoding="utf-8")
            _symlink_or_skip(self, source_dir / "_io.py", outside)

            with self.assertRaisesRegex(RuntimeError, "link or reparse point"):
                _restore_trusted_runtime_atomically(sandbox)

            self.assertEqual(outside.read_text(encoding="utf-8"), "DO_NOT_CHANGE = True\n")

    def test_workflow_rejects_post_agent_runtime_hardlink_before_hash_or_restore(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            output_dir = root / "output"
            audit_dir = root / "audit"
            output_dir.mkdir()
            audit_dir.mkdir()
            outside = root / "outside-runtime.py"
            outside.write_text("DO_NOT_CHANGE = True\n", encoding="utf-8")

            def fake_inject(project_dir: Path) -> Path:
                source_dir = project_dir / "src"
                source_dir.mkdir(parents=True, exist_ok=True)
                io_path = source_dir / "_io.py"
                io_path.write_text("TRUSTED = True\n", encoding="utf-8")
                (source_dir / "_backend.py").write_text("TRUSTED = True\n", encoding="utf-8")
                return io_path

            def fake_codex(**kwargs: object) -> dict[str, bool]:
                work_dir = Path(kwargs["work_dir"])
                io_path = work_dir / "src" / "_io.py"
                io_path.unlink()
                _hardlink_or_skip(self, io_path, outside)
                return {"ok": True}

            with patch(
                "geng_agent.agentic_foundation._collect_writer_analysis_artifacts",
                return_value={"scientific_architecture.json": {}},
            ), patch(
                "geng_agent.agentic_foundation._missing_required_analysis_artifacts",
                return_value=[],
            ), patch(
                "geng_agent.agentic_foundation._analysis_snapshot_hash",
                return_value="a" * 64,
            ), patch(
                "geng_agent.agentic_foundation._write_paper_evidence_bundle",
            ), patch(
                "geng_agent.agentic_foundation.inject_io_runtime",
                side_effect=fake_inject,
            ) as inject_runtime, patch(
                "geng_agent.agentic_foundation._trusted_hashes",
                return_value={"src/_io.py": "before", "src/_backend.py": "before"},
            ) as trusted_hashes, patch(
                "geng_agent.agentic_foundation.run_codex_subprocess",
                side_effect=fake_codex,
            ), patch(
                "geng_agent.agentic_foundation._restore_trusted_runtime_atomically",
            ) as restore_runtime, patch(
                "geng_agent.agentic_foundation._validate_foundation_delivery",
            ) as delivery_validator:
                with self.assertRaisesRegex(
                    RuntimeError,
                    r"unsafe filesystem layout: .*hard-linked regular file: src/_io\.py",
                ):
                    run_codex_foundation_writer_workflow(
                        facts={},
                        tasks={},
                        experiment_index={},
                        scientific_architecture={},
                        paper={},
                        paper_path=root / "paper.pdf",
                        paper_images=[],
                        paper_thesis=None,
                        output_dir=output_dir,
                        audit_dir=audit_dir,
                        resume=False,
                    )

            inject_runtime.assert_called_once()
            trusted_hashes.assert_called_once()
            restore_runtime.assert_not_called()
            delivery_validator.assert_not_called()
            self.assertEqual(outside.read_text(encoding="utf-8"), "DO_NOT_CHANGE = True\n")






    def test_failed_host_unittest_is_preserved_for_supervisor(self) -> None:
        with TemporaryDirectory() as temp:
            sandbox = Path(temp)
            source = sandbox / "src" / "model.py"
            source.parent.mkdir(parents=True)
            source.write_text("VALUE = 1\n", encoding="utf-8")
            with patch('geng_agent.agentic_foundation._required_foundation_modules', return_value={'src/model.py'}), patch('geng_agent.agentic_foundation._run_foundation_tests', return_value={'passed': False, 'returncode': 1}) as host_tests:
                issues, result = _validate_foundation_delivery(
                    sandbox=sandbox,
                    architecture={},
                    trusted_changed=[],
                )

            self.assertEqual(issues, [])
            self.assertTrue(any(item["kind"] == "test_execution_not_passed" for item in result["observations"]))
            self.assertFalse(result["passed"])
            host_tests.assert_called_once_with(sandbox)

    def test_host_tests_cannot_change_files_eligible_for_freezing(self) -> None:
        with TemporaryDirectory() as temp:
            sandbox = Path(temp)
            source = sandbox / "src" / "model.py"
            source.parent.mkdir(parents=True)
            source.write_text("VALUE = 1\n", encoding="utf-8")

            def mutate_delivery(work_dir: Path) -> dict[str, object]:
                source.write_text("VALUE = 2\n", encoding="utf-8")
                artifact = work_dir / "tests" / "runtime_artifacts" / "result.json"
                artifact.parent.mkdir(parents=True)
                artifact.write_text("{}\n", encoding="utf-8")
                return {"passed": True, "returncode": 0}

            with patch('geng_agent.agentic_foundation._required_foundation_modules', return_value={'src/model.py'}), patch('geng_agent.agentic_foundation._run_foundation_tests', side_effect=mutate_delivery):
                issues, result = _validate_foundation_delivery(
                    sandbox=sandbox,
                    architecture={},
                    trusted_changed=[],
                )

            self.assertTrue(result["passed"])  # Test exit and delivery integrity are separate facts.
            self.assertFalse(result["delivery_immutable"])
            self.assertEqual(result["changed_delivery_files"], ["src/model.py"])
            self.assertTrue(
                any("eligible for freezing" in item["message"] for item in issues)
            )

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
            pyo = project / "tests" / "legacy.pyo"
            pyo.write_bytes(b"optimized bytecode")
            cached_source = project / "src" / "__pycache__" / "injected.py"
            cached_source.write_text("VALUE = 5\n", encoding="utf-8")
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
                {"src/shadow.py", "src/payload.pyd", "src/__pycache__/injected.py", "tests/extra.py", "configs/foundation_override.yaml"}
                <= files,
                violations,
            )
            self.assertNotIn("src/__pycache__/rogue.pyc", files)
            self.assertNotIn("tests/legacy.pyo", files)

            restore_foundation_snapshot(project, foundation)
            self.assertEqual(foundation_violations(project, foundation), [])
            self.assertFalse((project / "src" / "shadow.py").exists())
            self.assertFalse((project / "src" / "payload.pyd").exists())
            self.assertFalse(pyc.exists())
            self.assertFalse(pyo.exists())
            self.assertFalse(cached_source.exists())
            self.assertFalse(extra_test.exists())
            self.assertFalse(override.exists())


if __name__ == "__main__":
    unittest.main()
