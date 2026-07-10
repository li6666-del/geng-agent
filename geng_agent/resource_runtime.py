from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
import uuid
from typing import Any, Iterator


GIB = 1024**3
RESOURCE_LIMIT_RETURN_CODE = 125
_SENSITIVE_CHILD_ENV = {
    "GENG_PYTHON",
    "GENG_TASK_CONTRACT_PATH",
    "GENG_TASK_MEMORY_SNAPSHOT_HASH",
    "GENG_WRITER_SELFTEST_MODE",
}


class ResourceUnavailable(RuntimeError):
    pass


class ResourceLease:
    """Client-side lease handle communicating with the host-owned broker."""

    def __init__(
        self,
        *,
        lease_id: str,
        channel_dir: Path,
        channel_token: str,
        task_id: str,
    ) -> None:
        self.lease_id = lease_id
        self.channel_dir = channel_dir
        self.channel_token = channel_token
        self.task_id = task_id
        self._released = False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._heartbeat_loop, name=f"geng-resource-{lease_id[:8]}", daemon=True)
        self._thread.start()

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._stop.set()
        self._thread.join(timeout=2.0)
        request_id = uuid.uuid4().hex
        _write_client_request(
            self.channel_dir,
            {
                "request_id": request_id,
                "operation": "release",
                "token": self.channel_token,
                "task_id": self.task_id,
                "lease_id": self.lease_id,
                "pid": os.getpid(),
                "created_at": time.time(),
            },
        )
        _wait_for_response(self.channel_dir, request_id, timeout=5.0, required=False)

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(5.0):
            try:
                _write_client_request(
                    self.channel_dir,
                    {
                        "request_id": uuid.uuid4().hex,
                        "operation": "heartbeat",
                        "token": self.channel_token,
                        "task_id": self.task_id,
                        "lease_id": self.lease_id,
                        "pid": os.getpid(),
                        "created_at": time.time(),
                    },
                )
            except OSError:
                continue


