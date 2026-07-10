import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from geng_agent.pipeline import ReviewPipeline


class _RevisionPipeline(ReviewPipeline):
    def _load_or_create_ensemble_stage_json(self, **kwargs):
        if kwargs["schema_stage"] == "engineering_facts":
            return {
                "engineering_facts": [
                    {"type": "simulation_parameter", "name": "new_parameter", "value": {"n": 64}}
                ],
                "missing_information": [],
            }
        return {"repro_tasks": [{"task_id": "task_a", "target": "revised target"}]}


class RevisionReentryTests(unittest.TestCase):
    def test_analysis_scope_revises_facts_before_replacing_affected_task(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            facts = {"engineering_facts": [], "missing_information": []}
            tasks = {"repro_tasks": [{"task_id": "task_a", "target": "old target"}]}
            revised_facts, revised_tasks, fact_delta, task_delta = _RevisionPipeline()._revise_analysis_from_requests(
                tasks=tasks,
                facts=facts,
                requests=[
                    {
                        "task_id": "task_a",
                        "category": "analysis_scope",
                        "scenario": "missing parameter",
                        "error": "missing paper evidence",
                        "requested_changes": ["recover parameter"],
                    }
                ],
                paper_context="{}",
                paper_images=[],
                output_dir=root,
                audit_dir=root / "audit",
                revision_round=1,
                max_attempts=1,
                tasks_timeout=1,
                analysis_backend="codex",
                codex_analysis_timeout=1,
                analysis_agent_width=2,
                valid_chunk_ids=set(),
                valid_pages=set(),
            )

            self.assertEqual(fact_delta, 1)
            self.assertEqual(task_delta, 1)
            self.assertEqual(revised_facts["engineering_facts"][0]["name"], "new_parameter")
            self.assertEqual(revised_tasks["repro_tasks"][0]["target"], "revised target")


if __name__ == "__main__":
    unittest.main()
