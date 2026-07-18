from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from .outputs import write_json
from .schemas import ValidationIssue


def write_analysis_warnings(
    *,
    output_dir: Path,
    audit_dir: Path,
    stage: str,
    groups: dict[str, Iterable[ValidationIssue]],
) -> dict:
    """Replace one stage's advisory diagnostics in the aggregate warning log."""
    warnings: list[dict[str, str]] = []
    for category, issues in groups.items():
        for issue in issues:
            warnings.append(
                {
                    "stage": stage,
                    "category": str(category),
                    "path": issue.path,
                    "message": issue.message,
                    "severity": "warning",
                }
            )

    aggregate_path = audit_dir / "analysis_warnings.json"
    existing: dict = {}
    if aggregate_path.is_file():
        try:
            loaded = json.loads(aggregate_path.read_text(encoding="utf-8"))
            existing = loaded if isinstance(loaded, dict) else {}
        except (OSError, ValueError):
            existing = {}

    retained = [] if stage.startswith("01_") else [
        item
        for item in existing.get("warnings", [])
        if isinstance(item, dict) and str(item.get("stage") or "") != stage
    ]
    combined = [*retained, *warnings]
    stage_counts: dict[str, int] = {}
    for item in combined:
        item_stage = str(item.get("stage") or "unknown")
        stage_counts[item_stage] = stage_counts.get(item_stage, 0) + 1

    aggregate = {
        "advisory_only": True,
        "warning_count": len(combined),
        "stage_counts": stage_counts,
        "warnings": combined,
    }
    stage_doc = {
        "advisory_only": True,
        "stage": stage,
        "warning_count": len(warnings),
        "warnings": warnings,
    }
    write_json(audit_dir / f"{stage}_warnings.json", stage_doc)
    write_json(aggregate_path, aggregate)
    write_json(output_dir / "analysis_warnings.json", aggregate)
    return aggregate