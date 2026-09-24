"""Expose architecture addresses for runtime use without approving their meaning."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


class ArchitectureAddressError(ValueError):
    """An architecture value cannot be addressed as a document."""

    def __init__(self, path: str, message: str):
        self.path = path
        super().__init__(f"{path}: {message}")


_ADDRESS_ALIASES = (
    ("components", "id", ("component_id",)),
    ("quantities", "id", ("quantity_id",)),
    ("bindings", "components", ("component_ids",)),
    ("bindings", "outputs", ("output_quantity_ids",)),
    ("bindings", "consistency_group", ("primary_consistency_group_id",)),
    ("consistency_groups", "id", ("group_id", "consistency_group_id")),
    ("invariants", "id", ("invariant_id",)),
)


def architecture_runtime_view(
    architecture: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Copy known address aliases into canonical keys, preserving the source.

    Missing addresses, unfamiliar structures and scientific fields stay as
    supplied. This adapter does not decide whether the architecture is correct.
    """
    if architecture is None:
        return None
    if not isinstance(architecture, dict):
        raise ArchitectureAddressError("$", "architecture must be an object or None")
    result = deepcopy(architecture)
    for collection, canonical, aliases in _ADDRESS_ALIASES:
        items = result.get(collection)
        if not isinstance(items, list):
            continue
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            supplied = [key for key in (canonical, *aliases) if key in item]
            if not supplied:
                continue
            first = supplied[0]
            # output_quantity_ids names the runtime scientific quantities;
            # outputs may name deliverable artifacts. Keep both owner fields.
            if collection == "bindings" and canonical == "outputs" and "output_quantity_ids" in item:
                item[canonical] = deepcopy(item["output_quantity_ids"])
            if canonical not in item:
                item[canonical] = deepcopy(item[first])
    return result
