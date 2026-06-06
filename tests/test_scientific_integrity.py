import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from geng_agent.outputs import write_file_manifest
from geng_agent.runner import run_repro_once
from geng_agent.scientific_integrity import (
    check_outputs_scientific_integrity,
    load_task_expectations,
    scientific_integrity_issues,
)
from tests.test_runner import PNG_B64, base_repro_files


def decreasing_expectation():
    return [
        {
            "output_columns": ["snr_db", "bit_error_rate"],
            "expected_trend": {"x_axis": "snr_db", "y_axis": "bit_error_rate", "direction": "decreasing"},
        }
    ]


def write_csv(outputs_dir: Path, text: str) -> Path:
    outputs_dir.mkdir(parents=True, exist_ok=True)
    path = outputs_dir / "results.csv"
    path.write_text(text, encoding="utf-8")
    return path


def issue_types(issues):
    return {issue["issue"] for issue in issues}


class CheckRulesTests(unittest.TestCase):
    def test_flat_curve_is_degenerate(self) -> None:
        with TemporaryDirectory() as d:
            outputs = Path(d) / "outputs"
            write_csv(outputs, "snr_db,bit_error_rate\n0,0.1\n2,0.1\n4,0.1\n")
            issues = check_outputs_scientific_integrity(outputs, decreasing_expectation())
            self.assertIn("degenerate/flat output column", issue_types(issues))

    def test_all_zero_curve_is_degenerate(self) -> None:
        with TemporaryDirectory() as d:
            outputs = Path(d) / "outputs"
            write_csv(outputs, "snr_db,bit_error_rate\n0,0\n2,0\n4,0\n")
            issues = check_outputs_scientific_integrity(outputs, decreasing_expectation())
            self.assertIn("degenerate/flat output column", issue_types(issues))

    def test_good_decreasing_curve_passes(self) -> None:
        with TemporaryDirectory() as d:
            outputs = Path(d) / "outputs"
            write_csv(outputs, "snr_db,bit_error_rate\n0,0.1\n2,0.05\n4,0.01\n")
            self.assertEqual(check_outputs_scientific_integrity(outputs, decreasing_expectation()), [])

    def test_increasing_data_contradicts_decreasing_expectation(self) -> None:
        with TemporaryDirectory() as d:
            outputs = Path(d) / "outputs"
            write_csv(outputs, "snr_db,bit_error_rate\n0,0.01\n2,0.05\n4,0.1\n")
            issues = check_outputs_scientific_integrity(outputs, decreasing_expectation())
            self.assertIn("trend contradicts expected direction", issue_types(issues))

    def test_text_only_output_is_flagged_as_no_numeric(self) -> None:
        with TemporaryDirectory() as d:
            outputs = Path(d) / "outputs"
            write_csv(outputs, "label,note\nfoo,bar\nbaz,qux\nspam,eggs\n")
            issues = check_outputs_scientific_integrity(outputs, decreasing_expectation())
            self.assertIn("no numeric output column produced", issue_types(issues))

    def test_differently_named_numeric_columns_not_flagged(self) -> None:
        with TemporaryDirectory() as d:
            outputs = Path(d) / "outputs"
            # Numeric data present but column names differ from the task's output_columns;
            # the relaxed gate must NOT flag this (valid different-but-equivalent layout).
            write_csv(outputs, "alpha,beta\n1,9\n3,4\n5,6\n")
            self.assertEqual(check_outputs_scientific_integrity(outputs, decreasing_expectation()), [])

    def test_only_x_like_column_is_skipped(self) -> None:
        with TemporaryDirectory() as d:
            outputs = Path(d) / "outputs"
            # Only an SNR-like column exists -> no identifiable metric -> skip, no issue.
            write_csv(outputs, "snr_db\n0\n2\n4\n")
            self.assertEqual(check_outputs_scientific_integrity(outputs, decreasing_expectation()), [])

    def test_few_rows_are_skipped(self) -> None:
        with TemporaryDirectory() as d:
            outputs = Path(d) / "outputs"
            write_csv(outputs, "snr_db,bit_error_rate\n0,0.1\n2,0.1\n")  # 2 data rows, flat
            self.assertEqual(check_outputs_scientific_integrity(outputs, decreasing_expectation()), [])

    def test_no_expectations_is_skipped(self) -> None:
        with TemporaryDirectory() as d:
            outputs = Path(d) / "outputs"
            write_csv(outputs, "snr_db,bit_error_rate\n0,0.1\n2,0.1\n4,0.1\n")
            self.assertEqual(check_outputs_scientific_integrity(outputs, []), [])

    def test_flat_direction_is_not_judged(self) -> None:
        with TemporaryDirectory() as d:
            outputs = Path(d) / "outputs"
            write_csv(outputs, "snr_db,bit_error_rate\n0,0.1\n2,0.1\n4,0.1\n")
            expectation = [{"output_columns": ["snr_db", "bit_error_rate"], "expected_trend": {"x_axis": "snr_db", "y_axis": "bit_error_rate", "direction": "flat"}}]
            self.assertEqual(check_outputs_scientific_integrity(outputs, expectation), [])

    def test_unordered_rows_are_sorted_by_x_before_trend(self) -> None:
        with TemporaryDirectory() as d:
            outputs = Path(d) / "outputs"
            # Rows out of x-order but genuinely decreasing once sorted -> no contradiction.
            write_csv(outputs, "snr_db,bit_error_rate\n4,0.01\n0,0.1\n2,0.05\n")
            self.assertEqual(check_outputs_scientific_integrity(outputs, decreasing_expectation()), [])

    def test_noisy_near_half_is_near_constant(self) -> None:
        with TemporaryDirectory() as d:
            outputs = Path(d) / "outputs"
            # BER pinned at ~0.5 with small noise: not exactly constant and not monotonic,
            # so degeneracy and strict-trend checks miss it; near-constant must catch it.
            write_csv(outputs, "snr_db,bit_error_rate\n0,0.4968\n5,0.5010\n10,0.4999\n15,0.4978\n20,0.5026\n25,0.4990\n")
            issues = check_outputs_scientific_integrity(outputs, decreasing_expectation())
            self.assertIn("near-constant / low-variance output column", issue_types(issues))

    def test_shallow_but_real_decrease_not_flagged(self) -> None:
        with TemporaryDirectory() as d:
            outputs = Path(d) / "outputs"
            # Low variance, but it does move in the expected (decreasing) direction -> keep.
            write_csv(outputs, "snr_db,bit_error_rate\n0,0.49\n5,0.48\n10,0.47\n")
            self.assertEqual(check_outputs_scientific_integrity(outputs, decreasing_expectation()), [])

    def test_long_format_with_mismatched_task_columns_not_flagged(self) -> None:
        # Reproduces the supervised-run stall: code emits a valid long-format BER table
        # whose column names do not overlap the task's declared (wide-format) columns.
        with TemporaryDirectory() as d:
            outputs = Path(d) / "outputs"
            write_csv(
                outputs,
                "snr_db,modulation,channel,bit_error_rate,bit_errors,num_bits\n"
                "0,BPSK,AWGN,0.2286,2286,10000\n"
                "5,BPSK,AWGN,0.0455,455,10000\n"
                "10,BPSK,AWGN,0.0008,8,10000\n",
            )
            exp = [{"output_columns": ["eb_n0_db", "ber_awgn_simulated", "ber_rayleigh_simulated"],
                    "expected_trend": {"x_axis": "eb_n0_db", "y_axis": "ber_awgn_simulated", "direction": "decreasing"}}]
            self.assertEqual(check_outputs_scientific_integrity(outputs, exp), [])

    def test_near_constant_caught_when_y_axis_name_differs(self) -> None:
        # y_axis declared as bit_error_rate but the CSV calls it ber; a single unambiguous
        # metric column means the near-constant rule must still fire.
        with TemporaryDirectory() as d:
            outputs = Path(d) / "outputs"
            write_csv(outputs, "snr_db,ber\n0,0.4968\n5,0.5010\n10,0.4999\n15,0.4978\n20,0.5026\n25,0.4990\n")
            exp = [{"output_columns": ["snr_db", "bit_error_rate"],
                    "expected_trend": {"x_axis": "snr_db", "y_axis": "bit_error_rate", "direction": "decreasing"}}]
            self.assertIn("near-constant / low-variance output column", issue_types(check_outputs_scientific_integrity(outputs, exp)))

    def test_negative_probability_value_is_flagged(self) -> None:
        # A BER column with a physically impossible negative value (the real -0.00187 case)
        # must be flagged even though the run produced valid, decreasing-looking data.
        with TemporaryDirectory() as d:
            outputs = Path(d) / "outputs"
            write_csv(outputs, "snr_db,ber\n0,0.1\n2,0.05\n4,-0.00187\n")
            issues = check_outputs_scientific_integrity(outputs, decreasing_expectation())
            self.assertIn("negative value for probability/rate metric", issue_types(issues))

    def test_probability_within_unit_range_not_flagged_for_range(self) -> None:
        # A BER column wholly within [0, 1] must not trip either range rule.
        with TemporaryDirectory() as d:
            outputs = Path(d) / "outputs"
            write_csv(outputs, "snr_db,ber\n0,0.1\n2,0.05\n4,0.0\n")
            issues = issue_types(check_outputs_scientific_integrity(outputs, decreasing_expectation()))
            self.assertNotIn("negative value for probability/rate metric", issues)
            self.assertNotIn("probability/rate metric exceeds 1", issues)

    def test_throughput_above_one_not_flagged(self) -> None:
        # throughput is not a probability token and legitimately exceeds 1; the range
        # check must leave it alone. A flat expectation avoids incidental trend judging.
        with TemporaryDirectory() as d:
            outputs = Path(d) / "outputs"
            write_csv(outputs, "snr_db,throughput\n0,1.5\n2,2.7\n4,3.9\n")
            exp = [{"output_columns": ["snr_db", "throughput"],
                    "expected_trend": {"x_axis": "snr_db", "y_axis": "throughput", "direction": "flat"}}]
            issues = issue_types(check_outputs_scientific_integrity(outputs, exp))
            self.assertNotIn("negative value for probability/rate metric", issues)
            self.assertNotIn("probability/rate metric exceeds 1", issues)

    def test_probability_above_one_is_flagged(self) -> None:
        # A BER value of 1.2 is impossible for a probability/rate metric -> ">1" rule fires.
        with TemporaryDirectory() as d:
            outputs = Path(d) / "outputs"
            write_csv(outputs, "snr_db,ber\n0,0.1\n2,0.5\n4,1.2\n")
            issues = check_outputs_scientific_integrity(outputs, decreasing_expectation())
            self.assertIn("probability/rate metric exceeds 1", issue_types(issues))

    def test_count_columns_sharing_a_token_not_range_checked(self) -> None:
        # bit_errors / num_bits are raw counts (>> 1) that share substrings with the
        # probability tokens; the range check must skip them while still vetting ber.
        with TemporaryDirectory() as d:
            outputs = Path(d) / "outputs"
            write_csv(
                outputs,
                "snr_db,ber,bit_errors,num_bits\n"
                "0,0.2286,2286,10000\n"
                "5,0.0455,455,10000\n"
                "10,0.0008,8,10000\n",
            )
            issues = issue_types(check_outputs_scientific_integrity(outputs, decreasing_expectation()))
            self.assertNotIn("probability/rate metric exceeds 1", issues)
            self.assertNotIn("negative value for probability/rate metric", issues)


