"""Conservative feasibility classification for communication-paper tasks.

The classifier is deliberately independent from pipeline and schema code.  It
accepts plain dictionaries so callers can use it before committing to a new
artifact contract.

Supported task requirement forms include ``requirements`` records and common
``required_*`` lists.  Required facts are matched against
``facts["engineering_facts"]``; runtime requirements are matched against
explicit availability/missing fields in ``environment``.  Unknown mandatory
requirements never produce a full-fidelity classification.
"""

from __future__ import annotations

from typing import Any


FEASIBILITY_MODES = frozenset(
    {
        "native_full",
        "scaled_full",
        "proxy_only",
        "environment_blocked",
        "upstream_patch_required",
    }
)

_REQUIREMENT_FIELDS = {
    "required_facts": "fact",
    "required_packages": "software",
    "required_software": "software",
    "required_hardware": "hardware",
    "required_capabilities": "capability",
    "required_services": "service",
    "required_licenses": "license",
    "required_network": "network",
    "required_compute": "compute",
    "required_data": "data",
    "required_datasets": "data",
    "required_assets": "asset",
    "required_models": "model",
    "required_code": "code",
}

_ENVIRONMENT_KINDS = frozenset(
    {"software", "package", "hardware", "capability", "service", "license", "network", "compute", "runtime"}
)

_AVAILABLE_STATUSES = frozenset(
    {"available", "ready", "installed", "present", "satisfied", "supported", "resolved", "applied", "ok", "true"}
)
_MISSING_STATUSES = frozenset(
    {"missing", "unavailable", "absent", "blocked", "unsupported", "incompatible", "failed", "false"}
)
_UNKNOWN_STATUSES = frozenset({"unknown", "pending", "unverified", "unchecked", "not_checked"})

_ENVIRONMENT_MISSING_FIELDS = (
    "missing",
    "unavailable",
    "missing_requirements",
    "unavailable_requirements",
    "missing_packages",
    "missing_software",
    "missing_hardware",
    "missing_capabilities",
    "missing_services",
    "missing_licenses",
)
_ENVIRONMENT_UNKNOWN_FIELDS = ("unknown", "unknown_requirements", "unverified_requirements")


def classify_repro_feasibility(
    task: dict[str, Any],
    facts: dict[str, Any],
    environment: dict[str, Any],
) -> dict[str, Any]:
    """Return a deterministic feasibility profile for one reproduction task.

    Precedence is intentional: an unapplied upstream patch is the first blocker,
    then unavailable runtime conditions, then scientific-fidelity gaps.  A
    reduced run is ``scaled_full`` only when the input explicitly says that the
    method and metric semantics are preserved.
    """
    _require_dict("task", task)
    _require_dict("facts", facts)
    _require_dict("environment", environment)

    evidence: list[dict[str, Any]] = []
    requirements = _collect_requirements(task, facts, environment, evidence)

    patch_required, patch_applied = _patch_state(task, facts, environment, evidence)
    if patch_required:
        requirements.append(
            {
                "name": "upstream_patch",
                "kind": "code",
                "status": "available" if patch_applied else "missing",
                "source": "task/facts/environment",
            }
        )
    if patch_required and not patch_applied:
        return _profile(
            "upstream_patch_required",
            ["upstream_patch_unapplied: faithful execution depends on an upstream patch"],
            requirements,
            evidence,
        )

    environment_reasons = _environment_blockers(task, environment, requirements, evidence)
    if environment_reasons:
        return _profile("environment_blocked", environment_reasons, requirements, evidence)

    proxy_reasons = _proxy_reasons(task, facts, requirements, evidence)
    scaled = _scale_reduced(task, environment, evidence)
    if scaled and not _scale_fidelity_preserved(task, facts, evidence):
        proxy_reasons.append(
            "scale_fidelity_unproven: reduced execution does not establish preservation of method and metric semantics"
        )

    if not _environment_readiness_evidenced(environment, requirements, evidence):
        proxy_reasons.append("environment_unverified: no positive runtime-readiness evidence was provided")

    if proxy_reasons:
        return _profile("proxy_only", proxy_reasons, requirements, evidence)

    if scaled:
        return _profile(
            "scaled_full",
            ["scale_reduced_with_fidelity_preserved: method and metric semantics remain faithful at reduced scale"],
            requirements,
            evidence,
        )

    return _profile(
        "native_full",
        ["full_native_execution_supported: task, facts, and environment contain no unresolved fidelity constraint"],
        requirements,
        evidence,
    )


