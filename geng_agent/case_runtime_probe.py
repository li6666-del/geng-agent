"""Command adaptation, capability probes, trusted roots, and no-follow reads."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import stat
from typing import Any, Mapping, Sequence

from packaging.utils import canonicalize_name

from .case_environment import (
    ArgvRunner,
    CommandResult,
    EnvironmentPolicyError,
    normalize_requirement,
)
from .case_runtime_contracts import (
    EnvironmentResolutionError,
    _CAPABILITY_PROBE_PREFIX,
    _request_for_runtime_name,
)


def _run_checked(
    runner: ArgvRunner,
    argv: Sequence[str],
    *,
    cwd: Path | None,
    timeout: float,
) -> CommandResult:
    try:
        result = runner(tuple(str(part) for part in argv), cwd=cwd, timeout=timeout)
    except Exception as exc:
        raise EnvironmentResolutionError(
            "environment_command_failed",
            f"host environment command failed: {type(exc).__name__}",
        ) from exc
    if isinstance(result, CommandResult):
        return result
    args = result.args if isinstance(result.args, Sequence) and not isinstance(result.args, str) else argv
    return CommandResult(
        argv=tuple(str(part) for part in args),
        returncode=int(result.returncode),
        stdout=result.stdout or "",
        stderr=result.stderr or "",
    )


def _probe_runtime_capabilities(
    *,
    case_python: Path,
    architecture: Mapping[str, Any] | None,
    output_dir: Path,
    run_argv: ArgvRunner,
) -> list[dict[str, Any]]:
    requests: list[dict[str, Any]] = []
    components = architecture.get("components") if isinstance(architecture, Mapping) else []
    for index, component in enumerate(components if isinstance(components, list) else []):
        if not isinstance(component, Mapping):
            continue
        execution = component.get("execution")
        if not isinstance(execution, Mapping):
            continue
        framework = str(execution.get("primary_framework") or "").strip()
        policy = str(execution.get("device_policy") or "cpu")
        component_id = str(component.get("id") or f"component_{index}")
        if policy == "external_runtime":
            executable = shutil.which(framework.casefold()) or shutil.which(framework)
            requests.append({
                "component_id": component_id,
                "framework": framework,
                "device_policy": policy,
                "external_executable": executable or "",
            })
            continue
        package_request = _request_for_runtime_name(
            framework,
            requested_by=f"architecture:{component_id}",
            reason="capability probe",
            capability=policy,
        )
        if package_request is None:
            continue
        distribution = canonicalize_name(normalize_requirement(package_request).distribution)
        requests.append({
            "component_id": component_id,
            "framework": distribution,
            "device_policy": policy,
            "external_executable": "",
            "trainable": execution.get("trainable") is True,
            "gradient_mode": str(execution.get("gradient_mode") or "not_applicable"),
            "checkpoint_policy": str(execution.get("checkpoint_policy") or "not_applicable"),
            "required_capabilities": [
                str(value)
                for value in execution.get("required_capabilities") or ()
                if str(value)
            ],
        })
    if not requests:
        return []
    result = _run_checked(
        run_argv,
        (
            str(case_python),
            "-I",
            "-c",
            _CAPABILITY_PROBE_SCRIPT,
            json.dumps({"requests": requests}, separators=(",", ":")),
        ),
        cwd=output_dir,
        timeout=180.0,
    )
    if result.returncode != 0:
        return [{
            "component_id": "host_probe",
            "framework": "unknown",
            "device_policy": "unknown",
            "ok": False,
            "error": (result.stderr or result.stdout or "capability probe failed")[-4000:],
        }]
    for line in reversed(result.stdout.splitlines()):
        if line.startswith(_CAPABILITY_PROBE_PREFIX):
            try:
                payload = json.loads(line[len(_CAPABILITY_PROBE_PREFIX):])
            except json.JSONDecodeError:
                break
            values = payload.get("capabilities") if isinstance(payload, Mapping) else None
            if isinstance(values, list):
                return [dict(item) for item in values if isinstance(item, Mapping)]
    return [{
        "component_id": "host_probe",
        "framework": "unknown",
        "device_policy": "unknown",
        "ok": False,
        "error": "capability probe returned no structured result",
    }]


def _trusted_runtime_roots(lock: Mapping[str, Any], python: Path) -> tuple[Path, ...]:
    identity = lock.get("interpreter") if isinstance(lock.get("interpreter"), Mapping) else {}
    values = [python.parent, identity.get("prefix"), identity.get("base_prefix")]
    sys_path = identity.get("sys_path")
    if isinstance(sys_path, list):
        values.extend(sys_path)
    roots: set[Path] = set()
    for value in values:
        if not value:
            continue
        try:
            roots.add(Path(str(value)).resolve())
        except OSError:
            continue
    return tuple(sorted(roots, key=lambda item: str(item)))


def _assert_regular_nofollow(path: Path) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise EnvironmentPolicyError(f"cannot inspect environment request: {exc}") from exc
    if (
        path.is_symlink()
        or _is_reparse_point(path)
        or not stat.S_ISREG(info.st_mode)
        or info.st_nlink > 1
    ):
        raise EnvironmentPolicyError("environment request must be a regular non-link file")


def _read_regular_file_nofollow(
    path: Path,
    *,
    max_bytes: int,
    allow_missing: bool,
) -> bytes | None:
    """Read and validate from one descriptor so links cannot be swapped after inspection."""

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if os.name != "nt":
        flags |= getattr(os, "O_NOFOLLOW", 0)
    elif path.exists():
        _assert_regular_nofollow(path)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        if allow_missing:
            return None
        raise EnvironmentPolicyError("environment request is missing") from None
    except OSError as exc:
        raise EnvironmentPolicyError("environment request could not be opened safely") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise EnvironmentPolicyError("environment request must be a regular non-link file")
        if info.st_size > max_bytes:
            raise EnvironmentPolicyError("environment request is too large")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            raw = handle.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise EnvironmentPolicyError("environment request is too large")
        return raw
    finally:
        os.close(descriptor)


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = path.lstat().st_file_attributes
    except (AttributeError, OSError):
        return False
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


_CAPABILITY_PROBE_SCRIPT = r"""
import io
import json
import os
import shutil
import sys
import tempfile

