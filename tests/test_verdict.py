from __future__ import annotations

import unittest

from geng_agent.verdict import derive_reproducibility_verdict


def _terminal_verification(*outcomes: str) -> dict:
    return {
        "schema_version": "2.0",
        "all_terminal": True,
        "all_successful": all(
            outcome in {"reproduced", "reproduced_with_assumptions"}
            for outcome in outcomes
        ),
        "tasks": [
            {"task_id": f"task_{index}", "host_action": "complete", "outcome": outcome}
            for index, outcome in enumerate(outcomes, start=1)
        ],
    }


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

    def test_engineering_risk_does_not_downgrade_exact_scientific_match(self) -> None:
        verdict = derive_reproducibility_verdict(
            risk_report={
                "risk_level": "high",
                "engineering_risk_level": "high",
                "scientific_risk_level": "low",
            },
            runtime_result={"enabled": True, "passed": True},
            result_review={
                "overall_alignment": "match",
                "overall_result_credibility": "high",
            },
        )

        self.assertEqual(verdict["verdict"], "fully_reproduced")

    def test_engineering_risk_does_not_downgrade_terminal_reproduction(self) -> None:
        verdict = derive_reproducibility_verdict(
            risk_report={
                "risk_level": "high",
                "engineering_risk_level": "high",
                "scientific_risk_level": "low",
                "verification_result": _terminal_verification("reproduced"),
            },
        )

        self.assertEqual(verdict["verdict"], "fully_reproduced")

    def test_legacy_engineering_risk_alone_does_not_downgrade_exact_match(self) -> None:
        verdict = derive_reproducibility_verdict(
            risk_report={"risk_level": "high"},
            runtime_result={"enabled": True, "passed": True},
            result_review={"overall_alignment": "match", "overall_result_credibility": "high"},
        )
        self.assertEqual(verdict["verdict"], "fully_reproduced")

    def test_terminal_inconclusive_cannot_be_promoted_to_partial_reproduction(self) -> None:
        verdict = derive_reproducibility_verdict(
            risk_report={
                "risk_level": "medium",
                "verification_result": _terminal_verification(
                    "inconclusive_missing_information"
                ),
            },
            runtime_result={"enabled": True, "passed": True},
            result_review={"overall_alignment": "mixed", "overall_result_credibility": "medium"},
        )

        self.assertEqual(verdict["verdict"], "inconclusive")

    def test_terminal_not_reproduced_cannot_receive_positive_label(self) -> None:
        verdict = derive_reproducibility_verdict(
            risk_report={
                "risk_level": "high",
                "verification_result": _terminal_verification("not_reproduced"),
            },
            runtime_result={"enabled": True, "passed": True},
            result_review={"overall_alignment": "mixed", "overall_result_credibility": "medium"},
        )

        self.assertEqual(verdict["verdict"], "failed_to_reproduce")

    def test_terminal_mixed_success_is_partial_reproduction(self) -> None:
        verdict = derive_reproducibility_verdict(
            risk_report={
                "risk_level": "medium",
                "verification_result": _terminal_verification(
                    "reproduced", "inconclusive_missing_information"
                ),
            },
            runtime_result={"enabled": True, "passed": True},
            result_review={"overall_alignment": "mixed", "overall_result_credibility": "medium"},
        )

        self.assertEqual(verdict["verdict"], "partially_reproduced")

    def test_all_success_with_assumptions_is_mostly_reproduced(self) -> None:
        verdict = derive_reproducibility_verdict(
            risk_report={
                "risk_level": "low",
                "verification_result": _terminal_verification("reproduced_with_assumptions"),
            }
        )

        self.assertEqual(verdict["verdict"], "mostly_reproduced")

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