def classify_feasibility(
    task: dict[str, Any],
    facts: dict[str, Any],
    environment: dict[str, Any],
) -> dict[str, Any]:
    """Short public alias for callers that already operate in a repro context."""
    return classify_repro_feasibility(task, facts, environment)


def _require_dict(name: str, value: Any) -> None:
    if not isinstance(value, dict):
        raise TypeError(f"{name} must be a dict")


def _profile(
    mode: str,
    reasons: list[str],
    requirements: list[dict[str, str]],
    evidence: list[dict[str, Any]],
) -> dict[str, Any]:
    if mode not in FEASIBILITY_MODES:
        raise ValueError(f"unknown feasibility mode: {mode}")
    return {
        "mode": mode,
        "reasons": _dedupe_strings(reasons),
        "requirements": _dedupe_requirements(requirements),
        "evidence": evidence,
    }


def _collect_requirements(
    task: dict[str, Any],
    facts: dict[str, Any],
    environment: dict[str, Any],
    evidence: list[dict[str, Any]],
) -> list[dict[str, str]]:
    requirements: list[dict[str, str]] = []

    generic = task.get("requirements")
    for index, item in enumerate(_as_list(generic)):
        if isinstance(item, dict) and item.get("required") is False:
            continue
        name = _item_name(item)
        if not name:
            continue
        kind = _normalize(item.get("kind") if isinstance(item, dict) else None) or "capability"
        explicit_status = _status_from_item(item)
        requirements.append(
            _resolve_requirement(
                name=name,
                kind=kind,
                source=f"task.requirements[{index}]",
                explicit_status=explicit_status,
                fact_type=_normalize(item.get("type")) if isinstance(item, dict) and kind == "fact" else "",
                facts=facts,
                environment=environment,
                evidence=evidence,
            )
        )

    for field, kind in _REQUIREMENT_FIELDS.items():
        for index, item in enumerate(_as_list(task.get(field))):
            name = _item_name(item)
            if not name:
                continue
            fact_type = _normalize(item.get("type")) if isinstance(item, dict) and kind == "fact" else ""
            requirements.append(
                _resolve_requirement(
                    name=name,
                    kind=kind,
                    source=f"task.{field}[{index}]",
                    explicit_status=_status_from_item(item),
                    fact_type=fact_type,
                    facts=facts,
                    environment=environment,
                    evidence=evidence,
                )
            )

    excluded = {"requires_upstream_patch", "requires_full_scale", "requires_native_full"}
    for field, value in task.items():
        if field in excluded or not field.startswith("requires_") or value is not True:
            continue
        name = field.removeprefix("requires_")
        kind = "hardware" if name in {"gpu", "cpu", "fpga", "radio", "sdr"} else "capability"
        requirements.append(
            _resolve_requirement(
                name=name,
                kind=kind,
                source=f"task.{field}",
                explicit_status="unknown",
                fact_type="",
                facts=facts,
                environment=environment,
                evidence=evidence,
            )
        )

    requirements.extend(_undeclared_environment_requirements(environment, "missing", _ENVIRONMENT_MISSING_FIELDS))
    requirements.extend(_undeclared_environment_requirements(environment, "unknown", _ENVIRONMENT_UNKNOWN_FIELDS))
    return _dedupe_requirements(requirements)