def real_path(value):
    try:
        return os.path.realpath(os.fspath(value))
    except (OSError, TypeError, ValueError):
        return ""

allowed_roots = {
    real_path(path)
    for path in [sys.prefix, sys.base_prefix, os.environ.get("HOME"), *sys.path]
    if path
}
allowed_roots.update(
    path for path in (
        "/usr/lib", "/usr/lib64", "/lib", "/lib64", "/usr/share",
        "/etc/ssl/certs", "/dev", "/proc/cpuinfo", "/proc/meminfo",
        "/proc/self", "/proc/driver/nvidia", "/sys/bus/pci/devices",
    )
    if os.path.exists(path)
)
writable_root = real_path(os.environ.get("HOME"))

def inside(path, roots):
    resolved = real_path(path)
    return bool(resolved) and any(
        resolved == root or resolved.startswith(root + os.sep) for root in roots if root
    )

def audit(event, args):
    if event.startswith("socket."):
        raise PermissionError("network is disabled during capability probes")
    if (
        event == "subprocess.Popen" or event == "os.system"
        or event.startswith("os.spawn") or event.startswith("os.posix_spawn")
        or event.startswith("os.exec") or event in {"os.fork", "os.forkpty"}
    ):
        raise PermissionError("process creation is disabled during capability probes")
    if event == "open" and args and not isinstance(args[0], int):
        mode = args[1] if len(args) > 1 else None
        flags = args[2] if len(args) > 2 else None
        writing = (
            isinstance(mode, str) and any(mark in mode for mark in ("w", "a", "x", "+"))
        ) or (
            isinstance(flags, int)
            and bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
        )
        if writing and not inside(args[0], {writable_root}):
            raise PermissionError("writes outside capability probe scratch space are disabled")
        if not writing and not inside(args[0], allowed_roots):
            raise PermissionError("reads outside capability probe runtime roots are disabled")

sys.addaudithook(audit)

