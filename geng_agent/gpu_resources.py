"""Select a CUDA device without reserving it or serializing experiments."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class ComputeDevice:
    request: str
    gpu_uuid: str | None
    cuda_visible_devices: str


def _visible_gpu_devices() -> list[str]:
    """Return physical UUIDs, honoring an existing host CUDA visibility limit."""
    try:
        completed = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,uuid", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if completed.returncode:
        return []
    devices = []
    for line in completed.stdout.splitlines():
        index, separator, uuid = line.partition(",")
        if separator and index.strip().isdigit() and uuid.strip().startswith("GPU-"):
            devices.append((index.strip(), uuid.strip()))
    allowed = os.environ.get("CUDA_VISIBLE_DEVICES")
    if allowed is not None:
        tokens = [token.strip() for token in allowed.split(",") if token.strip()]
        devices = [(index, uuid) for index, uuid in devices
                   if any(token == index or uuid.startswith(token) for token in tokens)]
    return [uuid for _index, uuid in devices]


def select_compute(request: str) -> ComputeDevice:
    """Choose visibility only; independent runs may share the same GPU.

    ``auto`` uses a visible GPU when available, otherwise CPU. Explicit CPU
    requests keep CUDA hidden, and explicit GPU requests never silently fall
    back to CPU. No reservation is held for the selected device.
    """
    if request not in {"cpu", "gpu", "auto"}:
        raise ValueError("device must be cpu, gpu, or auto")
    if request == "cpu":
        return ComputeDevice(request, None, "")
    devices = _visible_gpu_devices()
    if not devices:
        if request == "gpu":
            raise RuntimeError("GPU requested but no CUDA GPU is visible to the host")
        return ComputeDevice(request, None, "")
    return ComputeDevice(request, devices[0], devices[0])