class LoadExpectationsTests(unittest.TestCase):
    def test_missing_tasks_file_returns_empty(self) -> None:
        with TemporaryDirectory() as d:
            self.assertEqual(load_task_expectations(Path(d) / "repro_project"), [])

    def test_reads_columns_and_trend(self) -> None:
        with TemporaryDirectory() as d:
            case = Path(d)
            (case / "repro_tasks.json").write_text(
                json.dumps(
                    {
                        "repro_tasks": [
                            {"task_id": "t1", "output_columns": ["snr_db", "ber"], "expected_trend": {"direction": "decreasing"}}
                        ]
                    }
                ),
                encoding="utf-8",
            )
            expectations = load_task_expectations(case / "repro_project")
            self.assertEqual(len(expectations), 1)
            self.assertEqual(expectations[0]["output_columns"], ["snr_db", "ber"])


def experiment_writing(csv_body: str) -> str:
    return (
        "import base64\n"
        f"PNG_B64 = '{PNG_B64}'\n"
        "from pathlib import Path\n"
        "Path('outputs').mkdir(exist_ok=True)\n"
        f"Path('outputs/results.csv').write_text({csv_body!r}, encoding='utf-8')\n"
        "Path('outputs/plot.png').write_bytes(base64.b64decode(PNG_B64))\n"
        "Path('outputs/summary.json').write_text('{\"task_id\":\"t1\",\"metrics\":{},\"assumptions\":[]}', encoding='utf-8')\n"
    )


