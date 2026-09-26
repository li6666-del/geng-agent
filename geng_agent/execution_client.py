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


def _io_path(path: Path) -> Path:
    if os.name != "nt":
        return path
    value = str(path.absolute())
    if value.startswith("\\\\?\\"):
        return path
    return Path("\\\\?\\UNC\\" + value[2:] if value.startswith("\\\\") else "\\\\?\\" + value)


def _wait_for_result(queue: Path, request_id: str, task_id: str, *, heartbeat_timeout: float = 30.0) -> dict:
    result_path = queue / (request_id + ".result.json")
    started = time.time()
    while True:
        if result_path.is_file():
            return json.loads(result_path.read_text(encoding="utf-8"))
        try:
            status = json.loads((queue / "status.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            status = {}
        request = status.get("requests", {}).get(request_id, {})
        if request.get("task_id") == task_id and request.get("request_id") == request_id:
            receipt = request.get("receipt")
            if request.get("state") == "completed" and isinstance(receipt, dict):
                if receipt.get("task_id") == task_id and receipt.get("observer") == "orchestration_host":
                    return receipt  # Same host receipt through an independent delivery path.
            if request.get("state") == "failed":
                return request.get("result") or {"returncode": 1, "error": "Host execution failed"}
        try:
            heartbeat = json.loads((queue / "heartbeat.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            heartbeat = status
        if heartbeat.get("broker_state") == "stopped":
            return {"returncode": 1, "error_kind": "host_unavailable", "error": "Host stopped before delivering this request"}
        try:
            updated = float(heartbeat.get("updated_at", started))
        except (ValueError, TypeError):
            updated = started
        if time.time() - updated > heartbeat_timeout:
            return {"returncode": 1, "error_kind": "host_unavailable", "error": "Host heartbeat expired; inspect existing receipts before resubmitting"}
        time.sleep(0.2)


def main():
    parser = argparse.ArgumentParser(description="Run one task with its scientific configuration")
    parser.add_argument("--task", required=True)
    parser.add_argument("--config")
    parser.add_argument("--mode", choices=("smoke", "full"), default="full")
    parser.add_argument("--device", choices=("cpu", "gpu", "auto"), default="auto",
                        help="Select CPU or GPU; auto uses a visible GPU, otherwise CPU. GPU runs can execute concurrently.")
    parser.add_argument("--input", action="append", default=[], help="Persistent data/checkpoint consumed by this run, relative to project")
    parser.add_argument("--submit", action="store_true", help="Submit to the active host and return before completion")
    parser.add_argument("--status", action="store_true", help="Read the host's current task execution status")
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
        queue = _io_path(root / ".geng_execution" / session)
        status_path = queue / "status.json"
        status = {}
        if status_path.is_file():
            status = json.loads(status_path.read_text(encoding="utf-8")).get("tasks", {}).get(args.task, {})
        if args.status:
            print(json.dumps(status or {"task_id": args.task, "state": "not_started"}, ensure_ascii=True))
            return 0
        if status.get("state") in {"starting", "running"}:
            print(json.dumps({"state": "in_flight_conflict", "active_execution": status,
                "error": "Wait for the active task execution, then resubmit if another run is scientifically required."}, ensure_ascii=True))
            return 75  # A launch request must never look like a completed success.
        run_id = uuid.uuid4().hex
        path = queue / (run_id + ".request.json")
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"task_id": args.task, "config": config,
            "mode": args.mode, "device": args.device, "inputs": args.input}), encoding="utf-8")
        temporary.replace(path)
        if args.submit:
            print(json.dumps({"task_id": args.task, "state": "submitted", "request_id": run_id,
                              "status_command": f"python run_task.py --task {args.task} --status"}))
            return 0
        result = _wait_for_result(queue, run_id, args.task)
        print(json.dumps(result, ensure_ascii=True))
        return int(result.get("returncode", 1))
    if args.status or args.submit:
        print(json.dumps({"state": "host_unavailable", "error": "--status and --submit require an active orchestration host"}))
        return 1
    # Third-party execution does not claim observation by the original host.
    module = importlib.import_module("tasks." + entry["module"])
    result = module.main(config)
    return result if isinstance(result, int) and not isinstance(result, bool) else 0


if __name__ == "__main__":
    raise SystemExit(main())