def _resolve_requirement(
    *,
    name: str,
    kind: str,
    source: str,
    explicit_status: str,
    fact_type: str,
    facts: dict[str, Any],
    environment: dict[str, Any],
    evidence: list[dict[str, Any]],
) -> dict[str, str]:
    status = explicit_status
    status_source = source
    if status == "unknown":
        if kind == "fact":
            status, status_source = _fact_requirement_status(name, fact_type, facts)
        else:
            status, status_source = _environment_requirement_status(name, environment)
    _add_evidence(evidence, status_source, status, name=name, kind=kind)
    return {"name": name, "kind": kind, "status": status, "source": status_source}


def _fact_requirement_status(name: str, fact_type: str, facts: dict[str, Any]) -> tuple[str, str]:
    wanted = _normalize(name)
    for index, missing in enumerate(_as_list(facts.get("missing_information"))):
        if _normalize(_item_name(missing)) == wanted:
            return "missing", f"facts.missing_information[{index}]"

    for index, fact in enumerate(_as_list(facts.get("engineering_facts"))):
        if not isinstance(fact, dict) or _normalize(fact.get("name")) != wanted:
            continue
        if fact_type and _normalize(fact.get("type")) != fact_type:
            continue
        confidence = _normalize(fact.get("confidence"))
        if confidence == "low":
            return "unknown", f"facts.engineering_facts[{index}].confidence"
        return "available", f"facts.engineering_facts[{index}]"
    return "unknown", "facts.engineering_facts"


def _environment_requirement_status(name: str, environment: dict[str, Any]) -> tuple[str, str]:
    wanted = _normalize(name)
    for field, value in environment.items():
        field_name = _normalize(field)
        if field_name.endswith(("_available", "_installed", "_supported", "_ready")):
            suffix = next(suffix for suffix in ("_available", "_installed", "_supported", "_ready") if field_name.endswith(suffix))
            if field_name[: -len(suffix)] == wanted and isinstance(value, bool):
                return ("available" if value else "missing"), f"environment.{field}"

        if field_name in _ENVIRONMENT_MISSING_FIELDS:
            entry_status = "missing" if _contains_named_item(value, wanted) else "unknown"
        elif field_name in _ENVIRONMENT_UNKNOWN_FIELDS:
            entry_status = "unknown"
        else:
            entry_status = _named_status(value, wanted)
        if entry_status != "unknown":
            return entry_status, f"environment.{field}"
    return "unknown", "environment"


def _named_status(value: Any, wanted: str) -> str:
    if isinstance(value, dict):
        for key, item in value.items():
            if _normalize(key) != wanted:
                continue
            if isinstance(item, bool):
                return "available" if item else "missing"
            return _normalize_status(item)
        return "unknown"
    for item in _as_list(value):
        if _normalize(_item_name(item)) != wanted:
            continue
        return _status_from_item(item, default="available")
    return "unknown"


def _contains_named_item(value: Any, wanted: str) -> bool:
    if isinstance(value, dict):
        return any(_normalize(key) == wanted for key in value)
    return any(_normalize(_item_name(item)) == wanted for item in _as_list(value))


def _undeclared_environment_requirements(
    environment: dict[str, Any], status: str, fields: tuple[str, ...]
) -> list[dict[str, str]]:
    requirements: list[dict[str, str]] = []
    for field in fields:
        for item in _as_list(environment.get(field)):
            name = _item_name(item)
            if not name:
                continue
            kind = _normalize(item.get("kind")) if isinstance(item, dict) else ""
            kind = kind or _requirement_kind_from_environment_field(field)
            requirements.append(
                {"name": name, "kind": kind, "status": status, "source": f"environment.{field}"}
            )
    return requirements


def _requirement_kind_from_environment_field(field: str) -> str:
    normalized = _normalize(field)
    if "package" in normalized or "software" in normalized:
        return "software"
    for kind in ("hardware", "capability", "service", "license"):
        if kind in normalized:
            return kind
    return "capability"


