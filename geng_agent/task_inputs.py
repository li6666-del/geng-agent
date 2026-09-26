"""Copy explicitly requested upstream artifacts without judging their results."""
from pathlib import Path
import shutil
import hashlib
import json
from uuid import uuid4
from .artifact_paths import file_sha256, scan_tree
from .paper_evidence import safe_label
from .outputs import write_json
from .execution_receipts import _inside


def dependencies(task):
    raw = task.get("depends_on") or []
    if isinstance(raw, (str, dict)):
        raw = [raw]
    return [item if isinstance(item, dict) else {"task_id": str(item)} for item in raw]


def upstream_input_identity(task: dict, records: list[dict]) -> str:
    """A cache address for supplied files, never a judgment about their contents."""
    index = {str(record.get("task_id")): record for record in records}
    inventory = []
    for dependency in dependencies(task):
        task_id = str(dependency.get("task_id") or "")
        record = index.get(task_id, {})
        hashes = {}
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


def copy_upstream_inputs(sandbox: Path, task: dict, records: list[dict]) -> None:
    if not dependencies(task):
        return
    active = _inside(sandbox, "upstream_tasks")
    if active.exists():
        previous = _inside(sandbox, f"writer_progress/upstream_{uuid4().hex[:12]}")
        previous.parent.mkdir(parents=True, exist_ok=True)
        active.rename(previous)
    index = {str(record.get("task_id")): record for record in records}
    handoff = []
    for dependency in dependencies(task):
        task_id = str(dependency.get("task_id") or "")
        record = index.get(task_id, {})
        copied, errors = [], []
        root = Path(record["sandbox"]) if record.get("sandbox") else None
        requested = dependency.get("artifacts") or ["outputs", "execution_units"]
        if isinstance(requested, str):
            requested = [requested]
        for relative in requested:
            try:
                if root is None:
                    raise FileNotFoundError("producer workspace unavailable")
                source = _inside(root, str(relative))
                target = _inside(sandbox, f"upstream_tasks/{safe_label(task_id)}/{relative}")
                target.parent.mkdir(parents=True, exist_ok=True)
                if source.is_dir():
                    for file in scan_tree(source)[0]:
                        destination = _inside(sandbox, (target / file.relative_to(source)).relative_to(sandbox).as_posix())
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(file, destination)
                else:
                    shutil.copy2(source, target)
                copied.append(target.relative_to(sandbox).as_posix())
            except (OSError, ValueError) as exc:
                errors.append({"path": str(relative), "error": str(exc)})
        handoff.append({"task_id": task_id, "requested": requested, "copied": copied,
                        "errors": errors, "producer_observations": record})
    write_json(sandbox / "upstream_task_inputs.json", {"inputs": handoff})