def write_tasks(case_dir: Path) -> None:
    (case_dir / "repro_tasks.json").write_text(
        json.dumps(
            {
                "repro_tasks": [
                    {
                        "task_id": "t1",
                        "output_columns": ["snr_db", "bit_error_rate"],
                        "expected_trend": {"x_axis": "snr_db", "y_axis": "bit_error_rate", "direction": "decreasing"},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


class RunReproOnceIntegrationTests(unittest.TestCase):
    def _build(self, case_dir: Path, csv_body: str) -> Path:
        repro = case_dir / "repro_project"
        files = base_repro_files(experiment_writing(csv_body))
        write_file_manifest({"files": [{"path": p, "content": c} for p, c in files]}, repro)
        write_tasks(case_dir)
        return repro

    def test_gutted_flat_run_is_rejected(self) -> None:
        with TemporaryDirectory() as temp_dir:
            repro = self._build(Path(temp_dir), "snr_db,bit_error_rate\n0,0.1\n2,0.1\n4,0.1\n")
            result = run_repro_once(repro, timeout_seconds=20)
            self.assertFalse(result["passed"])
            self.assertTrue(result["scientific_issues"])

    def test_genuine_decreasing_run_passes(self) -> None:
        with TemporaryDirectory() as temp_dir:
            repro = self._build(Path(temp_dir), "snr_db,bit_error_rate\n0,0.1\n2,0.05\n4,0.01\n")
            result = run_repro_once(repro, timeout_seconds=20)
            self.assertTrue(result["passed"])
            self.assertEqual(result["scientific_issues"], [])

    def test_noisy_half_run_is_rejected(self) -> None:
        # The exact failure shape observed in the live run (BER ~0.5 = random guessing)
        # must now be rejected by the runtime gate via the near-constant rule.
        with TemporaryDirectory() as temp_dir:
            repro = self._build(Path(temp_dir), "snr_db,bit_error_rate\n0,0.4968\n5,0.5010\n10,0.4999\n15,0.4978\n20,0.5026\n25,0.4990\n")
            result = run_repro_once(repro, timeout_seconds=20)
            self.assertFalse(result["passed"])
            self.assertTrue(any("near-constant" in issue["issue"] for issue in result["scientific_issues"]))


if __name__ == "__main__":
    unittest.main()