def _patch_state(
    task: dict[str, Any],
    facts: dict[str, Any],
    environment: dict[str, Any],
    evidence: list[dict[str, Any]],
) -> tuple[bool, bool]:
    required = False
    applied = False
    for source, document in (("task", task), ("facts", facts), ("environment", environment)):
        for field in ("upstream_patch_required", "requires_upstream_patch"):
            if document.get(field) is True:
                required = True
                _add_evidence(evidence, f"{source}.{field}", True)
        for field in ("upstream_patch_applied", "patch_applied"):
            if document.get(field) is True:
                applied = True
                _add_evidence(evidence, f"{source}.{field}", True)
        status = _normalize(document.get("patch_status"))
        if status in {"required", "missing", "pending", "blocked", "unavailable"}:
            required = True
            _add_evidence(evidence, f"{source}.patch_status", document.get("patch_status"))
        elif status in {"applied", "resolved", "not_required", "not_needed"}:
            applied = status in {"applied", "resolved"}
            _add_evidence(evidence, f"{source}.patch_status", document.get("patch_status"))
    return required, applied


def _environment_blockers(
    task: dict[str, Any],
    environment: dict[str, Any],
    requirements: list[dict[str, str]],
    evidence: list[dict[str, Any]],
) -> list[str]:
    reasons: list[str] = []
    for field in ("blocked", "fatal"):
        if environment.get(field) is True:
            reasons.append(f"environment_{field}: environment reports {field}=true")
            _add_evidence(evidence, f"environment.{field}", True)
    for field in ("ready", "can_execute", "execution_available", "runtime_available"):
        if environment.get(field) is False:
            reasons.append(f"environment_{field}: environment reports {field}=false")
            _add_evidence(evidence, f"environment.{field}", False)
    if _normalize(environment.get("status")) in {"blocked", "unavailable", "incompatible", "failed"}:
        reasons.append(f"environment_status: environment status is {_normalize(environment.get('status'))}")
        _add_evidence(evidence, "environment.status", environment.get("status"))

    for requirement in requirements:
        if requirement["kind"] not in _ENVIRONMENT_KINDS or requirement["status"] == "available":
            continue
        reasons.append(
            f"environment_requirement_{requirement['status']}: {requirement['kind']} requirement {requirement['name']!r} is {requirement['status']}"
        )

    if environment.get("full_scale_supported") is False and not _scaled_execution_allowed(task):
        reasons.append("full_scale_unavailable: environment cannot run full scale and the task does not authorize faithful scaling")
        _add_evidence(evidence, "environment.full_scale_supported", False)
    return _dedupe_strings(reasons)


