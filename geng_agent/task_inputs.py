"""Verified, transactional transfer of explicitly addressed upstream artifacts."""
from pathlib import Path, PurePosixPath, PureWindowsPath
import shutil
import hashlib
import json
import os
from uuid import uuid4
from .artifact_paths import file_sha256, scan_tree
from .paper_evidence import safe_label
from .outputs import write_json
from .execution_receipts import _inside


class UpstreamHandoffError(RuntimeError):
    """A mechanical delivery failure, not a scientific result or a training run."""


def dependencies(task):
    raw = task.get("depends_on") or []
    if isinstance(raw, (str, dict)):
        raw = [raw]
    return [item if isinstance(item, dict) else {"task_id": str(item)} for item in raw]


def handoff_path(root: Path, audit_dir: Path) -> Path:
    address = hashlib.sha256(os.path.normcase(str(root.resolve())).encode()).hexdigest()
    return audit_dir / "upstream_handoffs" / address / "current.json"


def _files(root, relative):
    source = _inside(root, relative)
    if source.is_dir():
        files, _dirs, links, special = scan_tree(source)
        if links or special:
            raise ValueError("artifact directory contains links or special files")
        if not files:
            raise ValueError("required artifact directory is empty")
        return [(p, p.relative_to(source).as_posix()) for p in files]
    if not source.is_file():
        raise FileNotFoundError(f"required artifact missing: {relative}")
    return [(source, None)]


def _relative(value):
    text = str(value).replace("\\", "/")
    if not text or ":" in text or PureWindowsPath(text).drive or text.startswith("/") or ".." in PurePosixPath(text).parts:
        raise ValueError("artifact address must be a relative path without traversal")
    return PurePosixPath(text).as_posix()


def _bindings(record, requested):
    """Resolve exact exports; never guess a file from a stem, glob or similarity."""
    root = Path(record["sandbox"])
    result = record.get("result_json") or {}
    exports = result.get("artifact_exports", [])
    identifier = requested if isinstance(requested, str) else requested.get("artifact_id")
    matches = [e for e in exports if isinstance(e, dict) and e.get("artifact_id") == identifier]
    if len(matches) > 1:
        raise ValueError(f"ambiguous export: {identifier}")
    if matches:
        bindings = matches[0].get("files")
        if not isinstance(bindings, list) or not bindings:
            raise ValueError(f"empty export: {identifier}")
        return [(_relative(b["source"]), _relative(b["target"])) for b in bindings]
    if isinstance(requested, dict):
        if not requested.get("source") or not requested.get("target"):
            raise ValueError(f"producer must publish artifact_exports for {identifier}")
        return [(_relative(requested["source"]), _relative(requested["target"]))]
    relative = _relative(requested)
    candidates = [relative]
    unit_id = record.get("execution_unit_id")
    if unit_id and not relative.startswith("execution_units/"):
        candidates.append(f"execution_units/{unit_id}/{relative}")
    available = [p for p in candidates if _inside(root, p).exists()]
    if len(available) != 1:
        raise ValueError(f"required artifact address missing or ambiguous: {relative}; publish an explicit artifact_exports mapping")
    return [(available[0], relative)]


def plan_upstream_inputs(task: dict, records: list[dict]) -> dict:
    index = {str(r.get("task_id")): r for r in records}
    inputs, targets = [], {}
    for dependency in dependencies(task):
        task_id = str(dependency.get("task_id") or "")
        record = index.get(task_id, {})
        requested = dependency.get("artifacts")
        if not requested:
            root = Path(record.get("sandbox") or "")
            exports = (record.get("result_json") or {}).get("artifact_exports", [])
            receipt = (record.get("host_execution") or {}).get("receipt") or {}
            requested = [e["artifact_id"] for e in exports if isinstance(e, dict) and e.get("artifact_id")]
            requested = requested or sorted({**receipt.get("produced_artifacts", {}), **receipt.get("output_hashes", {})})
            if not requested:
                requested = [p for p in ("outputs", "execution_units") if record.get("sandbox")
                             and (root / p).is_dir() and any((root / p).iterdir())]
            requested = requested or ["outputs"]
        if isinstance(requested, (str, dict)):
            requested = [requested]
        entry = {"task_id": task_id, "requested": requested, "files": [], "copied": [], "errors": []}
        for item in requested:
            try:
                if record.get("writer_completed") is False or not record.get("sandbox"):
                    raise ValueError("producer has not delivered a completed workspace")
                root = Path(record["sandbox"])
                for source, target in _bindings(record, item):
                    for file, suffix in _files(root, source):
                        source_rel = file.relative_to(root).as_posix()
                        _inside(root, source_rel)
                        target_rel = f"upstream_tasks/{safe_label(task_id)}/{target}"
                        if suffix is not None:
                            target_rel += "/" + suffix
                        _inside(root, target_rel)
                        digest = file_sha256(file)
                        row = {"source": source_rel, "target": target_rel, "sha256": digest,
                               "bytes": file.stat().st_size, "producer_task_id": task_id,
                               "producer_workspace": str(root.resolve())}
                        key = target_rel.replace("\\", "/").casefold()
                        if key in targets and targets[key] != row:
                            raise ValueError(f"conflicting export destination: {target_rel}")
                        if key not in targets:
                            targets[key] = row
                            entry["files"].append(row)
            except (KeyError, TypeError, OSError, ValueError) as exc:
                entry["errors"].append({"path": item, "error": str(exc)})
        inputs.append(entry)
    return {"schema_version": 2, "state": "planned", "inputs": inputs}