class ResourceBroker:
    """Host-owned resource broker; writers can only access their own channel."""

    def __init__(self, *, plan: dict[str, Any], events_path: Path, state_path: Path) -> None:
        self.plan = plan
        self.events_path = events_path
        self.state_path = state_path
        self._channels: dict[str, dict[str, str]] = {}
        self._leases: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="geng-resource-broker", daemon=True)
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        self._persist_state()
        self._started = True
        self._thread.start()

    def __enter__(self) -> "ResourceBroker":
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.stop()

    def stop(self) -> None:
        if not self._started:
            return
        self._stop.set()
        self._thread.join(timeout=5.0)
        with self._lock:
            abandoned = list(self._leases.values())
            self._leases.clear()
        for lease in abandoned:
            _append_event(
                self.events_path,
                {
                    "event": "broker_stop_reclaimed",
                    "lease_id": lease.get("lease_id"),
                    "task_id": lease.get("task_id"),
                    "time": time.time(),
                },
            )
        self._persist_state()
        self._started = False

    def register_channel(self, *, task_id: str, channel_dir: Path) -> dict[str, str]:
        channel_dir = channel_dir.resolve()
        (channel_dir / "requests").mkdir(parents=True, exist_ok=True)
        (channel_dir / "responses").mkdir(parents=True, exist_ok=True)
        key = str(channel_dir)
        with self._lock:
            channel = self._channels.get(key)
            if channel is None:
                channel = {
                    "task_id": task_id,
                    "channel_dir": key,
                    "token": uuid.uuid4().hex,
                }
                self._channels[key] = channel
            elif channel["task_id"] != task_id:
                raise ResourceUnavailable(f"resource channel already belongs to {channel['task_id']}")
            return dict(channel)

    def _run(self) -> None:
        poll_seconds = max(0.05, min(0.5, float(self.plan["execution"].get("resource_poll_seconds") or 0.25)))
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as exc:
                _append_event(
                    self.events_path,
                    {"event": "broker_error", "error": f"{type(exc).__name__}: {exc}", "time": time.time()},
                )
            self._stop.wait(poll_seconds)
        try:
            self._tick()
        except Exception:
            pass

    def _tick(self) -> None:
        with self._lock:
            channels = [dict(item) for item in self._channels.values()]
        changed = self._reclaim_dead_leases()
        messages: list[tuple[int, float, Path, dict[str, Any], dict[str, str]]] = []
        priority = {"release": 0, "heartbeat": 1, "acquire": 2}
        for channel in channels:
            request_dir = Path(channel["channel_dir"]) / "requests"
            for path in request_dir.glob("*.json"):
                try:
                    message = json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if not isinstance(message, dict):
                    path.unlink(missing_ok=True)
                    continue
                operation = str(message.get("operation") or "")
                messages.append((priority.get(operation, 9), float(message.get("created_at") or 0.0), path, message, channel))
        messages.sort(key=lambda item: (item[0], item[1], str(item[2])))
        for _, _, path, message, channel in messages:
            if not self._valid_message(message, channel):
                self._respond(channel, message, ok=False, error="invalid resource broker credentials")
                path.unlink(missing_ok=True)
                continue
            operation = str(message.get("operation") or "")
            if operation == "release":
                changed = self._release(message, channel) or changed
                path.unlink(missing_ok=True)
            elif operation == "heartbeat":
                changed = self._heartbeat(message, channel) or changed
                path.unlink(missing_ok=True)
            elif operation == "acquire":
                processed, allocated = self._acquire(message, channel)
                changed = changed or allocated
                if processed:
                    path.unlink(missing_ok=True)
            else:
                self._respond(channel, message, ok=False, error=f"unsupported broker operation: {operation}")
                path.unlink(missing_ok=True)
        if changed:
            self._persist_state()

    @staticmethod
    def _valid_message(message: dict[str, Any], channel: dict[str, str]) -> bool:
        return (
            message.get("token") == channel["token"]
            and message.get("task_id") == channel["task_id"]
            and isinstance(message.get("request_id"), str)
        )

    def _acquire(self, message: dict[str, Any], channel: dict[str, str]) -> tuple[bool, bool]:
        request_id = str(message["request_id"])
        with self._lock:
            existing = next(
                (item for item in self._leases.values() if item.get("channel_dir") == channel["channel_dir"]),
                None,
            )
            if existing is not None:
                self._respond(channel, message, ok=False, error="resource channel already owns an active lease")
                return True, False
            try:
                request = normalize_resource_request(message.get("contract") or {}, self.plan)
            except ResourceUnavailable as exc:
                self._respond(channel, message, ok=False, error=str(exc))
                return True, False
            state = {"leases": list(self._leases.values())}
            allocation = _try_allocate(state, self.plan, request)
            if allocation is None:
                wait_timeout = max(1.0, float(self.plan["execution"].get("resource_wait_timeout_seconds") or 1800.0))
                if time.time() - float(message.get("created_at") or time.time()) >= wait_timeout:
                    self._respond(
                        channel,
                        message,
                        ok=False,
                        error=f"resource request for {channel['task_id']} was not granted within {wait_timeout:.0f}s",
                    )
                    return True, False
                return False, False
            lease_id = uuid.uuid4().hex
            lease = {
                "lease_id": lease_id,
                "request_id": request_id,
                "task_id": channel["task_id"],
                "channel_dir": channel["channel_dir"],
                "pid": int(message.get("pid") or 0),
                "started": time.time(),
                "heartbeat": time.time(),
                "resources": allocation,
            }
            self._leases[lease_id] = lease
        self._respond(channel, message, ok=True, lease_id=lease_id, allocation=allocation)
        _append_event(
            self.events_path,
            {
                "event": "acquired",
                "lease_id": lease_id,
                "task_id": channel["task_id"],
                "pid": lease["pid"],
                "resources": allocation,
                "time": time.time(),
            },
        )
        return True, True

    def _release(self, message: dict[str, Any], channel: dict[str, str]) -> bool:
        lease_id = str(message.get("lease_id") or "")
        with self._lock:
            lease = self._leases.get(lease_id)
            if lease is None or lease.get("channel_dir") != channel["channel_dir"]:
                self._respond(channel, message, ok=False, error="resource lease does not belong to this channel")
                return False
            self._leases.pop(lease_id, None)
        self._respond(channel, message, ok=True)
        _append_event(
            self.events_path,
            {"event": "released", "lease_id": lease_id, "task_id": channel["task_id"], "time": time.time()},
        )
        return True

    def _heartbeat(self, message: dict[str, Any], channel: dict[str, str]) -> bool:
        lease_id = str(message.get("lease_id") or "")
        with self._lock:
            lease = self._leases.get(lease_id)
            if lease is None or lease.get("channel_dir") != channel["channel_dir"]:
                return False
            lease["heartbeat"] = time.time()
        return True

    def _reclaim_dead_leases(self) -> bool:
        reclaimed: list[dict[str, Any]] = []
        with self._lock:
            for lease_id, lease in list(self._leases.items()):
                if not _pid_alive(int(lease.get("pid") or 0)):
                    reclaimed.append(self._leases.pop(lease_id))
        for lease in reclaimed:
            _append_event(
                self.events_path,
                {
                    "event": "dead_client_reclaimed",
                    "lease_id": lease.get("lease_id"),
                    "task_id": lease.get("task_id"),
                    "pid": lease.get("pid"),
                    "time": time.time(),
                },
            )
        return bool(reclaimed)

    @staticmethod
    def _respond(
        channel: dict[str, str],
        message: dict[str, Any],
        *,
        ok: bool,
        error: str | None = None,
        lease_id: str | None = None,
        allocation: dict[str, Any] | None = None,
    ) -> None:
        request_id = str(message.get("request_id") or uuid.uuid4().hex)
        response = {"request_id": request_id, "ok": ok, "time": time.time()}
        if error:
            response["error"] = error
        if lease_id:
            response["lease_id"] = lease_id
        if allocation is not None:
            response["allocation"] = allocation
        _atomic_write_json(Path(channel["channel_dir"]) / "responses" / f"{request_id}.json", response)

    def _persist_state(self) -> None:
        with self._lock:
            state = {
                "schema_version": "2.0",
                "host_pid": os.getpid(),
                "updated_at": time.time(),
                "leases": list(self._leases.values()),
            }
        _atomic_write_json(self.state_path, state)


