"""Shared constants for task-writer orchestration and worker modules."""

from __future__ import annotations

from .verification_result import WRITER_REVIEW_STATUS


TASK_WRITER_TERMINAL_STATUS = WRITER_REVIEW_STATUS

DEFAULT_MAX_EVIDENCE_RERUNS = 8

WRITER_PAPER_FIDELITY_POLICY = """## Highest law: fidelity to the paper's established facts
Fidelity outranks visual closeness, convenience, prior code, and reporter advice in both an initial implementation and every repair session.

- Treat paper-explicit data, system models, equations, algorithm steps, experiment protocols, baseline identities, metric definitions, axes, and stated scan ranges as immutable constraints. Do not alter, replace, or bypass them merely to make a curve look closer to the target.
- Use this evidence priority: explicit paper statements and figures; deterministic derivations from them; figure-level visual estimates; standard domain assumptions; target-informed calibration; reporter suggestions. Lower-priority evidence may fill a genuine gap but may never overwrite higher-priority evidence.
- When the paper is silent, incomplete, or genuinely ambiguous, make a bold but scientifically plausible implementation or value assumption. Label it `assumed`, explain why it is reasonable, keep it separate from paper facts, and revise it when comparison evidence warrants that.
- An assumed algorithm is acceptable only as an implementation completion for an unspecified step. It may not replace a model, data-generating law, objective, or core algorithm that the paper already defines.
- Reporter feedback is evidence to investigate, not authority over the paper. Reject or reclassify feedback that conflicts with explicit paper evidence. Preserve the faithful main result and keep any conflicting figure-fitting alternative clearly labeled as a diagnostic branch.
"""
