"""Scientific-integrity gate for reproduction outputs.

The guarded runner accepts a run when it exits 0 and writes a valid CSV/PNG/summary.
That gate cannot tell a real reproduction from a *gutted* one: a repair that makes
the code "pass" by emitting a flat/all-zero curve, dropping the expected columns, or
producing a trend opposite to the paper's claim would be accepted.

This module adds a lenient, evidence-first check using the expectations the pipeline
already extracted into ``repro_tasks.json`` (``output_columns`` and ``expected_trend``).
It only flags *clearly* degenerate output, so legitimate small/edge results pass:

- empty output: flagged only when the output CSV(s) contain NO numeric column at all
  (a genuinely gutted result). Column *names* are intentionally NOT matched against the
  task's declared output_columns -- a valid run may use different-but-equivalent names
  / layout (long vs wide, snr_db vs eb_n0_db, bit_error_rate vs ber).
- flat / near-constant curve / trend contradiction: judged only for tasks that expect
  variation (direction decreasing/increasing) and only on CSVs with >= 3 data rows.
  "Near-constant" catches low-variance noise around a fixed level (e.g. BER pinned at
  ~0.5 = random guessing) that the exact-constant and strict-monotonic checks miss.

When there are no task expectations, no parseable CSV, or too few rows, the check is
skipped (returns no issues), so behaviour is unchanged for those cases.
"""

from __future__ import annotations

import csv
import json
import statistics
from pathlib import Path
from typing import Any


MIN_ROWS_FOR_TREND = 3
_EPS = 1e-9
# A column whose coefficient of variation is below this, with no net movement in the
# expected direction, is treated as "near-constant" (e.g. BER pinned at ~0.5 = random
# guessing). Conservative so a genuine—if shallow—trend is not falsely flagged.
NEAR_CONSTANT_CV = 0.05
MIN_DIRECTIONAL_MOVE = 0.02

# Probability / error-rate metrics are physically confined to [0, 1]. A run can exit 0
# and emit valid CSV yet have silently miscomputed such a column (e.g. a BER of -0.00187
# or 1.2). Only column names containing one of these conservative tokens are range-checked,
# so quantities that legitimately exceed 1 (throughput, capacity, snr, spectral_efficiency)
# are never touched.
PROBABILITY_TOKENS = (
    "ber",
    "ser",
    "bler",
    "per",
    "fer",
    "sep",
    "bep",
    "bit_error",
    "symbol_error",
    "block_error",
    "outage",
)
# Raw-count columns (e.g. ``bit_errors``, ``symbol_errors``, ``num_bits``, ``error_count``)
# share substrings with the probability tokens above but are counts, not rates, and may be
# arbitrarily large. Exclude any column whose name carries a count marker so they are never
# range-checked against [0, 1].
_COUNT_TOKENS = ("count", "num", "_errors", "errors_", "n_bits", "n_symbols", "n_blocks")
# Tolerances keep floating-point noise at the physical boundaries from being flagged.
_VALUE_RANGE_NEG_EPS = 1e-9
_VALUE_RANGE_HIGH_EPS = 1e-6


