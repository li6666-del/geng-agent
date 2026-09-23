"""Expose explicit architecture addresses without rewriting scientific content."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


class ArchitectureAddressError(ValueError):
    """Two explicitly supplied addresses disagree at a known protocol path."""

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
            for other in supplied[1:]:
                if type(item[first]) is not type(item[other]) or item[first] != item[other]:
                    raise ArchitectureAddressError(
                        f"$.{collection}[{index}].{canonical}",
                        f"conflicting explicit addresses: {first} and {other}",
                    )
            if canonical not in item:
                item[canonical] = deepcopy(item[first])
    return result
