from __future__ import annotations

import csv
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from pydantic import ValidationError

from geng_agent.benchmark import BenchmarkError, evaluate_suite, load_suite, validate_suite
from geng_agent.benchmark_models import BenchmarkCase
from geng_agent.cli import build_parser


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _write_csv(path: Path, rows: list[tuple[float, float]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["snr_db", "ber"])
        writer.writerows(rows)


def _case(*, negative: bool = False, repeats: int = 1) -> dict:
    return {
        "schema_version": "1.0",
        "case_id": "case_1",
        "title": "Synthetic BER",
        "paper": "paper.pdf",
        "split": "regression",
        "difficulty": 1,
        "archetype": "ber_curve",
        "gold_status": "curated",
        "negative_case": negative,
        "repeat_runs": repeats,
        "gold_facts": [{"type": "channel_model", "name": "AWGN", "value": None, "required": True}],
        "gold_tasks": [] if negative else [{
            "task_id": "fig_1", "figure_or_claim": "Fig. 1", "metric": "bit_error_rate",
            "output_columns": ["snr_db", "ber"], "baselines": ["theory"],
            "expected_trend": "decreasing", "expected_artifacts": ["outputs/fig_1/results.csv"],
        }],
        "implementation_checks": [] if negative else [{
            "check_id": "formula", "path": "src/metrics.py", "contains": ["bit_errors / total_bits"], "absent": ["metric_value"], "weight": 1,
        }],
        "curve_checks": [] if negative else [{
            "check_id": "ber", "task_id": "fig_1", "actual_csv": "outputs/fig_1/results.csv",
            "reference_csv": "gold/reference.csv", "x_column": "snr_db", "y_columns": ["ber"],
            "scale": "log10", "nmae_tolerance": 0.15, "min_rank_correlation": 0.9, "weight": 1,
        }],
        "expected_missing_information": ["random seed"] if negative else [],
        "expected_verdicts": ["inconclusive"] if negative else [],
        "budgets": {"wall_clock_s": 100, "total_tokens": 1000},
        "notes": "",
    }


def _suite(root: Path, case: dict) -> tuple[Path, Path]:
    case_path = root / "cases" / "case.json"
    suite_path = root / "suite.json"
    _write_json(case_path, case)
    _write_json(suite_path, {
        "schema_version": "1.0", "suite_id": "synthetic", "title": "Synthetic",
        "domain": "communication", "cases": ["cases/case.json"], "description": "",
    })
    return suite_path, case_path.parent


def _positive_run(path: Path, rows: list[tuple[float, float]] | None = None, *, fallback: bool = False) -> None:
    rows = rows or [(0, 0.1), (2, 0.01), (4, 0.001)]
    _write_json(path / "engineering_facts.json", {
        "engineering_facts": [{"type": "channel_model", "name": "AWGN"}], "missing_information": [],
    })
    _write_json(path / "repro_tasks.json", {"repro_tasks": [{
        "task_id": "fig_1", "figure_or_claim": "Fig. 1", "metric": "bit_error_rate",
        "output_columns": ["snr_db", "ber"], "comparison": {"baselines": ["theory"]},
        "expected_trend": {"direction": "decreasing"},
    }]})
    _write_json(path / "runtime_result.json", {"passed": True, "template_fallback_used": fallback})
    _write_json(path / "code_review.json", {"passed": True})
    _write_json(path / "run_cost.json", {"wall_clock_s": 50, "totals": {"total_tokens": 500}})
    _write_json(path / "reproducibility_verdict.json", {"verdict": "fully_reproduced"})
    if fallback:
        _write_json(path / "risk_report.json", {"findings": [{"type": "template_fallback_used"}]})
    metrics = path / "repro_project" / "src" / "metrics.py"
    metrics.parent.mkdir(parents=True, exist_ok=True)
    metrics.write_text("ber = bit_errors / total_bits\n", encoding="utf-8")
    _write_csv(path / "repro_project" / "outputs" / "fig_1" / "results.csv", rows)


class BenchmarkContractTests(unittest.TestCase):
    def test_curated_case_requires_gold_signal(self) -> None:
        data = _case()
        for key in ("gold_facts", "gold_tasks", "implementation_checks", "curve_checks", "expected_missing_information", "expected_verdicts"):
            data[key] = []
        with self.assertRaises(ValidationError):
            BenchmarkCase.model_validate(data)

    def test_suite_rejects_manifest_path_escape(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_json(root / "suite.json", {"schema_version": "1.0", "suite_id": "x", "title": "x", "domain": "communication", "cases": ["../case.json"], "description": ""})
            _write_json(root.parent / "case.json", _case())
            with self.assertRaises(BenchmarkError):
                load_suite(root / "suite.json")

    def test_cli_exposes_validation_mode(self) -> None:
        args = build_parser().parse_args(["benchmark", "suite.json", "--validate-only"])
        self.assertEqual(args.command, "benchmark")
        self.assertTrue(args.validate_only)


class BenchmarkScoringTests(unittest.TestCase):
    def test_perfect_gold_run_scores_high(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            suite_path, case_dir = _suite(root / "suite", _case())
            _write_csv(case_dir / "gold" / "reference.csv", [(0, 0.1), (2, 0.01), (4, 0.001)])
            _positive_run(root / "runs" / "case_1" / "run_01")
            report = evaluate_suite(suite_path, root / "runs")
            self.assertGreaterEqual(report.score or 0, 90)
            self.assertEqual(report.cases[0].qualification, "high_reproduction")

    def test_wrong_curve_triggers_scientific_gate(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            suite_path, case_dir = _suite(root / "suite", _case())
            _write_csv(case_dir / "gold" / "reference.csv", [(0, 0.1), (2, 0.01), (4, 0.001)])
            _positive_run(root / "runs" / "case_1" / "run_01", [(0, 0.001), (2, 0.01), (4, 0.1)])
            case_score = evaluate_suite(suite_path, root / "runs").cases[0]
            self.assertEqual(case_score.qualification, "partial_reproduction")
            self.assertIn("scientific_result_fidelity_below_60", case_score.runs[0].gates)

    def test_template_fallback_cannot_claim_reproduction(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            suite_path, case_dir = _suite(root / "suite", _case())
            _write_csv(case_dir / "gold" / "reference.csv", [(0, 0.1), (2, 0.01), (4, 0.001)])
            _positive_run(root / "runs" / "case_1" / "run_01", fallback=True)
            case_score = evaluate_suite(suite_path, root / "runs").cases[0]
            self.assertEqual(case_score.qualification, "no_valid_reproduction")

    def test_negative_case_rewards_correct_limitation(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            suite_path, _ = _suite(root / "suite", _case(negative=True))
            run = root / "runs" / "case_1" / "run_01"
            _write_json(run / "engineering_facts.json", {
                "engineering_facts": [{"type": "channel_model", "name": "AWGN"}],
                "missing_information": [{"name": "random seed"}],
            })
            _write_json(run / "runtime_result.json", {"passed": False})
            _write_json(run / "reproducibility_verdict.json", {"verdict": "inconclusive"})
            case_score = evaluate_suite(suite_path, root / "runs").cases[0]
            self.assertEqual(case_score.qualification, "correctly_limited")

    def test_repeat_variance_reduces_stability(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            suite_path, case_dir = _suite(root / "suite", _case(repeats=3))
            _write_csv(case_dir / "gold" / "reference.csv", [(0, 0.1), (2, 0.01), (4, 0.001)])
            _positive_run(root / "runs" / "case_1" / "run_01")
            _positive_run(root / "runs" / "case_1" / "run_02")
            _positive_run(root / "runs" / "case_1" / "run_03", [(0, 0.001), (2, 0.01), (4, 0.1)])
            case_score = evaluate_suite(suite_path, root / "runs").cases[0]
            self.assertLess(case_score.stability_score or 100, 75)

    def test_pending_case_validates_but_is_not_scored(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = _case()
            data["gold_status"] = "pending"
            suite_path, _ = _suite(root / "suite", data)
            self.assertEqual(validate_suite(suite_path)["pending_cases"], ["case_1"])
            report = evaluate_suite(suite_path, root / "runs")
            self.assertIsNone(report.score)
            self.assertEqual(report.pending_cases, 1)


if __name__ == "__main__":
    unittest.main()
