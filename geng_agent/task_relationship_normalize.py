"""Permissive normalization for task handoffs and execution relationships."""

from __future__ import annotations

import copy
from typing import Any


_EXECUTION_RELATIONSHIP_KINDS = {
    "same_run_outputs",
    "checkpoint_flow",
    "shared_pretraining",
    "shared_random_realization",
    "shared_dataset_partition",
    "shared_definition",
    "other",
}
_EXECUTION_RELATIONSHIP_STRENGTHS = {"strong", "weak"}
_EXECUTION_RELATIONSHIP_KEYS = {
    "relationship_id",
    "kind",
    "strength",
    "task_ids",
    "producer_task_id",
    "consumer_task_ids",
    "artifact_ids",
    "rationale",
}
_INFERRED_HANDOFF_REASON = (
    "Host inferred a non-blocking Writer handoff because the Task Designer did "
    "not provide a valid backfill_handoff."
)


def normalize_backfill_handoff(value: Any, coercions: list[str]) -> dict[str, Any]:
    """Normalize task-expert advice without making an omitted handoff a gate."""

    provided = isinstance(value, dict)
    if not provided:
        if value is None:
            coercions.append("added inferred non-blocking backfill_handoff")
        else:
            coercions.append("replaced malformed backfill_handoff with an inferred handoff")
        value = {}

    raw_ready = value.get("ready_for_writer", True)
    if isinstance(raw_ready, bool):
        ready = raw_ready
    elif isinstance(raw_ready, str) and raw_ready.strip().lower() in {"false", "no", "0"}:
        ready = False
        coercions.append("backfill_handoff.ready_for_writer string -> false")
    else:
        ready = True
        if raw_ready is not True:
            coercions.append("backfill_handoff.ready_for_writer -> true")

    request_ids: list[str] = []
    raw_ids = value.get("blocking_request_ids")
    for item in raw_ids if isinstance(raw_ids, list) else []:
        request_id = item.strip() if isinstance(item, str) else ""
        if request_id and request_id not in request_ids:
            request_ids.append(request_id)
    reason = str(value.get("reason") or "").strip()
    inferred = bool(value.get("inferred")) if provided else True
    if inferred and not reason:
        reason = _INFERRED_HANDOFF_REASON
    return {
        "ready_for_writer": ready,
        "blocking_request_ids": request_ids,
        "reason": reason,
        "inferred": inferred,
    }


def normalize_execution_relationships(
    value: Any,
    coercions: list[str],
) -> list[dict[str, Any]]:
    """Repair relationship shape only; a later compiler owns task-reference checks."""

    if value is None:
        coercions.append("execution_relationships missing -> []")
        return []
    if not isinstance(value, list):
        coercions.append("execution_relationships was not a list -> []")
        return []

    normalized: list[dict[str, Any]] = []
    used_ids: set[str] = set()
    for index, raw in enumerate(value):
        if not isinstance(raw, dict):
            coercions.append(
                f"execution_relationships[{index}] ignored a non-object relationship"
            )
            continue

        relation = copy.deepcopy(raw)
        extra = sorted(set(relation) - _EXECUTION_RELATIONSHIP_KEYS)
        for key in extra:
            relation.pop(key, None)
        if extra:
            coercions.append(
                f"execution_relationships[{index}] dropped unknown keys {extra}"
            )

        relationship_id = str(relation.get("relationship_id") or "").strip()
        if not relationship_id:
            relationship_id = f"execution_relationship_{index + 1}"
            coercions.append(
                f"execution_relationships[{index}].relationship_id -> {relationship_id!r}"
            )
        if relationship_id in used_ids:
            base = relationship_id
            suffix = 2
            while f"{base}_{suffix}" in used_ids:
                suffix += 1
            relationship_id = f"{base}_{suffix}"
            coercions.append(
                f"execution_relationships[{index}].relationship_id deduplicated"
            )
        used_ids.add(relationship_id)
        relation["relationship_id"] = relationship_id

        kind = str(relation.get("kind") or "other").strip().casefold()
        if kind not in _EXECUTION_RELATIONSHIP_KINDS:
            kind = "other"
            coercions.append(f"execution_relationships[{index}].kind -> 'other'")
        relation["kind"] = kind

        strength = str(relation.get("strength") or "").strip().casefold()
        if strength not in _EXECUTION_RELATIONSHIP_STRENGTHS:
            # Ambiguous coupling must never silently create a cross-Writer
            # Foundation dependency. Conservative co-location keeps all
            # related tasks in one sandbox without adding a hard format gate.
            strength = "strong"
            coercions.append(
                f"execution_relationships[{index}].strength -> 'strong' for conservative co-location"
            )
        relation["strength"] = strength

        relation["task_ids"] = relationship_string_list(relation.get("task_ids"))
        if len(relation["task_ids"]) < 2:
            coercions.append(
                f"execution_relationships[{index}] dropped because fewer than two task_ids were recoverable"
            )
            continue

        producer = relation.get("producer_task_id")
        producer_task_id = str(producer).strip() if producer is not None else ""
        relation["producer_task_id"] = producer_task_id or None
        relation["consumer_task_ids"] = relationship_string_list(
            relation.get("consumer_task_ids")
        )
        relation["artifact_ids"] = relationship_string_list(relation.get("artifact_ids"))
        if (
            relation["strength"] == "weak"
            and relation["producer_task_id"] is not None
            and relation["consumer_task_ids"]
            and relation["artifact_ids"]
        ):
            # A complete runtime artifact flow cannot cross isolated Writers.
            # Treat a model's weak label as recoverable format debt and keep the
            # producer and consumers in one sandbox instead of stopping later.
            relation["strength"] = "strong"
            coercions.append(
                f"execution_relationships[{index}].strength weak -> 'strong' "
                "for a complete producer/consumer artifact flow"
            )
        relation["rationale"] = str(relation.get("rationale") or "").strip()
        normalized.append(relation)
    return normalized


def relationship_string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return list(
        dict.fromkeys(
            str(item).strip()
            for item in value
            if isinstance(item, (str, int, float)) and str(item).strip()
        )
    )


# Historical private names remain available to callers that imported them.
_normalize_backfill_handoff = normalize_backfill_handoff
_normalize_execution_relationships = normalize_execution_relationships
_relationship_string_list = relationship_string_list
