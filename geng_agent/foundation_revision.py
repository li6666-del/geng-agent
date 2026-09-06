"""Small evidence-backed request for a serialized shared-science revision."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .foundation_scope import affected_foundation_consumers, derive_foundation_scope
from .foundation_snapshot import file_sha256, path_is_foundation_link


FOUNDATION_REVISION_FILENAME = "foundation_revision_request.json"


class FoundationRevisionRequired(RuntimeError):
    """A Writer/Reporter requests host-owned repair after active Writers finish."""

    def __init__(self, request: dict[str, Any] | list[dict[str, Any]]) -> None:
        self.requests = request if isinstance(request, list) else [request]
        self.request = self.requests[0]
        self.affected_component_ids = tuple(sorted({key for item in self.requests for key in item.get("component_ids", [])}))
        self.affected_task_ids = tuple(sorted({key for item in self.requests for key in item.get("affected_task_ids", [])}))
        super().__init__("paper-grounded shared Foundation revision requested")


def collect_pending_foundation_revisions(
    records: list[dict[str, Any]],
    foundation: dict[str, Any] | None,
    declined_request_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Keep pending requests across resume, retire only applied older requests."""
    manifest = (foundation or {}).get("manifest") or {}
    revision = manifest.get("revision") or {}
    applied = set(revision.get("applied_request_ids") or []) | {str(revision.get("request_id") or "")}
    current_snapshot = str((foundation or {}).get("snapshot_hash") or manifest.get("snapshot_hash") or "")
    pending: dict[str, dict[str, Any]] = {}
    for record in records:
        request = record.get("foundation_revision_request")
        if not isinstance(request, dict):
            continue
        request_id = str(request.get("request_id") or "")
        origin = str(record.setdefault("foundation_revision_snapshot_hash", current_snapshot))
        if request_id in (declined_request_ids or set()) or record.get("scientific_stop_reason") == "foundation_revision_unresolved":
            record["scientific_stop_reason"] = "foundation_revision_unresolved"
            record["task_writer_status"] = "scientifically_blocked"
            record["writer_completed"] = False
            record["scientific_verdict"] = "needs_revision"
            record["blocked_reason"] = "The evidence-backed shared-science correction could not be validated; the prior implementation is retained without claiming reproduction."
            continue
        if request_id and request_id in applied and origin != current_snapshot:
            record["applied_foundation_revision_request"] = record.pop("foundation_revision_request")
            continue
        pending.setdefault(request_id or json.dumps(request, sort_keys=True), request)
    return list(pending.values())


def validate_foundation_revision_request(
    request: Any,
    *,
    architecture: dict[str, Any],
    evidence_root: Path,
    execution_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Authenticate component scope and actual paper evidence, without prose gates."""

    if not isinstance(request, dict):
        raise ValueError("Foundation revision request must be an object")
    raw_ids = request.get("component_ids", request.get("affected_components"))
    component_ids = sorted({str(value) for value in raw_ids}) if isinstance(raw_ids, list) else []
    scope = derive_foundation_scope(architecture, execution_plan)
    if not component_ids or not set(component_ids).issubset(scope["component_ids"]):
        raise ValueError("Foundation revision must identify existing frozen shared components")
    components = {
        str(item.get("id")): item
        for item in architecture.get("components", []) if isinstance(item, dict)
    }
    modules = {str(components[key].get("module")) for key in component_ids}
    # File-level edits affect every component implemented in that file.
    component_ids = sorted({
        key for key, component in components.items()
        if str(component.get("module")) in modules
    })
    causal_change = str(request.get("causal_change") or "").strip()
    if not causal_change:
        raise ValueError("Foundation revision requires a concrete paper-grounded causal change")
    raw_evidence = request.get("paper_evidence_files")
    if not isinstance(raw_evidence, list) or not raw_evidence:
        raise ValueError("Foundation revision requires existing paper evidence")
    if path_is_foundation_link(evidence_root):
        raise ValueError("Foundation revision evidence root must not be a link")
    root = evidence_root.resolve(strict=True)
    evidence: list[dict[str, str]] = []
    for raw in raw_evidence:
        relative = str(raw).replace("\\", "/")
        parts = relative.split("/")
        if (
            not relative or parts[0] != "paper_evidence"
            or any(part in {"", ".", ".."} or ":" in part for part in parts)
        ):
            raise ValueError("Foundation revision evidence must stay under paper_evidence/")
        candidate = evidence_root
        for part in parts:
            candidate /= part
            if path_is_foundation_link(candidate):
                raise ValueError("Foundation revision evidence must not follow filesystem links")
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
        if not resolved.is_file():
            raise ValueError("Foundation revision evidence is not a regular file")
        evidence.append({"path": relative, "sha256": file_sha256(resolved)})
    affected = affected_foundation_consumers(architecture, component_ids, execution_plan)
    result = {
        "component_ids": component_ids,
        "module_paths": sorted(modules),
        "paper_evidence_files": sorted({item["path"] for item in evidence}),
        "paper_evidence": sorted(evidence, key=lambda item: item["path"]),
        "causal_change": causal_change,
        "predicted_effect": str(request.get("predicted_effect") or "").strip(),
        "affected_task_ids": affected["task_ids"],
        "affected_execution_unit_ids": affected["execution_unit_ids"],
    }
    encoded = json.dumps(result, ensure_ascii=False, sort_keys=True).encode("utf-8")
    result["request_id"] = hashlib.sha256(encoded).hexdigest()
    return result


def read_foundation_revision_request(
    *,
    sandbox: Path,
    architecture: dict[str, Any],
    execution_plan: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    path = sandbox / FOUNDATION_REVISION_FILENAME
    if not path.exists():
        return None
    if path_is_foundation_link(path) or not path.is_file():
        raise ValueError("unsafe Foundation revision request path")
    return validate_foundation_revision_request(
        json.loads(path.read_text(encoding="utf-8-sig")),
        architecture=architecture,
        evidence_root=sandbox,
        execution_plan=execution_plan,
    )
