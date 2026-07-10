from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from geng_agent.benchmark import (
    build_benchmark,
    render_benchmark_json,
    render_benchmark_markdown,
    write_benchmark_reports,
)
from geng_agent.outputs import write_json


class BenchmarkTests(unittest.TestCase):
    def test_multiple_cases_are_aggregated(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = root / "case-a"
            second = root / "case-b"
            first.mkdir()
            second.mkdir()
            self._write_case(
                first,
                facts=2,
                tasks=2,
                statuses=["matched", "explained_gap"],
                wall_clock_s=12.5,
                llm_calls=3,
                total_tokens=400,
                cost_usd=1.25,
            )
            self._write_case(
                second,
                facts=1,
                tasks=1,
                statuses=["failed"],
                wall_clock_s=4.0,
                llm_calls=1,
                total_tokens=100,
                cost_usd=0.25,
            )

            report = build_benchmark([first, second])

            self.assertEqual(report["case_count"], 2)
            self.assertEqual(report["totals"]["facts_count"], 3)
            self.assertEqual(report["totals"]["tasks_count"], 3)
            self.assertEqual(report["totals"]["runtime_coverage"], "2/3")
            self.assertEqual(report["totals"]["matched"], 1)
            self.assertEqual(report["totals"]["explained_gap"], 1)
            self.assertEqual(report["totals"]["failed"], 1)
            self.assertEqual(report["totals"]["wall_clock_s"], 16.5)
            self.assertEqual(report["totals"]["llm_calls"], 4)
            self.assertEqual(report["totals"]["total_tokens"], 500)
            self.assertEqual(report["totals"]["cost_usd"], 1.5)

            runtime_stage = next(item for item in report["stage_totals"] if item["stage"] == "runtime")
            self.assertEqual(runtime_stage, {"stage": "runtime", "ok": 1, "not_ok": 1, "total": 2})

            rendered_json = render_benchmark_json(report)
            self.assertEqual(json.loads(rendered_json), report)
            rendered_markdown = render_benchmark_markdown(report)
            self.assertIn("| case-a |", rendered_markdown)
            self.assertIn("| **Total** |", rendered_markdown)
            self.assertIn("| runtime | 1 | 1 | 2 |", rendered_markdown)

            json_path, markdown_path = write_benchmark_reports(
                report,
                json_path=root / "reports" / "benchmark.json",
                markdown_path=root / "reports" / "benchmark.md",
            )
            self.assertEqual(json.loads(json_path.read_text(encoding="utf-8")), report)
            self.assertEqual(markdown_path.read_text(encoding="utf-8"), rendered_markdown)

    def test_missing_files_degrade_without_failing_batch(self) -> None:
        with TemporaryDirectory() as temp_dir:
            case = Path(temp_dir) / "empty-case"
            case.mkdir()

            report = build_benchmark([case])
            summary = report["cases"][0]

            self.assertEqual(summary["facts_count"], 0)
            self.assertEqual(summary["tasks_count"], 0)
            self.assertIsNone(summary["runtime_coverage"])
            self.assertIsNone(summary["runtime_tasks_passed"])
            self.assertIsNone(summary["runtime_tasks_total"])
            self.assertEqual(summary["matched"], 0)
            self.assertEqual(summary["explained_gap"], 0)
            self.assertEqual(summary["failed"], 0)
            self.assertIsNone(summary["wall_clock_s"])
            self.assertIsNone(summary["llm_calls"])
            self.assertIsNone(summary["total_tokens"])
            self.assertIsNone(summary["cost_usd"])
            self.assertTrue(summary["stages"])
            self.assertTrue(all(item["reason"] == "missing" for item in summary["stages"]))
            self.assertIsNone(report["totals"]["runtime_coverage"])
            self.assertIsNone(report["totals"]["wall_clock_s"])

    @staticmethod
    def _write_case(
        case: Path,
        *,
        facts: int,
        tasks: int,
        statuses: list[str],
        wall_clock_s: float,
        llm_calls: int,
        total_tokens: int,
        cost_usd: float,
    ) -> None:
        write_json(case / "engineering_facts.json", {"engineering_facts": [{} for _ in range(facts)]})
        write_json(case / "repro_tasks.json", {"repro_tasks": [{} for _ in range(tasks)]})
        passed = sum(status in {"matched", "explained_gap"} for status in statuses)
        write_json(
            case / "runtime_result.json",
            {
                "enabled": True,
                "passed": passed == len(statuses),
                "tasks_passed": passed,
                "tasks_total": len(statuses),
                "coverage": f"{passed}/{len(statuses)}",
                "per_task": [
                    {
                        "task_id": f"task-{index}",
                        "passed": status in {"matched", "explained_gap"},
                        "task_writer_status": status,
                    }
                    for index, status in enumerate(statuses, start=1)
                ],
            },
        )
        write_json(
            case / "run_cost.json",
            {
                "wall_clock_s": wall_clock_s,
                "totals": {
                    "llm_calls": llm_calls,
                    "prompt_tokens": total_tokens - 50,
                    "completion_tokens": 50,
                    "total_tokens": total_tokens,
                    "cost_usd": cost_usd,
                },
            },
        )


if __name__ == "__main__":
    unittest.main()
