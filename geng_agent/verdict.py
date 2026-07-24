from __future__ import annotations

from typing import Any


VERDICTS = {
    "fully_reproduced",
    "mostly_reproduced",
    "partially_reproduced",
    "inconclusive",
    "high_reproducibility_risk",
    "failed_to_reproduce",
}


def derive_reproducibility_verdict(
    risk_report: dict[str, Any] | None = None,
    runtime_result: dict[str, Any] | None = None,
    result_review: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Derive a compact final reproducibility verdict from pipeline artifacts."""
    risk_report = risk_report if isinstance(risk_report, dict) else {}
    runtime_result = runtime_result if isinstance(runtime_result, dict) else {}
    result_review = result_review if isinstance(result_review, dict) else {}

    reasons: list[str] = []
    risk_level = _risk_level(risk_report)
    scientific_verdict = _verdict_from_terminal_task_outcomes(
        risk_report=risk_report,
        result_review=result_review,
        risk_level=risk_level,
    )
    if scientific_verdict is not None:
        return scientific_verdict
    if runtime_result.get("passed") is False and not _has_partial_output(runtime_result):
        reasons.append("guarded runtime execution did not pass and produced no usable output")
        return _result(
            "failed_to_reproduce",
            "high",
            reasons,
            "Fix runtime, dependency, or security failures first, then rerun reproduction and result review.",
        )

    if runtime_result.get("passed") is False and _has_partial_output(runtime_result):
        # PARTIAL reproduction: some experiments produced valid output, others failed even
        # after repair. A single failed experiment must NOT negate the whole reproduction --
        # judge it per-experiment via the result review and report it as partial.
        reasons.append("partial reproduction: some experiments produced valid output, others did not complete")
        if not result_review:
            reasons.append("result_review is missing")
            return _result("inconclusive", "low", reasons, "Run the per-experiment result review on the partial outputs, then judge.")
        if result_review.get("passed") is False:
            reasons.append("result_review failed")
            return _result("inconclusive", "low", reasons, "Inspect result_review_error.json and rerun the per-experiment review.")
        alignment = _normalized_label(
            result_review.get("overall_alignment") or result_review.get("paper_alignment") or result_review.get("alignment")
        )
        credibility = _normalized_label(
            result_review.get("overall_result_credibility") or result_review.get("credibility") or result_review.get("local_result_credibility")
        )
        if alignment:
            reasons.append(f"overall_alignment={alignment}")
        if credibility:
            reasons.append(f"credibility={credibility}")
        if alignment in {"mismatch", "contradiction"}:
            return _result(
                "high_reproducibility_risk",
                "medium",
                reasons,
                "已完成的实验与论文结果不符；逐实验核对后再判断，不要报为复现。",
            )
        positive = alignment in {"match", "matched", "high", "exact", "close", "partial_match", "mostly_match"}
        return _result(
            "partially_reproduced",
            "medium" if positive else "low",
            reasons,
            "按部分复现处理：逐实验核对哪些已复现、哪些缺失或失败；尽量补齐失败的实验后重跑，再追求更完整的结论。",
        )

    if not result_review:
        reasons.append("result_review is missing")
        return _result(
            "inconclusive",
            "low",
            reasons,
            "Run result-level review before making a reproduction claim.",
        )

    if result_review.get("passed") is False:
        reasons.append("result_review failed")
        return _result(
            "inconclusive",
            "medium",
            reasons,
            "Inspect result_review_error.json and rerun the task-writer report assembly.",
        )

    alignment = _normalized_label(
        result_review.get("overall_alignment")
        or result_review.get("paper_alignment")
        or result_review.get("alignment")
    )
    credibility = _normalized_label(
        result_review.get("overall_result_credibility")
        or result_review.get("credibility")
        or result_review.get("local_result_credibility")
    )

    if alignment:
        reasons.append(f"overall_alignment={alignment}")
    if credibility:
        reasons.append(f"credibility={credibility}")
    if risk_level:
        reasons.append(f"risk_level={risk_level}")

    if alignment in {"mismatch", "contradiction"} or credibility == "low":
        return _result(
            "high_reproducibility_risk",
            "high" if alignment == "mismatch" and credibility == "low" else "medium",
            reasons,
            "Do not report this as reproduced; compare plots, metrics, baselines, and assumptions manually.",
        )

    if alignment in {"match", "matched", "high", "exact"} and credibility == "high":
        if risk_level in {"", "low"}:
            verdict = "fully_reproduced"
            confidence = "high"
        elif risk_level == "medium":
            verdict = "mostly_reproduced"
            confidence = "medium"
        else:
            verdict = "partially_reproduced"
            confidence = "medium"
        return _result(verdict, confidence, reasons, _positive_action(verdict))

    if alignment in {"close", "partial_match", "mostly_match", "match"} and credibility in {"high", "medium"}:
        verdict = "mostly_reproduced" if risk_level in {"", "low", "medium"} else "partially_reproduced"
        confidence = "medium" if risk_level != "high" else "low"
        return _result(verdict, confidence, reasons, _positive_action(verdict))

    if alignment in {"partial", "partial_match"} or credibility == "medium":
        return _result(
            "partially_reproduced",
            "medium" if risk_level != "high" else "low",
            reasons,
            "Treat the run as partial evidence; document gaps and rerun with full parameters where possible.",
        )

    reasons.append("result review did not contain enough positive alignment evidence")
    return _result(
        "inconclusive",
        "low",
        reasons,
        "Collect stronger result evidence and rerun result-level review.",
    )


def _verdict_from_terminal_task_outcomes(
    *,
    risk_report: dict[str, Any],
    result_review: dict[str, Any],
    risk_level: str,
) -> dict[str, Any] | None:
    """Make host-derived task outcomes authoritative when they are complete.

    This prevents a generic medium-credibility summary from turning a purely
    inconclusive or negative reproduction into a positive "partial" label.
    """

    verification: dict[str, Any] | None = None
    for container in (risk_report, result_review):
        candidate = container.get("verification_result")
        if isinstance(candidate, dict):
            verification = candidate
            break
    if not isinstance(verification, dict) or verification.get("all_terminal") is not True:
        return None
    raw_tasks = verification.get("tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        return None
    if any(
        not isinstance(item, dict) or item.get("host_action") != "complete"
        for item in raw_tasks
    ):
        return None

    known_outcomes = {
        "reproduced",
        "reproduced_with_assumptions",
        "inconclusive_missing_information",
        "not_reproduced",
        "execution_failed",
    }
    outcomes = [str(item.get("outcome") or "") for item in raw_tasks]
    if any(outcome not in known_outcomes for outcome in outcomes):
        return None
    counts = {outcome: outcomes.count(outcome) for outcome in sorted(known_outcomes)}
    reasons = [
        "host-derived terminal task outcomes: "
        + ", ".join(
            f"{outcome}={count}" for outcome, count in counts.items() if count
        )
    ]
    total = len(outcomes)
    success_count = counts["reproduced"] + counts["reproduced_with_assumptions"]

    if success_count == total:
        if counts["reproduced_with_assumptions"]:
            reasons.append("all core conclusions were supported with a material core assumption")
            return _result(
                "mostly_reproduced",
                "medium",
                reasons,
                "Report the supported conclusions together with the material core assumption.",
            )
        return _result(
            "fully_reproduced",
            "high",
            reasons,
            "Report the reproduced core conclusions and retain the task-level evidence.",
        )

    if success_count:
        reasons.append("at least one task reproduced its core conclusion and at least one did not")
        return _result(
            "partially_reproduced",
            "medium",
            reasons,
            "Report reproduction task by task; do not generalize successful tasks to unresolved or negative ones.",
        )

    inconclusive_count = counts["inconclusive_missing_information"]
    if inconclusive_count:
        reasons.append("no task produced positive reproduction evidence and decisive information remains missing")
        return _result(
            "inconclusive",
            "low",
            reasons,
            "Report the missing information and negative task outcomes without claiming reproduction.",
        )

    reasons.append("no task reproduced its assigned core conclusion")
    return _result(
        "failed_to_reproduce",
        "high" if counts["not_reproduced"] == total else "medium",
        reasons,
        "Report the negative result and execution failures; do not apply a positive reproduction label.",
    )


def _result(verdict: str, confidence: str, reasons: list[str], recommended_action: str) -> dict[str, Any]:
    if verdict not in VERDICTS:
        raise ValueError(f"unknown reproducibility verdict: {verdict}")
    return {
        "verdict": verdict,
        "confidence": confidence,
        "reasons": list(reasons),
        "recommended_action": recommended_action,
    }


def _has_partial_output(runtime_result: dict[str, Any]) -> bool:
    """True when a not-fully-passing run still produced usable per-experiment outputs (kept
    by task writers). Lets the verdict treat it as partial reproduction rather than total
    failure."""
    partial = runtime_result.get("partial_success")
    return isinstance(partial, dict) and bool(partial.get("has_partial_output"))


def _risk_level(risk_report: dict[str, Any]) -> str:
    # Engineering, dependency, packaging, and security risk remain reportable
    # but do not change the scientific reproduction label.
    level = _normalized_label(risk_report.get("scientific_risk_level"))
    if level in {"low", "medium", "high"}:
        return level
    dimensions = risk_report.get("risk_dimensions")
    if not isinstance(dimensions, dict):
        return ""
    alignment = dimensions.get("result_alignment")
    level = _normalized_label(alignment.get("level")) if isinstance(alignment, dict) else ""
    return level if level in {"low", "medium", "high"} else ""


def _normalized_label(value: Any) -> str:
    return str(value).strip().lower().replace("-", "_") if value is not None else ""


def _positive_action(verdict: str) -> str:
    if verdict == "fully_reproduced":
        return "Report as reproduced, while preserving links to runtime logs and result-review evidence."
    if verdict == "mostly_reproduced":
        return "Report as mostly reproduced and list the remaining risk dimensions or minor mismatches."
    return "Report as partial evidence only and identify the missing checks needed for a stronger claim."
