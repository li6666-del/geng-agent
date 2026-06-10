import base64
import json
import unittest.mock as um
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from geng_agent.result_review import (
    build_unassessable_experiment_review,
    collect_result_review_inputs,
    normalize_experiment_review_candidate,
    run_result_review,
)
from geng_agent.schemas import validate_stage


PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="


def _valid_review(task_id: str, verdict: str = "supports_paper_claim") -> dict:
    return {
        "task_id": task_id,
        "local_result_credibility": "medium",
        "paper_alignment": "match",
        "scientific_verdict": verdict,
        "dimension_reviews": [
            {"dimension": d, "rating": "acceptable", "finding": "符合预期。", "evidence": ["results.csv"]}
            for d in (
                "artifact_coverage", "reproduction_logic", "trend_shape", "metric_axis_scale",
                "baseline_comparison", "statistical_reliability", "conclusion_support",
            )
        ],
        "paper_result_summary": "论文：曲线下降。",
        "local_result_summary": "本地：曲线下降。",
        "differences": [],
        "possible_causes": [],
        "evidence": ["results.csv"],
        "limitations": [],
        "confidence": "medium",
    }


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


class ReviewShapeCoercionTests(unittest.TestCase):
    def test_structured_differences_are_coerced_to_strings(self) -> None:
        # The live polar-codes run burned all 6 attempts because the model emitted
        # differences as OBJECTS. Coercion turns them into strings on attempt 1.
        review = _valid_review("t1")
        review["differences"] = [{"aspect": "排序", "detail": "本地反了"}, "纯文本差异", 3]
        review["dimension_reviews"][0]["evidence"] = [{"file": "results.csv"}]
        normalized = normalize_experiment_review_candidate(review, expected_task_id="t1")
        self.assertEqual(validate_stage("result_review_experiment", normalized), [])
        self.assertIn("排序", normalized["differences"][0])  # JSON-stringified, content kept
        self.assertEqual(normalized["differences"][1], "纯文本差异")
        self.assertEqual(normalized["differences"][2], "3")

    def test_unassessable_review_is_schema_valid(self) -> None:
        doc = build_unassessable_experiment_review(task_id="t9", reason="boom " * 100)
        self.assertEqual(validate_stage("result_review_experiment", doc), [])
        self.assertEqual(doc["scientific_verdict"], "cannot_assess")


class PartialReviewSurvivalTests(unittest.TestCase):
    def test_one_failing_experiment_degrades_not_sinks_round4(self) -> None:
        # Experiment 2's review raises (e.g. 6 invalid-JSON attempts) -> it becomes
        # cannot_assess while experiment 1 keeps its real verdict and the stage SUCCEEDS.
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            task_dir = root / "repro_project" / "outputs" / "t1"
            task_dir.mkdir(parents=True)
            (task_dir / "results.csv").write_text("x,y\n1,2\n", encoding="utf-8")
            (task_dir / "p.png").write_bytes(base64.b64decode(PNG_B64))
            paper_path = root / "paper.md"
            paper_path.write_text("x", encoding="utf-8")
            (root / "audit").mkdir()

            calls = {"n": 0}

            def fake_call(**kwargs):
                calls["n"] += 1
                if calls["n"] == 1:
                    return _valid_review("t1", "does_not_support_paper_claim"), {"task_id": "t1", "attempts": 1}
                raise RuntimeError("did not pass JSON validation after 6 attempts")

            with um.patch("geng_agent.result_review.call_experiment_result_review", side_effect=fake_call):
                status = run_result_review(
                    client=object(),
                    prompt_book=um.MagicMock(render=um.MagicMock(return_value="p")),
                    system_message="s",
                    paper_path=paper_path,
                    paper={},
                    facts={},
                    tasks={"repro_tasks": [{"task_id": "t1"}, {"task_id": "t2"}]},
                    paper_context_json="[]",
                    repro_project_dir=root / "repro_project",
                    output_dir=root,
                    audit_dir=root / "audit",
                    max_attempts=2,
                )

            self.assertTrue(status["passed"])  # the stage survived
            doc = json.loads((root / "result_review.json").read_text(encoding="utf-8"))
            verdicts = {r["task_id"]: r["scientific_verdict"] for r in doc["experiment_reviews"]}
            self.assertEqual(verdicts["t1"], "does_not_support_paper_claim")  # real verdict kept
            self.assertEqual(verdicts["t2"], "cannot_assess")                  # degraded, not fatal
            failed = [s for s in status["experiment_review_statuses"] if s.get("review_failed")]
            self.assertEqual(len(failed), 1)
            self.assertTrue((root / "audit" / "review_failed_02_t2.json").exists())


if __name__ == "__main__":
    unittest.main()