def _proxy_reasons(
    task: dict[str, Any],
    facts: dict[str, Any],
    requirements: list[dict[str, str]],
    evidence: list[dict[str, Any]],
) -> list[str]:
    reasons: list[str] = []
    for source, document in (("task", task), ("facts", facts)):
        for field in ("proxy_only", "proxy_required", "uses_proxy"):
            if document.get(field) is True:
                reasons.append(f"proxy_explicit: {source}.{field}=true")
                _add_evidence(evidence, f"{source}.{field}", True)
        for field in ("mode", "execution_mode", "reproduction_mode", "fidelity"):
            if _normalize(document.get(field)) in {"proxy", "proxy_only", "surrogate", "approximate", "synthetic"}:
                reasons.append(f"proxy_explicit: {source}.{field}={document.get(field)}")
                _add_evidence(evidence, f"{source}.{field}", document.get(field))

    if task.get("executable") is False or task.get("can_execute") is False:
        reasons.append("task_not_executable: task explicitly reports that it cannot execute")
        _add_evidence(evidence, "task.executable", task.get("executable", task.get("can_execute")))
    elif not _task_executability_evidenced(task, evidence):
        reasons.append("task_executability_unproven: no concrete metric/output or explicit executable signal was provided")

    if _normalize(task.get("metric")) == "other":
        reasons.append("metric_unspecified: metric='other' does not define a testable full-fidelity target")
        _add_evidence(evidence, "task.metric", task.get("metric"))
    if "output_columns" in task and not (
        isinstance(task.get("output_columns"), list)
        and any(str(item).strip() for item in task["output_columns"])
    ):
        reasons.append("output_contract_missing: task has no concrete output columns")
        _add_evidence(evidence, "task.output_columns", task.get("output_columns"))

    for requirement in requirements:
        if requirement["kind"] in _ENVIRONMENT_KINDS or requirement["status"] == "available":
            continue
        reasons.append(
            f"fidelity_requirement_{requirement['status']}: {requirement['kind']} requirement {requirement['name']!r} is {requirement['status']}"
        )

    for index, item in enumerate(_as_list(facts.get("missing_information"))):
        if isinstance(item, dict) and _normalize(item.get("impact")) == "high":
            name = _item_name(item) or f"item_{index}"
            reasons.append(f"high_impact_information_missing: {name}")
            _add_evidence(evidence, f"facts.missing_information[{index}]", item)

    for index, assumption in enumerate(_as_list(task.get("assumptions"))):
        if isinstance(assumption, dict) and _normalize(assumption.get("risk")) == "high":
            name = _item_name(assumption) or f"assumption_{index}"
            reasons.append(f"high_risk_assumption: {name}")
            _add_evidence(evidence, f"task.assumptions[{index}]", assumption)

    conflicts = facts.get("conflicts")
    if not conflicts:
        meta = facts.get("_meta") if isinstance(facts.get("_meta"), dict) else {}
        semantic = meta.get("semantic_merge") if isinstance(meta.get("semantic_merge"), dict) else {}
        conflicts = semantic.get("fact_conflicts")
    if _as_list(conflicts):
        reasons.append("fact_conflicts_unresolved: conflicting engineering facts prevent a full-fidelity claim")
        _add_evidence(evidence, "facts.conflicts", conflicts)

    return _dedupe_strings(reasons)


def _task_executability_evidenced(task: dict[str, Any], evidence: list[dict[str, Any]]) -> bool:
    if task.get("executable") is True or task.get("can_execute") is True:
        field = "executable" if task.get("executable") is True else "can_execute"
        _add_evidence(evidence, f"task.{field}", True)
        return True
    if isinstance(task.get("run_command"), str) and task["run_command"].strip():
        _add_evidence(evidence, "task.run_command", task["run_command"])
        return True
    metric = _normalize(task.get("metric"))
    columns = task.get("output_columns")
    if metric and metric != "other" and isinstance(columns, list) and any(str(item).strip() for item in columns):
        _add_evidence(evidence, "task.metric", task.get("metric"))
        _add_evidence(evidence, "task.output_columns", columns)
        return True
    return False


def _scale_reduced(
    task: dict[str, Any], environment: dict[str, Any], evidence: list[dict[str, Any]]
) -> bool:
    for source, document in (("task", task), ("environment", environment)):
        for field in ("scaled", "scale_reduced", "reduced_scale"):
            if document.get(field) is True:
                _add_evidence(evidence, f"{source}.{field}", True)
                return True
        for field in ("scale_factor", "sample_fraction", "run_fraction"):
            value = document.get(field)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 < value < 1:
                _add_evidence(evidence, f"{source}.{field}", value)
                return True
    if _normalize(task.get("execution_mode")) in {"scaled", "scaled_full"}:
        _add_evidence(evidence, "task.execution_mode", task.get("execution_mode"))
        return True
    if environment.get("full_scale_supported") is False and _scaled_execution_allowed(task):
        _add_evidence(evidence, "environment.full_scale_supported", False)
        return True
    return False


def _scaled_execution_allowed(task: dict[str, Any]) -> bool:
    return any(
        task.get(field) is True
        for field in ("allow_scaled", "scalable", "scale_preserves_method", "method_faithful", "metric_preserved")
    ) or _normalize(task.get("execution_mode")) in {"scaled", "scaled_full"}


