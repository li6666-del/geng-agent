import unittest

from geng_agent.science_repair import (
    build_science_directive,
    collect_science_mismatches,
    diagnose_csv_symptoms,
    is_improvement,
    is_regression,
    review_score,
    run_science_repair,
)


def _rev(task_id: str, verdict: str) -> dict:
    return {
        "task_id": task_id,
        "scientific_verdict": verdict,
        "paper_result_summary": "论文：STAB 在上",
        "local_result_summary": "本地：ZF 在上",
        "differences": ["排序相反"],
        "possible_causes": ["空时维度没建出条件数优势"],
        "dimension_reviews": [
            {"dimension": "baseline_comparison", "rating": "weak", "finding": "曲线相对高低与论文相反", "evidence": ["x"]},
            {"dimension": "reproduction_logic", "rating": "weak", "finding": "等效信道构造可疑", "evidence": ["y"]},
        ],
    }


def _doc(*verdicts: str) -> dict:
    return {"experiment_reviews": [_rev(f"t{i}", v) for i, v in enumerate(verdicts)]}


def _rt(passed: int, total: int) -> dict:
    return {"coverage": f"{passed}/{total}"}


class PureHelperTests(unittest.TestCase):
    def test_collect_only_does_not_support(self) -> None:
        doc = _doc("supports_paper_claim", "does_not_support_paper_claim", "partially_supports_paper_claim")
        mismatches = collect_science_mismatches(doc)
        self.assertEqual([m["task_id"] for m in mismatches], ["t1"])
        self.assertEqual(mismatches[0]["baseline_finding"], "曲线相对高低与论文相反")

    def test_directive_carries_diagnosis_and_forbids_number_fudging(self) -> None:
        directive = build_science_directive(collect_science_mismatches(_doc("does_not_support_paper_claim")))
        self.assertIn("t0", directive)
        self.assertIn("排序相反", directive)
        self.assertIn("不要把数值硬凑", directive)
        self.assertIn("空时", directive)

    def test_score_and_gate(self) -> None:
        before = review_score(_doc("does_not_support_paper_claim", "supports_paper_claim"), _rt(2, 2))
        self.assertEqual(before, {"coverage_passed": 2, "coverage_total": 2, "mismatch_count": 1})
        fixed = {"coverage_passed": 2, "coverage_total": 2, "mismatch_count": 0}
        self.assertTrue(is_improvement(before, fixed))
        self.assertFalse(is_regression(before, fixed))
        lost_coverage = {"coverage_passed": 1, "coverage_total": 2, "mismatch_count": 0}
        self.assertTrue(is_regression(before, lost_coverage))     # fixed science but broke a task
        self.assertFalse(is_improvement(before, lost_coverage))


class SymptomDiagnosisTests(unittest.TestCase):
    def test_all_zero_column_flags_signal_zeroed(self) -> None:
        # the recurring all-zero bug (v7 fig_4: sum rates ~2e-11)
        tips = diagnose_csv_symptoms({"sum_rate": {"min": 0.0, "max": 2e-11}})
        joined = " ".join(tips)
        self.assertIn("整列≈0", joined)
        self.assertIn("SNR", joined)

    def test_one_curve_zero_others_vary_flags_that_method(self) -> None:
        # v7 fig_7: STAB+SDS identically 0 while ZF/power vary -> point at that method only
        tips = diagnose_csv_symptoms({
            "stab_sds": {"min": 0.0, "max": 0.0},
            "zf": {"min": 5.0, "max": 40.0},
            "power": {"min": 40.0, "max": 64.0},
        })
        self.assertIn("那几条对应的方法", " ".join(tips))

    def test_constant_column_flagged(self) -> None:
        self.assertIn("几乎是常数", " ".join(diagnose_csv_symptoms({"rate": {"min": 12.0, "max": 12.0}})))

    def test_blowup_magnitude_flagged(self) -> None:
        self.assertIn("量级", " ".join(diagnose_csv_symptoms({"x": {"min": -3e8, "max": 5e7}})))

    def test_healthy_columns_produce_no_false_alarm(self) -> None:
        tips = diagnose_csv_symptoms({
            "snr_db": {"min": 0.0, "max": 20.0},
            "ber": {"min": 1e-4, "max": 0.1},
        })
        self.assertEqual(tips, [])

    def test_directive_includes_symptom_leads(self) -> None:
        mismatches = collect_science_mismatches(_doc("does_not_support_paper_claim"))
        directive = build_science_directive(
            mismatches, {"t0": ["列 ['sum_rate'] 整列≈0 → 信号被清零：查 SNR/噪声方差归一化。"]}
        )
        self.assertIn("本地数值症状 → 排查方向", directive)
        self.assertIn("整列≈0", directive)


