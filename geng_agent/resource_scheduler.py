from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import os
import platform
import subprocess
from typing import Any

from .config import get_config_value


DEFAULT_WRITER_INITIAL_CONCURRENCY = 2
DEFAULT_WRITER_MAX_CONCURRENCY = 4
DEFAULT_WRITER_SUCCESS_WINDOW = 3
DEFAULT_WRITER_CAPACITY_RETRIES = 2
DEFAULT_WRITER_RETRY_BASE_SECONDS = 60.0


def detect_hardware() -> dict[str, Any]:
    logical = max(1, int(os.cpu_count() or 1))
    physical = _physical_cpu_count(logical)
    total_gb, available_gb = _memory_gb()
    gpus = _nvidia_gpus()
    torch_info = _torch_info()
    return {
        "schema_version": "1.0",
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "platform": platform.platform(),
        "cpu": {
            "physical_cores": physical,
            "logical_processors": logical,
        },
        "memory": {
            "total_gb": round(total_gb, 3),
            "available_gb": round(available_gb, 3),
        },
        "gpus": gpus,
        "torch": torch_info,
    }


def build_resource_plan(
    *,
    task_count: int,
    requested_writer_concurrency: int | None = None,
    hardware: dict[str, Any] | None = None,
) -> dict[str, Any]:
    snapshot = hardware or detect_hardware()
    task_count = max(0, int(task_count))
    memory = snapshot.get("memory") if isinstance(snapshot.get("memory"), dict) else {}
    cpu = snapshot.get("cpu") if isinstance(snapshot.get("cpu"), dict) else {}
    gpus = snapshot.get("gpus") if isinstance(snapshot.get("gpus"), list) else []
    total_gb = max(1.0, float(memory.get("total_gb") or 1.0))
    available_gb = max(0.0, float(memory.get("available_gb") or total_gb))
    reserve_default = min(max(2.0, min(8.0, total_gb * 0.25)), max(0.75, available_gb * 0.25))
    reserve_gb = max(0.5, _config_float("GENG_RESOURCE_RAM_RESERVE_GB", reserve_default))
    ram_budget_gb = max(0.75, available_gb - reserve_gb)
    writer_ram_gb = max(0.25, _config_float("GENG_RESOURCE_WRITER_RAM_GB", 0.75))
    memory_writer_cap = max(1, int(ram_budget_gb // writer_ram_gb))

    explicit = requested_writer_concurrency
    if explicit is None:
        legacy = get_config_value("GENG_CODEX_TASK_WRITER_CONCURRENCY")
        explicit = _parse_positive_int(legacy)
    configured_max = _config_int("GENG_CODEX_TASK_WRITER_MAX_CONCURRENCY", DEFAULT_WRITER_MAX_CONCURRENCY)
    if explicit is not None:
        writer_max = min(max(1, explicit), max(1, task_count or 1), memory_writer_cap)
        writer_initial = writer_max
        writer_source = "explicit"
    else:
        writer_max = min(max(1, configured_max), max(1, task_count or 1), memory_writer_cap)
        writer_initial = min(writer_max, DEFAULT_WRITER_INITIAL_CONCURRENCY if task_count > 1 else 1)
        writer_source = "adaptive"

    physical_cores = max(1, int(cpu.get("physical_cores") or cpu.get("logical_processors") or 1))
    cpu_budget = max(1, int(math.floor(physical_cores * 0.75)))
    gpu_slots = max(1, _config_int("GENG_TASK_WRITER_GPU_FULL_SLOTS", 1))
    cpu_full_default = max(1, min(cpu_budget // 4 or 1, int(ram_budget_gb // 3.0) or 1))
    cpu_full_max = max(1, _config_int("GENG_TASK_WRITER_CPU_FULL_SLOTS", cpu_full_default))

    planned_gpus: list[dict[str, Any]] = []
    for fallback_index, item in enumerate(gpus):
        if not isinstance(item, dict):
            continue
        planned_gpus.append(
            {
                "index": int(item.get("index", fallback_index)),
                "name": str(item.get("name") or f"GPU {fallback_index}"),
                "total_vram_gb": max(0.0, float(item.get("total_vram_gb") or 0.0)),
                "available_vram_gb": max(0.0, float(item.get("available_vram_gb") or item.get("total_vram_gb") or 0.0)),
                "max_full_jobs": gpu_slots,
            }
        )

    return {
        "schema_version": "1.0",
        "task_count": task_count,
        "hardware": snapshot,
        "writer": {
            "mode": writer_source,
            "initial_concurrency": writer_initial,
            "max_concurrency": writer_max,
            "minimum_concurrency": 1,
            "successes_before_increase": max(
                1, _config_int("GENG_CODEX_TASK_WRITER_SUCCESS_WINDOW", DEFAULT_WRITER_SUCCESS_WINDOW)
            ),
            "capacity_retries": max(
                0, _config_int("GENG_CODEX_TASK_WRITER_CAPACITY_RETRIES", DEFAULT_WRITER_CAPACITY_RETRIES)
            ),
            "retry_base_seconds": max(
                0.0,
                _config_float("GENG_CODEX_TASK_WRITER_RETRY_BASE_SECONDS", DEFAULT_WRITER_RETRY_BASE_SECONDS),
            ),
            "memory_cap": memory_writer_cap,
            "estimated_ram_per_writer_gb": writer_ram_gb,
        },
        "execution": {
            "cpu_cores_budget": cpu_budget,
            "ram_budget_gb": round(ram_budget_gb, 3),
            "ram_reserve_gb": round(reserve_gb, 3),
            "cpu_full_max": cpu_full_max,
            "gpus": planned_gpus,
            "resource_poll_seconds": max(0.1, _config_float("GENG_RESOURCE_POLL_SECONDS", 1.0)),
            "enforcement_poll_seconds": max(
                0.05, _config_float("GENG_RESOURCE_ENFORCEMENT_POLL_SECONDS", 0.1)
            ),
            "stale_lease_seconds": max(60.0, _config_float("GENG_RESOURCE_STALE_LEASE_SECONDS", 300.0)),
            "resource_wait_timeout_seconds": max(
                1.0, _config_float("GENG_RESOURCE_WAIT_TIMEOUT_SECONDS", 1800.0)
            ),
        },
        "reasons": [
            f"writer concurrency is independent from full execution slots ({writer_source})",
            f"memory cap={memory_writer_cap} from available={available_gb:.2f}GB reserve={reserve_gb:.2f}GB",
            f"execution budget={cpu_budget} physical CPU cores and {ram_budget_gb:.2f}GB RAM",
            f"detected GPU count={len(planned_gpus)}; default max full jobs per GPU={gpu_slots}",
        ],
    }


class WriterConcurrencyController:
    def __init__(self, writer_plan: dict[str, Any]) -> None:
        self.minimum = max(1, int(writer_plan.get("minimum_concurrency") or 1))
        self.maximum = max(self.minimum, int(writer_plan.get("max_concurrency") or self.minimum))
        self.current = max(self.minimum, min(self.maximum, int(writer_plan.get("initial_concurrency") or self.minimum)))
        self.success_window = max(1, int(writer_plan.get("successes_before_increase") or 3))
        self.success_streak = 0

    def record_success(self) -> tuple[int, int]:
        before = self.current
        self.success_streak += 1
        if self.success_streak >= self.success_window and self.current < self.maximum:
            self.current += 1
            self.success_streak = 0
        return before, self.current

    def record_capacity_error(self) -> tuple[int, int]:
        before = self.current
        self.current = max(self.minimum, int(math.ceil(self.current / 2.0)))
        self.success_streak = 0
        return before, self.current

    def record_other_failure(self) -> tuple[int, int]:
        self.success_streak = 0
        return self.current, self.current


def _physical_cpu_count(logical: int) -> int:
    try:
        import psutil  # type: ignore

        value = psutil.cpu_count(logical=False)
        if value:
            return max(1, int(value))
    except Exception:
        pass
    return max(1, logical // 2 if logical >= 4 else logical)


def _memory_gb() -> tuple[float, float]:
    try:
        import psutil  # type: ignore

        memory = psutil.virtual_memory()
        return float(memory.total) / (1024**3), float(memory.available) / (1024**3)
    except Exception:
        pass
    if os.name == "nt":
        try:
            import ctypes

            class MemoryStatus(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = MemoryStatus()
            status.dwLength = ctypes.sizeof(MemoryStatus)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return status.ullTotalPhys / (1024**3), status.ullAvailPhys / (1024**3)
        except Exception:
            pass
    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
        total_pages = os.sysconf("SC_PHYS_PAGES")
        available_pages = os.sysconf("SC_AVPHYS_PAGES")
        return page_size * total_pages / (1024**3), page_size * available_pages / (1024**3)
    except Exception:
        return 8.0, 4.0


def _nvidia_gpus() -> list[dict[str, Any]]:
    command = [
        "nvidia-smi",
        "--query-gpu=index,name,memory.total,memory.free,utilization.gpu",
        "--format=csv,noheader,nounits",
    ]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=5, check=False)
    except Exception:
        return []
    if completed.returncode != 0:
        return []
    result: list[dict[str, Any]] = []
    for line in completed.stdout.splitlines():
        parts = [item.strip() for item in line.split(",")]
        if len(parts) < 5:
            continue
        try:
            result.append(
                {
                    "index": int(parts[0]),
                    "name": parts[1],
                    "total_vram_gb": round(float(parts[2]) / 1024.0, 3),
                    "available_vram_gb": round(float(parts[3]) / 1024.0, 3),
                    "utilization_percent": float(parts[4]),
                }
            )
        except ValueError:
            continue
    return result


def _torch_info() -> dict[str, Any]:
    try:
        import torch  # type: ignore

        return {
            "available": True,
            "version": str(torch.__version__),
            "cuda_available": bool(torch.cuda.is_available()),
            "cuda_device_count": int(torch.cuda.device_count()) if torch.cuda.is_available() else 0,
        }
    except Exception as exc:
        return {
            "available": False,
            "version": None,
            "cuda_available": False,
            "cuda_device_count": 0,
            "error": f"{type(exc).__name__}: {exc}",
        }


def _parse_positive_int(raw: str | None) -> int | None:
    if raw is None:
        return None
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return None


def _config_int(name: str, default: int) -> int:
    raw = get_config_value(name)
    try:
        return int(raw) if raw is not None else int(default)
    except (TypeError, ValueError):
        return int(default)


def _config_float(name: str, default: float) -> float:
    raw = get_config_value(name)
    try:
        return float(raw) if raw is not None else float(default)
    except (TypeError, ValueError):
        return float(default)


def compact_plan_json(plan: dict[str, Any]) -> str:
    return json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
