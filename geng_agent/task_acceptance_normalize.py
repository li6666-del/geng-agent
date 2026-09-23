"""Compatibility copy of an owner-authored acceptance document.

The host no longer manufactures criteria, changes numeric thresholds, or
classifies presentation-only conclusions from their wording.
"""
from copy import deepcopy
from typing import Any


def normalize_scientific_acceptance(value: Any, *args: Any, **kwargs: Any) -> Any:
    return deepcopy(value)
