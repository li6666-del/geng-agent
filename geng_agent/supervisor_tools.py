"""Callable capabilities and an event-driven controller.

The host advances routine work and records what happened. Ambiguous failures
still go to the moderator for recovery routing. The controller owns process-safe
action claims, cancellation and durable events.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from contextvars import copy_context, ContextVar
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Any, Callable
import time
from uuid import uuid4

from .moderator import _digest, _event_claim, _read_record
from .observations import write_json
from .progress import PipelineCancelled
from .security import redact_text

_CONTROL_SCOPE: ContextVar[str | None] = ContextVar("supervisor_control_scope", default=None)


@dataclass
class SupervisorTool:
    name: str
    description: str
    invoke: Callable[[dict], Any]
    inputs: dict = field(default_factory=dict)
    # Resume must consult the owner's receipts/cache, never mere file presence.
    resume: Callable[[dict], Any] | None = None
    evidence: dict[str, Path] = field(default_factory=dict)
    # Tools with the same write resource cannot run concurrently. Different
    # Writer/Reporter workspaces can. The model cannot waive this constraint.
    resources: tuple[str, ...] = ()

    def describe(self) -> dict:
        return {"name": self.name, "description": self.description,
                "inputs": self.inputs, "resumable": self.resume is not None,
                "resources": list(self.resources)}


class SupervisorTools:
    def __init__(self, supervisor) -> None:
        self.supervisor = supervisor
        self.root = supervisor.root / "tools"
        self.events = supervisor.root / "events"
        self._lock = RLock()
        self._catalog: dict[str, dict] = {}
        self._controllers: dict[str, str | None] = {}
        self._child_finished: dict[str, float] = {}
        self._active: dict[tuple[str, str], float] = {}
        self._processes: dict[int, dict] = {}

    def observe_process(self, pid, **observation):
        from .supervisor import _now
        with self._lock:
            record = {**self._processes.get(pid, {}), "pid": pid, "scope": _CONTROL_SCOPE.get(),
                      "observed_at": _now(), **observation}
            self._processes[pid] = record
            write_json(self.supervisor.root / "processes" / f"{pid}.json", record)

    def process_observations(self):
        with self._lock:
            active = [dict(value) for value in self._processes.values() if value.get("status") == "running"]
            finished = [dict(value) for value in self._processes.values() if value.get("status") != "running"]
            return active + sorted(finished, key=lambda item: item.get("observed_at", ""))[-4:]

    @contextmanager
    def _controller_scope(self, scope):
        parent = _CONTROL_SCOPE.get()
        token = _CONTROL_SCOPE.set(scope)
        with self._lock:
            self._controllers[scope] = parent
        try:
            yield
        finally:
            with self._lock:
                self._controllers.pop(scope, None)
                if parent is not None:
                    self._child_finished[parent] = time.monotonic()
            _CONTROL_SCOPE.reset(token)

    def _heartbeat_due(self, scope, last_wake, check_interval):
        with self._lock:
            return scope not in self._controllers.values() and time.monotonic() - max(
                last_wake, self._child_finished.get(scope, 0)) >= check_interval

    @contextmanager
    def _cancel_workers_on_interrupt(self):
        # Set the shared signal before ThreadPoolExecutor waits for workers.
        try:
            yield
        except (PipelineCancelled, KeyboardInterrupt) as exc:
            self.supervisor.signal_cancelled(exc)
            raise

    def event(self, kind: str, scope: str, **data: Any) -> dict:
        from .supervisor import _compact, _now
        with self._lock:
            event = {"event_id": uuid4().hex, "goal_id": self.supervisor.goal_id,
                     "kind": kind, "scope": scope, "at": _now(), **_compact(data)}
            write_json(self.events / (event["event_id"] + ".json"), event)
        self.supervisor._emit("supervisor." + kind, scope, str(data.get("message") or kind),
                              event_id=event["event_id"])
        return event

    def register(self, scope: str, tool: SupervisorTool) -> None:
        from .supervisor import _compact
        with self._lock:
            description = {**tool.describe(), "inputs": _compact(tool.inputs)}
            key = f"{scope}:{tool.name}"
            if self._catalog.get(key) != description:
                self._catalog[key] = description
                write_json(self.root / "catalog.json", self._catalog)

    def catalog(self) -> list[dict]:
        with self._lock:
            return [{"address": key, "name": value.get("name"), "resumable": value.get("resumable")}
                    for key, value in self._catalog.items()]

    def active(self) -> list[dict]:
        now = time.monotonic()
        with self._lock:
            return [{"scope": scope, "tool": name, "elapsed_s": round(now - started, 1),
                     "observation": "invocation_still_active_not_execution_success"}
                    for (scope, name), started in self._active.items()]

    def status(self, scope: str, name: str) -> dict:
        return _read_record(self.root / _digest([scope, name])[:20] / "state.json")

    def call(self, scope: str, tool: SupervisorTool, decision: dict) -> Any:
        """Execute exactly one selected capability; never interpret its failure."""
        from .supervisor import StageBlocked, _compact
        self.register(scope, tool)
        directory = self.root / _digest([scope, tool.name])[:20]
        identity = _digest({"goal": self.supervisor.goal_id, "inputs": tool.inputs})
        with _event_claim(directory) as acquired:
            if not acquired:
                raise StageBlocked(tool.name, {"action": "block", "status": "in_progress",
                    "diagnosis": "工具已有活跃执行者；保留其现场，不重复启动。"})
            previous = self.status(scope, tool.name)
            interrupted = previous.get("status") in {"running", "interrupted"}
            if interrupted and tool.resume is None and decision.get("action") != "retry":
                from .supervisor import NodeFailure
                raise NodeFailure("该工具上次执行中断且没有对账适配器；请主持人决定是否重试或交付已有结果。")
            action = {"name": tool.name, "scope": scope, "identity": identity,
                      "goal_id": self.supervisor.goal_id, "decision": decision,
                      "attempt": int(previous.get("attempt", 0)) + 1,
                      "status": "running", "inputs": _compact(tool.inputs)}
            history_path = directory / f"attempt_{action['attempt']:04d}.json"
            self.supervisor._instructions[f"tool:{scope}:{tool.name}"] = decision
            write_json(directory / "state.json", action)
            write_json(history_path, action)
            self.event("tool_started", scope, tool=tool.name, attempt=action["attempt"],
                       message=f"启动工具：{tool.name}", resumed=interrupted)
            with self._lock:
                self._active[(scope, tool.name)] = time.monotonic()
            try:
                self.supervisor._check_cancelled()
                result = (tool.resume if interrupted and tool.resume is not None else tool.invoke)(decision)
                self.supervisor._check_cancelled()
            except BaseException as exc:
                if isinstance(exc, (PipelineCancelled, KeyboardInterrupt)):
                    self.supervisor.signal_cancelled(exc)
                action.update(status="interrupted" if isinstance(exc, (PipelineCancelled, KeyboardInterrupt, SystemExit)) else "failed",
                              error=redact_text(f"{type(exc).__name__}: {exc}")[:5000])
                write_json(directory / "state.json", action)
                write_json(history_path, action)
                self.event("tool_" + action["status"], scope, tool=tool.name, error=action["error"])
                raise
            finally:
                with self._lock:
                    self._active.pop((scope, tool.name), None)
            action.update(status="completed")
            write_json(directory / "state.json", action)
            write_json(history_path, action)
            self.event("tool_completed", scope, tool=tool.name, message=f"工具完成：{tool.name}")
            return result

    def run(self, scope: str, offer: Callable[[], list[SupervisorTool]], *,
            state: Callable[[], dict], on_result: Callable[[str, Any], None],
            on_error: Callable[[str, Exception], None], poll_seconds: float = 0.2,
            check_interval: float = 300.0,
            concurrency: int | None = None,
            on_dispatch: Callable[[list[str], dict], None] | None = None,
            routine_selector: Callable[[dict], dict | None] | None = None) -> dict:
        """Drive a capability session. Offers express prerequisites, not order.

        Results are reduced on the controller thread. A start may contain a
        batch. Ordinary handoffs use the caller's deterministic selector;
        exceptional routing wakes the moderator. A heartbeat only observes.
        """
        from .supervisor import StageBlocked, _compact
        history: list[dict] = []
        running = {}
        invocation = uuid4().hex
        last_wake = time.monotonic()
        terminal = None
        dispatched = 0
        with _event_claim(self.root / ("controller_" + _digest(scope)[:20])) as acquired:
            if not acquired:
                raise StageBlocked(scope, {"action": "block", "diagnosis": "本范围已有主持人调度器运行。"})
            # Worker operations retain the calling model config, supervisor and
            # activity contexts. No state/catalog lock spans a model invocation.
            with self._controller_scope(scope), ThreadPoolExecutor(
                    max_workers=max(1, concurrency or len(offer())), thread_name_prefix="supervisor-tool") as pool, self._cancel_workers_on_interrupt():
                while True:
                    self.supervisor._check_cancelled()
                    finished = [future for future in running if future.done()]
                    for future in finished:
                        tool = running.pop(future)
                        try:
                            value = future.result()
                            on_result(tool.name, value)
                        except PipelineCancelled:
                            raise
                        except Exception as exc:
                            on_error(tool.name, exc)
                            history.append({"tool": tool.name, "status": "failed",
                                            "error": redact_text(str(exc))[:3000]})
                        else:
                            history.append({"tool": tool.name, "status": "completed"})
                    if terminal is not None:
                        if not running:
                            return terminal
                        wait(running, timeout=poll_seconds, return_when=FIRST_COMPLETED)
                        continue
                    tools = {tool.name: tool for tool in offer()}
                    busy = {resource for tool in running.values() for resource in tool.resources}
                    active_names = {tool.name for tool in running.values()}
                    ready = {name: tool for name, tool in tools.items()
                             if name not in active_names and not busy.intersection(tool.resources)}
                    for tool in tools.values():
                        self.register(scope, tool)
                    # Once told to wait, do not wake repeatedly for unchanged
                    # available work. Completion or the explicit heartbeat wakes.
                    should_wake = not running or bool(finished) or (
                        self._heartbeat_due(scope, last_wake, check_interval))
                    if not should_wake:
                        wait(running, timeout=poll_seconds, return_when=FIRST_COMPLETED)
                        continue
                    observation = self.event("wake", scope, ready=list(ready), active=sorted(active_names),
                                             reason="completion" if finished else "check" if running else "dispatch")
                    context = {"ready": list(ready), "tools": [
                                   {**tool.describe(), "inputs": {key: _compact(value, limit=6000)
                                    for key, value in tool.inputs.items()}} for tool in ready.values()],
                               "active": sorted(active_names), "state": {
                                   key: _compact(value, limit=6000) for key, value in state().items()},
                               "active_observations": self.active(),
                               "process_observations": self.process_observations(),
                               "history": history[-12:], "event_id": observation["event_id"],
                               "invocation": invocation, "dispatched_actions": dispatched,
                               "purpose": "自主安排工具。可并行启动互不冲突的工具；有活跃工作可等待；可明确保留未完成事项后结束。"}
                    actions = (("start",) if ready else ()) + (("wait",) if running else ("finish",))
                    evidence = {"e_" + _digest(str(path))[:16]: path for tool in ready.values()
                                for path in tool.evidence.values()}
                    context["tool_evidence"] = {
                        name: {key: "e_" + _digest(str(path))[:16] for key, path in tool.evidence.items()}
                        for name, tool in ready.items() if tool.evidence}
                    decision = routine_selector(context) if routine_selector is not None else None
                    if decision is None and actions == ("wait",):
                        decision = {"action": "wait", "status": "routine_wait",
                                    "diagnosis": "An active tool has not completed."}
                    if decision is None:
                        decision = self.supervisor._request("tools:" + scope, trigger="tool_dispatch",
                            context=context, roots=evidence, actions=actions, routine=False)
                    elif decision.get("action") not in actions:
                        decision = self.supervisor._request("tools:" + scope, trigger="dispatch_unavailable",
                            context={**context, "unavailable_instruction": decision}, roots=evidence,
                            actions=actions, routine=False)
                    last_wake = time.monotonic()
                    if decision.get("action") == "unavailable":
                        # Drain in-flight tools before returning the unresolved
                        # coordination state. Never turn transport loss into a
                        # model-authored stop, or spin on paid model retries.
                        terminal = decision
                        continue
                    if decision.get("action") in {"stop", "block", "finish"}:
                        if running:
                            # Do not silently abandon running side effects.
                            terminal = decision
                            wait(running, timeout=poll_seconds, return_when=FIRST_COMPLETED)
                            continue
                        return decision
                    if decision.get("action") == "wait" and running:
                        continue
                    selected = decision.get("next_nodes") or [decision.get("next_node")]
                    if decision.get("action") != "start" or not selected or any(name not in ready for name in selected):
                        history.append({"status": "dispatch_unavailable", "decision": decision,
                                        "error": "No selected tool address is currently executable; clarify the instruction."})
                        continue
                    selected = list(dict.fromkeys(selected))
                    selected_resources: set[str] = set()
                    executable = []
                    for name in selected:
                        tool = ready[name]
                        if selected_resources.intersection(tool.resources):
                            # Keep conflicting work in the offer for the next batch.
                            continue
                        selected_resources.update(tool.resources)
                        executable.append(name)
                    selected = executable
                    self.event("decision", scope, decision=decision)
                    if on_dispatch is not None:
                        on_dispatch(selected, decision)
                    for name in selected:
                        tool = ready[name]
                        running[pool.submit(copy_context().run, self.call, scope, tool, decision)] = tool
                        dispatched += 1