def _scale_fidelity_preserved(
    task: dict[str, Any], facts: dict[str, Any], evidence: list[dict[str, Any]]
) -> bool:
    if _normalize(task.get("execution_mode")) == "scaled_full":
        _add_evidence(evidence, "task.execution_mode", task.get("execution_mode"))
        return True
    for source, document in (("task", task), ("facts", facts)):
        if document.get("scale_preserves_method") is True and document.get("metric_preserved") is not False:
            _add_evidence(evidence, f"{source}.scale_preserves_method", True)
            return True
        if document.get("method_faithful") is True and document.get("metric_preserved") is True:
            _add_evidence(evidence, f"{source}.method_faithful", True)
            _add_evidence(evidence, f"{source}.metric_preserved", True)
            return True
    return False


def _environment_readiness_evidenced(
    environment: dict[str, Any],
    requirements: list[dict[str, str]],
    evidence: list[dict[str, Any]],
) -> bool:
    for field in ("ready", "can_execute", "execution_available", "runtime_available"):
        if environment.get(field) is True:
            _add_evidence(evidence, f"environment.{field}", True)
            return True
    if environment.get("fatal") is False:
        _add_evidence(evidence, "environment.fatal", False)
        return True
    if _normalize(environment.get("status")) in {"ready", "available", "supported", "ok"}:
        _add_evidence(evidence, "environment.status", environment.get("status"))
        return True
    env_requirements = [item for item in requirements if item["kind"] in _ENVIRONMENT_KINDS]
    if env_requirements and all(item["status"] == "available" for item in env_requirements):
        return True
    if environment.get("full_scale_supported") is True:
        _add_evidence(evidence, "environment.full_scale_supported", True)
        return True
    return False


def _status_from_item(item: Any, default: str = "unknown") -> str:
    if not isinstance(item, dict):
        return default
    for field in ("status", "availability"):
        if field in item:
            return _normalize_status(item[field])
    for field in ("available", "installed", "supported", "satisfied", "present"):
        if isinstance(item.get(field), bool):
            return "available" if item[field] else "missing"
    return default


def _normalize_status(value: Any) -> str:
    if isinstance(value, bool):
        return "available" if value else "missing"
    status = _normalize(value)
    if status in _AVAILABLE_STATUSES:
        return "available"
    if status in _MISSING_STATUSES:
        return "missing"
    if status in _UNKNOWN_STATUSES:
        return "unknown"
    return "unknown"


def _item_name(item: Any) -> str:
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        for field in ("name", "id", "requirement", "package", "capability"):
            value = item.get(field)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, (tuple, set, frozenset)):
        return list(value)
    if isinstance(value, (str, dict)):
        return [value]
    return []


def _normalize(value: Any) -> str:
    if value is None:
        return ""
    return "_".join(str(value).strip().lower().replace("-", "_").split())


def _add_evidence(
    evidence: list[dict[str, Any]],
    field: str,
    value: Any,
    *,
    name: str | None = None,
    kind: str | None = None,
) -> None:
    item: dict[str, Any] = {"source": field, "value": value}
    if name is not None:
        item["name"] = name
    if kind is not None:
        item["kind"] = kind
    marker = repr(item)
    if all(repr(existing) != marker for existing in evidence):
        evidence.append(item)


def _dedupe_strings(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _dedupe_requirements(values: list[dict[str, str]]) -> list[dict[str, str]]:
    rank = {"available": 0, "unknown": 1, "missing": 2}
    positions: dict[tuple[str, str], int] = {}
    result: list[dict[str, str]] = []
    for value in values:
        key = (_normalize(value.get("kind")), _normalize(value.get("name")))
        if not all(key):
            continue
        if key not in positions:
            positions[key] = len(result)
            result.append(value)
            continue
        index = positions[key]
        if rank.get(value.get("status", "unknown"), 1) > rank.get(result[index].get("status", "unknown"), 1):
            result[index] = value
    return result


__all__ = ["FEASIBILITY_MODES", "classify_feasibility", "classify_repro_feasibility"]
