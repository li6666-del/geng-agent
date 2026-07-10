from __future__ import annotations

import unittest

from geng_agent.verdict import derive_reproducibility_verdict


class ReproducibilityVerdictTests(unittest.TestCase):
    def test_runtime_failure_returns_failed_to_reproduce(self) -> None:
        verdict = derive_reproducibility_verdict(
            runtime_result={"enabled": True, "passed": False},
            result_review={"overall_alignment": "match", "overall_result_credibility": "high"},
        )

        self.assertEqual(verdict["verdict"], "failed_to_reproduce")
        self.assertEqual(verdict["confidence"], "high")
        self.assertTrue(any("did not pass" in reason for reason in verdict["reasons"]))

    def test_partial_output_yields_partially_reproduced(self) -> None:
        # One experiment failing must not negate the whole run: partial output + a passing
        # per-experiment review -> partially_reproduced, not failed_to_reproduce.
        verdict = derive_reproducibility_verdict(
            risk_report={"risk_level": "medium"},
            runtime_result={"enabled": True, "passed": False, "partial_success": {"has_partial_output": True}},
            result_review={"overall_alignment": "match", "overall_result_credibility": "high"},
        )
        self.assertEqual(verdict["verdict"], "partially_reproduced")
        self.assertTrue(any("partial reproduction" in reason for reason in verdict["reasons"]))

    def test_partial_output_with_mismatch_is_high_risk(self) -> None:
        verdict = derive_reproducibility_verdict(
            runtime_result={"enabled": True, "passed": False, "partial_success": {"has_partial_output": True}},
            result_review={"overall_alignment": "mismatch", "overall_result_credibility": "medium"},
        )
        self.assertEqual(verdict["verdict"], "high_reproducibility_risk")

    def test_partial_output_without_review_is_inconclusive(self) -> None:
        verdict = derive_reproducibility_verdict(
            runtime_result={"enabled": True, "passed": False, "partial_success": {"has_partial_output": True}},
            result_review=None,
        )
        self.assertEqual(verdict["verdict"], "inconclusive")

    def test_no_usable_output_is_failed_to_reproduce(self) -> None:
        # No partial output at all -> still a hard failure.
        verdict = derive_reproducibility_verdict(
            runtime_result={"enabled": True, "passed": False},
            result_review=None,
        )
        self.assertEqual(verdict["verdict"], "failed_to_reproduce")

    def test_mismatch_returns_high_reproducibility_risk(self) -> None:
        verdict = derive_reproducibility_verdict(
            risk_report={"risk_level": "medium"},
            runtime_result={"enabled": True, "passed": True},
            result_review={"overall_alignment": "mismatch", "overall_result_credibility": "medium"},
        )

        self.assertEqual(verdict["verdict"], "high_reproducibility_risk")
        self.assertIn("overall_alignment=mismatch", verdict["reasons"])

    def test_close_alignment_with_high_credibility_returns_mostly_or_fully(self) -> None:
        close = derive_reproducibility_verdict(
            risk_report={"risk_level": "medium"},
            runtime_result={"enabled": True, "passed": True},
            result_review={"overall_alignment": "close", "overall_result_credibility": "high"},
        )
        exact = derive_reproducibility_verdict(
            risk_report={"risk_level": "low"},
            runtime_result={"enabled": True, "passed": True},
            result_review={"overall_alignment": "match", "overall_result_credibility": "high"},
        )

        self.assertEqual(close["verdict"], "mostly_reproduced")
        self.assertEqual(exact["verdict"], "fully_reproduced")

    def test_missing_result_review_is_inconclusive(self) -> None:
        verdict = derive_reproducibility_verdict(
            risk_report={"risk_level": "low"},
            runtime_result={"enabled": True, "passed": True},
            result_review=None,
        )

        self.assertEqual(verdict["verdict"], "inconclusive")
        self.assertEqual(verdict["confidence"], "low")
        self.assertIn("result_review is missing", verdict["reasons"])


if __name__ == "__main__":
    unittest.main()