request = json.loads(sys.argv[1])
results = []
for item in request.get("requests", []):
    result = dict(item)
    framework = str(item.get("framework") or "").lower()
    policy = str(item.get("device_policy") or "cpu")
    trainable = item.get("trainable") is True
    gradient_required = str(item.get("gradient_mode") or "").lower() == "required"
    checkpoint_required = str(item.get("checkpoint_policy") or "").lower() == "required"
    declared_capabilities = [str(value) for value in item.get("required_capabilities") or ()]
    advanced_required = bool(
        trainable or gradient_required or checkpoint_required
        or policy == "accelerator_required"
    )
    try:
        if policy == "external_runtime":
            executable = item.get("external_executable")
            if not executable or not shutil.which(executable):
                raise RuntimeError("external runtime executable is unavailable")
            result["evidence"] = {"executable": executable}
        elif framework == "torch":
            import torch
            device = "cuda" if policy == "accelerator_required" else "cpu"
            if device == "cuda" and not torch.cuda.is_available():
                raise RuntimeError("CUDA is required but torch.cuda.is_available() is false")
            evidence = {
                "version": torch.__version__,
                "device": device,
                "cuda_available": bool(torch.cuda.is_available()),
            }
            if trainable or gradient_required:
                parameter = torch.nn.Parameter(torch.tensor([1.0, 2.0], device=device))
                before = parameter.detach().clone()
                loss = (parameter * parameter).sum()
                loss.backward()
                if parameter.grad is None or not bool(torch.isfinite(parameter.grad).all()):
                    raise RuntimeError("autograd did not produce finite gradients")
                torch.optim.SGD([parameter], lr=0.1).step()
                if trainable and bool(torch.equal(before, parameter.detach())):
                    raise RuntimeError("optimizer step did not change a trainable parameter")
                evidence["gradient"] = parameter.grad.detach().cpu().tolist()
                evidence["parameter_updated"] = not bool(torch.equal(before, parameter.detach()))
            if checkpoint_required:
                buffer = io.BytesIO()
                torch.save({"value": torch.tensor([1.0, 2.0])}, buffer)
                buffer.seek(0)
                restored = torch.load(buffer, map_location="cpu", weights_only=True)
                if not bool(torch.equal(restored["value"], torch.tensor([1.0, 2.0]))):
                    raise RuntimeError("torch checkpoint round-trip changed state")
                evidence["checkpoint_roundtrip"] = True
            result["evidence"] = evidence
        elif framework == "tensorflow":
            import tensorflow as tf
            gpus = tf.config.list_physical_devices("GPU")
            if policy == "accelerator_required" and not gpus:
                raise RuntimeError("GPU is required but TensorFlow sees no GPU")
            evidence = {"version": tf.__version__, "gpu_count": len(gpus)}
            variable = tf.Variable([1.0, 2.0])
            if trainable or gradient_required:
                before = variable.numpy().copy()
                with tf.GradientTape() as tape:
                    loss = tf.reduce_sum(variable * variable)
                gradient = tape.gradient(loss, variable)
                if gradient is None:
                    raise RuntimeError("GradientTape produced no gradient")
                tf.keras.optimizers.SGD(0.1).apply_gradients([(gradient, variable)])
                if trainable and bool((before == variable.numpy()).all()):
                    raise RuntimeError("optimizer step did not change a trainable variable")
                evidence["parameter_updated"] = not bool((before == variable.numpy()).all())
            if checkpoint_required:
                checkpoint_dir = tempfile.mkdtemp(prefix="tf-checkpoint-", dir=os.environ["HOME"])
                path = tf.train.Checkpoint(value=variable).write(os.path.join(checkpoint_dir, "state"))
                restored = tf.Variable([0.0, 0.0])
                tf.train.Checkpoint(value=restored).restore(path).assert_existing_objects_matched()
                if not bool((restored.numpy() == variable.numpy()).all()):
                    raise RuntimeError("TensorFlow checkpoint round-trip changed state")
                evidence["checkpoint_roundtrip"] = True
            result["evidence"] = evidence
        elif framework == "jax":
            import jax
            import jax.numpy as jnp
            devices = jax.devices()
            if policy == "accelerator_required" and not any(d.platform == "gpu" for d in devices):
                raise RuntimeError("GPU is required but JAX sees no GPU device")
            evidence = {
                "version": jax.__version__,
                "devices": [d.platform for d in devices],
            }
            value = jnp.array([1.0, 2.0])
            if trainable or gradient_required:
                gradient = jax.grad(lambda z: jnp.sum(z * z))(value)
                updated = value - 0.1 * gradient
                if trainable and bool(jnp.array_equal(value, updated)):
                    raise RuntimeError("JAX parameter update did not change state")
                evidence["gradient"] = [float(v) for v in gradient]
                evidence["parameter_updated"] = not bool(jnp.array_equal(value, updated))
            if checkpoint_required:
                import numpy as np
                path = os.path.join(os.environ["HOME"], "jax_state.npy")
                np.save(path, np.asarray(value))
                if not bool(np.array_equal(np.load(path), np.asarray(value))):
                    raise RuntimeError("JAX array checkpoint round-trip changed state")
                evidence["checkpoint_roundtrip"] = True
            result["evidence"] = evidence
        elif framework == "numpy":
            import numpy as np
            if advanced_required:
                raise RuntimeError(
                    "NumPy cannot prove requested training, gradient, checkpoint, or accelerator capabilities"
                )
            matrix = np.array([[1.0, 2.0], [3.0, 4.0]])
            value = float(np.linalg.det(matrix @ matrix.T))
            if not np.isfinite(value):
                raise RuntimeError("NumPy linear algebra returned a non-finite value")
            result["evidence"] = {"version": np.__version__, "linear_algebra": value}
        else:
            if advanced_required:
                raise RuntimeError(
                    "no trusted generic probe can verify this framework's requested advanced capabilities"
                )
            result["evidence"] = {
                "mode": "import_and_version_probe_from_case_lock",
                "advanced_capabilities_required": False,
            }
        if declared_capabilities:
            result["evidence"]["declared_component_capabilities"] = declared_capabilities
            result["evidence"]["declared_component_capabilities_verification"] = (
                "deferred_to_foundation_or_task_runtime_tests"
            )
        result["ok"] = True
        result["error"] = None
    except BaseException as exc:
        result["ok"] = False
        result["error"] = type(exc).__name__ + ": " + str(exc)
    results.append(result)

print("GENG_CASE_CAPABILITY_JSON:" + json.dumps({"capabilities": results}, sort_keys=True))
"""
