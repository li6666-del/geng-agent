from __future__ import annotations

import math
from typing import Any


SCIENTIFIC_POLICY_ID = "reporter-decision-v3"
# Legacy diagnostic API only; no host decision or rerun uses this threshold.
KEY_NUMERIC_RATIO_THRESHOLD = 10.0
WRITER_RERUN_REASONS = frozenset(
    {"invalid_run", "core_conclusion_failed", "key_numeric_ratio_ge_10", "material_numeric_discrepancy"}
)
TERMINAL_SCIENTIFIC_OUTCOMES = frozenset(
    {
        "reproduced",
        "reproduced_with_assumptions",
        "inconclusive_missing_information",
        "not_reproduced",
        "execution_failed",
        "review_incomplete",  # Engineering terminal, never missing paper information.
    }
)


def symmetric_magnitude_ratio(paper_value: Any, local_value: Any) -> float | None:
    """Return a host-computed ratio only for finite, non-zero magnitudes."""

    if isinstance(paper_value, bool) or isinstance(local_value, bool):
        return None
    if not isinstance(paper_value, (int, float)) or not isinstance(local_value, (int, float)):
        return None
    paper = float(paper_value)
    local = float(local_value)
    if not math.isfinite(paper) or not math.isfinite(local) or paper == 0.0 or local == 0.0:
        return None
    ratio = max(abs(local) / abs(paper), abs(paper) / abs(local))
    return ratio if math.isfinite(ratio) else None


def is_material_numeric_ratio(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and float(value) >= KEY_NUMERIC_RATIO_THRESHOLD
    )


CORE_RESULT_STOP_POLICY = """## Core-result stopping policy
- The Reporter owns scientific decisions: method identity, ordering, trend, thresholds, scaling, mechanism and absolute accuracy. The host records the explicit decision and routes the next action.
- `scientific_acceptance` IDs are navigation aids, not scientific authority. Check the original paper and account for each ID, correcting mistaken criteria with cited evidence and retaining independent findings.
- Judge comparability and materiality using the metric, units, conditions, sign, natural scale and uncertainty. Explain equivalent expressions and any conversion. A magnitude ratio is optional diagnostic arithmetic, never a universal acceptance threshold: below 10 can be material, above 10 can be irrelevant to a particular claim. Do not apply ratios to zero, signed or logarithmic scales without scientific justification.
- For probabilities/BER, inspect bounds, trial counts, zero-event uncertainty and confidence intervals; for rankings and training, inspect sampling variability, seeds, convergence and evaluation protocol. Do not tune seeds or select runs until a desired ordering appears.
- Another Writer run requires `invalid_run`, `core_conclusion_failed`, or `material_numeric_discrepancy`, with cited paper/local evidence, a concrete causal change and a predicted effect. Incomplete handoff, missing images, style and communication failure never request another scientific run.
- A faithful valid run with an unsupported conclusion and no justified next change ends as `not_reproduced`; decisive missing paper information ends as `inconclusive_missing_information`. Disclose material assumptions as `reproduced_with_assumptions`. Engineering/evidence-access failures are separate from paper omissions.
- Stop when the evidence supports the assigned conclusions at scientifically justified accuracy and uncertainty. Preserve failures and limitations rather than tuning toward a pass.
"""
