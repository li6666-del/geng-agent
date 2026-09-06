"""Keep host execution evidence verifiable after project assembly relocates files."""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from .execution_receipts import _inside, file_hash
from .outputs import write_json
from .paper_evidence import safe_label


def package_execution_evidence(project: Path, records: list[dict[str, Any]]) -> set[str]:
    """Publish original host receipts plus explicit old-to-delivery byte mappings.

    Assembly can rename configurations and normalize Python text. The host
    receipt is never rewritten to pretend those delivered bytes were executed.
    Only differing executed inputs are archived, and only if their original
    bytes still match the host observation.
    """
    expected: set[str] = {"execution_evidence.json"}
    tasks = []
    for record in records:
        host = record.get("host_execution")
        receipt = host.get("receipt") if isinstance(host, dict) else None
        if not isinstance(receipt, dict) or receipt.get("observer") != "orchestration_host":
            continue
        sandbox = Path(str(record.get("sandbox") or ""))
        task_id = str(record.get("task_id") or receipt.get("task_id") or "")
        output = str(record.get("output_subdir") or task_id)
        receipt_relative = f"outputs/{output}/execution_receipt.json"
        write_json(_inside(project, receipt_relative), receipt)
        expected.add(receipt_relative)
        mappings = []
        for group in ("source_hashes", "input_hashes", "output_hashes"):
            for relative, digest in receipt.get(group, {}).items():
                item = {"kind": group, "original_path": relative, "sha256": digest,
                        "packaged_path": None, "verified": False}
                candidates = [relative]
                module = str(record.get("module") or "")
                if relative in {"config.json", "config_smoke.json"}:
                    candidates.insert(0, f"configs/{module}_{relative}")
                for candidate in candidates:
                    try:
                        path = _inside(project, candidate)
                        if path.is_file() and file_hash(path) == digest:
                            item.update(packaged_path=candidate, verified=True)
                            break
                    except (OSError, ValueError):
                        continue
                if not item["verified"] and group != "output_hashes":
                    try:
                        original = _inside(sandbox, relative)
                        if original.is_file() and file_hash(original) == digest:
                            archive_relative = f"execution_records/{safe_label(task_id)}/{relative}"
                            if Path(relative).suffix == ".py":
                                archive_relative += ".original"
                            archived = _inside(project, archive_relative)
                            archived.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copyfile(original, archived)
                            expected.add(archive_relative)
                            item.update(packaged_path=archive_relative, verified=True)
                    except (OSError, ValueError):
                        pass
                mappings.append(item)
        tasks.append({"task_id": task_id, "run_id": receipt.get("run_id"),
                      "receipt": receipt_relative, "host_execution_passed": bool(host.get("passed")),
                      "host_issues": host.get("issues", []),
                      "all_bytes_available": bool(mappings) and all(item["verified"] for item in mappings),
                      "files": mappings})
    write_json(project / "execution_evidence.json", {"schema_version": 1,
        "meaning": "Original host observations; path mappings describe assembly, not a new scientific execution.",
        "tasks": tasks})
    return expected
