"""Permissive normalization of task trends and scientific acceptance contracts."""

from __future__ import annotations

import copy
import math
import re
import unicodedata
from typing import Any, get_args

from .facts_normalize import _map_enum, _norm_token
from .schema_models import TrendDirection


_ALLOWED_DIRECTIONS = set(get_args(TrendDirection))
_TREND_KEYS = {"x_axis", "y_axis", "direction", "reason"}
_COMPARISON_KEYS = {"baselines", "curve_groups", "tolerance"}
_SCIENTIFIC_ACCEPTANCE_KEYS = {
    "contract_version",
    "core_conclusions",
    "key_numeric_targets",
    "information_gaps",
}
_CONCLUSION_KINDS = {
    "ordering",
    "trend",
    "crossing",
    "threshold",
    "scaling",
    "gain_loss",
    "mechanism",
    "absolute_level",
    "other",
}
_NUMERIC_EVIDENCE_QUALITIES = {
    "paper_explicit",
    "paper_derived",
    "visual_estimate",
    "unavailable",
}
_GAP_DISPOSITIONS = {
    "assume_and_disclose",
    "single_sensitivity_if_core",
    "terminal_inconclusive",
}
_STYLE_ONLY_RE = re.compile(
    r"(?:pixel[\s_-]*perfect|exact[\s_-]*pixels?|plot[\s_-]*styling|"
    r"marker[\s_-]*style|line[\s_-]*(?:width|colou?r)|font[\s_-]*(?:size|family)|"
    r"\u50cf\u7d20\u7ea7|\u9010\u50cf\u7d20|\u7ed8\u56fe\u6837\u5f0f|\u66f2\u7ebf\u989c\u8272|\u7ebf\u6761\u989c\u8272|\u7ebf\u5bbd|\u5b57\u4f53|\u6807\u8bb0\u6837\u5f0f|\u6392\u7248\u4e00\u81f4)",
    re.IGNORECASE,
)
_SCIENTIFIC_IMAGE_RE = re.compile(
    r"(?:image[\s_-]*reconstruct|pixel[\s_-]*reconstruct|reconstruct(?:ion|ed)?[\s_-]*(?:image|pixel)|"
    r"per[\s_-]*pixel[\s_-]*(?:loss|error|accuracy)|psnr|ssim|super[\s_-]*resolution|"
    r"semantic[\s_-]*segment|object[\s_-]*detect|image[\s_-]*(?:quality|distortion)|"
    r"\u56fe\u50cf\u91cd\u5efa|\u50cf\u7d20\u7ea7(?:\u635f\u5931|\u8bef\u5dee|\u51c6\u786e\u7387|\u91cd\u5efa)|"
    r"\u5cf0\u503c\u4fe1\u566a\u6bd4|\u7ed3\u6784\u76f8\u4f3c|\u8bed\u4e49\u5206\u5272|\u76ee\u6807\u68c0\u6d4b)",
    re.IGNORECASE,
)

DIRECTION_SYNONYMS = {
    "decreases": "decreasing",
    "decrease": "decreasing",
    "down": "decreasing",
    "falling": "decreasing",
    "declining": "decreasing",
    "monotonically_decreasing": "decreasing",
    "increases": "increasing",
    "increase": "increasing",
    "up": "increasing",
    "rising": "increasing",
    "growing": "increasing",
    "monotonically_increasing": "increasing",
    "constant": "flat",
    "stable": "flat",
    "unchanged": "flat",
    "non_monotonic": "unknown",
    "mixed": "unknown",
    "varies": "unknown",
    "na": "unknown",
    "none": "unknown",
}


def map_direction(value: Any) -> tuple[str, bool]:
    mapped, changed = _map_enum(
        value, _ALLOWED_DIRECTIONS, DIRECTION_SYNONYMS, "unknown"
    )
    if mapped != "unknown" or (
        isinstance(value, str)
        and _norm_token(value)
        in {"unknown", "na", "none", "non_monotonic", "mixed", "varies"}
    ):
        return mapped, changed
    if isinstance(value, str):
        token = _norm_token(value)
        if "decreas" in token or "fall" in token or "declin" in token:
            return "decreasing", True
        if "increas" in token or "ris" in token or "grow" in token:
            return "increasing", True
        if "flat" in token or "constant" in token or "stable" in token:
            return "flat", True
    return "unknown", changed


