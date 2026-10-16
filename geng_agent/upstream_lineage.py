"""Host-owned source bindings for copies crossing Writer workspace boundaries."""
import json
from pathlib import Path
from .artifact_paths import file_sha256
from .execution_receipts import _inside


def _origin_check(row, receipt, audit_dir, *, required_mode, ancestry, cache):
    from .execution_receipts import _producer_chain_issues
    root = Path(row["producer_workspace"])
    key = (str(root.resolve()), row["producer_run_id"], required_mode)
    if key in ancestry:
        raise ValueError("cyclic upstream producer chain")
    if key not in cache:
        if (receipt.get("observer") != "orchestration_host" or receipt.get("returncode") != 0
                or receipt.get("inputs_stable") is not True or receipt.get("dependency_issues")
                or receipt.get("cancelled") or receipt.get("task_id") != row["producer_task_id"]
                or receipt.get("mode") not in ({"smoke", "full"} if required_mode == "smoke" else {"full"})):
            raise ValueError("producer receipt is not a stable successful execution")
        if receipt.get("workspace") and receipt["workspace"] != str(root.resolve()):
            raise ValueError("producer workspace does not match receipt")
        for path, digest in {**receipt.get("source_hashes", {}), **receipt.get("input_hashes", {})}.items():
            if file_sha256(_inside(root, path)) != digest:
                raise ValueError(f"producer source/input changed: {path}")
        issues = _producer_chain_issues(root, audit_dir, receipt.get("input_hashes", {}),
                                       required_mode=required_mode, _upstream_ancestry=ancestry | {key})
        # Keep the same runtime/source ancestry checks used for local generated
        # state. Relocation must not turn a stale checkpoint into a valid one.
        if row["source"].startswith(("execution_units/", "outputs/")):
            issues += _producer_chain_issues(root, audit_dir, {row["source"]: row["sha256"]},
                                            required_mode=required_mode, _upstream_ancestry=ancestry | {key})
        if issues:
            raise ValueError("; ".join(issues))
        cache.add(key)
    recorded = {**receipt.get("input_hashes", {}), **receipt.get("output_hashes", {}),
                **receipt.get("produced_artifacts", {})}
    if recorded.get(row["source"]) != row["sha256"]:
        raise ValueError("published artifact does not match producer receipt")
    if file_sha256(_inside(root, row["source"])) != row["sha256"]:
        raise ValueError("producer artifact changed after publication")


def bind_origins(manifest, audit_dir):
    """Choose real host receipts, never Writer-supplied receipt contents."""
    from .task_inputs import UpstreamHandoffError
    candidates = []
    for path in (audit_dir / "execution_runs").glob("*/execution_receipt.json"):
        try:
            receipt = json.loads(path.read_text(encoding="utf-8"))
            candidates.append((path, receipt))
        except (OSError, ValueError):
            continue
    candidates.sort(key=lambda p: p[1].get("finished_at", 0), reverse=True)
    cache = set()
    for entry in manifest["inputs"]:
        for row in entry["files"]:
            for path, receipt in candidates:
                if receipt.get("task_id") != row["producer_task_id"]:
                    continue
                recorded = {**receipt.get("input_hashes", {}), **receipt.get("output_hashes", {}),
                            **receipt.get("produced_artifacts", {})}
                if recorded.get(row["source"]) != row["sha256"]:
                    continue
                row["producer_run_id"] = path.parent.name
                try:
                    _origin_check(row, receipt, audit_dir, required_mode="full", ancestry=set(), cache=cache)
                except (OSError, ValueError, KeyError):
                    continue
                row["producer_receipt_sha256"] = file_sha256(path)
                break
            else:
                raise UpstreamHandoffError(f"no current producer receipt for {row['producer_task_id']}:{row['source']}")


def upstream_input_issues(root, audit_dir, inputs, *, required_mode="full", ancestry=None):
    from .task_inputs import handoff_path
    selected = {p: h for p, h in inputs.items() if p.startswith("upstream_tasks/")}
    if not selected:
        return []
    try:
        manifest = json.loads(handoff_path(root, audit_dir).read_text(encoding="utf-8"))
        if manifest.get("state") != "verified" or manifest.get("workspace") != str(root.resolve()):
            raise ValueError("upstream manifest is not verified")
        rows = {f["target"]: f for e in manifest["inputs"] for f in e["files"]}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return [f"upstream source unavailable: {exc}"]
    issues, cache = [], set()
    for path, digest in selected.items():
        try:
            row = rows[path]
            if row["sha256"] != digest or file_sha256(_inside(root, path)) != digest:
                raise ValueError("consumer copy differs from verified delivery")
            receipt_path = _inside(audit_dir, f"execution_runs/{row['producer_run_id']}/execution_receipt.json")
            if file_sha256(receipt_path) != row["producer_receipt_sha256"]:
                raise ValueError("producer receipt changed")
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            _origin_check(row, receipt, audit_dir, required_mode=required_mode,
                          ancestry=ancestry or set(), cache=cache)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            issues.append(f"upstream source invalid: {path}: {exc}")
    return issues
