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
    _host_revalidation_already_attempted,
    _load_foundation_validation_record,
    _load_cached_foundation_failure,
    _restore_trusted_runtime_atomically,
    _validation_allows_writer_delivery_reuse,
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
                    "returncode": 1,
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

    def test_validation_reuse_classifier_separates_host_failures_from_writer_defects(self) -> None:
        infrastructure = {
            "ok": False,
            "issues": [{"file": "tests", "message": "contract tests failed"}],
            "tests": {
                "passed": False,
                "returncode": 1,
                "stderr": (
                    "C:/env/Lib/site-packages/numpy/core.py: "
                    "NameError raised while NumPy initialized through the host guard"
                ),
                "delivery_immutable": True,
            },
        }
        assertion_failure = {
            "ok": False,
            "issues": [{"file": "tests", "message": "contract tests failed"}],
            "tests": {
                "passed": False,
                "returncode": 1,
                "stderr": "AssertionError: expected shape (4, 4)",
                "delivery_immutable": True,
            },
        }
        pretest_failure = {
            "ok": False,
            "issues": [{"file": "src/model.py", "message": "syntax error"}],
            "tests": {"passed": False, "skipped": True},
        }
        immutable_timeout = {
            "ok": False,
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
        mutated_timeout = {
            "ok": False,
            "issues": [
                {
                    "file": "tests",
                    "message": "Foundation contract tests failed or timed out",
                }
            ],
            "tests": {
                "passed": False,
                "timed_out": True,
                "delivery_immutable": False,
            },
        }
        timeout_with_assertion = {
            "ok": False,
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
                "stderr": "FAIL: test_shape\nAssertionError: expected shape (4, 4)",
            },
        }
        timeout_with_returncode = {
            "ok": False,
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
                "returncode": 1,
            },
        }
        malformed_empty_timeout = {
            "ok": False,
            "issues": [],
            "tests": {
                "passed": False,
                "timed_out": True,
                "delivery_immutable": True,
            },
        }

        self.assertTrue(_validation_allows_writer_delivery_reuse(None))
        self.assertTrue(_validation_allows_writer_delivery_reuse({"ok": True}))
        self.assertTrue(_validation_allows_writer_delivery_reuse(infrastructure))
        self.assertTrue(
            _validation_allows_writer_delivery_reuse(immutable_timeout)
        )
        self.assertFalse(
            _validation_allows_writer_delivery_reuse(mutated_timeout)
        )
        self.assertFalse(
            _validation_allows_writer_delivery_reuse(timeout_with_assertion)
        )
        self.assertFalse(
            _validation_allows_writer_delivery_reuse(timeout_with_returncode)
        )
        self.assertFalse(
            _validation_allows_writer_delivery_reuse(malformed_empty_timeout)
        )
        self.assertFalse(
            _validation_allows_writer_delivery_reuse(
                {
                    "ok": False,
                    "issues": [{"file": "tests", "message": "delivery changed"}],
                    "tests": {"passed": False, "delivery_immutable": False},
                }
            )
        )
        self.assertFalse(_validation_allows_writer_delivery_reuse(assertion_failure))
        self.assertFalse(_validation_allows_writer_delivery_reuse(pretest_failure))
        self.assertFalse(
            _validation_allows_writer_delivery_reuse(
                {
                    "ok": False,
                    "issues": [{"file": "tests", "message": "contract tests failed"}],
                    "tests": {
                        "passed": False,
                        "returncode": 1,
                        "stderr": "NameError: missing in numpy.asarray(missing)",
                        "delivery_immutable": True,
                    },
                }
            )
        )
        self.assertFalse(
            _validation_allows_writer_delivery_reuse(
                {
                    "ok": False,
                    "issues": [{"file": "tests", "message": "contract tests failed"}],
                    "tests": {
                        "passed": False,
                        "returncode": 1,
                        "stderr": "PermissionError: Foundation runtime guard: open outside output roots",
                        "delivery_immutable": True,
                    },
                }
            )
        )

    def test_host_delivery_revalidation_is_limited_to_one_attempt(self) -> None:
        with TemporaryDirectory() as temp:
            resume_path = Path(temp) / "03b_foundation_writer_resume.json"
            resume_path.write_text(
                json.dumps(
                    {
                        "source": "cached_writer_delivery_host_revalidation",
                        "input_hash": "current-input",
                        "host_validation_policy_hash": "policy-v1",
                        "writer_rerun": False,
                    }
                ),
                encoding="utf-8",
            )

            attempted = _host_revalidation_already_attempted(
                resume_path=resume_path,
                expected_input_hash="current-input",
                expected_policy_hash="policy-v1",
            )
            stale = _host_revalidation_already_attempted(
                resume_path=resume_path,
                expected_input_hash="different-input",
                expected_policy_hash="policy-v1",
            )
            changed_policy = _host_revalidation_already_attempted(
                resume_path=resume_path,
                expected_input_hash="current-input",
                expected_policy_hash="policy-v2",
            )

        self.assertTrue(attempted)
        self.assertFalse(stale)
        self.assertFalse(changed_policy)

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
                "geng_agent.agentic_foundation._host_validation_policy_hash",
                return_value="policy-v1",
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
        self.assertEqual(resume_record["host_validation_policy_hash"], "policy-v1")

    def test_repeated_timed_out_revalidation_stops_without_accepting_or_rerunning_writer(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            output_dir = root / "output"
            audit_dir = root / "audit"
            output_dir.mkdir()
            audit_dir.mkdir()
            (audit_dir / "03b_foundation_writer_resume.json").write_text(
                json.dumps(
                    {
                        "ok": None,
                        "source": "cached_writer_delivery_host_revalidation",
                        "input_hash": "current-input",
                        "writer_rerun": False,
                        "host_validation_policy_hash": "policy-v1",
                    }
                ),
                encoding="utf-8",
            )
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
                return_value={"trusted_changed": []},
            ), patch(
                "geng_agent.agentic_foundation._load_foundation_validation_record",
                return_value=validation_record,
            ), patch(
                "geng_agent.agentic_foundation._required_foundation_modules",
                return_value={"src/model.py"},
            ), patch(
                "geng_agent.agentic_foundation._host_validation_policy_hash",
                return_value="policy-v1",
            ), patch(
                "geng_agent.agentic_foundation.restore_foundation_writer_delivery",
            ) as restore, patch(
                "geng_agent.agentic_foundation._finalize_foundation_delivery",
            ) as finalize, patch(
                "geng_agent.agentic_foundation._publish_foundation_snapshot",
            ) as publish, patch(
                "geng_agent.agentic_foundation.run_codex_subprocess",
            ) as writer:
                with self.assertRaisesRegex(
                    RuntimeError,
                    "still fails after one pristine delivery revalidation",
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
                        resume=True,
                    )

            writer.assert_not_called()
            restore.assert_not_called()
            finalize.assert_not_called()
            publish.assert_not_called()
            self.assertFalse((output_dir / "foundation_manifest.json").exists())

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
        self.assertTrue(_validation_allows_writer_delivery_reuse(record))

    def test_cached_foundation_failure_returns_matching_nonempty_issues(self) -> None:
        with TemporaryDirectory() as temp:
            audit_dir = Path(temp)
            issues = [
                {
                    "file": "src/model.py",
                    "message": "Foundation contract tests failed",
                }
            ]
            (audit_dir / "03b_foundation_validation.json").write_text(
                (
                    '{"ok": false, "input_hash": "current-input", '
                    '"issues": [{"file": "src/model.py", '
                    '"message": "Foundation contract tests failed"}]}\n'
                ),
                encoding="utf-8",
            )

            cached = _load_cached_foundation_failure(
                validation_path=audit_dir / "03b_foundation_validation.json",
                expected_input_hash="current-input",
            )

            self.assertEqual(cached, issues)

    def test_cached_foundation_failure_ignores_different_input_hash(self) -> None:
        with TemporaryDirectory() as temp:
            audit_dir = Path(temp)
            (audit_dir / "03b_foundation_validation.json").write_text(
                (
                    '{"ok": false, "input_hash": "stale-input", '
                    '"issues": [{"message": "stale failure"}]}\n'
                ),
                encoding="utf-8",
            )

            cached = _load_cached_foundation_failure(
                validation_path=audit_dir / "03b_foundation_validation.json",
                expected_input_hash="current-input",
            )

            self.assertIsNone(cached)

    def test_cached_foundation_failure_ignores_successful_validation(self) -> None:
        with TemporaryDirectory() as temp:
            audit_dir = Path(temp)
            (audit_dir / "03b_foundation_validation.json").write_text(
                (
                    '{"ok": true, "input_hash": "current-input", '
                    '"issues": [{"message": "not a cached failure"}]}\n'
                ),
                encoding="utf-8",
            )

            cached = _load_cached_foundation_failure(
                validation_path=audit_dir / "03b_foundation_validation.json",
                expected_input_hash="current-input",
            )

            self.assertIsNone(cached)

    def test_cached_foundation_failure_requires_nonempty_issue_list(self) -> None:
        with TemporaryDirectory() as temp:
            audit_dir = Path(temp)
            validation_path = audit_dir / "03b_foundation_validation.json"
            for issues_json in ("[]", '"not-a-list"', "null"):
                with self.subTest(issues=issues_json):
                    validation_path.write_text(
                        (
                            '{"ok": false, "input_hash": "current-input", '
                            f'"issues": {issues_json}}}\n'
                        ),
                        encoding="utf-8",
                    )

                    cached = _load_cached_foundation_failure(
                        validation_path=validation_path,
                        expected_input_hash="current-input",
                    )

                    self.assertIsNone(cached)

    def test_cached_foundation_failure_ignores_corrupt_json(self) -> None:
        with TemporaryDirectory() as temp:
            audit_dir = Path(temp)
            (audit_dir / "03b_foundation_validation.json").write_text(
                '{"ok": false,',
                encoding="utf-8",
            )

            cached = _load_cached_foundation_failure(
                validation_path=audit_dir / "03b_foundation_validation.json",
                expected_input_hash="current-input",
            )

            self.assertIsNone(cached)

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

    def test_unsafe_foundation_link_layout_skips_host_tests(self) -> None:
        with TemporaryDirectory() as temp:
            sandbox = Path(temp)
            source = sandbox / "src" / "model.py"
            source.parent.mkdir(parents=True)
            source.write_text("VALUE = 1\n", encoding="utf-8")
            with patch(
                "geng_agent.agentic_foundation._validate_foundation_execution_contracts",
                return_value=([], []),
            ) as execution_validator, patch(
                "geng_agent.agentic_foundation._foundation_project_files",
                side_effect=RuntimeError(
                    "Foundation output contains a link or reparse point: src/escape"
                ),
            ), patch(
                "geng_agent.agentic_foundation._run_foundation_tests",
            ) as host_tests:
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
            execution_validator.assert_not_called()

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

            with patch(
                "geng_agent.agentic_foundation._validate_foundation_execution_contracts",
            ) as execution_validator, patch(
                "geng_agent.agentic_foundation._run_foundation_tests",
            ) as host_tests:
                issues, result = _validate_foundation_delivery(
                    sandbox=sandbox,
                    architecture={},
                    trusted_changed=[],
                )

            self.assertTrue(result["skipped"])
            self.assertEqual(result["reason"], "unsafe Foundation filesystem layout")
            self.assertTrue(any("foundation_result.json" in item["message"] for item in issues), issues)
            execution_validator.assert_not_called()
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

            with patch(
                "geng_agent.agentic_foundation._validate_foundation_execution_contracts",
            ) as execution_validator, patch(
                "geng_agent.agentic_foundation._run_foundation_tests",
            ) as host_tests:
                issues, result = _validate_foundation_delivery(
                    sandbox=sandbox,
                    architecture={},
                    trusted_changed=[],
                )

            self.assertTrue(result["skipped"])
            self.assertEqual(result["reason"], "unsafe Foundation filesystem layout")
            self.assertTrue(any("src/model.py" in item["message"] for item in issues), issues)
            execution_validator.assert_not_called()
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

    def test_static_security_finding_remains_blocking(self) -> None:
        with TemporaryDirectory() as temp:
            sandbox = Path(temp)
            source = sandbox / "src" / "model.py"
            source.parent.mkdir(parents=True)
            source.write_text("VALUE = 1\n", encoding="utf-8")
            with patch(
                "geng_agent.agentic_foundation._validate_foundation_execution_contracts",
                return_value=([], []),
            ), patch(
                "geng_agent.agentic_foundation._required_foundation_modules",
                return_value={"src/model.py"},
            ), patch(
                "geng_agent.agentic_foundation._missing_local_imports",
                return_value=[],
            ), patch(
                "geng_agent.agentic_foundation.static_scan_repro_project",
                return_value=[
                    {
                        "file": "src/model.py",
                        "line": "1",
                        "message": "forbidden dynamic builtin: eval",
                    }
                ],
            ), patch(
                "geng_agent.agentic_foundation._run_foundation_tests",
                return_value={"passed": True, "returncode": 0},
            ) as host_tests:
                issues, result = _validate_foundation_delivery(
                    sandbox=sandbox,
                    architecture={},
                    trusted_changed=[],
                )

            self.assertTrue(
                any("forbidden dynamic builtin: eval" in item["message"] for item in issues)
            )
            self.assertTrue(result["skipped"])
            host_tests.assert_not_called()

    def test_authorized_foundation_security_findings_are_advisory(self) -> None:
        with TemporaryDirectory() as temp:
            sandbox = Path(temp)
            source = sandbox / "src" / "model.py"
            source.parent.mkdir(parents=True)
            source.write_text(
                (
                    "import importlib\n"
                    "import os\n"
                    "FOUND = importlib.util.find_spec('numpy')\n"
                    "ENV = os.environ.get('MODEL_SIZE')\n"
                    "ALT = os.getenv('MODEL_SIZE')\n"
                    "SECRET = os.getenv('OPENAI_API_KEY')\n"
                    "ENV_KEY = 'MODEL_' + 'SIZE'\n"
                    "DYNAMIC = os.getenv(ENV_KEY)\n"
                    "SUBSCRIPT = os.environ[ENV_KEY]\n"
                    "BULK = dict(os.environ)\n"
                    "os.environ.update({'MODEL_SIZE': 'small'})\n"
                    "os.environ.pop('OPENAI_API_KEY', None)\n"
                    "ATTR_NAME = 'environ'\n"
                    "DYNAMIC_ATTR = getattr(os, ATTR_NAME)\n"
                    "VALUE = getattr(object(), '__class__', None)\n"
                    "setattr(VALUE, 'tag', 1)\n"
                    "delattr(VALUE, 'tag')\n"
                    "GLOBAL_KEYS = tuple(globals())\n"
                    "VALUE_KEYS = tuple(vars(VALUE))\n"
                ),
                encoding="utf-8",
            )
            with patch(
                "geng_agent.agentic_foundation._validate_foundation_execution_contracts",
                return_value=([], []),
            ), patch(
                "geng_agent.agentic_foundation._required_foundation_modules",
                return_value={"src/model.py"},
            ), patch(
                "geng_agent.agentic_foundation._missing_local_imports",
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
            security_warnings = {
                item["message"]: item
                for item in result["warnings"]
                if item.get("category") in {
                    "environment_access",
                    "importlib_usage",
                    "ordinary_reflection",
                }
            }
            expected = {
                "forbidden import: importlib": "importlib_usage",
                "forbidden environment access: os.environ": "environment_access",
                "forbidden environment access: os.getenv": "environment_access",
                "forbidden dynamic builtin: getattr": "ordinary_reflection",
                "forbidden dynamic builtin: setattr": "ordinary_reflection",
                "forbidden dynamic builtin: delattr": "ordinary_reflection",
                "forbidden dynamic builtin: globals": "ordinary_reflection",
                "forbidden dynamic builtin: vars": "ordinary_reflection",
            }
            self.assertTrue(set(expected) <= set(security_warnings), security_warnings)
            for message, category in expected.items():
                self.assertEqual(security_warnings[message]["category"], category)
                self.assertEqual(security_warnings[message]["severity"], "warning")
            environment_warnings = [
                item
                for item in result["warnings"]
                if (
                    "os.environ" in item["message"]
                    or "os.getenv" in item["message"]
                )
            ]
            self.assertTrue(environment_warnings)
            self.assertTrue(
                all(
                    item["category"] == "environment_access"
                    and item["severity"] == "warning"
                    for item in environment_warnings
                ),
                environment_warnings,
            )
            self.assertTrue(
                any("sensitive key" in item["message"] for item in environment_warnings),
                environment_warnings,
            )
            self.assertTrue(
                any("dynamic key" in item["message"] for item in environment_warnings),
                environment_warnings,
            )
            self.assertTrue(
                any(
                    "bulk or mutating operation" in item["message"]
                    for item in environment_warnings
                ),
                environment_warnings,
            )

    def test_unapproved_dangerous_security_findings_remain_blocking(self) -> None:
        with TemporaryDirectory() as temp:
            sandbox = Path(temp)
            source = sandbox / "src" / "model.py"
            source.parent.mkdir(parents=True)
            source.write_text(
                (
                    "import importlib as loader\n"
                    "import os as operating\n"
                    "module_name = 'socket'\n"
                    "loader.import_module(module_name)\n"
                    "loader.import_module('socket')\n"
                    "lib = loader\n"
                    "lib.import_module(name='socket')\n"
                    "loader.util.spec_from_file_location('x', '/outside/module.py')\n"
                    "dynamic_loader = loader.machinery.SourceFileLoader('x', '/outside/module.py')\n"
                    "dynamic_loader.exec_module(None)\n"
                    "getattr(operating, 'sys' + 'tem')\n"
                    "eval('1')\n"
                    "exec('x = 1')\n"
                    "compile('1', '<generated>', 'eval')\n"
                    "__import__('socket')\n"
                    "operating.system('echo blocked')\n"
                    "op = operating\n"
                    "op.system('echo blocked module alias')\n"
                    "run_eval = eval\n"
                    "run_eval('1')\n"
                    "run_process = operating.system\n"
                    "run_process('echo blocked alias')\n"
                    "open('/outside-case.txt', 'w')\n"
                ),
                encoding="utf-8",
            )
            with patch(
                "geng_agent.agentic_foundation._validate_foundation_execution_contracts",
                return_value=([], []),
            ), patch(
                "geng_agent.agentic_foundation._required_foundation_modules",
                return_value={"src/model.py"},
            ), patch(
                "geng_agent.agentic_foundation._missing_local_imports",
                return_value=[],
            ), patch(
                "geng_agent.agentic_foundation._run_foundation_tests",
            ) as host_tests:
                issues, result = _validate_foundation_delivery(
                    sandbox=sandbox,
                    architecture={},
                    trusted_changed=[],
                )

            self.assertTrue(result["skipped"])
            host_tests.assert_not_called()
            categories = {item.get("category") for item in issues}
            self.assertTrue(
                {
                    "dangerous_dynamic_import",
                    "dangerous_reflection",
                    "dynamic_execution",
                    "security_violation",
                }
                <= categories,
                issues,
            )
            messages = {item["message"] for item in issues}
            self.assertIn(
                "dangerous dynamic import: module target is not a string literal",
                messages,
            )
            self.assertIn(
                "dangerous reflection: sensitive module os attribute 'system'",
                messages,
            )
            self.assertIn(
                "dangerous dynamic import: forbidden module target 'socket'",
                messages,
            )
            self.assertTrue(
                any("spec_from_file_location" in message for message in messages),
                messages,
            )
            self.assertTrue(
                any("loader construction" in message for message in messages),
                messages,
            )
            self.assertIn("forbidden dynamic builtin: eval", messages)
            self.assertIn("forbidden dynamic builtin: exec", messages)
            self.assertIn("forbidden dynamic builtin: compile", messages)
            self.assertIn("forbidden dynamic builtin: __import__", messages)
            self.assertIn("forbidden call: os.system", messages)
            self.assertTrue(any(message.startswith("absolute path literal") for message in messages))

    def test_environment_and_static_contract_gaps_are_advisory(self) -> None:
        with TemporaryDirectory() as temp:
            sandbox = Path(temp)
            source = sandbox / "src" / "model.py"
            source.parent.mkdir(parents=True)
            source.write_text("VALUE = 1\n", encoding="utf-8")
            static_findings = [
                {
                    "file": "scientific_architecture.json",
                    "message": "component encoder environment_extension_required: no trusted training capability probe is registered for framework 'numpy'",
                },
                {
                    "file": "scientific_architecture.json",
                    "message": "component adapter environment_extension_required: no trusted host invocation adapter is registered for external runtime 'MATLAB'",
                },
                {
                    "file": "src/model.py",
                    "message": "component encoder callable Encoder.forward is absent from its declared module",
                },
                {
                    "file": "src/model.py",
                    "message": "component encoder primary framework 'numpy' is never imported by Foundation-owned source",
                },
                {
                    "file": "foundation_result.json",
                    "message": "component encoder execution contract weakens or changes precision",
                },
                {
                    "file": "foundation_result.json",
                    "message": "component encoder lacks passing capability_tests evidence for gradient/back-propagation",
                },
            ]
            with patch(
                "geng_agent.agentic_foundation._validate_foundation_execution_contracts",
                return_value=(static_findings, []),
            ), patch(
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
            categorized = [
                item
                for item in result["warnings"]
                if item.get("category") in {
                    "execution_capability_advisory",
                    "static_contract_advisory",
                }
            ]
            self.assertEqual(len(categorized), len(static_findings))
            self.assertTrue(all(item.get("severity") == "warning" for item in categorized))
            self.assertEqual(
                sum(item.get("category") == "execution_capability_advisory" for item in categorized),
                2,
            )

    def test_missing_local_import_remains_blocking_before_host_tests(self) -> None:
        with TemporaryDirectory() as temp:
            sandbox = Path(temp)
            source = sandbox / "src" / "model.py"
            source.parent.mkdir(parents=True)
            source.write_text("from src.missing import Model\n", encoding="utf-8")
            with patch(
                "geng_agent.agentic_foundation._validate_foundation_execution_contracts",
                return_value=([], []),
            ), patch(
                "geng_agent.agentic_foundation._required_foundation_modules",
                return_value={"src/model.py"},
            ), patch(
                "geng_agent.agentic_foundation._missing_local_imports",
                return_value=[{"file": "src/model.py", "message": "missing local import: src.missing"}],
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

            self.assertTrue(any("missing local import" in item["message"] for item in issues))
            self.assertTrue(result["skipped"])
            host_tests.assert_not_called()

    def test_failed_host_unittest_remains_blocking(self) -> None:
        with TemporaryDirectory() as temp:
            sandbox = Path(temp)
            source = sandbox / "src" / "model.py"
            source.parent.mkdir(parents=True)
            source.write_text("VALUE = 1\n", encoding="utf-8")
            with patch(
                "geng_agent.agentic_foundation._validate_foundation_execution_contracts",
                return_value=([{"file": "scientific_architecture.json", "message": "component encoder environment_extension_required: no trusted probe"}], []),
            ), patch(
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
                return_value={"passed": False, "returncode": 1},
            ) as host_tests:
                issues, result = _validate_foundation_delivery(
                    sandbox=sandbox,
                    architecture={},
                    trusted_changed=[],
                )

            self.assertTrue(any("contract tests failed" in item["message"] for item in issues))
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

            with patch(
                "geng_agent.agentic_foundation._validate_foundation_execution_contracts",
                return_value=([], []),
            ), patch(
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
                side_effect=mutate_delivery,
            ):
                issues, result = _validate_foundation_delivery(
                    sandbox=sandbox,
                    architecture={},
                    trusted_changed=[],
                )

            self.assertFalse(result["passed"])
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