def normalize_trend(trend: dict[str, Any], index: int, coercions: list[str]) -> None:
    extra = [key for key in trend if key not in _TREND_KEYS]
    for key in extra:
        trend.pop(key, None)
    if extra:
        coercions.append(
            f"tasks[{index}].expected_trend dropped unknown keys {sorted(extra)}"
        )
    raw_dir = trend.get("direction")
    direction, changed = map_direction(raw_dir)
    trend["direction"] = direction
    if changed:
        coercions.append(
            f"tasks[{index}].expected_trend.direction {raw_dir!r} -> {direction!r}"
        )
    for key in ("x_axis", "y_axis", "reason"):
        value = trend.get(key)
        if value is None or key not in trend:
            trend[key] = ""
        elif not isinstance(value, str):
            trend[key] = str(value)


def normalize_comparison(
    comparison: dict[str, Any], index: int, coercions: list[str]
) -> None:
    extra = [key for key in comparison if key not in _COMPARISON_KEYS]
    for key in extra:
        comparison.pop(key, None)
    if extra:
        coercions.append(
            f"tasks[{index}].comparison dropped unknown keys {sorted(extra)}"
        )
    for key in ("baselines", "curve_groups"):
        value = comparison.get(key)
        comparison[key] = (
            [item for item in value if isinstance(item, str)]
            if isinstance(value, list)
            else []
        )
    tolerance = comparison.get("tolerance")
    if not (isinstance(tolerance, str) and tolerance.strip()):
        comparison["tolerance"] = "unspecified"
        coercions.append(f"tasks[{index}].comparison.tolerance -> 'unspecified'")