def load_task_expectations(repro_project_dir: Path) -> list[dict[str, Any]]:
    """Read ``repro_tasks.json`` from the case directory (the project's parent).

    Returns ``[]`` on any missing/unreadable/unexpected input so the caller stays lenient.
    """
    tasks_path = repro_project_dir.parent / "repro_tasks.json"
    if not tasks_path.exists():
        return []
    try:
        data = json.loads(tasks_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return []
    tasks = data.get("repro_tasks") if isinstance(data, dict) else None
    if not isinstance(tasks, list):
        return []

    expectations: list[dict[str, Any]] = []
    for task in tasks:
        if not isinstance(task, dict):
            continue
        columns = task.get("output_columns")
        trend = task.get("expected_trend")
        expectations.append(
            {
                "task_id": task.get("task_id"),
                "output_columns": [c for c in columns if isinstance(c, str)] if isinstance(columns, list) else [],
                "expected_trend": trend if isinstance(trend, dict) else {},
            }
        )
    return expectations


def scientific_integrity_issues(repro_project_dir: Path) -> list[dict[str, str]]:
    expectations = load_task_expectations(repro_project_dir)
    if not expectations:
        return []
    return check_outputs_scientific_integrity(repro_project_dir / "outputs", expectations)


def check_outputs_scientific_integrity(outputs_dir: Path, expectations: list[dict[str, Any]]) -> list[dict[str, str]]:
    if not expectations or not outputs_dir.exists():
        return []

    parsed: list[tuple[Path, list[str], list[list[str]]]] = []
    for path in sorted(outputs_dir.glob("*.csv")):
        header, rows = _read_csv(path)
        if header:
            parsed.append((path, header, rows))
    if not parsed:
        return []

    issues: list[dict[str, str]] = []
    issues.extend(_column_absence_issues(parsed, expectations))
    issues.extend(_trend_and_degeneracy_issues(parsed, expectations))
    issues.extend(_value_range_issues(parsed))
    return issues


def _column_absence_issues(
    parsed: list[tuple[Path, list[str], list[list[str]]]],
    expectations: list[dict[str, Any]],
) -> list[dict[str, str]]:
    # Relaxed: flag only when the outputs contain NO numeric data column at all (a
    # genuinely empty/gutted result). We deliberately do NOT require the produced column
    # *names* to overlap the task's declared output_columns -- a valid reproduction may
    # use a different-but-equivalent layout/naming. Whether the data is degenerate is
    # judged separately by the trend/degeneracy rules on the identified metric column.
    for _, header, rows in parsed:
        for index in range(len(header)):
            if len(_numeric_column(rows, index)) >= 2:
                return []
    return [
        {
            "file": ", ".join(path.name for path, _, _ in parsed),
            "issue": "no numeric output column produced",
            "detail": "output CSV(s) contain no numeric data column; a reproduction must emit at least one numeric metric column",
        }
    ]


def _trend_and_degeneracy_issues(
    parsed: list[tuple[Path, list[str], list[list[str]]]],
    expectations: list[dict[str, Any]],
) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    for exp in expectations:
        trend = exp.get("expected_trend") or {}
        direction = str(trend.get("direction") or "").strip().lower()
        if direction not in {"decreasing", "increasing"}:
            continue  # no variation expected -> do not judge flatness or trend
        y_axis = str(trend.get("y_axis") or "").strip().lower()
        x_axis = str(trend.get("x_axis") or "").strip().lower()

        for path, header, rows in parsed:
            y_col = y_axis if (y_axis and y_axis in header) else _pick_metric_column(header, rows, x_axis)
            if y_col is None:
                continue
            y_idx = header.index(y_col)
            y_values = _numeric_column(rows, y_idx)
            if len(y_values) < MIN_ROWS_FOR_TREND:
                continue

            if _is_constant(y_values) or _is_all_zero(y_values):
                issues.append(
                    {
                        "file": path.name,
                        "issue": "degenerate/flat output column",
                        "detail": f"column '{y_col}' is constant or all-zero across {len(y_values)} rows while a {direction} trend was expected",
                    }
                )
                continue  # a flat column cannot also "contradict" a direction

            ordered = _y_ordered_by_x(header, rows, x_axis, y_idx)
            if _is_near_constant(y_values, ordered, direction):
                issues.append(
                    {
                        "file": path.name,
                        "issue": "near-constant / low-variance output column",
                        "detail": f"column '{y_col}' is near-constant (CV < {NEAR_CONSTANT_CV}) with no net {direction} movement across {len(y_values)} rows; a {direction} trend was expected",
                    }
                )
                continue
            if _strictly_monotonic_opposite(ordered, direction):
                observed = "increasing" if direction == "decreasing" else "decreasing"
                issues.append(
                    {
                        "file": path.name,
                        "issue": "trend contradicts expected direction",
                        "detail": f"expected {direction} but column '{y_col}' is strictly {observed}",
                    }
                )
    return issues


def _is_probability_column(name: str) -> bool:
    if any(token in name for token in _COUNT_TOKENS):
        return False  # raw count (bit_errors, num_bits, ...), not a [0, 1] rate
    return any(token in name for token in PROBABILITY_TOKENS)


def _value_range_issues(
    parsed: list[tuple[Path, list[str], list[list[str]]]],
) -> list[dict[str, str]]:
    """Flag probability/error-rate columns whose values escape the physical [0, 1] range.

    Runs unconditionally on every parsed CSV (no dependency on expected_trend): a value
    < 0 is physically impossible and a value > 1 is equally invalid for these metrics, so
    either signals math that went silently wrong even when the process exited 0. Only
    columns whose (lowercased) name contains a PROBABILITY_TOKENS token are checked, and at
    most one issue per category per column is emitted to avoid flooding the report."""
    issues: list[dict[str, str]] = []
    for path, header, rows in parsed:
        for index, name in enumerate(header):
            if not _is_probability_column(name):
                continue
            values = _numeric_column(rows, index)
            if not values:
                continue

            negative = next((value for value in values if value < -_VALUE_RANGE_NEG_EPS), None)
            if negative is not None:
                issues.append(
                    {
                        "file": path.name,
                        "issue": "negative value for probability/rate metric",
                        "detail": f"column '{name}' has value {negative} < 0 in {path.name}; a probability/error-rate metric must lie in [0, 1]",
                    }
                )

            too_high = next((value for value in values if value > 1 + _VALUE_RANGE_HIGH_EPS), None)
            if too_high is not None:
                issues.append(
                    {
                        "file": path.name,
                        "issue": "probability/rate metric exceeds 1",
                        "detail": f"column '{name}' has value {too_high} > 1 in {path.name}; a probability/error-rate metric must lie in [0, 1]",
                    }
                )
    return issues


def _read_csv(path: Path) -> tuple[list[str], list[list[str]]]:
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            rows = list(csv.reader(handle))
    except (OSError, UnicodeDecodeError, csv.Error):
        return [], []
    if not rows or not rows[0]:
        return [], []
    header = [cell.strip().lower() for cell in rows[0]]
    return header, rows[1:]


def _to_float(cell: str) -> float | None:
    try:
        return float(cell.strip())
    except (TypeError, ValueError, AttributeError):
        return None


def _numeric_column(rows: list[list[str]], index: int) -> list[float]:
    values: list[float] = []
    for row in rows:
        if index < len(row):
            value = _to_float(row[index])
            if value is not None:
                values.append(value)
    return values


_X_AXIS_TOKENS = ("snr", "eb_n0", "es_n0", "ebn0", "esn0", "ebno", "esno", "_n0", "n0_", "index", "idx")
_METRIC_TOKENS = ("ber", "ser", "error", "rate", "outage", "throughput", "capacity", "loss", "accuracy", "spectral", "efficiency", "prob", "p_b", "p_e")


def _looks_like_x(name: str, x_axis: str) -> bool:
    if x_axis and name == x_axis:
        return True
    return any(token in name for token in _X_AXIS_TOKENS)


def _pick_metric_column(header: list[str], rows: list[list[str]], x_axis: str) -> str | None:
    """Identify the metric (y) column when the task's declared y_axis is not in the CSV
    header. Conservative: drop independent-variable columns (snr/eb_n0/...), then accept
    only an UNAMBIGUOUS metric column. Returns None (skip judging) when ambiguous, so a
    different-but-valid layout/naming is never falsely flagged as degenerate."""
    candidates = [
        name
        for index, name in enumerate(header)
        if not _looks_like_x(name, x_axis) and len(_numeric_column(rows, index)) >= MIN_ROWS_FOR_TREND
    ]
    if len(candidates) == 1:
        return candidates[0]
    metric_like = [name for name in candidates if any(token in name for token in _METRIC_TOKENS)]
    if len(metric_like) == 1:
        return metric_like[0]
    return None


def _y_ordered_by_x(header: list[str], rows: list[list[str]], x_axis: str, y_idx: int) -> list[float]:
    if x_axis not in header:
        return _numeric_column(rows, y_idx)
    x_idx = header.index(x_axis)
    pairs: list[tuple[float, float]] = []
    for row in rows:
        if x_idx < len(row) and y_idx < len(row):
            x_value = _to_float(row[x_idx])
            y_value = _to_float(row[y_idx])
            if x_value is not None and y_value is not None:
                pairs.append((x_value, y_value))
    pairs.sort(key=lambda pair: pair[0])
    return [y for _, y in pairs]


def _is_constant(values: list[float]) -> bool:
    return len(values) >= 2 and all(abs(value - values[0]) <= _EPS for value in values)


def _is_all_zero(values: list[float]) -> bool:
    return bool(values) and all(abs(value) <= 1e-12 for value in values)


def _is_near_constant(values: list[float], ordered: list[float], direction: str) -> bool:
    """True when the column barely varies AND shows no net movement in the expected
    direction -- i.e. low-variance noise around a fixed level (e.g. BER ~0.5), which the
    exact-constant and strict-monotonic checks miss. A genuine shallow trend that still
    moves the right way is NOT flagged."""
    if len(values) < MIN_ROWS_FOR_TREND:
        return False
    mean = sum(values) / len(values)
    denom = max(abs(mean), _EPS)
    if statistics.pstdev(values) / denom >= NEAR_CONSTANT_CV:
        return False  # genuinely varies relative to its level
    series = ordered if len(ordered) >= 2 else values
    net_rel = (series[-1] - series[0]) / denom
    moves_expected = net_rel <= -MIN_DIRECTIONAL_MOVE if direction == "decreasing" else net_rel >= MIN_DIRECTIONAL_MOVE
    return not moves_expected


def _strictly_monotonic_opposite(values: list[float], direction: str) -> bool:
    if len(values) < MIN_ROWS_FOR_TREND:
        return False
    diffs = [b - a for a, b in zip(values, values[1:])]
    strictly_increasing = all(diff > _EPS for diff in diffs)
    strictly_decreasing = all(diff < -_EPS for diff in diffs)
    if direction == "decreasing":
        return strictly_increasing
    return strictly_decreasing
