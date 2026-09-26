"""Agent-owned recovery routing; dispatch tools execute its instructions."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Iterator, Literal
import os
import time
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from .codex_runner import run_codex_subprocess
from .config import get_config_value
from .artifact_paths import path_is_link
from .json_utils import parse_json_object
from .model_config import resolve_model_config
from .outputs import write_text
from .observations import write_json, record_error
from .prompts import PromptBook
from .progress import PipelineCancelled
from .security import redact_text


class EvidenceReference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    root: str = Field(min_length=1)
    path: str = Field(min_length=1)


class ModeratorDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["1.0"]
    action: Literal["stop", "continue", "revise_writer", "repair_reporter",
                    "retry", "repair_artifacts", "block", "start", "finish", "wait"]
    diagnosis: str = Field(min_length=1)
    instructions: str
    component_ids: list[str]
    task_ids: list[str] = Field(default_factory=list)
    evidence_refs: list[EvidenceReference]
    expected_change: str
    next_node: str = ""
    next_nodes: list[str] = Field(default_factory=list)
    repair_operation: str = ""
    repair_arguments: dict[str, Any] = Field(default_factory=dict)


def _decision_output_schema() -> dict:
    """Encode arbitrary repair arguments without an open object in strict JSON."""
    schema = ModeratorDecision.model_json_schema()
    schema["properties"]["repair_arguments"] = {
        "type": "string",
        "description": "JSON-encoded argument object for the registered repair; use '{}' when unused.",
    }
    # Structured outputs require every property to be present, even when the
    # application accepts older decisions that omit operational fields.
    schema["required"] = list(schema["properties"])
    for field in schema["properties"].values():
        field.pop("default", None)
    return schema


def _parse_decision(text: str) -> dict:
    payload = parse_json_object(text)
    # Some providers encode multiline text as arrays of paragraphs despite the
    # closed wire schema requesting strings. Keep paragraph order, then apply
    # address decoding below without rejecting descriptive content.
    for field in ("diagnosis", "instructions", "expected_change"):
        value = payload.get(field)
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            payload[field] = "\n".join(item.strip() for item in value if item.strip())
    if isinstance(payload.get("repair_arguments"), str):
        try:
            payload["repair_arguments"] = json.loads(payload["repair_arguments"])
        except ValueError:
            pass  # Keep original arguments for the actual tool/owner to handle.
    # The action is consumed by the selected tool; descriptive content is not
    # an admission test and unknown fields remain available to the recipient.
    defaults = {"diagnosis": "", "instructions": "", "expected_change": "",
                "evidence_refs": [], "component_ids": [], "task_ids": [],
                "next_nodes": [], "next_node": "", "repair_operation": "", "repair_arguments": {}}
    return {**defaults, **payload}


def _canonical_start_nodes(decision: dict, ready: list[str], scope_id: str) -> list[str]:
    """Map a tool-qualified wire address back to the advertised local name.

    ``context.ready`` deliberately exposes names local to one tool gate, while
    moderator prompts often describe them as ``execution:environment`` or
    ``backfill:replan``.  This only removes the current gate's prefix when the
    remaining name is already an advertised option; it never selects a tool or
    changes the moderator's routing choice.
    """
    selected = decision.get("next_nodes") or [decision.get("next_node", "")]
    gate = scope_id.rsplit(":", 1)[-1]
    prefix = f"{gate}:"
    canonical: list[str] = []
    for node in selected:
        value = str(node)
        if value.startswith(prefix) and value[len(prefix):] in ready:
            value = value[len(prefix):]
        canonical.append(value)
    return canonical


_CURRENT: ContextVar[Moderator | None] = ContextVar("geng_moderator", default=None)
_CLAIM_LOCK = Lock()  # Reservations only; never held across model calls.
_EXCLUDED = {".git", ".venv", "venv", "node_modules", "__pycache__", "moderator", "supervisor", "runtime_home"}
_TEXT = {".py", ".json", ".jsonl", ".md", ".txt", ".csv", ".toml", ".yaml", ".yml", ".log"}
_MEDIA = {".pdf", ".png", ".jpg", ".jpeg", ".webp"}
_MAX_FILES = 160
_MAX_BYTES = 24 * 1024 * 1024


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _unavailable(status: str, diagnosis: str, decision_id: str = "") -> dict[str, Any]:
    # A transport/recording failure is no moderator decision to stop work.
    return {"schema_version": "1.0", "action": "unavailable", "status": status,
            "diagnosis": diagnosis, "instructions": "", "component_ids": [],
            "evidence_refs": [], "expected_change": "", "decision_id": decision_id}


def _regular_source(root: Path, relative: str) -> Path:
    if root.is_file():
        if relative != root.name or path_is_link(root):
            raise ValueError("evidence file root does not authorize sibling files")
        root = root.parent
    parts = relative.replace("\\", "/").split("/")
    if any(part in {"", ".", ".."} or ":" in part for part in parts):
        raise ValueError("unsafe evidence path")
    current = root
    if path_is_link(root):
        raise ValueError("linked evidence root")
    for part in parts:
        current /= part
        if path_is_link(current):
            raise ValueError("linked evidence path")
    current.resolve(strict=True).relative_to(root.resolve(strict=True))
    if not current.is_file():
        raise ValueError("missing evidence file")
    return current


def _snapshot_evidence(workspace: Path, roots: dict[str, Path], *,
                       preferred_paths: set[str] | None = None) -> tuple[list[dict], list[dict]]:
    """Copy a bounded read-only view, with explicit omissions and source hashes."""
    inventory: list[dict] = []
    omitted: list[dict] = []
    total = 0
    for name, raw_root in sorted(roots.items()):
        if not name or not all(char.isalnum() or char in "_-" for char in name):
            raise ValueError("invalid evidence root name")
        selected = Path(raw_root)
        if path_is_link(selected) or not (selected.is_dir() or selected.is_file()):
            omitted.append({"root": name, "path": ".", "reason": "root_unavailable"})
            continue
        root = selected.parent if selected.is_file() else selected
        # Path.walk is unavailable on some supported runtimes. os.walk never
        # follows linked directories here, including Windows junctions.
        import os

        candidates = [selected] if selected.is_file() else []
        for directory, dirs, files in ([] if selected.is_file() else os.walk(root, followlinks=False)):
            parent = Path(directory)
            dirs[:] = sorted(item for item in dirs if item.lower() not in _EXCLUDED
                             and not path_is_link(parent / item))
            for filename in sorted(files):
                path = parent / filename
                if (path.suffix.lower() not in _TEXT | _MEDIA or filename.lower().startswith(".env")
                        or filename.lower() in {"auth.json", "credentials.json", "id_rsa", "id_ed25519"}):
                    continue
                candidates.append(path)
        # Definitions and current code precede large historical logs/plots.
        candidates.sort(key=lambda path: (
            path.relative_to(root).as_posix() not in (preferred_paths or set()),
            any(part in {"writer_progress", "execution_runs"} for part in path.relative_to(root).parts),
            path.suffix.lower() in _MEDIA, path.suffix.lower() in {".csv", ".log", ".jsonl"},
            path.relative_to(root).as_posix(),
        ))
        for path in candidates:
            relative = path.relative_to(root).as_posix()
            try:
                if (path.suffix.lower() not in _TEXT | _MEDIA or path.name.lower().startswith(".env")
                        or path.name.lower() in {"auth.json", "credentials.json", "id_rsa", "id_ed25519"}):
                    omitted.append({"root": name, "path": relative, "reason": "unsupported_or_private"})
                    continue
                source = _regular_source(selected, relative)
                size = source.stat().st_size
                limit = 8 * 1024 * 1024 if source.suffix.lower() in _MEDIA else 512 * 1024
                if len(inventory) >= _MAX_FILES or size > limit or total + size > _MAX_BYTES:
                    omitted.append({"root": name, "path": relative, "reason": "context_limit"})
                    continue
                content = source.read_bytes()
                original_hash = hashlib.sha256(content).hexdigest()
                if source.suffix.lower() in _TEXT:
                    content = redact_text(content.decode("utf-8-sig")).encode("utf-8")
                # Diagnosis only needs readable files, not an importable copy.
                # Flat names avoid magnifying deep case paths on Windows.
                snapshot_path = f"evidence/{len(inventory):04d}{source.suffix.lower()}"
                destination = workspace / snapshot_path
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)
                inventory.append({"root": name, "path": relative, "snapshot_path": snapshot_path, "sha256": original_hash,
                                  "snapshot_sha256": hashlib.sha256(content).hexdigest()})
                total += len(content)
            except (OSError, ValueError, UnicodeError):
                omitted.append({"root": name, "path": relative, "reason": "unavailable_or_unsafe"})
    return inventory, omitted


def _evidence_unchanged(workspace: Path, roots: dict[str, Path], inventory: list[dict]) -> bool:
    try:
        for item in inventory:
            source = _regular_source(Path(roots[item["root"]]), item["path"])
            snapshot = _regular_source(workspace, item["snapshot_path"])
            if _file_hash(source) != item["sha256"] or _file_hash(snapshot) != item["snapshot_sha256"]:
                return False
        return True
    except (OSError, ValueError, KeyError):
        return False


def _read_record(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


@contextmanager
def _event_claim(event_dir: Path) -> Iterator[bool]:
    """Per-incident OS lease, released even on process death; no global model lock."""
    claim = event_dir / "active.json"
    identity = {"pid": os.getpid(), "token": uuid4().hex}
    acquired = False
    handle = None
    with _CLAIM_LOCK:
        event_dir.mkdir(parents=True, exist_ok=True)
        handle = (event_dir / "lease.lock").open("a+b")
        handle.seek(0, os.SEEK_END)
        if not handle.tell():
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
            write_json(claim, identity)
        except OSError:
            pass
    try:
        yield acquired
    finally:
        if acquired:
            with _CLAIM_LOCK:
                if _read_record(claim).get("token") == identity["token"]:
                    try:
                        claim.unlink(missing_ok=True)
                    except OSError as exc:
                        record_error(claim, exc)
        if handle is not None:
            handle.close()  # OS releases the per-incident lease.


class Moderator:
    def __init__(self, audit_dir: Path, reporter=None, context_provider: Callable[[], dict] | None = None) -> None:
        self.root = audit_dir / "moderator"
        self.reporter = reporter
        self.context_provider = context_provider

    def request(self, *, trigger: str, scope_id: str, state_id: str, context: dict,
                evidence_roots: dict[str, Path], allowed_actions: tuple[str, ...],
                reuse_continue: bool = False, routine: bool = False,
                reuse_decision: bool = False) -> dict:
        event_id = _digest([trigger, scope_id, state_id])
        # Keep audit paths usable on Windows. Full identities remain in the
        # reservation; 128-bit directory keys avoid two nested 64-char names.
        scope_dir = self.root / _digest(scope_id)[:32]
        event_dir = scope_dir / event_id[:32]
        with _event_claim(event_dir) as acquired:
            if not acquired:
                return _unavailable("in_progress", "另一个活跃调用正在处理该节点；等待其真实结果，不重复派发。", event_id)
            previous = _read_record(event_dir / "reservation.json")
            saved = _read_record(event_dir / "decision.json")
            if saved and (reuse_decision or reuse_continue):
                cached = self._reuse_continue(event_dir, evidence_roots, context, allowed_actions,
                                              all_decisions=reuse_decision)
                if cached is not None:
                    return cached
            attempts = int(previous.get("attempts", 0))
            reservation = {**previous, "decision_id": event_id, "scope_id": scope_id,
                           "trigger": trigger, "state_id": state_id, "routine": routine,
                           "created_at": previous.get("created_at", datetime.now(timezone.utc).isoformat()),
                           "invocation": uuid4().hex, "attempts": attempts + 1, "status": "prepared"}
            write_json(event_dir / "reservation.json", reservation)
            attempt_dir = event_dir if attempts == 0 else event_dir / f"attempt_{attempts + 1:02d}"
            while (attempt_dir / "workspace").exists():
                # A lost observation record must not strand the next diagnosis.
                attempts += 1
                attempt_dir = event_dir / f"attempt_{attempts + 1:02d}"
            attempt_dir.mkdir(parents=True, exist_ok=True)
            try:
                return self._diagnose(event_id=event_id, event_dir=event_dir, attempt_dir=attempt_dir,
                                      reservation=reservation, trigger=trigger, scope_id=scope_id, state_id=state_id,
                                      context=context, evidence_roots=evidence_roots, allowed_actions=allowed_actions)
            except BaseException:
                write_json(event_dir / "reservation.json", {**reservation, "status": "interrupted"})
                raise

    def _diagnose(self, *, event_id: str, event_dir: Path, attempt_dir: Path, reservation: dict,
                  trigger: str, scope_id: str, state_id: str, context: dict,
                  evidence_roots: dict[str, Path], allowed_actions: tuple[str, ...]) -> dict:
        workspace = attempt_dir / "workspace"
        started = time.monotonic()
        self._emit("moderator.started", "主持人正在分析异常", event_id, trigger)
        try:
            workspace.mkdir()
            inventory, omitted = _snapshot_evidence(workspace, evidence_roots, preferred_paths={
                str(item.get("file") or "").replace("\\", "/") for item in context.get("findings", [])
                if isinstance(item, dict)
            })
            packet = {"trigger": trigger, "scope_id": scope_id, "state_id": state_id,
                      "allowed_actions": list(allowed_actions), "context": context,
                      "evidence": inventory, "omitted_evidence": omitted,
                      "policy_hash": _file_hash(Path(__file__).with_name("prompts") / "moderate_recovery.md"),
                      "global_state": self.context_provider() if self.context_provider else {}}
            write_text(workspace / "incident.json", redact_text(json.dumps(packet, ensure_ascii=False, indent=2, default=str)))
            packet_hash = _file_hash(workspace / "incident.json")
            write_json(attempt_dir / "decision_schema.json", _decision_output_schema())
            prompt = PromptBook().load("moderate_recovery.md")
            profile = resolve_model_config("moderator")
            if profile.supports_json_schema:
                prompt += ("\nTransport format: include all fields in the supplied output schema. "
                           "Use empty lists/strings for unused operational fields. Encode repair_arguments "
                           "as a JSON object inside a string, using \"{}\" when unused.\n")
            write_json(event_dir / "reservation.json", {**reservation, "status": "dispatched"})
            status = run_codex_subprocess(
                role="moderator", work_dir=workspace, prompt=prompt, audit_dir=attempt_dir,
                label="moderator", sandbox="read-only",
                output_schema=attempt_dir / "decision_schema.json" if profile.supports_json_schema else None,
            )
            message = attempt_dir / "moderator_last_message.txt"
            if not status.get("ok") and not message.is_file():
                decision = _unavailable("model_failed", "主持人没有完成诊断，保留原任务状态。", event_id)
            else:
                if path_is_link(message):
                    raise ValueError("unsafe moderator response")
                decision = _parse_decision(message.read_text(encoding="utf-8-sig"))
                if decision.get("action") == "start":
                    selected = _canonical_start_nodes(decision, context.get("ready", []), scope_id)
                    decision["next_nodes"] = selected
                    decision["next_node"] = selected[0] if len(selected) == 1 else ""
                decision.update(status="decided", decision_id=event_id)
                try:
                    decision["evidence_changed_during_diagnosis"] = (
                        not _evidence_unchanged(workspace, evidence_roots, inventory)
                        or _file_hash(workspace / "incident.json") != packet_hash)
                except Exception as exc:
                    decision["evidence_observation_error"] = redact_text(f"{type(exc).__name__}: {exc}")
                if not status.get("ok"):
                    decision["process_observation"] = status
            decision["workspace"] = workspace.relative_to(event_dir).as_posix()
            write_json(event_dir / "decision.json", decision)
        except PipelineCancelled:
            raise
        except Exception as exc:
            decision = _unavailable("invalid_or_unavailable", f"主持人响应暂时无法读取（{type(exc).__name__}），保留原任务状态。", event_id)
            write_json(attempt_dir / "failure.json", {"error_kind": type(exc).__name__, "error": redact_text(str(exc))[:2000]})
            write_json(event_dir / "decision.json", decision)
        write_json(event_dir / "reservation.json", {**reservation, "status": "completed",
                   "elapsed_s": time.monotonic() - started, "action": decision["action"]})
        self._emit("moderator.completed", decision["diagnosis"], event_id, trigger, decision)
        return decision

    def _reuse_continue(self, event_dir: Path, roots: dict[str, Path], context: dict,
                        allowed_actions: tuple[str, ...], *, all_decisions: bool = False) -> dict | None:
        """Only a completed read-only clearance can be replayed, never a repair."""
        try:
            decision = json.loads((event_dir / "decision.json").read_text(encoding="utf-8"))
            workspace = event_dir / decision.get("workspace", "workspace")
            packet = json.loads((workspace / "incident.json").read_text(encoding="utf-8"))
            if ((decision.get("action") not in {*allowed_actions, "stop"} if all_decisions else decision.get("action") != "continue")
                    or decision.get("status") != "decided"
                    or packet.get("context") != context
                    or packet.get("allowed_actions") != list(allowed_actions)
                    or packet.get("policy_hash") != _file_hash(Path(__file__).with_name("prompts") / "moderate_recovery.md")
                    or not _evidence_unchanged(workspace, roots, packet["evidence"])):
                return None
            return {**decision, "reused": True}
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _emit(self, event_type, message, event_id, trigger, decision=None):
        if self.reporter is not None:
            try:
                self.reporter.emit(event_type, phase="task_reproduction", message=message,
                                   data={"decision_id": event_id, "trigger": trigger,
                                         "action": (decision or {}).get("action")})
            except Exception:
                pass  # Progress display cannot decide scientific recovery.


@contextmanager
def moderator_scope(audit_dir: Path, reporter=None, *, moderator: Moderator | None = None) -> Iterator[Moderator]:
    existing = _CURRENT.get()
    # Nested execution scopes share the run authority and its compact context.
    moderator = moderator or (existing if existing and existing.root == audit_dir / "moderator" else None) or Moderator(audit_dir, reporter)
    token = _CURRENT.set(moderator)
    try:
        yield moderator
    finally:
        _CURRENT.reset(token)


def request_moderation(*, trigger: str, scope_id: str, state_id: str, context: dict,
                       evidence_roots: dict[str, Path], allowed_actions: tuple[str, ...],
                       reuse_continue: bool = False) -> dict:
    moderator = _CURRENT.get()
    if moderator is None:
        return _unavailable("not_active", "当前调用未启用主持人工作流，保留原任务的未解决状态。")
    try:
        return moderator.request(trigger=trigger, scope_id=scope_id, state_id=state_id,
                                 context=context, evidence_roots=evidence_roots, allowed_actions=allowed_actions,
                                 reuse_continue=reuse_continue)
    except PipelineCancelled:
        raise
    except Exception as exc:
        # Failure to create an actual workspace/lease is unavailable coordination,
        # not a scientific stop. Later observation writes are best-effort.
        decision = _unavailable("audit_unavailable", f"主持人审计记录不可用（{type(exc).__name__}），保留原任务与未解决状态。",
                         _digest([trigger, scope_id, state_id]))
        moderator._emit("moderator.completed", decision["diagnosis"], decision["decision_id"], trigger, decision)
        return decision
