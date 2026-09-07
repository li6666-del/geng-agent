"""Shared repair semantics for Codex and compatible API analysis workers."""

from __future__ import annotations

import re
from typing import Any, Callable

from .schemas import ValidationIssue


def scientific_correction_paths(
    issues: list[ValidationIssue], candidate: dict[str, Any] | None = None,
    baseline: dict[str, Any] | None = None,
) -> list[str]:
    # An error on an entire document/list is not permission to rewrite every
    # scientific item. Missing items can already be added by the preservation
    # validator. Only concrete field errors authorize changing existing values.
    paths = [issue.path for issue in issues
        if re.match(r"^\$\.[A-Za-z_]+\[\d+\]\.[A-Za-z_]", issue.path)
        and not issue.path.endswith((".id", ".task_id", ".experiment_id"))
    ]
    # A global/override conflict has two evidence-dependent resolutions: the
    # override may be wrong, or the quantity may have been scoped incorrectly.
    # Authorize only that quantity's scope, never its unit/default/normalization.
    if candidate is not None:
        quantities = candidate.get("quantities", [])
        for issue in issues:
            match = re.match(r"^\$\.bindings\[\d+\]\.overrides\.(.+)$", issue.path)
            if match and "global quantities" in issue.message:
                paths.extend(f"$.quantities[{index}].scope" for index, quantity in enumerate(quantities)
                             if isinstance(quantity, dict) and quantity.get("id") == match.group(1))
    if candidate is not None and baseline is not None:
        aligned = []
        for path in paths:
            match = re.match(r"^\$\.(\w+)\[(\d+)\](.*)$", path)
            if match:
                name, raw_index, suffix = match.groups()
                current_items, original_items = candidate.get(name, []), baseline.get(name, [])
                index = int(raw_index)
                if index < len(current_items) and isinstance(current_items[index], dict):
                    item = current_items[index]
                    keys = ("task_id", "experiment_id") if name == "bindings" else ("id",)
                    identity = tuple(item.get(key) for key in keys)
                    original_index = next((i for i, original in enumerate(original_items)
                                           if isinstance(original, dict) and identity == tuple(original.get(key) for key in keys)), None)
                    if original_index is not None:
                        path = f"$.{name}[{original_index}]{suffix}"
            aligned.append(path)
        paths = aligned
    return list(dict.fromkeys(paths))


def preserved_science_issues(
    validator: Callable[[dict[str, Any], dict[str, Any]], list[ValidationIssue]] | None,
    before: dict[str, Any] | None,
    after: dict[str, Any],
    authorized_paths: list[str],
) -> list[ValidationIssue]:
    if validator is None or before is None:
        return []
    issues = validator(before, after)
    return [issue for issue in issues if not any(
        issue.path == path or issue.path.startswith(path + ".") or issue.path.startswith(path + "[")
        for path in authorized_paths
    )]