def normalize_scientific_acceptance(
    task: dict[str, Any], index: int, coercions: list[str]
) -> None:
    """Keep a permissive, deterministic scientific contract on every task."""

    raw = task.get("scientific_acceptance")
    acceptance = copy.deepcopy(raw) if isinstance(raw, dict) else {}
    if not isinstance(raw, dict):
        coercions.append(
            f"tasks[{index}].scientific_acceptance generated from task semantics"
        )

    extra = [key for key in acceptance if key not in _SCIENTIFIC_ACCEPTANCE_KEYS]
    for key in extra:
        acceptance.pop(key, None)
    if extra:
        coercions.append(
            f"tasks[{index}].scientific_acceptance dropped unknown keys {sorted(extra)}"
        )

    if acceptance.get("contract_version") != "1.0":
        acceptance["contract_version"] = "1.0"
        coercions.append(
            f"tasks[{index}].scientific_acceptance.contract_version -> '1.0'"
        )

    used_claim_ids: set[str] = set()
    claim_aliases: dict[str, str] = {}
    conclusions: list[dict[str, str]] = []
    raw_conclusions = acceptance.get("core_conclusions")
    for position, item in enumerate(
        raw_conclusions if isinstance(raw_conclusions, list) else []
    ):
        if not isinstance(item, dict):
            statement = acceptance_text(item)
            if not statement:
                continue
            item = {"statement": statement}
            coercions.append(
                f"tasks[{index}].scientific_acceptance.core_conclusions[{position}] recovered from text"
            )
        statement = acceptance_text(item.get("statement"))
        if not statement or is_presentation_only(statement):
            if statement:
                coercions.append(
                    f"tasks[{index}].scientific_acceptance.core_conclusions[{position}] "
                    "dropped presentation-only criterion"
                )
            continue
        raw_id = acceptance_text(item.get("claim_id"))
        claim_id = stable_contract_id(
            raw_id,
            task_id=acceptance_text(task.get("task_id")),
            kind="claim",
            position=position,
            used=used_claim_ids,
        )
        if raw_id:
            claim_aliases.setdefault(raw_id, claim_id)
            claim_aliases.setdefault(contract_id_token(raw_id), claim_id)
        raw_kind = acceptance_text(item.get("kind")).casefold()
        conclusions.append(
            {
                "claim_id": claim_id,
                "statement": statement,
                "kind": raw_kind if raw_kind in _CONCLUSION_KINDS else "other",
                "regime": acceptance_text(item.get("regime"))
                or "paper-defined regime",
                "paper_anchor": acceptance_text(item.get("paper_anchor"))
                or acceptance_text(task.get("figure_or_claim"))
                or "paper result targeted by this task",
            }
        )

    generated_conclusion = False
    if not conclusions:
        generated_conclusion = True
        default = default_core_conclusion(task)
        default["claim_id"] = stable_contract_id(
            "",
            task_id=acceptance_text(task.get("task_id")),
            kind="claim",
            position=0,
            used=used_claim_ids,
        )
        conclusions.append(default)
        coercions.append(
            f"tasks[{index}].scientific_acceptance added a minimal core conclusion"
        )

    used_target_ids: set[str] = set()
    numeric_targets: list[dict[str, Any]] = []
    raw_targets = acceptance.get("key_numeric_targets")
    for position, item in enumerate(
        raw_targets if isinstance(raw_targets, list) else []
    ):
        if not isinstance(item, dict):
            target_text = acceptance_text(item)
            if not target_text:
                continue
            item = {"name": target_text, "paper_magnitude": target_text}
            coercions.append(
                f"tasks[{index}].scientific_acceptance.key_numeric_targets[{position}] recovered from text"
            )
        name = acceptance_text(item.get("name")) or f"key numeric target {position + 1}"
        if is_presentation_only(name):
            coercions.append(
                f"tasks[{index}].scientific_acceptance.key_numeric_targets[{position}] "
                "dropped presentation-only target"
            )
            continue
        magnitude, magnitude_unit = numeric_magnitude_and_unit(
            item.get("paper_magnitude"), item.get("unit")
        )
        evidence_quality = acceptance_text(item.get("evidence_quality")).casefold()
        if evidence_quality not in _NUMERIC_EVIDENCE_QUALITIES or magnitude is None:
            evidence_quality = "unavailable"
        numeric_targets.append(
            {
                "target_id": stable_contract_id(
                    acceptance_text(item.get("target_id")),
                    task_id=acceptance_text(task.get("task_id")),
                    kind="target",
                    position=position,
                    used=used_target_ids,
                ),
                "name": name,
                "paper_magnitude": magnitude,
                "unit": magnitude_unit,
                "regime": acceptance_text(item.get("regime"))
                or "paper-defined regime",
                "evidence_quality": evidence_quality,
            }
        )

    used_gap_ids: set[str] = set()
    gaps: list[dict[str, Any]] = []
    raw_gaps = acceptance.get("information_gaps")
    for position, item in enumerate(raw_gaps if isinstance(raw_gaps, list) else []):
        if not isinstance(item, dict):
            gap_text = acceptance_text(item)
            if not gap_text:
                continue
            item = {"description": gap_text}
            coercions.append(
                f"tasks[{index}].scientific_acceptance.information_gaps[{position}] recovered from text"
            )
        raw_refs = (
            item.get("affects_claim_ids")
            if isinstance(item.get("affects_claim_ids"), list)
            else []
        )
        affects: list[str] = []
        for raw_ref in raw_refs:
            ref = acceptance_text(raw_ref)
            if not ref:
                continue
            normalized = (
                claim_aliases.get(ref)
                or claim_aliases.get(contract_id_token(ref))
                or contract_id_token(ref)
            )
            if normalized and normalized not in affects:
                affects.append(normalized)
        raw_disposition = acceptance_text(item.get("disposition")).casefold()
        gaps.append(
            {
                "gap_id": stable_contract_id(
                    acceptance_text(item.get("gap_id")),
                    task_id=acceptance_text(task.get("task_id")),
                    kind="gap",
                    position=position,
                    used=used_gap_ids,
                ),
                "description": acceptance_text(item.get("description"))
                or "A paper-specific acceptance detail remains unavailable.",
                "affects_claim_ids": affects,
                "disposition": raw_disposition
                if raw_disposition in _GAP_DISPOSITIONS
                else "assume_and_disclose",
            }
        )

    if generated_conclusion:
        gaps.append(
            {
                "gap_id": stable_contract_id(
                    "",
                    task_id=acceptance_text(task.get("task_id")),
                    kind="gap_acceptance_evidence",
                    position=len(gaps),
                    used=used_gap_ids,
                ),
                "description": (
                    "The task designer did not provide a paper-specific scientific "
                    "acceptance contract; the minimal conclusion was derived from the "
                    "task target or expected trend."
                ),
                "affects_claim_ids": [conclusions[0]["claim_id"]],
                "disposition": "assume_and_disclose",
            }
        )

    task["scientific_acceptance"] = {
        "contract_version": "1.0",
        "core_conclusions": conclusions,
        "key_numeric_targets": numeric_targets,
        "information_gaps": gaps,
    }