def acquire_resource_lease(
    *,
    channel_dir: Path,
    channel_token: str,
    contract: dict[str, Any],
    task_id: str,
    wait_timeout_s: float = 1800.0,
) -> tuple[ResourceLease, dict[str, Any], float]:
    started = time.monotonic()
    request_id = uuid.uuid4().hex
    _write_client_request(
        channel_dir,
        {
            "request_id": request_id,
            "operation": "acquire",
            "token": channel_token,
            "task_id": task_id,
            "pid": os.getpid(),
            "contract": contract,
            "created_at": time.time(),
        },
    )
    response = _wait_for_response(channel_dir, request_id, timeout=max(1.0, wait_timeout_s) + 5.0, required=True)
    if not response.get("ok"):
        raise ResourceUnavailable(str(response.get("error") or "resource broker rejected request"))
    allocation = response.get("allocation")
    lease_id = response.get("lease_id")
    if not isinstance(allocation, dict) or not isinstance(lease_id, str):
        raise ResourceUnavailable("resource broker returned an invalid grant")
    waited = time.monotonic() - started
    return (
        ResourceLease(
            lease_id=lease_id,
            channel_dir=channel_dir,
            channel_token=channel_token,
            task_id=task_id,
        ),
        allocation,
        waited,
    )


def normalize_resource_request(contract: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    raw = contract.get("resources") if isinstance(contract.get("resources"), dict) else {}
    backend = contract.get("backend") if isinstance(contract.get("backend"), dict) else {}
    execution_class = str(raw.get("execution_class") or "unknown")
    defaults = {
        "cpu_light": (2, 1.5, 0),
        "cpu_heavy": (8, 6.0, 0),
        "gpu": (4, 3.0, 1),
        "unknown": (6, 4.0, 1),
    }
    if execution_class not in defaults:
        execution_class = "unknown"
    default_cpu, default_ram, default_gpu = defaults[execution_class]
    cpu_cores = max(1, int(raw.get("cpu_cores") or default_cpu))
    ram_gb = max(0.25, float(raw.get("ram_gb") or default_ram))
    gpu_count = max(0, int(raw.get("gpu_count") if raw.get("gpu_count") is not None else default_gpu))
    vram_gb = max(0.0, float(raw.get("vram_gb") or 0.0))
    requested_backend = str(backend.get("requested") or "auto")
    allow_cpu_fallback = bool(backend.get("allow_cpu_fallback", True))
    gpus = plan["execution"].get("gpus") if isinstance(plan["execution"].get("gpus"), list) else []
    if requested_backend == "cpu" or execution_class in {"cpu_light", "cpu_heavy"}:
        gpu_count = 0
    elif requested_backend == "gpu" or execution_class == "gpu":
        gpu_count = max(1, gpu_count)
    if gpu_count and not gpus:
        if allow_cpu_fallback and requested_backend != "gpu":
            gpu_count = 0
        else:
            raise ResourceUnavailable("task requires GPU resources but no compatible GPU is available")
    cpu_budget = max(1, int(plan["execution"].get("cpu_cores_budget") or 1))
    ram_budget = max(0.25, float(plan["execution"].get("ram_budget_gb") or 0.25))
    if cpu_cores > cpu_budget:
        raise ResourceUnavailable(f"task requests {cpu_cores} CPU cores but the resource budget is {cpu_budget}")
    if ram_gb > ram_budget:
        raise ResourceUnavailable(f"task requests {ram_gb:.2f}GB RAM but the current resource budget is {ram_budget:.2f}GB")
    if gpu_count and vram_gb > 0:
        largest_vram = max(float(item.get("available_vram_gb") or 0.0) for item in gpus)
        if vram_gb > largest_vram:
            raise ResourceUnavailable(
                f"task requests {vram_gb:.2f}GB VRAM but the largest available GPU budget is {largest_vram:.2f}GB"
            )
    return {
        "execution_class": execution_class,
        "cpu_cores": cpu_cores,
        "ram_gb": ram_gb,
        "gpu_count": gpu_count,
        "vram_gb": vram_gb,
        "confidence": str(raw.get("confidence") or "low"),
    }


def subprocess_environment(allocation: dict[str, Any], *, real_python: str | None = None) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GENG_TASK_WRITER_") and key not in _SENSITIVE_CHILD_ENV
    }
    gpu_indices = allocation.get("gpu_indices") if isinstance(allocation.get("gpu_indices"), list) else []
    if gpu_indices:
        env["CUDA_VISIBLE_DEVICES"] = ",".join(str(index) for index in gpu_indices)
        fraction = float(allocation.get("gpu_memory_fraction") or 0.0)
        if fraction > 0:
            env["GENG_ENFORCED_CUDA_MEMORY_FRACTION"] = f"{min(1.0, max(0.01, fraction)):.6f}"
    threads = max(1, int(allocation.get("cpu_cores") or 1))
    for name in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        env[name] = str(threads)
    if real_python:
        env["PYTHON"] = real_python
        env["GENG_PYTHON"] = real_python
    runtime_dir = str(Path(__file__).resolve().parent)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = runtime_dir + (os.pathsep + existing if existing else "")
    return env


