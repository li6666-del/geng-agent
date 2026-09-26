"""Route exceptional task repairs without making scientific verdicts."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from .artifact_paths import file_sha256, path_is_link, scan_tree
from .moderator import request_moderation
from .verification_result import rerun_evidence_path_issues






def _context(record: dict, reporter: dict) -> tuple[Path, Path, dict, dict | None]:
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
    return sandbox, workspace, architecture, plan


def recovery_state_id(record: dict, trigger: str) -> str:
    """Changing diagnosis wording never creates a new executable recovery state."""
    sandbox = Path(str(record.get("sandbox") or ""))
    if path_is_link(sandbox):
        raise ValueError("Recovery sandbox must not be a filesystem link")
    inventory: dict[str, str] = {}
    paths = []
    for name in ("outputs", "execution_units", "tasks", "src", "configs"):
        root = sandbox / name
        if not path_is_link(root) and root.is_dir():
            paths.extend(scan_tree(root)[0])
    paths.extend(sandbox / name for name in (
        "environment.lock.json", "environment_lock.json",
        "config.json", "config_smoke.json", "requirements.txt",
    ))
    for path in sorted(set(paths)):
        if (path_is_link(path) or not path.is_file()
                or "__pycache__" in path.parts or path.suffix.lower() in {".pyc", ".pyo"}
                or path.name in {"task_agent_result.json", "task_agent_result.md", "execution_receipt.json"}):
            continue
        inventory[path.relative_to(sandbox).as_posix()] = file_sha256(path)
    receipt = (record.get("host_execution") or {}).get("receipt") or {}
    state = {"trigger": trigger, "files": inventory, "environment_hash": receipt.get("environment_hash"),
             "analysis_snapshot_hash": record.get("analysis_snapshot_hash")}
    return hashlib.sha256(json.dumps(state, sort_keys=True).encode("utf-8")).hexdigest()




def _apply_decision(*, record: dict, reporter: dict, verification: dict, decision: dict,
                    allowed_actions: tuple[str, ...]) -> dict:
    action = decision.get("action")
    if action not in allowed_actions:
        return {"action": "unavailable", "decision": decision, "reason": "moderator_dispatch_unavailable"}
    if action == "repair_reporter":
        return {"action": action, "decision": decision}
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
    sandbox, workspace, architecture, plan = _context(record, reporter)
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
    return _moderate(record=record, reporter=reporter, verification=verification,
                     trigger="reporter_revision_request",
                     allowed_actions=("stop", "revise_writer", "repair_reporter"))