class _Harness:
    def __init__(self, evaluations: list[tuple[dict, dict]]) -> None:
        # evaluations: successive (runtime, review_doc) returned by evaluate()
        self._evaluations = evaluations
        self._index = 0
        self.regen_targets: list[list[str]] = []
        self.snapshots = 0
        self.restores = 0

    def regenerate(self, mismatches) -> None:
        self.regen_targets.append([m["task_id"] for m in mismatches])

    def evaluate(self):
        runtime, doc = self._evaluations[self._index]
        self._index += 1
        return runtime, {"passed": True, "round_index": self._index}, doc

    def snapshot(self) -> None:
        self.snapshots += 1

    def restore(self) -> None:
        self.restores += 1

    def run(self, *, initial_doc, initial_runtime, max_rounds):
        return run_science_repair(
            result_review_doc=initial_doc,
            runtime_result=initial_runtime,
            max_rounds=max_rounds,
            regenerate=self.regenerate,
            evaluate=self.evaluate,
            snapshot=self.snapshot,
            restore=self.restore,
        )


class OrchestrationTests(unittest.TestCase):
    def test_no_mismatch_is_a_noop(self) -> None:
        harness = _Harness([])
        result = harness.run(initial_doc=_doc("supports_paper_claim"), initial_runtime=_rt(1, 1), max_rounds=2)
        self.assertFalse(result["applied"])
        self.assertEqual(harness.regen_targets, [])
        self.assertEqual(harness.snapshots, 0)

    def test_single_round_fix_is_kept(self) -> None:
        harness = _Harness([(_rt(2, 2), _doc("supports_paper_claim", "supports_paper_claim"))])
        result = harness.run(
            initial_doc=_doc("does_not_support_paper_claim", "supports_paper_claim"),
            initial_runtime=_rt(2, 2),
            max_rounds=2,
        )
        self.assertTrue(result["kept"])
        self.assertEqual(harness.regen_targets, [["t0"]])
        self.assertEqual(harness.restores, 0)
        self.assertIsNotNone(result["result_review_result"])

    def test_regression_is_reverted(self) -> None:
        # the repair fixed the verdict but broke a task (coverage 2/2 -> 1/2) -> revert.
        harness = _Harness([(_rt(1, 2), _doc("supports_paper_claim", "supports_paper_claim"))])
        result = harness.run(
            initial_doc=_doc("does_not_support_paper_claim", "supports_paper_claim"),
            initial_runtime=_rt(2, 2),
            max_rounds=2,
        )
        self.assertFalse(result["kept"])
        self.assertEqual(harness.restores, 1)
        self.assertEqual(result["rounds"][-1]["decision"], "reverted")

    def test_no_improvement_is_reverted(self) -> None:
        harness = _Harness([(_rt(2, 2), _doc("does_not_support_paper_claim", "supports_paper_claim"))])
        result = harness.run(
            initial_doc=_doc("does_not_support_paper_claim", "supports_paper_claim"),
            initial_runtime=_rt(2, 2),
            max_rounds=2,
        )
        self.assertFalse(result["kept"])
        self.assertEqual(harness.restores, 1)
        self.assertEqual(result["rounds"][-1]["decision"], "reverted_no_improvement")

    def test_failed_rereview_empty_doc_is_not_scored_as_win(self) -> None:
        # An empty doc scores 0 mismatches; without the guard that would look like a fix.
        harness = _Harness([(_rt(2, 2), {})])
        result = harness.run(
            initial_doc=_doc("does_not_support_paper_claim"),
            initial_runtime=_rt(2, 2),
            max_rounds=2,
        )
        self.assertFalse(result["kept"])
        self.assertEqual(harness.restores, 1)

    def test_two_rounds_until_clean(self) -> None:
        harness = _Harness([
            (_rt(2, 2), _doc("supports_paper_claim", "does_not_support_paper_claim")),  # one fixed, one left
            (_rt(2, 2), _doc("supports_paper_claim", "supports_paper_claim")),          # second fixed
        ])
        result = harness.run(
            initial_doc=_doc("does_not_support_paper_claim", "does_not_support_paper_claim"),
            initial_runtime=_rt(2, 2),
            max_rounds=3,
        )
        self.assertTrue(result["kept"])
        self.assertEqual(len(result["rounds"]), 2)
        self.assertEqual([r["decision"] for r in result["rounds"]], ["kept", "kept"])
        # round 1 regenerates both mismatches, round 2 only the remaining one.
        self.assertEqual(harness.regen_targets, [["t0", "t1"], ["t1"]])


if __name__ == "__main__":
    unittest.main()
