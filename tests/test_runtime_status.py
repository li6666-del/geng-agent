import hashlib
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from geng_agent.runtime_status import (
    _assess_partial_success,
    _load_valid_stage_cache,
    _paper_cache_matches,
    build_stage_cache_metadata,
)


class RuntimeStatusCacheTests(unittest.TestCase):
    def test_paper_cache_requires_and_matches_pdf_content_hash(self) -> None:
        with TemporaryDirectory() as temp:
            paper = Path(temp) / "paper.pdf"
            paper.write_bytes(b"first paper payload")
            cached = {
                "source_path": str(paper),
                "source_sha256": hashlib.sha256(paper.read_bytes()).hexdigest(),
                "chunks": [{"chunk_id": "c1", "text": "paper"}],
            }

            self.assertTrue(_paper_cache_matches(cached, paper))
            moved = Path(temp) / "moved-paper.pdf"
            moved.write_bytes(paper.read_bytes())
            self.assertTrue(_paper_cache_matches(cached, moved))

            self.assertFalse(_paper_cache_matches({key: value for key, value in cached.items() if key != "source_sha256"}, paper))

            paper.write_bytes(b"replacement payload")
            self.assertFalse(_paper_cache_matches(cached, paper))

    def test_stage_cache_identity_ignores_prompt_wording_but_tracks_policy_and_inputs(self) -> None:
        base = build_stage_cache_metadata(
            stage_label="tasks",
            schema_stage="repro_tasks",
            prompt="prompt one",
            policy_version="policy-v1",
            inputs={"paper_sha256": "a" * 64, "task_contract": {"claim_id": "c1"}},
        )
        prompt_changed = build_stage_cache_metadata(
            stage_label="tasks",
            schema_stage="repro_tasks",
            prompt="prompt two",
            policy_version="policy-v1",
            inputs={"paper_sha256": "a" * 64, "task_contract": {"claim_id": "c1"}},
        )
        policy_changed = build_stage_cache_metadata(
            stage_label="tasks",
            schema_stage="repro_tasks",
            prompt="prompt one",
            policy_version="policy-v2",
            inputs={"paper_sha256": "a" * 64, "task_contract": {"claim_id": "c1"}},
        )
        inputs_changed = build_stage_cache_metadata(
            stage_label="tasks",
            schema_stage="repro_tasks",
            prompt="prompt one",
            policy_version="policy-v1",
            inputs={"paper_sha256": "b" * 64, "task_contract": {"claim_id": "c1"}},
        )

        self.assertEqual(base["format_version"], "semantic_inputs_v2")
        self.assertEqual(len(base["schema_sha256"]), 64)
        self.assertEqual(base["fingerprint"], prompt_changed["fingerprint"])
        self.assertNotEqual(base["fingerprint"], policy_changed["fingerprint"])
        self.assertNotEqual(base["fingerprint"], inputs_changed["fingerprint"])

    def test_resume_mismatch_is_cache_invalidation_not_a_stage_gate(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            cache = root / "repro_tasks.json"
            audit = root / "audit"
            audit.mkdir()
            cache.write_text(
                json.dumps({"_meta": {"cache": {"fingerprint": "old"}}}),
                encoding="utf-8",
            )

            result = _load_valid_stage_cache(
                path=cache,
                audit_dir=audit,
                stage_label="tasks",
                schema_stage="repro_tasks",
                expected_cache_metadata={"fingerprint": "new"},
            )

            self.assertIsNone(result)
            diagnostic = json.loads((audit / "resume_invalid_tasks.json").read_text(encoding="utf-8"))
            self.assertIn("scientific inputs or policy changed", diagnostic["errors"][0]["message"])

    def test_partial_success_accepts_structured_summary_without_png_or_csv(self) -> None:
        result = _assess_partial_success(
            {"artifacts": {"csv_files": [], "png_files": [], "summary_json_files": ["summary.json"]}}
        )

        self.assertTrue(result["has_partial_output"])
        self.assertEqual(result["valid_summary_json_files"], ["summary.json"])


if __name__ == "__main__":
    unittest.main()
