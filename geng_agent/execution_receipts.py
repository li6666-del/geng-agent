"""Host observations binding scientific outputs to an actual process and inputs.

The writer can request an execution but cannot supply its exit status or the
host's receipt. The same launcher is portable; standalone receipts are clearly
distinguished from receipts observed by the orchestration host.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .outputs import _io_path
from .observations import write_json


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _runtime_prefix(python_executable: Path) -> Path:
    parent = python_executable.parent
    return parent.parent if parent.name.lower() in {"bin", "scripts"} else parent


def _observed_runtime_distributions(
    python_executable: Path, roots: list[str], inventory: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Bind imported module names to distributions while that runtime exists."""
    from packaging.utils import canonicalize_name
    from .writer_lineage import runtime_distribution_metadata

    _graph, names, versions = runtime_distribution_metadata(_runtime_prefix(python_executable))
    aliases = {"sklearn": "scikit-learn", "yaml": "pyyaml", "pil": "pillow", "pytorch": "torch"}
    executed_versions = {canonicalize_name(str(item[0])): str(item[1])
                         for item in (inventory or {}).get("packages", [])
                         if isinstance(item, (list, tuple)) and len(item) == 2}
    consumed: set[str] = set()
    for imported in roots:
        name = str(imported).split(".")[0]
        consumed.update(names.get(name, set()))
        canonical = canonicalize_name(name)
        canonical = aliases.get(canonical, canonical)
        if canonical in executed_versions:
            consumed.add(canonical)
    return {name: executed_versions.get(name, versions.get(name, "")) for name in sorted(consumed)}


def probe_execution_environment(python: Path) -> dict[str, Any]:
    """Observe the selected runtime's installed versions without importing science."""
    from .security_env import build_safe_env
    started = time.monotonic()
    script = ("import importlib.metadata as m,json,sys;print(json.dumps({"
              "'python':sys.version,'packages':sorted([(d.metadata.get('Name','').lower(),d.version) "
              "for d in m.distributions()])},sort_keys=True))")
    try:
        completed = subprocess.run([str(python), "-I", "-c", script], env=build_safe_env(),
                                   capture_output=True, text=True, timeout=30, check=True)
        inventory = json.loads(completed.stdout)
        digest = hashlib.sha256(json.dumps(inventory, sort_keys=True).encode()).hexdigest()
        return {"ok": True, "sha256": digest, "inventory": inventory,
                "duration_s": round(time.monotonic() - started, 4)}
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return {"ok": False, "sha256": None, "error": f"{type(exc).__name__}: {exc}",
                "duration_s": round(time.monotonic() - started, 4)}


def _installed_distribution_versions(items: Any) -> dict[str, str]:
    """Compare runtime inventories without judging any package's scientific use."""
    from packaging.utils import canonicalize_name

    versions: dict[str, str] = {}
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict):
            name, version = item.get("distribution"), item.get("version")
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            name, version = item
        else:
            continue
        if str(name or "").strip():
            versions[canonicalize_name(str(name))] = str(version or "")
    return versions


def _inside(root: Path, relative: str) -> Path:
    path = root / relative
    if Path(relative).is_absolute() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("execution path escapes project")
    for part in (path, *path.parents):
        if part == root.parent:
            break
        if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
            raise ValueError("execution path contains a link")
    if path.is_file() and path.stat().st_nlink > 1:
        raise ValueError("execution path contains a hard-linked file")
    return path


def source_hashes(root: Path) -> dict[str, str]:
    paths = [p for name in ("tasks", "src", "configs") for p in (root / name).rglob("*")]
    paths += [root / name for name in ("config.json", "config_smoke.json", "requirements.txt", "tasks_manifest.json")]
    paths += list(root.glob("*.py"))
    return {p.relative_to(root).as_posix(): file_hash(_inside(root, p.relative_to(root).as_posix()))
            for p in sorted(set(paths)) if p.is_file() and "__pycache__" not in p.parts
            and p.suffix not in {".pyc", ".pyo"}}


def artifact_hashes(root: Path, output_subdir: str) -> dict[str, str]:
    output = _inside(root, f"outputs/{output_subdir}")
    # Result notes and report artwork may be written after execution. Numerical
    # results, scientific figures and checkpoints remain bound byte for byte.
    return {p.relative_to(root).as_posix(): file_hash(_inside(root, p.relative_to(root).as_posix()))
            for p in sorted(output.rglob("*")) if p.is_file()
            and p.name not in {"execution_receipt.json", "task_agent_result.json", "task_agent_result.md"}
            and p.suffix.lower() not in {".md", ".log"}}