def upstream_input_identity(task: dict, records: list[dict]) -> str:
    plan = plan_upstream_inputs(task, records)
    return hashlib.sha256(json.dumps(plan, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def legacy_upstream_input_identity(task: dict, records: list[dict]) -> str:
    """Read old cache addresses without changing or rerunning an existing case."""
    index = {str(record.get("task_id")): record for record in records}
    inventory = []
    for dependency in dependencies(task):
        task_id = str(dependency.get("task_id") or "")
        record, hashes = index.get(task_id, {}), {}
        paths = dependency.get("artifacts") or ["outputs", "execution_units"]
        for relative in [paths] if isinstance(paths, str) else paths:
            try:
                root = Path(record["sandbox"])
                source = _inside(root, str(relative))
                for file in scan_tree(source)[0] if source.is_dir() else [source]:
                    hashes[file.relative_to(root).as_posix()] = file_sha256(file)
            except (KeyError, OSError, ValueError) as exc:
                hashes[str(relative)] = {"unavailable": type(exc).__name__}
        inventory.append({"task_id": task_id, "files": hashes,
                          "producer_inputs": record.get("analysis_snapshot_hash")})
    return hashlib.sha256(json.dumps(inventory, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def copy_upstream_inputs(sandbox: Path, task: dict, records: list[dict], *, audit_dir: Path | None = None) -> dict | None:
    if not dependencies(task):
        return None
    from .upstream_lineage import bind_origins
    from .supervisor import current_supervisor
    supervisor = current_supervisor()
    def cancelled():
        if supervisor is not None:
            supervisor._check_cancelled()
    cancelled()
    plan = plan_upstream_inputs(task, records)
    by_id = {str(record.get("task_id")): record for record in records}
    for entry in plan["inputs"]:
        producer = by_id.get(entry["task_id"], {})
        entry["producer_observations"] = {key: producer[key] for key in (
            "task_id", "execution_unit_id", "writer_completed", "task_writer_status",
            "result_json", "task_verification") if key in producer}
    plan.update(workspace=str(sandbox.resolve()), transfer_id=uuid4().hex)
    public = _inside(sandbox, "upstream_task_inputs.json")
    trusted = handoff_path(sandbox, audit_dir) if audit_dir is not None else None
    def publish(state):
        plan["state"] = state
        write_json(public, plan)
        if trusted is not None:
            write_json(trusted, plan)
        if supervisor is not None:
            supervisor.tools.event("upstream_handoff", "writers", state=state,
                                   file_count=sum(len(e["files"]) for e in plan["inputs"]),
                                   transfer_id=plan["transfer_id"], workspace=str(sandbox))
    try:
        if any(e["errors"] for e in plan["inputs"]):
            raise UpstreamHandoffError(json.dumps([e["errors"] for e in plan["inputs"]], ensure_ascii=False))
        if audit_dir is not None:
            bind_origins(plan, audit_dir)
        rows = [f for e in plan["inputs"] for f in e["files"]]
        active = _inside(sandbox, "upstream_tasks")
        if active.is_dir():
            existing, _dirs, links, special = scan_tree(active)
            if not links and not special and {p.relative_to(sandbox).as_posix() for p in existing} == {f["target"] for f in rows}:
                if all(file_sha256(_inside(sandbox, f["target"])) == f["sha256"] for f in rows):
                    for entry in plan["inputs"]:
                        entry["copied"] = [f["target"] for f in entry["files"]]
                    cancelled()
                    publish("verified")
                    return plan
        stage = _inside(sandbox, f".geng_handoff/{plan['transfer_id']}")
        stage.mkdir(parents=True, exist_ok=False)
        publish("copying")
        for row in rows:
            cancelled()
            source = _inside(Path(row["producer_workspace"]), row["source"])
            destination = _inside(stage, row["target"])
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            if (destination.stat().st_size != row["bytes"] or file_sha256(destination) != row["sha256"]
                    or file_sha256(source) != row["sha256"]):
                raise UpstreamHandoffError(f"artifact changed during copy: {row['source']}")
        cancelled()
        previous = None
        if active.exists():
            previous = _inside(sandbox, f"writer_progress/upstream_{plan['transfer_id'][:12]}")
            previous.parent.mkdir(parents=True, exist_ok=True)
            active.rename(previous)
        try:
            (stage / "upstream_tasks").rename(active)
        except BaseException:
            if previous is not None and not active.exists():
                previous.rename(active)
            raise
        for entry in plan["inputs"]:
            entry["copied"] = [f["target"] for f in entry["files"]]
        publish("verified")
        return plan
    except BaseException as exc:
        # Preserve previous inputs and failed staging bytes, including on cancellation.
        plan["error"] = f"{type(exc).__name__}: {exc}"
        publish("failed")
        if isinstance(exc, (OSError, ValueError, KeyError, TypeError)):
            raise UpstreamHandoffError(plan["error"]) from exc
        raise


def require_verified_handoff(root: Path, audit_dir: Path) -> dict | None:
    """Gate a new run before outputs are archived or a GPU is selected."""
    from .upstream_lineage import upstream_input_issues
    trusted = handoff_path(root, audit_dir)
    if not trusted.exists():
        if (root / "upstream_task_inputs.json").exists():
            raise UpstreamHandoffError("upstream delivery has no host-verified manifest; rebind inputs before execution")
        return None
    manifest = json.loads(trusted.read_text(encoding="utf-8"))
    if manifest.get("state") != "verified" or manifest.get("workspace") != str(root.resolve()):
        raise UpstreamHandoffError("upstream delivery is not verified; repair transfer before execution")
    files = {f["target"]: f["sha256"] for e in manifest["inputs"] for f in e["files"]}
    issues = upstream_input_issues(root, audit_dir, files, required_mode="full")
    if issues:
        raise UpstreamHandoffError("; ".join(issues))
    return manifest
