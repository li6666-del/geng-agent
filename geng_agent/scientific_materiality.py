from __future__ import annotations


SCIENTIFIC_POLICY_ID = "reporter-decision-v3"
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


CORE_RESULT_STOP_POLICY = """## Core-result stopping policy
- The Reporter owns scientific decisions: method identity, ordering, trend, thresholds, scaling, mechanism and absolute accuracy. The host records the explicit decision and routes the next action.
- Acceptance is limited to the assigned reproduction task's goals and their necessary scientific conditions. Reading the full paper does not expand acceptance to other claims, even in the same figure. Record out-of-scope findings separately as `additional_observations`; they do not change the task outcome or justify another run.
- `scientific_acceptance` IDs are navigation aids within those goals, not scientific authority. Check the original paper and account for each ID, correcting mistaken criteria with cited evidence. An independently found implementation or method failure that affects an assigned goal remains decisive even without a Designer ID; explain its connection to that goal. The agents decide scientific relevance; the host does not infer it from wording or ID membership.
- Judge comparability and materiality using the metric, units, conditions, sign, natural scale and uncertainty. Explain equivalent expressions and any conversion. A magnitude ratio is optional diagnostic arithmetic, never a universal acceptance threshold: below 10 can be material, above 10 can be irrelevant to a particular claim. Do not apply ratios to zero, signed or logarithmic scales without scientific justification.
- For probabilities/BER, inspect bounds, trial counts, zero-event uncertainty and confidence intervals; for rankings and training, inspect sampling variability, seeds, convergence and evaluation protocol. Do not tune seeds or select runs until a desired ordering appears.
- A proposed Writer run should explain the observed problem, cited paper/local evidence, a concrete causal change and its predicted effect; field names and reason labels are aids, not an approval gate. Incomplete handoff, missing images, style and communication failure never request another scientific run.
- A faithful valid run with an unsupported conclusion and no justified next change ends as `not_reproduced`; decisive missing paper information ends as `inconclusive_missing_information`. Disclose material assumptions as `reproduced_with_assumptions`. Engineering/evidence-access failures are separate from paper omissions.
- Stop when the evidence supports the assigned conclusions at scientifically justified accuracy and uncertainty. Preserve failures and limitations rather than tuning toward a pass.
"""
