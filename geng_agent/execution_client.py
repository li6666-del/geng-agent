"""Portable task entry point. Copied verbatim into generated projects."""
from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
import time
import uuid
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="Run one task with its scientific configuration")
    parser.add_argument("--task", required=True)
    parser.add_argument("--config")
    parser.add_argument("--mode", choices=("smoke", "full"), default="full")
    parser.add_argument("--input", action="append", default=[], help="Persistent data/checkpoint consumed by this run, relative to project")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    os.chdir(root)
    entries = json.loads((root / "tasks_manifest.json").read_text(encoding="utf-8"))["tasks"]
    entry = next(t for t in entries if t["task_id"] == args.task)
    config = args.config or entry.get("config_smoke" if args.mode == "smoke" else "config_full") or "config.json"
    session = os.environ.get("GENG_EXECUTION_BROKER")
    if session:
        if not session.isalnum():
            raise ValueError("invalid execution session")
        queue = root / ".geng_execution" / session
        run_id = uuid.uuid4().hex
        path = queue / (run_id + ".request.json")
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"task_id": args.task, "config": config,
            "mode": args.mode, "inputs": args.input}), encoding="utf-8")
        temporary.replace(path)
        result_path = queue / (run_id + ".result.json")
        while not result_path.exists():
            time.sleep(0.2)
        result = json.loads(result_path.read_text(encoding="utf-8"))
        print(json.dumps(result, ensure_ascii=True))
        return int(result.get("returncode", 1))
    # Third-party execution does not claim observation by the original host.
    module = importlib.import_module("tasks." + entry["module"])
    result = module.main(config)
    return result if isinstance(result, int) and not isinstance(result, bool) else 0


if __name__ == "__main__":
    raise SystemExit(main())
