"""Record routine handoffs and execute the moderator's recovery instructions.

Observations do not approve scientific outputs or impose a repair quota.
Explicit user cancellation and ownership of active processes remain enforced.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from threading import RLock, Event
from typing import Any, Callable, Iterator

from .moderator import (Moderator, _digest, _event_claim, _file_hash, _read_record,
                        _EXCLUDED, _TEXT, _MEDIA, moderator_scope)
from .artifact_paths import path_is_link
from .observations import write_json
from .progress import PipelineCancelled
from .security import redact_text


class NodeFailure(RuntimeError):
    """An operation explicitly could not deliver; scientific non-support is not this."""

    def __init__(self, message: str, result: Any = None) -> None:
        super().__init__(message)
        self.result = result


class StageBlocked(RuntimeError):
    def __init__(self, node_id: str, decision: dict, original_error: BaseException | None = None) -> None:
        self.node_id = node_id
        self.decision = decision
        self.original_error = original_error
        super().__init__(f"{node_id}: {decision.get('diagnosis', '节点暂时无法继续')}")


_CURRENT: ContextVar[RunSupervisor | None] = ContextVar("geng_run_supervisor", default=None)
_STATE_LOCK = RLock()  # Disk state only. Never held while executing a model or owner.
REPLAY_REQUIRED = object()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _compact(value: Any, *, limit: int = 14000) -> Any:
    """A bounded view, never a replacement for the original evidence."""
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    text = redact_text(json.dumps(value, ensure_ascii=False, default=str))
    if len(text) <= limit:
        return json.loads(text)
    return {"preview": text[:limit], "truncated": True, "full_sha256": _digest(value)}


def _evidence_identity(roots: dict[str, Path]) -> list[dict]:
    """Hash the declared node evidence, including missing files and new files."""
    inventory = []
    for name, root in sorted(roots.items()):
        root = Path(root)
        if not (root.is_dir() or root.is_file()) or path_is_link(root):
            inventory.append({"root": name, "unavailable": True})
            continue
        selected_file = root if root.is_file() else None
        if selected_file:
            root = root.parent
        walk = [(str(root), [], [selected_file.name])] if selected_file else os.walk(root, followlinks=False)
        for directory, dirs, files in walk:
            parent = Path(directory)
            dirs[:] = sorted(item for item in dirs if item.lower() not in _EXCLUDED
                             and not path_is_link(parent / item))
            for filename in sorted(files):
                path = parent / filename
                if (path.suffix.lower() not in _TEXT | _MEDIA or path_is_link(path)
                        or filename.lower().startswith(".env")
                        or filename.lower() in {"auth.json", "credentials.json", "id_rsa", "id_ed25519"}):
                    continue
                try:
                    inventory.append({"root": name, "path": path.relative_to(root).as_posix(),
                                      "sha256": _file_hash(path)})
                except OSError:
                    inventory.append({"root": name, "path": path.relative_to(root).as_posix(), "unavailable": True})
    return inventory


class RunSupervisor:
    def __init__(self, output_dir: Path, audit_dir: Path, goal: dict, reporter=None) -> None:
        self.output_dir = Path(output_dir)
        self.audit_dir = Path(audit_dir)
        self.root = self.audit_dir / "supervisor"
        self.reporter = reporter
        self.goal = _compact(goal)
        self.goal_id = _digest(goal)
        self.root.mkdir(parents=True, exist_ok=True)
        write_json(self.root / "goal.json", {"goal": self.goal, "identity": self.goal_id})
        self.moderator = Moderator(self.audit_dir, reporter, context_provider=self.snapshot)
        self._repair_handlers: dict[str, Callable[[dict], Any]] = {}
        self._repair_reconcilers: dict[str, Callable[[dict, dict], Any]] = {}
        self._repair_specs: dict[str, dict] = {}
        self._instructions: dict[str, dict] = {}
        self._results: dict[str, Any] = {}
        self._cancelled = Event()
        self._cancel_message = "用户已停止本次运行"
        from .supervisor_tools import SupervisorTools
        self.tools = SupervisorTools(self)

    def register_repair_handler(self, name: str, handler: Callable[[dict], Any], *,
                                reconcile: Callable[[dict, dict], Any] | None = None,
                                description: str = "", parameters: dict | None = None) -> None:
        if not name or not callable(handler):
            raise ValueError("a named repair implementation is required")
        self._repair_handlers[name] = handler
        if reconcile is not None:
            self._repair_reconcilers[name] = reconcile
        self._repair_specs[name] = {"description": description, "parameters": parameters or {}}

    def record_phase(self, phase: str, status: str, summary: Any = None, error: Any = None) -> dict:
        """Publish root scheduling progress without adding a second approval call."""
        return self._save(f"phase:{phase}", status=status, summary=_compact(summary), error=_compact(error))

    def current_instruction(self, node_id: str) -> dict | None:
        if node_id in self._instructions:
            return self._instructions[node_id]
        assignment = _read_record(self.root / "assignments" / (_digest(node_id)[:20] + ".json"))
        return assignment.get("decision") if assignment.get("goal_id") == self.goal_id else None

    def assign(self, node_id: str, decision: dict) -> None:
        """Persist an explicit owner assignment across process restarts."""
        self._instructions[node_id] = decision
        write_json(self.root / "assignments" / (_digest(node_id)[:20] + ".json"),
                   {"goal_id": self.goal_id, "node_id": node_id, "decision": decision})

    def _node_dir(self, node_id: str) -> Path:
        return self.root / "nodes" / _digest(node_id)[:20]

    def _save(self, node_id: str, **changes: Any) -> dict:
        with _STATE_LOCK:
            path = self._node_dir(node_id) / "state.json"
            record = _read_record(path)
            if "status" in changes and changes.get("status") != record.get("status"):
                record.setdefault("transitions", []).append({"status": changes.get("status"), "at": _now(),
                                                              "attempt": changes.get("attempt", record.get("attempt", 0))})
            record.update(node_id=node_id, updated_at=_now(), goal_id=self.goal_id, **changes)
            write_json(path, record)
            return record

    def snapshot(self) -> dict:
        records = []
        for path in sorted((self.root / "nodes").glob("*/state.json")):
            record = _read_record(path)
            if record.get("goal_id") != self.goal_id:
                continue
            record["state_record"] = path.relative_to(self.audit_dir).as_posix()
            records.append(record)
        # Keep every node addressable. A large completed plan must never turn
        # the whole status board into a truncated string or hide later tasks.
        nodes = [{"node_id": record.get("node_id"), "status": record.get("status"),
                  "attempt": record.get("attempt", 0), "updated_at": record.get("updated_at"),
                  "state_record": record["state_record"]} for record in records]

        def priority(record: dict) -> int:
            status = record.get("status")
            if status in {"blocked", "failed", "interrupted"}:
                return 0
            if status in {"published", "validated"} or (
                    status == "completed" and str(record.get("node_id", "")).startswith("phase:")):
                return 2
            return 1

        def short_text(value: Any, limit: int = 700) -> str:
            # A byte bound also bounds Chinese diagnostics predictably.
            text = redact_text(str(value or ""))
            encoded = text.encode("utf-8")
            return text if len(encoded) <= limit else encoded[:limit].decode("utf-8", errors="ignore") + "…"

        ordered = sorted(records, key=lambda record: str(record.get("updated_at") or ""), reverse=True)
        ordered.sort(key=priority)  # Stable: recent items within each priority.
        selected = []
        completed_details = 0
        for record in ordered:
            if priority(record) == 2:
                if completed_details >= 2:
                    continue
                completed_details += 1
            selected.append(record)
            if len(selected) == 8:
                break
        details = []
        for record in selected:
            detail = {"node_id": record.get("node_id")}
            error = record.get("error")
            if isinstance(error, dict):
                detail["error"] = {"kind": short_text(error.get("error_kind"), 120),
                                   "message": short_text(error.get("message") or error.get("error"))}
            elif error:
                detail["error"] = short_text(error)
            decision = record.get("decision")
            if isinstance(decision, dict):
                detail["decision"] = {"action": decision.get("action"),
                                      "diagnosis": short_text(decision.get("diagnosis"), 500)}
            summary = record.get("summary")
            if isinstance(summary, dict):
                # The current node's incident and declared evidence contain
                # the actual facts/plan. Global memory needs only their shape.
                detail["summary_fields"] = [str(key) for key in list(summary)[:12]]
                detail["summary_counts"] = {str(key): len(value) for key, value in list(summary.items())[:12]
                                            if isinstance(value, (dict, list))}
                if summary.get("truncated") is True:
                    detail["summary_truncated_in_record"] = True
            elif summary is not None:
                detail["summary_type"] = type(summary).__name__
                if isinstance(summary, list):
                    detail["summary_count"] = len(summary)
            details.append(detail)
        from .observations import observation_errors
        return {"goal": self.goal, "nodes": nodes, "node_details": details, "host_observation_errors": observation_errors(),
                "detail_omissions": len(records) - len(details),
                "detail_policy": "All node identities/states are retained. Details prioritize failures and active work; at most two completed summaries. Full scientific documents remain in each node's declared evidence.",
                "repair_operations": self._repair_specs,
                "tool_catalog": self.tools.catalog(), "active_tools": self.tools.active(),
                "process_observations": self.tools.process_observations()}

    def _emit(self, event: str, node_id: str, message: str, **data: Any) -> None:
        if self.reporter is not None:
            try:
                self.reporter.emit(event, phase="supervision", message=message,
                                   data={"node_id": node_id, **data})
            except Exception:
                pass

    def _request(self, node_id: str, *, trigger: str, context: dict, roots: dict[str, Path],
                 actions: tuple[str, ...], routine: bool) -> dict:
        self._check_cancelled()
        binding = {"goal_id": self.goal_id, "context": context, "evidence": _evidence_identity(roots),
                   "policy": _file_hash(Path(__file__).with_name("prompts") / "moderate_recovery.md")}
        identity = _digest(binding)
        # This immutable packet is also citable when the node has no file outputs.
        packet_dir = self._node_dir(node_id) / "packets" / identity[:20]
        packet_dir.mkdir(parents=True, exist_ok=True)
        if not (packet_dir / "handoff.json").exists():
            write_json(packet_dir / "handoff.json", {"node_id": node_id, **binding})
        evidence = {**roots, "handoff": packet_dir}
        decision = self.moderator.request(
            trigger=trigger, scope_id=f"run:{self.goal_id[:20]}:node:{node_id}", state_id=identity, context=context,
            evidence_roots=evidence, allowed_actions=actions, routine=routine, reuse_decision=True,
        )
        self._check_cancelled()
        return decision

    def _check_cancelled(self) -> None:
        if self._cancelled.is_set():
            raise PipelineCancelled(self._cancel_message)
        check = getattr(self.reporter, "check_cancelled", None)
        if callable(check):
            try:
                check()
            except PipelineCancelled as exc:
                self.signal_cancelled(exc)
                raise
            except Exception as exc:
                self._emit("supervisor.observation_error", "cancellation", redact_text(str(exc)))

    def signal_cancelled(self, error: BaseException) -> None:
        if not self._cancelled.is_set() and str(error):
            self._cancel_message = str(error)
        self._cancelled.set()

    def _block(self, node_id: str, decision: dict, error: BaseException | None) -> None:
        self._save(node_id, status="blocked", decision=_compact(decision))
        self._emit("supervisor.blocked", node_id, decision.get("diagnosis", "节点待处理"))
        raise StageBlocked(node_id, decision, error)

    def _apply_repair(self, node_id: str, decision: dict, repair: Callable[[dict], None] | None) -> None:
        action_id = str(decision.get("decision_id") or _digest(decision))
        path = self._node_dir(node_id) / "actions" / action_id[:20] / "state.json"
        record = _read_record(path)
        # Approval is a different decision from the instruction used to create
        # the accepted result. Keep the latter durable for owner cache identity.
        self._save(node_id, owner_instruction=decision)
        if record.get("status") in {"completed", "validated", "published"}:
            # Restore the pending instruction for the owner's own resume checks.
            self._instructions[node_id] = decision
            if decision["action"] == "retry" and repair is not None:
                repair(decision)
            return
        reconciled = REPLAY_REQUIRED
        if record.get("status") == "dispatched" and decision["action"] == "repair_artifacts":
            reconcile = self._repair_reconcilers.get(decision.get("repair_operation", ""))
            if reconcile is None:
                raise NodeFailure("Repair was interrupted and has no resume adapter; choose another repair or handoff.",
                                  result={"action_record": str(path)})
            reconciled = reconcile(decision.get("repair_arguments", {}), record)
        write_json(path, {"status": "prepared", "decision": decision, "created_at": _now()})
        self._instructions[node_id] = decision
        write_json(path, {"status": "dispatched", "decision": decision, "updated_at": _now()})
        if reconciled is not REPLAY_REQUIRED:
            result = reconciled
        elif decision["action"] == "repair_artifacts":
            handler = self._repair_handlers.get(decision.get("repair_operation", ""))
            if handler is None:
                raise ValueError("repair operation is not registered")
            result = handler(decision.get("repair_arguments", {}))
        else:
            if repair is None:
                raise ValueError("this node has no owner repair adapter")
            # This callback prepares the owner's instruction; it must not run
            # experiments or dispatch the owner itself. operation() does that.
            result = repair(decision)
        write_json(path, {"status": "completed", "decision": decision, "result": _compact(result), "updated_at": _now()})

    def _publish_actions(self, node_id: str) -> None:
        for path in (self._node_dir(node_id) / "actions").glob("*/state.json"):
            record = _read_record(path)
            if record.get("status") == "completed":
                write_json(path, {**record, "status": "validated", "validated_at": _now()})
                write_json(path, {**record, "status": "published", "validated_at": _now(), "published_at": _now()})

    def run_node(self, node_id: str, operation: Callable[[], Any], *, inputs: dict | None = None,
                 evidence_roots: dict[str, Path] | None = None, summarize: Callable[[Any], Any] | None = None,
                 repair: Callable[[dict], None] | None = None,
                 degrade: Callable[[dict, BaseException], Any] | None = None,
                 reconcile: Callable[[dict], Any] | None = None,
                 passthrough: tuple[type[BaseException], ...] = ()) -> Any:
        # A caller may fill in an output workspace after operation() completes;
        # preserve that mapping rather than capturing only pre-run paths.
        roots = evidence_roots if evidence_roots is not None else {}
        inputs = _compact(inputs or {})
        node_dir = self._node_dir(node_id)
        with _event_claim(node_dir / "owner") as acquired:
            if not acquired:
                raise StageBlocked(node_id, {"action": "block", "status": "in_progress",
                                             "diagnosis": "该节点仍有活跃执行者，不重复启动。"})
            previous = _read_record(node_dir / "state.json")
            # Owners with external side effects must supply a receipt/cache-aware
            # reconciler. Never infer success or rerun an unknown dispatched job.
            recovered = REPLAY_REQUIRED
            recovery_error = None
            same_inputs = previous.get("inputs") == inputs and previous.get("goal_id") == self.goal_id
            if not same_inputs:
                self._instructions.pop(node_id, None)
                self._save(node_id, owner_instruction=None, decision=None, summary=None)
            previous_instruction = previous.get("owner_instruction") or previous.get("decision")
            if same_inputs and isinstance(previous_instruction, dict):
                previous_decision = previous_instruction
                if previous_decision.get("action") in {"retry", "repair_artifacts"}:
                    self._instructions[node_id] = previous_decision
                    if previous.get("status") == "repairing":
                        try:
                            self._apply_repair(node_id, previous_decision, repair)
                        except PipelineCancelled:
                            raise
                        except Exception as exc:
                            recovery_error = exc
            if same_inputs and previous.get("status") in {"dispatched", "completed", "interrupted"}:
                if node_id in self._results:
                    recovered = self._results[node_id]
                elif reconcile is not None:
                    try:
                        recovered = reconcile(previous)
                    except PipelineCancelled:
                        raise
                    except Exception as exc:
                        recovery_error = exc
                else:
                    recovery_error = NodeFailure("Interrupted node has no resume adapter; decide whether to retry or hand off existing artifacts.", result=previous)
            attempt = int(previous.get("attempt", 0)) if same_inputs else 0
            while True:
                error: Exception | None = None
                self._save(node_id, status="prepared", inputs=inputs, attempt=attempt, error=None)
                try:
                    self._check_cancelled()
                    if recovery_error is not None:
                        pending, recovery_error = recovery_error, None
                        raise pending
                    self._save(node_id, status="dispatched")
                    self._emit("supervisor.node_started", node_id, f"执行节点：{node_id}")
                    if recovered is REPLAY_REQUIRED:
                        self._results.pop(node_id, None)
                    if recovered is REPLAY_REQUIRED:
                        from .supervisor_tools import SupervisorTool
                        tool = SupervisorTool(node_id, str(inputs.get("owner") or node_id),
                            lambda _decision: operation(), inputs=inputs,
                            resume=(lambda _decision: operation()) if reconcile is not None else None,
                            evidence=roots, resources=(node_id,))
                        result = self.tools.call("nodes", tool, self._instructions.get(node_id) or {
                            "authority": "supervisor_selected_capability", "node_id": node_id})
                    else:
                        result = recovered
                    recovered = REPLAY_REQUIRED
                    self._results[node_id] = result
                    try:
                        summary = _compact(summarize(result) if summarize else result)
                    except Exception as exc:
                        summary = {"observation_error": str(exc), "result_available": True}
                    self._save(node_id, status="completed", summary=summary)
                except (StageBlocked, PipelineCancelled, *passthrough):
                    self._save(node_id, status="handed_off")
                    raise
                except Exception as exc:
                    error = exc
                    summary = {"error_kind": type(exc).__name__, "message": redact_text(str(exc))[:5000]}
                    if hasattr(exc, "result"):
                        summary["result"] = _compact(exc.result)
                    self._save(node_id, status="failed", error=summary)
                except BaseException:
                    self._save(node_id, status="interrupted")
                    raise
                if error is None:
                    # A completed owner handoff is a recorded observation, not
                    # a scientific clearance that needs another model call.
                    # Downstream owners receive the result and any observations;
                    # only an operation that cannot hand off enters recovery.
                    self._publish_actions(node_id)
                    self._save(node_id, status="published", decision={
                        "action": "continue", "status": "owner_handoff_recorded",
                        "diagnosis": "Owner completed; downstream review owns interpretation.",
                    })
                    self._emit("supervisor.node_handoff", node_id, "节点交付已记录")
                    return result
                context = {"node_id": node_id, "inputs": inputs, "result": summary,
                           "operation_completed": error is None, "attempt": attempt,
                           "previous_instruction": _compact(self._instructions.get(node_id)),
                           "repair_operations": sorted(self._repair_handlers),
                           "repair_tools": self._repair_specs}
                actions = ["retry"]
                if degrade is not None:
                    actions.append("continue")
                if self._repair_handlers:
                    actions.append("repair_artifacts")
                actions.append("block")
                # Failed repair/partial-handoff tools return to the moderator;
                # they are not host-authored stop decisions or automatic reruns.
                coordination_round = 0
                while error is not None:
                    coordination_round += 1
                    context["coordination_round"] = coordination_round
                    decision = self._request(node_id, trigger="node_failed",
                        context=context, roots=roots, actions=tuple(actions), routine=False)
                    if decision.get("action") in {"stop", "block"}:
                        self._block(node_id, decision, error)
                    if decision.get("status") in {"model_failed", "invalid_or_unavailable", "not_active", "in_progress"}:
                        raise StageBlocked(node_id, {**decision, "action": "wait",
                            "diagnosis": "主持人暂不可用；已运行任务和产物保留，等待恢复协调。"}, error)
                    try:
                        if decision.get("action") == "continue" and degrade is not None:
                            partial = degrade(decision, error)
                            self._results[node_id] = partial
                            try:
                                partial_summary = _compact(summarize(partial) if summarize else partial)
                            except Exception as exc:
                                partial_summary = {"observation_error": str(exc), "result_available": True}
                            self._save(node_id, status="published", summary=partial_summary,
                                       decision=_compact(decision), degraded=True, operation_completed=False)
                            self._emit("supervisor.node_handoff", node_id, "主持人保留已有成果并继续交接")
                            return partial
                        if decision.get("action") not in {"retry", "repair_artifacts"}:
                            raise ValueError("No available tool for this instruction; choose an offered action.")
                        attempt += 1
                        self._save(node_id, status="repairing", decision=_compact(decision), attempt=attempt)
                        if decision.get("action") == "retry" and repair is None:
                            self._instructions[node_id] = decision
                        else:
                            self._apply_repair(node_id, decision, repair)
                        error = None
                    except PipelineCancelled:
                        self._save(node_id, status="interrupted")
                        raise
                    except Exception as exc:
                        error = exc
                        context = {**context, "attempt": attempt, "previous_instruction": decision,
                                   "repair_error": {"kind": type(exc).__name__, "message": redact_text(str(exc))}}
                        self._save(node_id, status="failed", error=context["repair_error"])


def current_supervisor() -> RunSupervisor | None:
    return _CURRENT.get()


@contextmanager
def supervisor_scope(supervisor: RunSupervisor) -> Iterator[RunSupervisor]:
    token = _CURRENT.set(supervisor)
    try:
        with moderator_scope(supervisor.audit_dir, supervisor.reporter, moderator=supervisor.moderator):
            yield supervisor
    finally:
        _CURRENT.reset(token)


def supervised_call(node_id: str, operation: Callable[[], Any], **kwargs: Any) -> Any:
    supervisor = current_supervisor()
    return operation() if supervisor is None else supervisor.run_node(node_id, operation, **kwargs)
