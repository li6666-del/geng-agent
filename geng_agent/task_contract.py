from __future__ import annotations

import hashlib
import json
from typing import Any


TASK_CONTRACT_SCHEMA_VERSION = "1.0"


def build_task_contract_draft(
    task: dict[str, Any],
    *,
    memory_snapshot_hash: str,
) -> dict[str, Any]:
    """Build a conservative, valid contract that the writer must review before full."""
    task_id = str(task.get("task_id") or "task").strip()
    experiment_id = str(task.get("experiment_id") or task_id).strip()
    assumptions = task.get("assumptions") if isinstance(task.get("assumptions"), list) else []
    required_facts = task.get("required_facts") if isinstance(task.get("required_facts"), list) else []
    inputs: list[dict[str, Any]] = []
    for item in required_facts:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if name:
            inputs.append({"name": name, "source": f"engineering_fact:{item.get('type', 'other')}", "value": None, "required": True})
    for item in assumptions:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if name:
            inputs.append({"name": name, "source": "task_assumption", "value": item.get("default_value"), "required": False})

    outputs: list[dict[str, Any]] = [
        {"path_pattern": "outputs/<task>/*.csv", "kind": "csv", "required": True},
        {"path_pattern": "outputs/<task>/*.png", "kind": "png", "required": True},
        {"path_pattern": "outputs/<task>/summary*.json", "kind": "json", "required": True},
    ]
    criteria = _acceptance_criteria(task)
    mode = str(task.get("reproducibility_mode") or "native_full")
    if mode not in {"native_full", "scaled_full", "proxy_only", "environment_blocked", "upstream_patch_required"}:
        mode = "native_full"
    return {
        "schema_version": TASK_CONTRACT_SCHEMA_VERSION,
        "task_id": task_id,
        "experiment_id": experiment_id,
        "memory_snapshot_hash": memory_snapshot_hash or "unavailable",
        "reproducibility_mode": mode,
        "inputs": inputs,
        "outputs": outputs,
        "equations": [str(task.get("metric_formula") or "").strip()] if str(task.get("metric_formula") or "").strip() else [],
        "algorithm_steps": [f"Reproduce {str(task.get('target') or task.get('figure_or_claim') or task_id).strip()}"],
        "invariants": ["Use one shared parameter set for proposed and baseline methods.", "Record deterministic seeds and backend selection."],
        "backend": {"requested": "auto", "allow_cpu_fallback": True},
        "resources": {
            "execution_class": "unknown",
            "cpu_cores": 6,
            "ram_gb": 4.0,
            "gpu_count": 1,
            "vram_gb": 0.0,
            "confidence": "low",
        },
        "seed": 1,
        "acceptance_criteria": criteria,
        "assumptions": [
            f"{str(item.get('name') or '').strip()}={item.get('default_value')}"
            for item in assumptions
            if isinstance(item, dict) and str(item.get("name") or "").strip()
        ],
    }


def contract_hash(contract: dict[str, Any]) -> str:
    payload = json.dumps(contract, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _acceptance_criteria(task: dict[str, Any]) -> list[str]:
    explicit = task.get("acceptance_criteria")
    if isinstance(explicit, list):
        values = [str(item).strip() for item in explicit if str(item).strip()]
        if values:
            return values
    criteria: list[str] = []
    trend = task.get("expected_trend") if isinstance(task.get("expected_trend"), dict) else {}
    if trend:
        criteria.append(
            f"{trend.get('y_axis') or 'metric'} is {trend.get('direction') or 'consistent'} versus {trend.get('x_axis') or 'sweep variable'}."
        )
    comparison = task.get("comparison") if isinstance(task.get("comparison"), dict) else {}
    tolerance = str(comparison.get("tolerance") or "").strip()
    if tolerance:
        criteria.append(f"Paper comparison tolerance: {tolerance}")
    return criteria or ["Local trend, scale, ordering, and baseline comparison agree with the cited paper evidence."]
