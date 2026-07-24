from __future__ import annotations

import math
from typing import Any


SCIENTIFIC_POLICY_ID = "core-conclusion-v2"
KEY_NUMERIC_RATIO_THRESHOLD = 10.0
WRITER_RERUN_REASONS = frozenset(
    {"invalid_run", "core_conclusion_failed", "key_numeric_ratio_ge_10"}
)
TERMINAL_SCIENTIFIC_OUTCOMES = frozenset(
    {
        "reproduced",
        "reproduced_with_assumptions",
        "inconclusive_missing_information",
        "not_reproduced",
        "execution_failed",
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
    return max(abs(local) / abs(paper), abs(paper) / abs(local))


def is_material_numeric_ratio(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and float(value) >= KEY_NUMERIC_RATIO_THRESHOLD
    )


CORE_RESULT_STOP_POLICY = f"""## Core-result stopping policy
- Judge the paper at the level of its scientific conclusion: method identity, ordering, trend, crossing or threshold region, scaling, gain/loss region, mechanism, or an explicitly claimed absolute level.
- `scientific_acceptance` IDs are navigation aids shared by Task Designer, Writer, and Reporter. Missing or imperfect structure is not itself a scientific failure: recover the intended claim from the task and paper, record uncertainty, and continue.
- For finite non-zero key magnitudes, the host computes `max(abs(local)/abs(paper), abs(paper)/abs(local))`. A ratio below {KEY_NUMERIC_RATIO_THRESHOLD:g} is non-material unless tighter accuracy is itself an explicit core conclusion. Zero and near-zero cases are judged by the core claim, sign, and natural scale, not by an arbitrary epsilon.
- Another Writer execution is allowed only for `invalid_run`, `core_conclusion_failed`, or `key_numeric_ratio_ge_10`, and only when paper evidence plus a concrete causal code/config change and predicted effect are available.
- Pixel alignment, typography, crop quality, plotting style, unspecified seeds/sample counts/solvers, and reasonable choices in paper-silent space never reopen the Writer.
- A valid faithful run with an unsupported conclusion but no evidence-based next change ends as `not_reproduced`. A conclusion that cannot be assessed because the paper omits necessary information ends as `inconclusive_missing_information`. Both are normal reportable outcomes, not reasons for an endless loop.
- Once the latest valid full supports the core conclusions and every available key numeric ratio is below {KEY_NUMERIC_RATIO_THRESHOLD:g}, stop immediately and disclose remaining assumptions and uncertainty.
"""
