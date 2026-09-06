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

from .outputs import write_json


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
    consumed: set[str] = set()
    for imported in roots:
        name = str(imported).split(".")[0]
        consumed.update(names.get(name, set()))
        canonical = canonicalize_name(name)
        canonical = aliases.get(canonical, canonical)
        if canonical in versions:
            consumed.add(canonical)
    executed_versions = {canonicalize_name(str(item[0])): str(item[1])
                         for item in (inventory or {}).get("packages", [])
                         if isinstance(item, (list, tuple)) and len(item) == 2}
    return {name: executed_versions.get(name, versions[name]) for name in sorted(consumed)}


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

    def __init__(self, root: Path, audit_dir: Path, python: Path, *, environment_hash: str = ""):
        self.root, self.audit_dir, self.python = root.resolve(), audit_dir.resolve(), python
        self.environment_hash = environment_hash
        self.session_id = uuid.uuid4().hex
        self.queue = self.root / ".geng_execution" / self.session_id
        manifest_path = _inside(self.root, "tasks_manifest.json")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
        # A dependency-only interrupted session may have no scaffold yet. It
        # can request an environment, but cannot launch an undeclared task.
        self.entries = {str(t["task_id"]): dict(t) for t in manifest.get("tasks", [])}
        self.receipts: list[dict[str, Any]] = []
        self.process: subprocess.Popen | None = None
        self.cancelled = threading.Event()
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self):
        self.queue.mkdir(parents=True, exist_ok=True)
        self.thread.start()
        return self

    def __exit__(self, exc_type, *_args):
        self.stopped.set()
        # Never launch a duplicate full after the worker CLI exits. Complete
        # the scientific process already in flight and preserve its result.
        if exc_type is not None:
            self.cancelled.set()
            self._stop_process()
        try:
            self.thread.join()
        except KeyboardInterrupt:
            self.cancelled.set()
            self._stop_process()
            self.thread.join(timeout=5)
            raise

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
        while not self.stopped.is_set():
            for path in sorted(self.queue.glob("*.request.json")):
                if path.name in handled:
                    continue
                handled.add(path.name)
                result_path = path.with_name(path.name.replace(".request.", ".result."))
                try:
                    request = json.loads(_inside(self.root, path.relative_to(self.root).as_posix()).read_text(encoding="utf-8"))
                    receipt = self.execute(request)
                    self.receipts.append(receipt)
                    self._publish_response(result_path, receipt)
                except Exception as exc:
                    try:
                        self._publish_response(result_path, {"returncode": 1, "error": f"{type(exc).__name__}: {exc}"})
                    except (ValueError, OSError):
                        # An unsafe Writer-controlled response path must never
                        # redirect a host write outside the sandbox.
                        write_json(self.audit_dir / "execution_runs" / f"rejected_{uuid.uuid4().hex}.json",
                                   {"error": "unsafe execution request or response path"})
            self.stopped.wait(0.15)

    def _publish_response(self, path: Path, value: dict[str, Any]) -> None:
        relative = path.relative_to(self.root).as_posix()
        target = _inside(self.root, relative)
        temporary = _inside(self.root, relative + "." + uuid.uuid4().hex + ".tmp")
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=True)
        _inside(self.root, relative)
        temporary.replace(target)

    def execute(self, request: dict[str, Any]) -> dict[str, Any]:
        from .codex_runner import _FOUNDATION_UNITTEST_GUARD, _foundation_unittest_guard_config
        from .security_env import build_safe_env

        task_id = str(request.get("task_id") or "")
        entry = self.entries[task_id]
        config_rel = str(request.get("config") or entry.get("config_full") or "config.json")
        config = _inside(self.root, config_rel)
        if not config.is_file() or config.suffix.lower() != ".json":
            raise ValueError("execution requires an existing project JSON configuration")
        config_doc = json.loads(config.read_text(encoding="utf-8-sig"))
        if str(request.get("mode") or "full") == "full" and (
            config_rel == entry.get("config_smoke") or config_doc.get("smoke") is True
            or str(config_doc.get("run_profile") or config_doc.get("profile") or "").lower() == "smoke"
        ):
            raise ValueError("a smoke configuration cannot establish a full execution")
        module = str(entry["module"])
        if not module.isidentifier():
            raise ValueError("invalid task module")
        output_rel = str(entry.get("output_subdir") or task_id)
        output = _inside(self.root, f"outputs/{output_rel}")
        output.mkdir(parents=True, exist_ok=True)
        run_id = uuid.uuid4().hex
        run_dir = self.audit_dir / "execution_runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        before = source_hashes(self.root)
        before[config_rel] = file_hash(config)
        input_paths = set(map(str, request.get("inputs", []))) | _configuration_file_inputs(self.root, config_doc)
        inputs = {p: file_hash(_inside(self.root, p)) for p in input_paths}
        asset_stats_before = _persistent_asset_stats(self.root)
        dependency_errors = self._check_producers(inputs, required_mode=str(request.get("mode") or "full"))
        if dependency_errors:
            raise ValueError("; ".join(dependency_errors))
        old_outputs = artifact_hashes(self.root, output_rel)
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
        guard_config = _foundation_unittest_guard_config(work_dir=self.root, start_dir="tasks",
            python_executable=self.python, write_roots=(output, assets, runtime_home))
        guard_config.update(task_module=module, task_config=config_rel, task_output_prefix=f"outputs/{output_rel}/")
        environment_before = probe_execution_environment(self.python)
        if not environment_before.get("ok"):
            raise RuntimeError("Selected execution environment could not be inventoried")
        prefix, separator, _tail = _FOUNDATION_UNITTEST_GUARD.partition("\nimport unittest\n")
        if not separator:
            raise RuntimeError("scientific process guard is unavailable")
        guard = prefix + _TASK_READ_TRACE + "\nimport importlib\n_TRACE_INITIAL_MODULES = set(sys.modules)\ntry:\n    _MODULE = importlib.import_module('tasks.' + _CONFIG['task_module'])\n    _RESULT = _MODULE.main(_CONFIG['task_config'])\nfinally:\n    _TRACE_STREAM.write('GENG_OBSERVED_MODULES ' + json.dumps(sorted({name.split('.')[0] for name in set(sys.modules) - _TRACE_INITIAL_MODULES})) + '\\n')\n    _TRACE_STREAM.flush()\nraise SystemExit(_RESULT if isinstance(_RESULT, int) and not isinstance(_RESULT, bool) else 0)\n"
        started = time.time()
        write_json(run_dir / "started.json", {"run_id": run_id, "task_id": task_id,
                   "source_hashes": before, "input_hashes": inputs, "started_at": started})
        from .execution_sandbox import scientific_sandbox_launch
        launch = scientific_sandbox_launch([str(self.python), "-I", "-B", "-c", guard, json.dumps(guard_config)],
            work_dir=self.root, write_roots=(output, assets, runtime_home), env=env)
        with (run_dir / "stdout.log").open("w", encoding="utf-8") as stdout, (run_dir / "stderr.log").open("w", encoding="utf-8") as stderr:
            process = subprocess.Popen(launch["command"], cwd=self.root, env=launch["env"], stdout=stdout, stderr=stderr,
                start_new_session=os.name != "nt", creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0)
            self.process = process
            if self.cancelled.is_set():
                self._stop_process()
            returncode = process.wait()
            self.process = None
        environment_after = probe_execution_environment(self.python)
        environment_stable = bool(environment_after.get("ok")) and environment_before["sha256"] == environment_after.get("sha256")
        after = source_hashes(self.root)
        after[config_rel] = file_hash(config)
        output_hashes = artifact_hashes(self.root, output_rel)
        asset_stats_after = _persistent_asset_stats(self.root)
        stderr_text = (run_dir / "stderr.log").read_text(encoding="utf-8", errors="replace")
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
        sources[config_rel] = before.get(config_rel) or file_hash(config)
        inputs.update({p: h for p, h in observed_reads.items() if p not in sources})
        dependency_issues = self._check_producers(inputs, required_mode=str(request.get("mode") or "full"))
        # Native scientific serializers (e.g. PyTorch's C++ zip writer) do not
        # emit Python open events. Observe their real filesystem products too.
        written_paths.update(p for p, metadata in asset_stats_after.items() if asset_stats_before.get(p) != metadata)
        produced = {p: file_hash(_inside(self.root, p)) for p in written_paths if _inside(self.root, p).is_file()}
        inputs_stable = environment_stable and before == after and all(file_hash(_inside(self.root, p)) == h for p, h in inputs.items())
        observed_distributions = _observed_runtime_distributions(
            self.python, observed_import_roots, environment_before.get("inventory"),
        )
        receipt = {"schema_version": 1, "observer": "orchestration_host", "run_id": run_id,
            "task_id": task_id, "output_subdir": output_rel,
            "mode": str(request.get("mode") or "full"), "config": config_rel,
            "python_executable": str(self.python), "environment_hash": self.environment_hash,
            "sandbox_policy": launch["policy"],
            "environment_observation": {"before": environment_before, "after": environment_after,
                "stable": environment_stable, "probe_duration_s": round(environment_before["duration_s"] + environment_after["duration_s"], 4)},
            "started_at": started, "finished_at": time.time(), "returncode": returncode,
            "pid": process.pid, "pid_kind": "os_sandbox_supervisor", "source_hashes": sources,
            "source_snapshot_hashes": before, "input_hashes": inputs,
            "inputs_stable": inputs_stable, "output_hashes": output_hashes,
            "produced_artifacts": produced, "dependency_issues": dependency_issues,
            "observed_import_roots": observed_import_roots,
            "observed_distributions": observed_distributions,
            "input_observation_scope": "Python file events plus explicit --input and configuration file paths; native loads require one of those declarations",
            "cancelled": self.cancelled.is_set(),
            "unchanged_output_paths": sorted(p for p, h in output_hashes.items() if old_outputs.get(p) == h),
            "stderr_tail": "\n".join(line for line in stderr_text.splitlines() if not line.startswith("GENG_OBSERVED_"))[-12000:]}
        write_json(run_dir / "execution_receipt.json", receipt)
        write_json(output / "execution_receipt.json", receipt)
        return receipt

    def _check_producers(self, inputs: dict[str, str], *, required_mode: str = "full") -> list[str]:
        """An old generated checkpoint cannot silently outlive its training recipe."""
        return _producer_chain_issues(self.root, self.audit_dir, inputs,
                                      required_mode=required_mode, python_executable=self.python)


