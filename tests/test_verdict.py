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
        self.assertIn("guarded runtime execution did not pass", verdict["reasons"])

    def test_template_fallback_caps_positive_verdict_at_partial(self) -> None:
        verdict = derive_reproducibility_verdict(
            risk_report={"risk_level": "low"},
            runtime_result={"enabled": True, "passed": True},
            result_review={"overall_alignment": "match", "overall_result_credibility": "high"},
            manifest={"_meta": {"template_fallback_used": True}},
        )

        self.assertEqual(verdict["verdict"], "partially_reproduced")
        self.assertEqual(verdict["confidence"], "medium")
        self.assertTrue(any("template_fallback_used=true" in reason for reason in verdict["reasons"]))

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