@contextmanager
def timeout_exclusion(path: Path, phase: str) -> Iterator[None]:
    reporter = _TimeoutPhaseReporter(path=path, phase=phase)
    reporter.start()
    try:
        yield
    finally:
        reporter.stop()


class _TimeoutPhaseReporter:
    def __init__(self, *, path: Path, phase: str) -> None:
        self.path = path
        self.phase = phase
        self.started = time.time()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"geng-timeout-{phase}", daemon=True)

    def start(self) -> None:
        self._write()
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if int(value.get("pid") or 0) == os.getpid():
                self.path.unlink(missing_ok=True)
        except Exception:
            self.path.unlink(missing_ok=True)

    def _run(self) -> None:
        while not self._stop.wait(0.5):
            self._write()

    def _write(self) -> None:
        _atomic_write_json(
            self.path,
            {
                "schema_version": "1.0",
                "pid": os.getpid(),
                "phase": self.phase,
                "started_at": self.started,
                "heartbeat": time.time(),
            },
        )


def run_guarded_process(
    *,
    command: list[str],
    env: dict[str, str],
    timeout: float | None,
    allocation: dict[str, Any],
    cwd: Path | None = None,
) -> dict[str, Any]:
    """Run a task process with affinity, RAM, timeout, and GPU-memory enforcement."""
    try:
        import psutil  # type: ignore
    except Exception as exc:
        raise ResourceUnavailable(f"psutil is required for resource enforcement: {type(exc).__name__}: {exc}") from exc

    selected_gpus = [int(item) for item in allocation.get("gpu_indices", [])]
    baseline_gpu = _gpu_used_memory_gb(selected_gpus)
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        creationflags=creationflags,
        start_new_session=os.name != "nt",
    )
    started = time.monotonic()
    ram_limit_bytes = max(1, int(float(allocation.get("ram_gb") or 0.25) * GIB))
    vram_limit_gb = max(0.0, float(allocation.get("vram_gb") or 0.0))
    poll_seconds = max(0.05, min(0.5, float(allocation.get("monitor_poll_seconds") or 0.1)))
    affinity_count = max(1, int(allocation.get("cpu_cores") or 1))
    peak_rss = 0
    peak_gpu_delta = 0.0
    timed_out = False
    violation: str | None = None
    affinity_applied = False
    # The RSS monitor enforces the declared limit; the Job Object is a final
    # backstop for sudden allocation spikes between monitor samples.
    job = _WindowsJobMemoryLimit(process, int(ram_limit_bytes * 2.0)) if os.name == "nt" else None
    last_gpu_poll = 0.0
    try:
        while process.poll() is None:
            now = time.monotonic()
            if timeout is not None and timeout > 0 and now - started >= timeout:
                timed_out = True
                _terminate_process_tree(process, psutil)
                break
            tree = _process_tree(process.pid, psutil)
            if tree:
                affinity_applied = _apply_cpu_affinity(tree, affinity_count) or affinity_applied
                rss = _tree_rss_bytes(tree)
                peak_rss = max(peak_rss, rss)
                if rss > ram_limit_bytes:
                    violation = f"RAM limit exceeded: {rss / GIB:.3f}GB > {ram_limit_bytes / GIB:.3f}GB"
                    _terminate_process_tree(process, psutil)
                    break
            if selected_gpus and now - last_gpu_poll >= max(0.25, poll_seconds):
                current_gpu = _gpu_used_memory_gb(selected_gpus)
                if baseline_gpu is not None and current_gpu is not None:
                    delta = max(0.0, sum(current_gpu.values()) - sum(baseline_gpu.values()))
                    peak_gpu_delta = max(peak_gpu_delta, delta)
                    if vram_limit_gb > 0 and delta > vram_limit_gb + 0.064:
                        violation = f"GPU VRAM limit exceeded: {delta:.3f}GB > {vram_limit_gb:.3f}GB"
                        _terminate_process_tree(process, psutil)
                        break
                last_gpu_poll = now
            time.sleep(poll_seconds)
        try:
            raw_returncode = process.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            _terminate_process_tree(process, psutil)
            raw_returncode = process.wait(timeout=5.0)
    finally:
        if job is not None:
            job.close()
    returncode = RESOURCE_LIMIT_RETURN_CODE if violation else 124 if timed_out else int(raw_returncode)
    return {
        "returncode": returncode,
        "raw_returncode": int(raw_returncode),
        "timed_out": timed_out,
        "resource_violation": violation,
        "peak_resources": {
            "rss_gb": round(peak_rss / GIB, 4),
            "gpu_vram_delta_gb": round(peak_gpu_delta, 4),
        },
        "enforcement": {
            "rss_monitor": True,
            "cpu_affinity": affinity_applied,
            "windows_job_memory_limit": bool(job and job.active),
            "gpu_vram_monitor": bool(selected_gpus and baseline_gpu is not None),
            "torch_cuda_fraction": bool(selected_gpus and allocation.get("gpu_memory_fraction")),
        },
    }


