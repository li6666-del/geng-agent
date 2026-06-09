import base64
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from geng_agent.result_review import collect_result_review_inputs


PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="


class PerTaskEvidenceTests(unittest.TestCase):
    def test_round4_evidence_includes_per_task_subdir_outputs(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            task_dir = root / "outputs" / "reproduce_fig_7"
            task_dir.mkdir(parents=True)
            (task_dir / "results.csv").write_text("power_dbm,sum_rate\n20,3.5\n40,7.1\n", encoding="utf-8")
            (task_dir / "sum_rate.png").write_bytes(base64.b64decode(PNG_B64))
            (task_dir / "summary.json").write_text(
                '{"task_id":"reproduce_fig_7","metrics":{},"assumptions":[]}', encoding="utf-8"
            )
            paper_path = root / "paper.md"  # non-PDF -> no page rendering
            paper_path.write_text("x", encoding="utf-8")

            evidence, images = collect_result_review_inputs(
                paper_path=paper_path,
                paper={},
                facts={},
                tasks={"repro_tasks": [{"task_id": "reproduce_fig_7"}]},
                repro_project_dir=root,
            )

            # The actual per-task artifacts are now surfaced, with task-relative names, so the
            # reviewer can SEE a passing task's real outputs (no more spurious cannot_assess).
            csv_files = [item["file"] for item in evidence["csv_summaries"]]
            self.assertIn("reproduce_fig_7/results.csv", csv_files)
            summary_files = [item["file"] for item in evidence["summary_jsons"]]
            self.assertIn("reproduce_fig_7/summary.json", summary_files)
            image_files = [item["file"] for item in evidence["output_images"]]
            self.assertIn("reproduce_fig_7/sum_rate.png", image_files)
            # the real CSV content (header) made it into the evidence
            self.assertTrue(any("sum_rate" in (item.get("header") or []) for item in evidence["csv_summaries"]))


if __name__ == "__main__":
    unittest.main()
