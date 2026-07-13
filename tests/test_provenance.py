import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from geng_agent.provenance import build_automation_provenance


class AutomationProvenanceTests(unittest.TestCase):
    def test_links_memory_analysis_and_writer_execution_evidence(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paper = root / "paper.pdf"
            paper.write_bytes(b"paper")
            (root / "paper_memory.json").write_text(json.dumps({"memory_hash": "m1"}), encoding="utf-8")
            runtime = {
                "per_task": [
                    {
                        "task_id": "fig_1",
                        "task_writer_status": "matched",
                        "passed": True,
                        "execution_summary": {"full_run_count": 2, "last_returncode": 0},
                    }
                ]
            }
            result = build_automation_provenance(
                output_dir=root,
                paper_path=paper,
                memory_manifest={"snapshot_hash": "snapshot"},
                facts={
                    "engineering_facts": [{"name": "x"}],
                    "_meta": {"task_driven_backfill": {"request_count": 1}},
                },
                tasks={"repro_tasks": [{"task_id": "fig_1"}]},
                experiment_index={"experiments": [{"experiment_id": "e1"}]},
                runtime_result=runtime,
                agentic_status={"mode": "task_writers", "analysis_revision_history": []},
                settings={"analysis_backend": "codex"},
            )

            self.assertEqual(result["memory_snapshot_hash"], "snapshot")
            self.assertEqual(result["analysis"]["facts_count"], 1)
            self.assertEqual(result["task_writers"]["tasks"][0]["execution_summary"]["full_run_count"], 2)
            self.assertIn("paper_memory.json", result["artifacts"])


if __name__ == "__main__":
    unittest.main()