def enforce_torch_cuda_fraction() -> None:
    raw = os.environ.get("GENG_ENFORCED_CUDA_MEMORY_FRACTION")
    if not raw:
        return
    try:
        fraction = min(1.0, max(0.01, float(raw)))
        import torch  # type: ignore

        if torch.cuda.is_available():
            for index in range(torch.cuda.device_count()):
                torch.cuda.set_per_process_memory_fraction(fraction, index)
    except Exception:
        return


class _WindowsJobMemoryLimit:
    def __init__(self, process: subprocess.Popen[Any], memory_limit_bytes: int) -> None:
        self.handle: Any = None
        self.active = False
        try:
            import ctypes
            from ctypes import wintypes

            class IO_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("ReadOperationCount", ctypes.c_ulonglong),
                    ("WriteOperationCount", ctypes.c_ulonglong),
                    ("OtherOperationCount", ctypes.c_ulonglong),
                    ("ReadTransferCount", ctypes.c_ulonglong),
                    ("WriteTransferCount", ctypes.c_ulonglong),
                    ("OtherTransferCount", ctypes.c_ulonglong),
                ]

            class BASIC_LIMITS(ctypes.Structure):
                _fields_ = [
                    ("PerProcessUserTimeLimit", ctypes.c_longlong),
                    ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD),
                ]

            class EXTENDED_LIMITS(ctypes.Structure):
                _fields_ = [
                    ("BasicLimitInformation", BASIC_LIMITS),
                    ("IoInfo", IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t),
                ]

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateJobObjectW.restype = wintypes.HANDLE
            kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
            kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            handle = kernel32.CreateJobObjectW(None, None)
            if not handle:
                return
            limits = EXTENDED_LIMITS()
            limits.BasicLimitInformation.LimitFlags = 0x00000200 | 0x00002000
            limits.JobMemoryLimit = memory_limit_bytes
            if not kernel32.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                kernel32.CloseHandle(handle)
                return
            process_handle = wintypes.HANDLE(int(getattr(process, "_handle")))
            if not kernel32.AssignProcessToJobObject(handle, process_handle):
                kernel32.CloseHandle(handle)
                return
            self.handle = handle
            self._kernel32 = kernel32
            self.active = True
        except Exception:
            self.handle = None
            self.active = False

    def close(self) -> None:
        if self.handle is not None:
            try:
                self._kernel32.CloseHandle(self.handle)
            except Exception:
                pass
            self.handle = None


