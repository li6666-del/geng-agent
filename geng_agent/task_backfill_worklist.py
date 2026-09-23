"""Missing-fact request planning and actionable worklist management."""

from __future__ import annotations

import copy
from typing import Any

def collect_missing_fact_requests(tasks: dict[str, Any], **_legacy: Any) -> list[dict[str, Any]]:
    """Enumerate exact Planner request IDs without merging similar wording."""
    result = []
    for task in tasks.get("repro_tasks", []):
        if not isinstance(task, dict):
            continue
        raw = task.get("missing_fact_requests")
        for request in raw if isinstance(raw, list) else []:
            if not isinstance(request, dict):
                continue
            item = copy.deepcopy(request)
            item["task_ids"] = [task.get("task_id")]
            item["source_request_ids"] = [request.get("request_id")]
            result.append(item)
    return result


def merge_request_worklists(base: list[dict[str, Any]], addition: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the latest explicitly addressed request and retain earlier evidence in the ledger."""
    result = copy.deepcopy(base)
    indexes = {item.get("request_id"): index for index, item in enumerate(result)
               if isinstance(item.get("request_id"), str)}
    for item in addition:
        request_id = item.get("request_id")
        if isinstance(request_id, str) and request_id in indexes:
            result[indexes[request_id]] = copy.deepcopy(item)
        else:
            result.append(copy.deepcopy(item))
            if isinstance(request_id, str):
                indexes[request_id] = len(result) - 1
    return result
