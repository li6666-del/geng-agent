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
    manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Derive a compact final reproducibility verdict from pipeline artifacts."""
    risk_report = risk_report if isinstance(risk_report, dict) else {}
    runtime_result = runtime_result if isinstance(runtime_result, dict) else {}
    result_review = result_review if isinstance(result_review, dict) else {}
    manifest = manifest if isinstance(manifest, dict) else {}

    reasons: list[str] = []
    template_fallback_used = _template_fallback_used(risk_report, runtime_result, manifest)
    risk_level = _risk_level(risk_report)

    if runtime_result.get("passed") is False:
        reasons.append("guarded runtime execution did not pass")
        return _result(
            "failed_to_reproduce",
            "high",
            reasons,
            "Fix runtime, dependency, or security failures first, then rerun reproduction and result review.",
        )

    if template_fallback_used:
        reasons.append("template_fallback_used=true limits the strength of any reproduction claim")

    if not result_review:
        reasons.append("result_review is missing")
        return _limited_by_template(
            _result(
                "inconclusive",
                "low",
                reasons,
                "Run result-level review before making a reproduction claim.",
            ),
            template_fallback_used,
        )

    if result_review.get("passed") is False:
        reasons.append("result_review failed")
        return _limited_by_template(
            _result(
                "inconclusive",
                "medium",
                reasons,
                "Inspect result_review_error.json and rerun the multimodal result review.",
            ),
            template_fallback_used,
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
        if risk_level == "low":
            verdict = "fully_reproduced"
            confidence = "high"
        elif risk_level == "medium":
            verdict = "mostly_reproduced"
            confidence = "medium"
        else:
            verdict = "partially_reproduced"
            confidence = "medium"
        return _limited_by_template(
            _result(verdict, confidence, reasons, _positive_action(verdict)),
            template_fallback_used,
        )

    if alignment in {"close", "partial_match", "mostly_match", "match"} and credibility in {"high", "medium"}:
        verdict = "mostly_reproduced" if risk_level in {"", "low", "medium"} else "partially_reproduced"
        confidence = "medium" if risk_level != "high" else "low"
        return _limited_by_template(
            _result(verdict, confidence, reasons, _positive_action(verdict)),
            template_fallback_used,
        )

    if alignment in {"partial", "partial_match"} or credibility == "medium":
        return _limited_by_template(
            _result(
                "partially_reproduced",
                "medium" if risk_level != "high" else "low",
                reasons,
                "Treat the run as partial evidence; document gaps and rerun with full parameters where possible.",
            ),
            template_fallback_used,
        )

    reasons.append("result review did not contain enough positive alignment evidence")
    return _limited_by_template(
        _result(
            "inconclusive",
            "low",
            reasons,
            "Collect stronger result evidence and rerun result-level review.",
        ),
        template_fallback_used,
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


def _limited_by_template(result: dict[str, Any], template_fallback_used: bool) -> dict[str, Any]:
    if not template_fallback_used:
        return result
    if result["verdict"] in {"fully_reproduced", "mostly_reproduced"}:
        result = dict(result)
        result["verdict"] = "partially_reproduced"
        result["confidence"] = _min_confidence(result["confidence"], "medium")
        result["recommended_action"] = (
            "Template fallback was used; label the outcome as partial and request human review of the generated assumptions."
        )
    return result


def _template_fallback_used(
    risk_report: dict[str, Any],
    runtime_result: dict[str, Any],
    manifest: dict[str, Any],
) -> bool:
    if runtime_result.get("template_fallback_used"):
        return True
    meta = manifest.get("_meta") if isinstance(manifest.get("_meta"), dict) else {}
    if meta.get("template_fallback_used"):
        return True
    for finding in risk_report.get("findings", []):
        if isinstance(finding, dict) and finding.get("type") == "template_fallback_used":
            return True
    return False


def _risk_level(risk_report: dict[str, Any]) -> str:
    level = _normalized_label(risk_report.get("risk_level"))
    if level:
        return level
    dimensions = risk_report.get("risk_dimensions")
    if not isinstance(dimensions, dict):
        return ""
    levels = {
        _normalized_label(dimension.get("level"))
        for dimension in dimensions.values()
        if isinstance(dimension, dict)
    }
    if "high" in levels:
        return "high"
    if "medium" in levels:
        return "medium"
    if "low" in levels:
        return "low"
    return ""


def _normalized_label(value: Any) -> str:
    return str(value).strip().lower().replace("-", "_") if value is not None else ""


def _min_confidence(current: str, cap: str) -> str:
    order = {"low": 0, "medium": 1, "high": 2}
    current_level = order.get(current, 0)
    cap_level = order.get(cap, 0)
    return min(order, key=lambda key: abs(order[key] - min(current_level, cap_level)))


def _positive_action(verdict: str) -> str:
    if verdict == "fully_reproduced":
        return "Report as reproduced, while preserving links to runtime logs and result-review evidence."
    if verdict == "mostly_reproduced":
        return "Report as mostly reproduced and list the remaining risk dimensions or minor mismatches."
    return "Report as partial evidence only and identify the missing checks needed for a stronger claim."