def trusted_input_snapshot(root: Path, names: tuple[str, ...]) -> dict[str, str]:
    paths = [p for name in names for p in (root / name).rglob("*") if p.is_file()]
    return {p.relative_to(root).as_posix(): file_hash(_inside(root, p.relative_to(root).as_posix())) for p in paths}


def _persistent_asset_stats(root: Path) -> dict[str, tuple[int, int]]:
    result = {}
    for path in (root / "execution_units").rglob("*"):
        if path.is_file():
            relative = path.relative_to(root).as_posix()
            metadata = _inside(root, relative).stat()
            result[relative] = (metadata.st_size, metadata.st_mtime_ns)
    return result


def _configuration_file_inputs(root: Path, value: Any) -> set[str]:
    if isinstance(value, dict):
        return set().union(*(_configuration_file_inputs(root, item) for item in value.values())) if value else set()
    if isinstance(value, list):
        return set().union(*(_configuration_file_inputs(root, item) for item in value)) if value else set()
    if isinstance(value, str):
        try:
            path = _inside(root, value)
            if path.is_file():
                return {path.relative_to(root).as_posix()}
        except (ValueError, OSError):
            pass
    return set()


def validate_receipt(root: Path, receipt: dict[str, Any], *, task_id: str) -> dict[str, Any]:
    issues: list[str] = []
    unobserved: list[str] = []
    image_suffixes = {".png", ".jpg", ".jpeg", ".svg", ".pdf", ".eps", ".webp"}
    if receipt.get("task_id") != task_id or receipt.get("returncode") != 0:
        issues.append("no successful observed process for this task")
    if receipt.get("mode") != "full":
        issues.append("smoke execution does not establish a full result")
    try:
        for relative, expected in receipt.get("source_hashes", {}).items():
            if file_hash(_inside(root, relative)) != expected:
                issues.append(f"source or configuration changed after execution: {relative}")
        if receipt.get("inputs_stable") is not True:
            issues.append("source, configuration, runtime environment or consumed inputs changed during execution")
        issues.extend(str(issue) for issue in receipt.get("dependency_issues", []))
        for relative, expected in receipt.get("input_hashes", {}).items():
            if file_hash(_inside(root, relative)) != expected:
                issues.append(f"input changed after execution: {relative}")
        outputs = receipt.get("output_hashes") or {}
        if not outputs:
            issues.append("observed process produced no scientific artifacts")
        current = artifact_hashes(root, str(receipt.get("output_subdir") or task_id))
        unobserved = sorted(p for p, digest in current.items() if outputs.get(p) != digest)
        new_data = [p for p in current.keys() - outputs.keys() if Path(p).suffix.lower() not in image_suffixes]
        if new_data:
            issues.append("scientific artifacts added outside the observed process: " + ", ".join(sorted(new_data)))
        for relative, expected in outputs.items():
            if Path(relative).suffix.lower() in image_suffixes:
                # Presentation work may replot existing measurements. Its new
                # bytes are not observed evidence, but do not invalidate data.
                continue
            if current.get(relative) != expected:
                issues.append(f"output changed after execution: {relative}")
        if outputs and not any(current.get(p) == digest for p, digest in outputs.items()):
            issues.append("no observed scientific artifact remains available")
    except (OSError, ValueError) as exc:
        issues.append(f"execution evidence unavailable: {exc}")
    return {"passed": not issues, "returncode": receipt.get("returncode"),
            "run_id": receipt.get("run_id"), "issues": issues,
            "unobserved_artifacts": unobserved, "receipt": receipt}