def _producer_chain_issues(
    root: Path, audit_dir: Path, inputs: dict[str, str], *, required_mode: str = "full",
    python_executable: Path | None = None,
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


_TASK_READ_TRACE = r'''
import hashlib
import importlib.machinery
# -B prevents new bytecode, but still reads existing __pycache__. Load only
# project modules from source so the read trace binds the code actually run.
# Runtime/site-packages retain their bytecode caches (notably large ML stacks).
_ORIGINAL_SOURCE_GET_CODE = importlib.machinery.SourceFileLoader.get_code
def _case_source_get_code(loader, fullname):
    filename = loader.get_filename(fullname)
    path = _real_path(filename)
    if path is not None and _inside(path, _WORK_DIR):
        return loader.source_to_code(loader.get_data(filename), filename)
    return _ORIGINAL_SOURCE_GET_CODE(loader, fullname)
importlib.machinery.SourceFileLoader.get_code = _case_source_get_code
_TRACE_BUSY = False
_TRACE_READS = set()
_TRACE_WRITES = set()
_TRACE_STREAM = sys.stderr
def _trace_task_reads(event, args):
    global _TRACE_BUSY
    if event != 'open' or not args or _TRACE_BUSY:
        return
    path = _real_path(args[0])
    if path is None or not _inside(path, _WORK_DIR):
        return
    relative = os.path.relpath(path, _WORK_DIR).replace('\\', '/')
    if relative.startswith(('.geng_runtime/', '.geng_execution/', _CONFIG['task_output_prefix'])) or '__pycache__' in relative:
        return
    if _open_is_write(args[1] if len(args)>1 else None, args[2] if len(args)>2 else None):
        if relative not in _TRACE_WRITES:
            _TRACE_STREAM.write('GENG_OBSERVED_WRITE ' + json.dumps({'path': relative}) + '\n')
            _TRACE_STREAM.flush()
        _TRACE_WRITES.add(relative)
        return
    if relative in _TRACE_READS or relative in _TRACE_WRITES:
        return
    _TRACE_BUSY = True
    try:
        digest = hashlib.sha256()
        with open(path, 'rb') as source:
            for block in iter(lambda: source.read(1024 * 1024), b''):
                digest.update(block)
        _TRACE_READS.add(relative)
        _TRACE_STREAM.write('GENG_OBSERVED_READ ' + json.dumps({'path': relative, 'sha256': digest.hexdigest()}) + '\n')
        _TRACE_STREAM.flush()
    finally:
        _TRACE_BUSY = False
sys.addaudithook(_trace_task_reads)
'''
