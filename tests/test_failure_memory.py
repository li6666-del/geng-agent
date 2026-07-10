from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from geng_agent.failure_memory import (
    FailureMemory,
    FailureMemoryFormatError,
    append_failure,
    failure_fingerprint,
    load_failures,
    query_failures,
)
from geng_agent.revision_router import (
    ErrorCategory,
    RevisionRequest,
    can_reenter,
    classify_revision_error,
    parse_revision_request,
    validate_revision_request,
)


def failure(**overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        "task_id": "task-ber",
        "scenario": "awgn",
        "category": "code_or_runtime",
        "message": "simulation exited with code 1",
        "details": {"returncode": 1, "command": ["python", "run.py"]},
        "created_at": "2026-07-10T10:00:00Z",
    }
    record.update(overrides)
    return record


def revision_request(**overrides: object) -> dict[str, object]:
    request: dict[str, object] = {
        "task_id": "task-ber",
        "scenario": "awgn",
        "error": "schema validation failed: missing required field metric",
        "requested_changes": ["Add the metric field"],
        "reentry_count": 0,
    }
    request.update(overrides)
    return request


class FailureFingerprintTests(unittest.TestCase):
    def test_fingerprint_is_stable_across_key_order_and_observation_time(self) -> None:
        first = failure()
        second = dict(reversed(list(first.items())))
        second["created_at"] = "2026-07-10T11:00:00Z"
        second["fingerprint"] = "stale-value"

        self.assertEqual(failure_fingerprint(first), failure_fingerprint(second))
        self.assertNotEqual(
            failure_fingerprint(first),
            failure_fingerprint({**first, "message": "different failure"}),
        )

    def test_fingerprint_rejects_non_finite_json(self) -> None:
        with self.assertRaises(ValueError):
            failure_fingerprint(failure(details={"loss": float("nan")}))


class FailureMemoryTests(unittest.TestCase):
    def test_append_is_idempotent_and_load_repairs_stored_fingerprint(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "audit" / "failures.jsonl"
            self.assertTrue(append_failure(path, failure()))
            self.assertFalse(append_failure(path, failure(created_at="later")))

            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            stored = json.loads(lines[0])
            self.assertEqual(stored["fingerprint"], failure_fingerprint(stored))
            self.assertEqual(load_failures(path), [stored])

    def test_load_deduplicates_duplicate_lines_and_uses_first_record(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "failures.jsonl"
            first = failure(created_at="first", fingerprint="incorrect")
            duplicate = failure(created_at="second")
            path.write_text(
                json.dumps(first) + "\n" + json.dumps(duplicate) + "\n",
                encoding="utf-8",
            )

            loaded = load_failures(path)
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0]["created_at"], "first")
            self.assertEqual(loaded[0]["fingerprint"], failure_fingerprint(first))

    def test_append_repairs_missing_final_line_separator(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "failures.jsonl"
            path.write_text(json.dumps(failure()), encoding="utf-8")

            self.assertTrue(append_failure(path, failure(task_id="task-capacity")))
            self.assertEqual(len(load_failures(path)), 2)

    def test_load_strict_and_lenient_corruption_boundaries(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "failures.jsonl"
            path.write_text(json.dumps(failure()) + "\nnot-json\n[]\n", encoding="utf-8")

            with self.assertRaises(FailureMemoryFormatError) as raised:
                load_failures(path)
            self.assertEqual(raised.exception.line_number, 2)
            self.assertEqual(len(load_failures(path, strict=False)), 1)

    def test_query_filters_task_and_scenario_and_accepts_legacy_task_key(self) -> None:
        records = [
            failure(),
            failure(task_id="task-capacity", scenario="rayleigh"),
            {
                "task": "legacy-task",
                "scenario": "awgn",
                "message": "old record",
            },
        ]

        self.assertEqual(len(query_failures(records, scenario="awgn")), 2)
        self.assertEqual(query_failures(records, task_id="task-capacity")[0]["scenario"], "rayleigh")
        self.assertEqual(query_failures(records, task="legacy-task")[0]["message"], "old record")
        with self.assertRaises(ValueError):
            query_failures(records, task="a", task_id="b")

    def test_path_bound_api_handles_missing_file(self) -> None:
        with TemporaryDirectory() as temp_dir:
            memory = FailureMemory(Path(temp_dir) / "missing" / "failures.jsonl")
            self.assertEqual(memory.load(), [])
            self.assertTrue(memory.append(failure()))
            self.assertEqual(len(memory.query(task_id="task-ber", scenario="awgn")), 1)


class RevisionRequestTests(unittest.TestCase):
    def test_valid_request_round_trips(self) -> None:
        raw = revision_request(category="contract_error")
        self.assertEqual(validate_revision_request(raw), [])
        parsed = parse_revision_request(raw)
        self.assertIsInstance(parsed, RevisionRequest)
        self.assertEqual(parsed.category, ErrorCategory.CONTRACT_ERROR)

    def test_validation_reports_missing_empty_extra_and_boolean_count(self) -> None:
        raw = revision_request(
            task_id=" ",
            requested_changes=[],
            reentry_count=True,
            unexpected="typo",
        )
        issues = validate_revision_request(raw)
        paths = {issue["path"] for issue in issues}
        self.assertIn("$.task_id", paths)
        self.assertIn("$.requested_changes", paths)
        self.assertIn("$.reentry_count", paths)
        self.assertIn("$.unexpected", paths)
        self.assertEqual(
            validate_revision_request("not-an-object"),
            [{"path": "$", "message": "revision request must be an object"}],
        )

    def test_error_object_must_be_nonempty_json(self) -> None:
        self.assertTrue(validate_revision_request(revision_request(error={})))
        self.assertTrue(validate_revision_request(revision_request(error={"loss": float("inf")})))


class RevisionRoutingTests(unittest.TestCase):
    def test_classifies_each_error_family(self) -> None:
        cases = {
            "insufficient paper evidence for this scenario": ErrorCategory.ANALYSIS_SCOPE,
            "schema validation failed: missing required field": ErrorCategory.CONTRACT_ERROR,
            "script execution failed with SyntaxError": ErrorCategory.CODE_OR_RUNTIME,
            "CUDA driver unavailable in current environment": ErrorCategory.ENVIRONMENT,
            "ModuleNotFoundError: No module named 'numpy'": ErrorCategory.ENVIRONMENT,
        }
        for message, expected in cases.items():
            with self.subTest(message=message):
                self.assertEqual(classify_revision_error(message), expected)

    def test_explicit_category_wins_and_unknown_category_fails(self) -> None:
        parsed = parse_revision_request(
            revision_request(error="CUDA unavailable", category="analysis_scope")
        )
        self.assertEqual(classify_revision_error(parsed), ErrorCategory.ANALYSIS_SCOPE)
        self.assertEqual(
            classify_revision_error("failure", explicit_category=ErrorCategory.ENVIRONMENT),
            ErrorCategory.ENVIRONMENT,
        )
        with self.assertRaises(ValueError):
            classify_revision_error("failure", explicit_category="not-a-category")

    def test_reentry_limit_is_exclusive_at_boundary(self) -> None:
        self.assertTrue(can_reenter(0, max_reentries=2))
        self.assertTrue(can_reenter(revision_request(reentry_count=1), max_reentries=2))
        self.assertFalse(can_reenter(2, max_reentries=2))
        with self.assertRaises(ValueError):
            can_reenter(-1, max_reentries=2)
        with self.assertRaises(TypeError):
            can_reenter(True, max_reentries=2)
        self.assertTrue(can_reenter({}, max_reentries=2))


if __name__ == "__main__":
    unittest.main()