class ExecutionBroker:
    """One serial scientific execution queue per isolated Writer workspace."""

    def __init__(self, root: Path, audit_dir: Path, python: Path, *, environment_hash: str = "", allow_full: bool = True,
                 expected_installed_distributions: list[dict[str, str]] | None = None,
                 shared_runtime_python: Path | None = None):
        self.root, self.audit_dir, self.python = root.resolve(), audit_dir.resolve(), python
        self.environment_hash = environment_hash
        self.shared_runtime_python = shared_runtime_python or python
        self.expected_installed_distributions = expected_installed_distributions
        self.environment_refresh_required = False
        self.allow_full = allow_full
        self.session_id = uuid.uuid4().hex
        self.queue = self.root / ".geng_execution" / self.session_id
        manifest_path = _inside(self.root, "tasks_manifest.json")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
        # A dependency-only interrupted session may have no scaffold yet. It
        # can request an environment, but cannot launch an undeclared task.
        self.entries = {str(t["task_id"]): dict(t) for t in manifest.get("tasks", [])}
        self.receipts: list[dict[str, Any]] = []
        self.task_status: dict[str, dict[str, Any]] = {}
        self.request_status: dict[str, dict[str, Any]] = {}
        self.status_lock = threading.RLock()
        self.heartbeat_thread = threading.Thread(target=self._heartbeat, daemon=True)
        self.completed_pending: dict[str, dict[str, Any]] = {}
        self.process: subprocess.Popen | None = None
        self.cancelled = threading.Event()
        self.closing = threading.Event()
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self):
        _io_path(self.queue).mkdir(parents=True, exist_ok=True)
        try:
            self._publish_status()
        except Exception as exc:
            from .observations import record_error
            record_error(self.queue / "status.json", exc)
        self.heartbeat_thread.start()
        self.thread.start()
        return self

    def __exit__(self, exc_type, *_args):
        self.closing.set()
        # Never launch a duplicate full after the worker CLI exits. Complete
        # the scientific process already in flight and preserve its result.
        from .progress import PipelineCancelled
        if exc_type is not None and issubclass(exc_type, (PipelineCancelled, KeyboardInterrupt)):
            self.cancelled.set()
            self._stop_process()
        try:
            from .supervisor import current_supervisor
            supervisor = current_supervisor()
            while self.thread.is_alive():
                self.thread.join(timeout=.2)
                if supervisor is not None:
                    supervisor._check_cancelled()
        except (PipelineCancelled, KeyboardInterrupt):
            self.cancelled.set()
            self._stop_process()
            self.thread.join(timeout=5)
            raise
        finally:
            self.stopped.set()
            self.heartbeat_thread.join(timeout=2)

    def _publish_status(self) -> None:
        _io_path(_inside(self.root, self.queue.relative_to(self.root).as_posix())).mkdir(parents=True, exist_ok=True)
        with self.status_lock:
            self._publish_response(self.queue / "status.json", {"tasks": self.task_status,
                "requests": self.request_status, "broker_state": "stopped" if self.stopped.is_set() else "running",
                "updated_at": time.time()})

    def _heartbeat(self) -> None:
        while not self.stopped.wait(1.0):
            try:
                self._publish_response(self.queue / "heartbeat.json", {"updated_at": time.time(), "broker_state": "running"})
            except OSError:
                pass  # Client detects stale heartbeat; no science is relaunched.
        try:
            self._publish_response(self.queue / "heartbeat.json", {"updated_at": time.time(), "broker_state": "stopped"})
        except OSError:
            pass

    def _queue_requests(self):
        return sorted(self.queue / path.name for path in _io_path(self.queue).glob("*.request.json"))

    def _deliver_receipt(self, task_id: str, request_id: str, path: Path, receipt: dict[str, Any]) -> None:
        # The receipt already exists in the host audit. Delivery cannot turn a
        # successful scientific execution into a failed one.
        self._set_status(task_id, {"state": "completed", "request_id": request_id,
            "result": {key: receipt[key] for key in ("run_id", "task_id", "returncode") if key in receipt},
            "receipt": receipt, "transport_state": "pending"})
        try:
            self._publish_response(path, receipt)
        except OSError as exc:
            self._set_status(task_id, {"state": "completed", "request_id": request_id,
                "transport_state": "failed", "transport_error": f"{type(exc).__name__}: {exc}"})
            write_json(self.audit_dir / "execution_runs" / f"delivery_{request_id}.json",
                {"error_kind": "result_delivery_failed", "request_id": request_id,
                 "run_id": receipt.get("run_id"), "error": str(exc)})
        else:
            self._set_status(task_id, {"transport_state": "delivered", "request_id": request_id})

    def _stop_process(self) -> None:
        """Cancel the sandbox supervisor and science child as one execution."""
        process = self.process
        if process is None or process.poll() is not None:
            return
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               capture_output=True, timeout=10, check=True)
            else:
                os.killpg(process.pid, signal.SIGKILL)
        except (OSError, subprocess.SubprocessError):
            # Race with normal process completion is harmless. Preserve a
            # direct termination fallback if the system tree utility failed.
            if process.poll() is None:
                process.kill()

    def _serve(self):
        handled: set[str] = set()
        while not self.closing.is_set():
            for path in self._queue_requests():
                if self.closing.is_set():
                    break
                if path.name in handled:
                    continue
                handled.add(path.name)
                result_path = path.with_name(path.name.replace(".request.", ".result."))
                task_id = ""
                receipt = None
                try:
                    request = json.loads(_io_path(_inside(self.root, path.relative_to(self.root).as_posix())).read_text(encoding="utf-8"))
                    task_id = str(request.get("task_id") or "")
                    completed = self.completed_pending.pop(path.name, None)
                    if completed is not None and (
                        # Enumerate only when consuming an actual pending result:
                        # adding an optional module can change execution as well
                        # as modifying an existing source file.
                        self._observed_request_identity(request)
                        == completed["request_identity"]
                        and self._observed_identity_is_current(completed["receipt"])
                    ):
                        # A second client submitted while this task was still running.
                        # Return the original observation instead of duplicating science.
                        self._deliver_receipt(task_id, path.name.removesuffix(".request.json"), result_path, completed["receipt"])
                        continue
                    self._set_status(task_id, {"state": "starting", "request_id": path.name.removesuffix(".request.json")})
                    try:
                        request_sources = source_hashes(self.root)
                        request_identity = self._observed_request_identity(request, source_snapshot=request_sources)
                    except Exception as exc:
                        from .observations import record_error
                        record_error(path, exc)
                        request_sources, request_identity = {}, None
                    receipt = self.execute(request)
                    self.receipts.append(receipt)
                    # Snapshot already queued requests while the just-completed
                    # execution is still current. Do not infer ordering from
                    # filesystem mtimes (some case volumes round them forward).
                    for pending in self._queue_requests():
                        if pending.name in handled:
                            continue
                        try:
                            queued = json.loads(_io_path(_inside(self.root, pending.relative_to(self.root).as_posix())).read_text(encoding="utf-8"))
                            if request_identity is not None and self._observed_request_identity(queued, source_snapshot=self._current_recorded_hashes(request_sources)) == request_identity:
                                self.completed_pending[pending.name] = {
                                    "request_identity": request_identity,
                                    "receipt": receipt,
                                }
                        except (OSError, ValueError):
                            pass  # Normal request handling will report the malformed entry.
                    self._deliver_receipt(task_id, path.name.removesuffix(".request.json"), result_path, receipt)
                except Exception as exc:
                    if receipt is not None:
                        write_json(self.audit_dir / "execution_runs" / f"delivery_{uuid.uuid4().hex}.json",
                            {"error_kind": "result_delivery_failed", "run_id": receipt.get("run_id"),
                             "error": f"{type(exc).__name__}: {exc}"})
                        continue
                    try:
                        failure = {"returncode": 1, "error": f"{type(exc).__name__}: {exc}"}
                        if task_id:
                            self._set_status(task_id, {"state": "failed", "result": failure})
                        self._publish_response(result_path, failure)
                    except (ValueError, OSError):
                        # An unsafe Writer-controlled response path must never
                        # redirect a host write outside the sandbox.
                        write_json(self.audit_dir / "execution_runs" / f"rejected_{uuid.uuid4().hex}.json",
                                   {"error_kind": "request_or_status_failure", "error": f"{type(exc).__name__}: {exc}"})
            self.stopped.wait(0.15)

    def _set_status(self, task_id: str, update: dict[str, Any]) -> None:
        with self.status_lock:
            previous = self.task_status.get(task_id, {})
            request_id = str(update.get("request_id") or previous.get("request_id") or "")
            if request_id != previous.get("request_id"):
                previous = {}
            current = {**previous, **update, "task_id": task_id, "request_id": request_id}
            self.task_status[task_id] = current
            if request_id:
                self.request_status[request_id] = current
            try:
                self._publish_status()
            except Exception as exc:
                from .observations import record_error
                record_error(self.queue / "status.json", exc)

    def _current_recorded_hashes(self, recorded: dict[str, Any]) -> dict[str, Any]:
        """Recheck known files without enumerating the project again."""
        current = {}
        for relative in recorded:
            try:
                current[relative] = file_hash(_inside(self.root, relative))
            except (OSError, ValueError):
                current[relative] = None
        return current

    def _observed_identity_is_current(self, receipt: dict[str, Any]) -> bool:
        """Include hard-coded reads captured by the process, not only CLI inputs."""
        if receipt.get("inputs_stable") is not True:
            return False
        for key in ("source_hashes", "input_hashes"):
            recorded = receipt.get(key)
            if not isinstance(recorded, dict) or self._current_recorded_hashes(recorded) != recorded:
                return False
        return True

    def _request_identity(self, request: dict[str, Any], *, source_snapshot: dict[str, Any] | None = None) -> str:
        """Only identical science requests may reuse an in-flight observation."""
        task_id = str(request.get("task_id") or "")
        mode = str(request.get("mode") or "full")
        entry = self.entries[task_id]
        config_name = str(request.get("config") or entry.get("config_smoke" if mode == "smoke" else "config_full") or "config.json")
        config = _inside(self.root, config_name)
        input_names = set(map(str, request.get("inputs", [])))
        if config.is_file():
            try:
                input_names.update(_configuration_file_inputs(self.root, json.loads(config.read_text(encoding="utf-8-sig"))))
            except (OSError, ValueError, TypeError):
                pass  # The task's loader, not the request identity, consumes it.
        inputs = sorted({_inside(self.root, path).resolve().relative_to(self.root).as_posix()
                         for path in input_names})
        identity = {"task_id": task_id, "mode": mode,
            "device": str(request.get("device") or "auto"),
            "config": config.resolve().relative_to(self.root).as_posix(),
            "config_hash": file_hash(config) if config.is_file() else None,
            "inputs": {path: file_hash(_inside(self.root, path)) if _inside(self.root, path).is_file() else None for path in inputs},
            "source_hashes": source_snapshot if source_snapshot is not None else source_hashes(self.root)}
        return hashlib.sha256(json.dumps(identity, sort_keys=True).encode("utf-8")).hexdigest()

    def _observed_request_identity(self, request, *, source_snapshot=None):
        try:
            return self._request_identity(request, source_snapshot=source_snapshot)
        except Exception as exc:
            from .observations import record_error
            record_error(self.queue, exc)
            return None  # Unknown identity cannot reuse a prior result; execution still proceeds.

    def _publish_response(self, path: Path, value: dict[str, Any]) -> None:
        relative = path.relative_to(self.root).as_posix()
        target = _inside(self.root, relative)
        temporary = _inside(self.root, (path.parent / (uuid.uuid4().hex[:12] + ".tmp")).relative_to(self.root).as_posix())
        try:
            with _io_path(temporary).open("x", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=True)
            _inside(self.root, relative)
            _io_path(temporary).replace(_io_path(target))
        finally:
            _io_path(temporary).unlink(missing_ok=True)

    def execute(self, request: dict[str, Any]) -> dict[str, Any]:
        from .case_runtime_locking import _host_shared_runtime_read_guard
        from .gpu_resources import select_compute
        from .progress import PipelineCancelled

        if not self.allow_full and str(request.get("mode") or "full") == "full":
            raise ValueError("full execution is disabled for this preparation session; smoke remains available")
        if self.cancelled.is_set():
            raise PipelineCancelled("execution cancelled before launch")
        allocation = select_compute(str(request.get("device") or "auto"))
        # Experiments share this runtime read lock and may use the same GPU
        # concurrently. Only package installation takes the exclusive lock.
        with _host_shared_runtime_read_guard(self.shared_runtime_python):
            if self.cancelled.is_set():
                raise PipelineCancelled("execution cancelled before launch")
            return self._execute_with_runtime_lease(request, allocation=allocation)

    def _execute_with_runtime_lease(self, request: dict[str, Any], *, allocation) -> dict[str, Any]:
        from .scientific_process import DRIVER
        from .security_env import build_safe_env

        observation_errors = []
        def observe(label, operation, fallback):
            try:
                return operation()
            except Exception as exc:
                observation_errors.append({"observation": label, "error": f"{type(exc).__name__}: {exc}"})
                return fallback
        def observed_hash(path):
            return observe(str(path), lambda: file_hash(path), None)

        task_id = str(request.get("task_id") or "")
        entry = self.entries[task_id]
        config_rel = str(request.get("config") or entry.get("config_smoke" if request.get("mode") == "smoke" else "config_full") or "config.json")
        config = _inside(self.root, config_rel)
        try:
            config_doc = json.loads(config.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            config_doc = {}
        if not isinstance(config_doc, dict):
            config_doc = {}
        environment_before = observe("environment_before", lambda: probe_execution_environment(self.python), {})
        module = str(entry["module"])
        output_rel = str(entry.get("output_subdir") or task_id)
        output = _inside(self.root, f"outputs/{output_rel}")
        output.mkdir(parents=True, exist_ok=True)
        run_id = uuid.uuid4().hex
        run_dir = self.audit_dir / "execution_runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        before = observe("source_before", lambda: source_hashes(self.root), {})
        before[config_rel] = observed_hash(config)
        input_paths = set(map(str, request.get("inputs", []))) | _configuration_file_inputs(self.root, config_doc)
        inputs = {p: observe(p, lambda p=p: file_hash(_inside(self.root, p)), None) for p in input_paths}
        asset_stats_before = observe("assets_before", lambda: _persistent_asset_stats(self.root), {})
        old_outputs = observe("old_outputs", lambda: artifact_hashes(self.root, output_rel), {})
        if output.exists():
            # A successful no-op must not relabel yesterday's CSV as a new run.
            # Deterministic rewrites are fine because the new directory is empty.
            output.resolve().relative_to(self.root)
            shutil.move(str(output), str(run_dir / "previous_outputs"))
            output.mkdir(parents=True)
        runtime_home = self.root / ".geng_runtime"
        runtime_home.mkdir(exist_ok=True)
        assets = self.root / "execution_units"
        assets.mkdir(exist_ok=True)
        env = build_safe_env()
        for key in ("HOME", "USERPROFILE", "TEMP", "TMP", "TMPDIR", "XDG_CACHE_HOME", "MPLCONFIGDIR", "TORCH_HOME"):
            env[key] = str(runtime_home)
        for key in ("USER", "LOGNAME", "LNAME", "USERNAME"):
            env[key] = "geng-case-runtime"
        env["TORCHINDUCTOR_CACHE_DIR"] = str(runtime_home / "torchinductor")
        env["TORCH_EXTENSIONS_DIR"] = str(runtime_home / "torch_extensions")
        env["PATH"] = str(self.python.parent) + os.pathsep + env.get("PATH", "")
        if os.name == "nt":
            env["Path"] = env["PATH"]
        for key in ("CUDA_VISIBLE_DEVICES", "CUDA_PATH", "CUDA_HOME", "LD_LIBRARY_PATH"):
            if os.environ.get(key):
                env[key] = os.environ[key]
        # The child sees its selected GPU, which other tasks may also use. CPU requests cannot
        # accidentally pick CUDA through a library's automatic device choice.
        env["CUDA_VISIBLE_DEVICES"] = allocation.cuda_visible_devices
        guard_config = {"task_module": module, "task_config": config_rel,
                        "task_output_prefix": f"outputs/{output_rel}/"}
        guard = DRIVER
        started = time.time()
        write_json(run_dir / "started.json", {"run_id": run_id, "task_id": task_id,
                   "source_hashes": before, "input_hashes": inputs, "started_at": started})
        from .execution_sandbox import scientific_sandbox_launch
        launch = scientific_sandbox_launch([str(self.python), "-I", "-B", "-c", guard, json.dumps(guard_config)],
            work_dir=self.root, write_roots=(self.root,), env=env)
        with (run_dir / "stdout.log").open("w", encoding="utf-8") as stdout, (run_dir / "stderr.log").open("w", encoding="utf-8") as stderr:
            process = subprocess.Popen(launch["command"], cwd=self.root, env=launch["env"], stdout=stdout, stderr=stderr,
                start_new_session=os.name != "nt", creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0)
            self.process = process
            self._set_status(task_id, {"state": "running", "run_id": run_id,
                "pid": process.pid, "pid_kind": "os_sandbox_supervisor",
                "device_request": allocation.request, "gpu_uuid": allocation.gpu_uuid,
                "stdout_log": str(run_dir / "stdout.log"), "stderr_log": str(run_dir / "stderr.log"),
                "started_at": started})
            if self.cancelled.is_set():
                self._stop_process()
            returncode = process.wait()
            self.process = None
        environment_after = observe("environment_after", lambda: probe_execution_environment(self.python), {})
        environment_stable = bool(environment_after.get("ok")) and environment_before.get("sha256") == environment_after.get("sha256")
        after = observe("source_after", lambda: source_hashes(self.root), {})
        after[config_rel] = observed_hash(config)
        output_hashes = observe("outputs_after", lambda: artifact_hashes(self.root, output_rel), {})
        asset_stats_after = observe("assets_after", lambda: _persistent_asset_stats(self.root), {})
        stderr_text = observe("stderr", lambda: (run_dir / "stderr.log").read_text(encoding="utf-8", errors="replace"), "")
        observed_reads: dict[str, str] = {}
        written_paths: set[str] = set()
        observed_import_roots: list[str] = []
        for line in stderr_text.splitlines():
            if line.startswith("GENG_OBSERVED_READ "):
                try:
                    item = json.loads(line[len("GENG_OBSERVED_READ "):])
                    observed_reads.setdefault(item["path"], item["sha256"])
                except (ValueError, KeyError, TypeError):
                    continue
            elif line.startswith("GENG_OBSERVED_MODULES "):
                try:
                    observed_import_roots = sorted(set(json.loads(line[len("GENG_OBSERVED_MODULES "):])))
                except (ValueError, TypeError):
                    continue
            elif line.startswith("GENG_OBSERVED_WRITE "):
                try:
                    written_paths.add(str(json.loads(line[len("GENG_OBSERVED_WRITE "):])["path"]))
                except (ValueError, KeyError, TypeError):
                    continue
        sources = {p: h for p, h in observed_reads.items() if p in before}
        sources[config_rel] = before.get(config_rel)
        inputs.update({p: h for p, h in observed_reads.items() if p not in sources})
        dependency_issues = observe("producer_history", lambda: self._check_producers(inputs,
            required_mode=str(request.get("mode") or "full"),
            current_inventory=environment_before.get("inventory")), [])
        # Native scientific serializers (e.g. PyTorch's C++ zip writer) do not
        # emit Python open events. Observe their real filesystem products too.
        written_paths.update(p for p, metadata in asset_stats_after.items() if asset_stats_before.get(p) != metadata)
        produced = {p: observe(p, lambda p=p: file_hash(_inside(self.root, p)), None) for p in written_paths}
        inputs_stable = environment_stable and before == after and all(observe(p, lambda p=p: file_hash(_inside(self.root, p)), None) == h for p, h in inputs.items()) and not observation_errors
        observed_distributions = observe("runtime_distributions", lambda: _observed_runtime_distributions(
            self.python, observed_import_roots, environment_before.get("inventory")), {})
        receipt = {"schema_version": 1, "observer": "orchestration_host", "run_id": run_id,
            "task_id": task_id, "output_subdir": output_rel,
            "mode": str(request.get("mode") or "full"), "config": config_rel, "config_observation": config_doc,
            "python_executable": str(self.python), "environment_hash": self.environment_hash,
            "compute_resource": {"request": allocation.request, "gpu_uuid": allocation.gpu_uuid,
                "cuda_visible_devices": allocation.cuda_visible_devices},
            "sandbox_policy": launch["policy"],
            "environment_observation": {"before": environment_before, "after": environment_after,
                "stable": environment_stable, "probe_duration_s": round(environment_before.get("duration_s", 0) + environment_after.get("duration_s", 0), 4)},
            "started_at": started, "finished_at": time.time(), "returncode": returncode,
            "pid": process.pid, "pid_kind": "os_sandbox_supervisor", "source_hashes": sources,
            "source_snapshot_hashes": before, "input_hashes": inputs,
            "inputs_stable": inputs_stable, "output_hashes": output_hashes,
            "produced_artifacts": produced, "dependency_issues": dependency_issues,
            "observed_import_roots": observed_import_roots,
            "observed_distributions": observed_distributions,
            "input_observation_scope": "Python file events plus explicit --input and configuration file paths; native loads require one of those declarations",
            "cancelled": self.cancelled.is_set(), "observation_errors": observation_errors,
            "unchanged_output_paths": sorted(p for p, h in output_hashes.items() if old_outputs.get(p) == h),
            "stderr_tail": "\n".join(line for line in stderr_text.splitlines() if not line.startswith("GENG_OBSERVED_"))[-12000:]}
        write_json(run_dir / "execution_receipt.json", receipt)
        write_json(output / "execution_receipt.json", receipt)
        return receipt

    def _check_producers(self, inputs: dict[str, str], *, required_mode: str = "full",
                         current_inventory: dict | None = None) -> list[str]:
        """Describe recorded origins; this observation never prevents execution."""
        return _producer_chain_issues(self.root, self.audit_dir, inputs,
            required_mode=required_mode, python_executable=self.python, current_inventory=current_inventory)


def _producer_chain_issues(
    root: Path, audit_dir: Path, inputs: dict[str, str], *, required_mode: str = "full",
    python_executable: Path | None = None, current_inventory: dict | None = None,
) -> list[str]:
    """Revalidate only the supplied assets' ancestry, for execution and resume."""
    generated_inputs = {path: digest for path, digest in inputs.items()
                        if path.startswith(("execution_units/", "outputs/"))}
    if not generated_inputs:
        return []
    candidates = []
    for path in (audit_dir / "execution_runs").glob("*/execution_receipt.json"):
        try:
            receipt = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(receipt, dict):
                candidates.append(receipt)
        except (ValueError, OSError):
            continue
    verified: set[tuple[str, str, str]] = set()
    runtime_metadata: dict[Path, dict[str, str]] = {}

    def producer_environment_current(producer: dict[str, Any]) -> bool:
        from .writer_lineage import runtime_distribution_metadata

        previous = producer.get("observed_distributions")
        # Historical module names cannot identify an uninstalled alias package.
        # Require the distribution identities saved when execution was observed.
        if not isinstance(previous, dict):
            return False
        if not previous:
            return True
        selected_python = python_executable or (Path(producer["python_executable"]) if producer.get("python_executable") else None)
        if selected_python is None:
            return False
        prefix = _runtime_prefix(selected_python)
        if prefix not in runtime_metadata:
            _graph, _names, versions = runtime_distribution_metadata(prefix)
            inventory = current_inventory
            if inventory is None and selected_python.is_file() and not previous.keys() <= versions.keys():
                # A private venv can import shared packages through .pth files.
                # Directory-only inventory must not misreport them as removed.
                inventory = probe_execution_environment(selected_python).get("inventory")
            if inventory is not None:
                versions = _installed_distribution_versions(inventory.get("packages"))
            runtime_metadata[prefix] = versions
        versions = runtime_metadata[prefix]
        return all(versions.get(name) == version for name, version in previous.items())

    def current_producer_chain(relative: str, expected: str, mode: str, visiting: set[tuple[str, str, str]]) -> bool:
        key = (relative, expected, mode)
        if key in visiting:
            return False
        if key in verified:
            return True
        producers = [r for r in candidates if (r.get("produced_artifacts", {}).get(relative)
            or r.get("output_hashes", {}).get(relative)) == expected
            and r.get("observer") == "orchestration_host"
            and r.get("mode") in ({"smoke", "full"} if mode == "smoke" else {"full"})
            and r.get("returncode") == 0]
        for producer in sorted(producers, key=lambda r: r.get("finished_at", 0), reverse=True):
            if producer.get("inputs_stable") is not True or producer.get("dependency_issues"):
                continue
            if not producer_environment_current(producer):
                continue
            try:
                dependencies = {**producer.get("source_hashes", {}), **producer.get("input_hashes", {})}
                valid = all(file_hash(_inside(root, p)) == h for p, h in dependencies.items())
            except (OSError, ValueError):
                valid = False
            if valid and all(
                current_producer_chain(path, digest, str(producer["mode"]), visiting | {key})
                for path, digest in producer.get("input_hashes", {}).items()
                if path.startswith(("execution_units/", "outputs/"))
            ):
                # A cyclic candidate must not hide another grounded origin.
                verified.add(key)
                return True
        return False

    return [f"persistent generated input has no current producer receipt: {relative}"
            for relative, expected in generated_inputs.items()
            if not current_producer_chain(relative, expected, required_mode, set())]


def find_host_execution(root: Path, audit_dir: Path, task_id: str) -> dict[str, Any]:
    candidates = []
    for path in (audit_dir / "execution_runs").glob("*/execution_receipt.json"):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if value.get("task_id") == task_id:
                candidates.append(value)
        except (OSError, ValueError):
            continue
    if not candidates:
        return {"passed": False, "issues": ["no host-observed full execution; use run_task.py"], "receipt": None}
    receipt = max(candidates, key=lambda r: r.get("finished_at", 0))
    observed = validate_receipt(root, receipt, task_id=task_id)
    if observed["passed"]:
        chain_issues = _producer_chain_issues(root, audit_dir, receipt.get("input_hashes") or {},
                                             required_mode=str(receipt.get("mode") or "full"),
                                             python_executable=Path(receipt["python_executable"]) if receipt.get("python_executable") else None)
        observed["issues"].extend(chain_issues)
        observed["passed"] = not observed["issues"]
    return observed