def default_core_conclusion(task: dict[str, Any]) -> dict[str, str]:
    trend = task.get("expected_trend")
    trend = trend if isinstance(trend, dict) else {}
    direction = acceptance_text(trend.get("direction")).casefold()
    if direction in {"decreasing", "increasing", "flat"}:
        x_axis = acceptance_text(trend.get("x_axis")) or "the swept variable"
        y_axis = (
            acceptance_text(trend.get("y_axis"))
            or acceptance_text(task.get("metric"))
            or "the metric"
        )
        statement = f"{y_axis} is {direction} as {x_axis} changes"
        reason = acceptance_text(trend.get("reason"))
        if reason:
            statement += f"; {reason}"
        kind = "trend"
    else:
        statement = (
            acceptance_text(task.get("target"))
            or acceptance_text(task.get("figure_or_claim"))
            or "Reproduce the task's primary scientific result."
        )
        kind = "other"
    return {
        "claim_id": "",
        "statement": statement,
        "kind": kind,
        "regime": "paper-defined regime",
        "paper_anchor": acceptance_text(task.get("figure_or_claim"))
        or "paper result targeted by this task",
    }


def stable_contract_id(
    value: str,
    *,
    task_id: str,
    kind: str,
    position: int,
    used: set[str],
) -> str:
    base = contract_id_token(value)
    if not base:
        task_token = contract_id_token(task_id) or "task"
        base = f"{task_token}_{kind}_{position + 1}"
    candidate = base
    suffix = 2
    while candidate in used:
        candidate = f"{base}_{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def contract_id_token(value: Any) -> str:
    token = re.sub(r"[^\w.:-]+", "_", unicodedata.normalize("NFC", acceptance_text(value))).strip(
        "_.:-"
    )
    return token.casefold()


def is_presentation_only(value: str) -> bool:
    return bool(_STYLE_ONLY_RE.search(value)) and not bool(
        _SCIENTIFIC_IMAGE_RE.search(value)
    )


def numeric_magnitude_and_unit(value: Any, unit: Any) -> tuple[float | None, str]:
    declared_unit = (
        unit.strip()
        if isinstance(unit, str)
        else str(unit).strip()
        if unit is not None
        else ""
    )
    if not isinstance(value, str):
        return finite_float_or_none(value), declared_unit or "unspecified"

    text = value.strip().replace("\u2212", "-")
    if not text:
        return None, declared_unit or "unspecified"
    power_match = re.search(
        r"(?:([-+]?(?:\d+(?:\.\d*)?|\.\d+))\s*[x\u00d7]\s*)?10\s*(?:\^|\*\*)\s*([-+]?\d+)",
        text,
        re.IGNORECASE,
    )
    if power_match:
        coefficient = float(power_match.group(1) or "1")
        try:
            magnitude = coefficient * 10.0 ** int(power_match.group(2))
        except (OverflowError, ValueError):
            magnitude = None
        span = power_match.span()
    else:
        number_match = re.search(
            r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?",
            text,
        )
        if number_match is None:
            return None, declared_unit or f"paper text: {text}"
        magnitude = finite_float_or_none(number_match.group())
        span = number_match.span()
    residual = (text[: span[0]] + " " + text[span[1] :]).strip(" \t=:;,()[]")
    if declared_unit and residual and residual.casefold() not in declared_unit.casefold():
        preserved_unit = f"{declared_unit}; source unit: {residual}"
    else:
        preserved_unit = declared_unit or residual or "unspecified"
    return finite_float_or_none(magnitude), preserved_unit


def finite_float_or_none(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def acceptance_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


# Historical private names remain available to callers and the facade.
_map_direction = map_direction
_normalize_trend = normalize_trend
_normalize_comparison = normalize_comparison
_normalize_scientific_acceptance = normalize_scientific_acceptance
_default_core_conclusion = default_core_conclusion
_stable_contract_id = stable_contract_id
_contract_id_token = contract_id_token
_is_presentation_only = is_presentation_only
_numeric_magnitude_and_unit = numeric_magnitude_and_unit
_finite_float_or_none = finite_float_or_none
_acceptance_text = acceptance_text
