"""Route exceptional task repairs without making scientific verdicts."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from .foundation_revision import validate_foundation_revision_request
from .foundation_scope import derive_foundation_scope
from .foundation_snapshot import file_sha256, path_is_foundation_link, scan_foundation_tree
from .moderator import request_moderation
from .verification_result import rerun_evidence_path_issues


def _target_path(value: Any) -> str | None:
    raw = str(value or "").strip().strip("`").replace("\\", "/")
    if not raw or PureWindowsPath(raw).drive or raw.startswith("/") or "\x00" in raw:
        return None
    # The suffix is a function, configuration key or line reference. Ownership
    # is the exact containing file; a repair may legitimately add a new function.
    path = raw.partition(":")[0]
    if path.startswith("./"):
        path = path[2:]
    if any(part in {"", ".", ".."} for part in path.split("/")):
        return None
    return PurePosixPath(path).as_posix()


def resolve_targets(*, targets: list, architecture: dict, execution_plan: dict | None,
                    foundation_present: bool) -> dict:
    """Match component IDs or complete module paths, never prose substrings."""
    components = {str(item["id"]): item for item in architecture.get("components", [])
                  if isinstance(item, dict) and item.get("id")}
    scope = derive_foundation_scope(architecture, execution_plan)
    shared = set(scope["component_ids"]) if foundation_present else set()
    owners: set[str] = set()
    selected: set[str] = set()
    unresolved: list[str] = []
    for raw in targets:
        target = _target_path(raw)
        matches = set()
        for component_id, component in components.items():
            module = _target_path(component.get("module"))
            aliases = {component_id, module}
            if module and module.endswith(".py"):
                aliases.add(module[:-3].replace("/", "."))
            if target is not None and target in aliases:
                matches.add(component_id)
        if matches:
            selected.update(matches & shared)
            owners.update("shared" if value in shared else "private" for value in matches)
        elif target and foundation_present and (
            target.startswith("configs/foundation") or (target.startswith("src/") and not components)
        ):
            # Shared configuration lacks a component ID of its own, and a
            # missing architecture cannot establish ownership of frozen code.
            unresolved.append(str(raw))
        elif target and (target.startswith(("tasks/", "src/", "configs/"))
                         or target in {"config.json", "config_smoke.json", "requirements.txt"}):
            owners.add("private")
        else:
            unresolved.append(str(raw))
    owner = next(iter(owners)) if len(owners) == 1 and not unresolved else "ambiguous"
    return {"owner": owner, "component_ids": sorted(selected), "unresolved": unresolved,
            "target_count": len(targets)}


def _context(record: dict, reporter: dict) -> tuple[Path, Path, dict, dict | None, bool]:
    sandbox = Path(str(record.get("sandbox") or ""))
    workspace = Path(str(reporter.get("workspace") or sandbox))
    directory = sandbox / "paper_evidence" / "analysis_artifacts"
    architecture: dict = {}
    plan = None
    for name in ("scientific_architecture.json", "execution_plan.json"):
        path = directory / name
        if path.is_file():
            try:
                value = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                continue
            if isinstance(value, dict):
                if name == "scientific_architecture.json":
                    architecture = value
                else:
                    plan = value
    return sandbox, workspace, architecture, plan, (sandbox / "foundation_manifest.json").is_file()


def recovery_state_id(record: dict, trigger: str) -> str:
    """Changing diagnosis wording never creates a new executable recovery state."""
    sandbox = Path(str(record.get("sandbox") or ""))
    if path_is_foundation_link(sandbox):
        raise ValueError("Recovery sandbox must not be a filesystem link")
    inventory: dict[str, str] = {}
    paths = []
    for name in ("outputs", "execution_units", "tasks", "src", "configs"):
        root = sandbox / name
        if not path_is_foundation_link(root) and root.is_dir():
            paths.extend(scan_foundation_tree(root)[0])
    paths.extend(sandbox / name for name in (
        "environment.lock.json", "environment_lock.json", "foundation_manifest.json",
        "config.json", "config_smoke.json", "requirements.txt",
    ))
    for path in sorted(set(paths)):
        if (path_is_foundation_link(path) or not path.is_file()
                or "__pycache__" in path.parts or path.suffix.lower() in {".pyc", ".pyo"}
                or path.name in {"task_agent_result.json", "task_agent_result.md", "execution_receipt.json"}):
            continue
        inventory[path.relative_to(sandbox).as_posix()] = file_sha256(path)
    receipt = (record.get("host_execution") or {}).get("receipt") or {}
    state = {"trigger": trigger, "files": inventory, "environment_hash": receipt.get("environment_hash"),
             "analysis_snapshot_hash": record.get("analysis_snapshot_hash")}
    return hashlib.sha256(json.dumps(state, sort_keys=True).encode("utf-8")).hexdigest()


def _foundation_request(*, record: dict, reporter: dict, verification: dict,
                        component_ids: list[str], decision: dict | None = None) -> dict:
    sandbox, workspace, architecture, plan, present = _context(record, reporter)
    if not present:
        raise ValueError("No frozen shared Foundation is available")
    evidence = verification.get("rerun_evidence")
    evidence = evidence if isinstance(evidence, dict) else {}
    request = validate_foundation_revision_request(
        {"component_ids": component_ids, "paper_evidence_files": evidence.get("paper_evidence_files"),
         "causal_change": evidence.get("causal_change"),
         "predicted_effect": evidence.get("predicted_effect")},
        architecture=architecture, execution_plan=plan, evidence_root=workspace,
    )
    request["evidence_root"] = str(workspace)
    if decision:
        request["moderator_recovery"] = {key: decision.get(key) for key in
                                         ("decision_id", "instructions", "expected_change")}
    return request


def _apply_decision(*, record: dict, reporter: dict, verification: dict, decision: dict,
                    allowed_actions: tuple[str, ...]) -> dict:
    action = decision.get("action")
    if action not in allowed_actions:
        return {"action": "stop", "decision": decision, "reason": "moderator_action_outside_allowed_scope"}
    if action == "repair_reporter":
        return {"action": action, "decision": decision}
    if action == "revise_foundation":
        try:
            request = _foundation_request(record=record, reporter=reporter, verification=verification,
                                          component_ids=decision.get("component_ids") or [], decision=decision)
        except (ValueError, OSError):
            return {"action": "stop", "decision": decision, "reason": "invalid_moderated_foundation_request"}
        return {"action": action, "request": request, "decision": decision}
    if action == "revise_writer":
        feedback = deepcopy(verification)
        # Preserve the Reporter's scientific conclusion and causal evidence.
        # Moderator guidance is additional implementation advice, not a verdict.
        feedback["moderator_instructions"] = {key: deepcopy(decision.get(key)) for key in
                                             ("decision_id", "diagnosis", "instructions", "expected_change", "evidence_refs")}
        return {"action": action, "feedback": feedback, "decision": decision}
    return {"action": "stop", "decision": decision}


def _moderate(*, record: dict, reporter: dict, verification: dict, trigger: str,
              allowed_actions: tuple[str, ...], routing: dict | None = None) -> dict:
    sandbox, workspace, architecture, plan, _ = _context(record, reporter)
    task_id = str(record.get("task_id") or verification.get("task_id") or "")
    unit_id = str(record.get("execution_unit_id") or "")
    decision = request_moderation(
        trigger=trigger, scope_id=f"{unit_id}:task:{task_id}" if unit_id else f"task:{task_id}",
        state_id=recovery_state_id(record, trigger),
        context={"task_id": record.get("task_id"), "reporter_verification": verification,
                 "ownership": routing, "architecture": architecture, "execution_plan": plan,
                 "writer_status": record.get("writer_status"),
                 "constraint": "Preserve the assigned goal and Reporter scientific conclusion; identify an evidence-backed owner or concrete implementation repair."},
        evidence_roots={"writer": sandbox, "reporter": workspace}, allowed_actions=allowed_actions,
    )
    record.setdefault("moderator_decisions", []).append(decision)
    return _apply_decision(record=record, reporter=reporter, verification=verification,
                           decision=decision, allowed_actions=allowed_actions)


def resolve_revision_owner(*, record: dict, reporter: dict, verification: dict) -> dict:
    _, _, architecture, plan, present = _context(record, reporter)
    evidence = verification.get("rerun_evidence")
    evidence = evidence if isinstance(evidence, dict) else {}
    targets = evidence.get("change_targets")
    routing = resolve_targets(targets=targets if isinstance(targets, list) else [], architecture=architecture,
                              execution_plan=plan, foundation_present=present)
    routing["evidence_observations"] = rerun_evidence_path_issues(verification, reporter.get("workspace"))
    return _moderate(record=record, reporter=reporter, verification=verification,
                     trigger="reporter_revision_request", routing=routing,
                     allowed_actions=("stop", "revise_writer", "revise_foundation", "repair_reporter"))


def recover_writer_stall(*, record: dict, verification: dict, trigger: str,
                         writer_budget_available: bool) -> dict:
    reporter = record.get("task_reporter") or {}
    actions = ("stop", "revise_foundation", "repair_reporter")
    if writer_budget_available:
        actions += ("revise_writer",)
    return _moderate(record=record, reporter=reporter, verification=verification,
                     trigger=trigger, allowed_actions=actions)
