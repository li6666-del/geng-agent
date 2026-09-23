"""Public evidence utilities; scientific revisions remain whole owner snapshots."""
from copy import deepcopy
from typing import Any
from .task_backfill_worklist import collect_missing_fact_requests, merge_request_worklists
from .task_backfill_ledger import (
    cumulative_resolution_from_ledger, summarize_backfill_resolution,
    update_search_ledger, compute_material_backfill_delta,
)


def reconcile_final_tasks(preliminary_tasks: dict, candidate_tasks: dict,
                          resolution: dict, *, relationship_snapshot: bool = False) -> dict:
    """Do not combine old/new scientific contracts or restore deleted task scope.

    Both snapshots are evidence for the supervisor, which decides whether the
    Planner's revision is faithful. The selected candidate is copied unchanged.
    """
    if not isinstance(candidate_tasks, dict):
        raise ValueError("Planner revision must be a JSON object")
    return deepcopy(candidate_tasks)