def _process_tree(pid: int, psutil_module: Any) -> list[Any]:
    try:
        root = psutil_module.Process(pid)
        return [root, *root.children(recursive=True)]
    except (psutil_module.NoSuchProcess, psutil_module.AccessDenied):
        return []


def _tree_rss_bytes(processes: list[Any]) -> int:
    total = 0
    for process in processes:
        try:
            total += int(process.memory_info().rss)
        except Exception:
            continue
    return total


def _apply_cpu_affinity(processes: list[Any], count: int) -> bool:
    applied = False
    for process in processes:
        try:
            current = process.cpu_affinity()
            target = current[: min(len(current), count)]
            if target:
                process.cpu_affinity(target)
                applied = True
        except Exception:
            continue
    return applied


def _terminate_process_tree(process: subprocess.Popen[Any], psutil_module: Any) -> None:
    tree = _process_tree(process.pid, psutil_module)
    for item in reversed(tree):
        try:
            item.kill()
        except Exception:
            continue
    try:
        process.kill()
    except Exception:
        pass


def _gpu_used_memory_gb(indices: list[int]) -> dict[int, float] | None:
    if not indices:
        return {}
    executable = shutil.which("nvidia-smi")
    if not executable:
        return None
    try:
        completed = subprocess.run(
            [executable, "--query-gpu=index,memory.used", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception:
        return None
    if completed.returncode != 0:
        return None
    wanted = set(indices)
    result: dict[int, float] = {}
    for line in completed.stdout.splitlines():
        parts = [item.strip() for item in line.split(",")]
        if len(parts) < 2:
            continue
        try:
            index = int(parts[0])
            if index in wanted:
                result[index] = float(parts[1]) / 1024.0
        except ValueError:
            continue
    return result if wanted.issubset(result) else None


def _try_allocate(state: dict[str, Any], plan: dict[str, Any], request: dict[str, Any]) -> dict[str, Any] | None:
    execution = plan["execution"]
    leases = state.get("leases", [])
    used_cpu = sum(int(item.get("resources", {}).get("cpu_cores") or 0) for item in leases)
    used_ram = sum(float(item.get("resources", {}).get("ram_gb") or 0.0) for item in leases)
    if used_cpu + request["cpu_cores"] > int(execution.get("cpu_cores_budget") or 1):
        return None
    if used_ram + request["ram_gb"] > float(execution.get("ram_budget_gb") or 1.0):
        return None
    allocation = dict(request)
    allocation["gpu_indices"] = []
    allocation["monitor_poll_seconds"] = max(0.05, float(execution.get("enforcement_poll_seconds") or 0.1))
    if request["gpu_count"] <= 0:
        active_cpu_full = sum(1 for item in leases if not item.get("resources", {}).get("gpu_indices"))
        if active_cpu_full >= max(1, int(execution.get("cpu_full_max") or 1)):
            return None
        return allocation
    gpus = execution.get("gpus") if isinstance(execution.get("gpus"), list) else []
    candidates: list[tuple[float, int]] = []
    for gpu in gpus:
        index = int(gpu.get("index") or 0)
        active = [item for item in leases if index in item.get("resources", {}).get("gpu_indices", [])]
        if len(active) >= max(1, int(gpu.get("max_full_jobs") or 1)):
            continue
        used_vram = sum(float(item.get("resources", {}).get("vram_gb") or 0.0) for item in active)
        available = float(gpu.get("available_vram_gb") or gpu.get("total_vram_gb") or 0.0)
        requested_vram = request["vram_gb"] or max(0.5, available * 0.8)
        if used_vram + requested_vram <= available + 1e-9:
            candidates.append((available - used_vram - requested_vram, index))
    if len(candidates) < request["gpu_count"]:
        return None
    candidates.sort(reverse=True)
    selected = [index for _, index in candidates[: request["gpu_count"]]]
    allocation["gpu_indices"] = selected
    selected_gpus = [gpu for gpu in gpus if int(gpu.get("index") or 0) in selected]
    if allocation["vram_gb"] <= 0:
        allocation["vram_gb"] = min(
            max(0.5, float(gpu.get("available_vram_gb") or gpu.get("total_vram_gb") or 0.0) * 0.8)
            for gpu in selected_gpus
        )
    allocation["gpu_memory_fraction"] = min(
        1.0,
        min(
            allocation["vram_gb"] / max(0.001, float(gpu.get("total_vram_gb") or gpu.get("available_vram_gb") or 0.001))
            for gpu in selected_gpus
        ),
    )
    return allocation


def _write_client_request(channel_dir: Path, message: dict[str, Any]) -> None:
    request_id = str(message["request_id"])
    _atomic_write_json(channel_dir / "requests" / f"{request_id}.json", message)


def _wait_for_response(channel_dir: Path, request_id: str, *, timeout: float, required: bool) -> dict[str, Any]:
    path = channel_dir / "responses" / f"{request_id}.json"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            path.unlink(missing_ok=True)
            if isinstance(value, dict):
                return value
        except FileNotFoundError:
            pass
        except Exception:
            pass
        time.sleep(0.05)
    if required:
        raise ResourceUnavailable(f"resource broker did not respond within {timeout:.0f}s")
    return {"ok": False, "error": "resource broker response timeout"}


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temp, path)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        import psutil  # type: ignore

        return psutil.pid_exists(pid) and psutil.Process(pid).is_running()
    except Exception:
        try:
            os.kill(pid, 0)
            return True
        except PermissionError:
            return True
        except OSError:
            return False


def _append_event(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
