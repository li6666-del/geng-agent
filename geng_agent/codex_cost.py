"""Append-only Codex invocation accounting; absent usage is never zero usage."""
from __future__ import annotations

import json
from pathlib import Path
import time
from typing import Any
import uuid

TOKEN_FIELDS = ("prompt_tokens", "cached_prompt_tokens", "completion_tokens", "total_tokens")


def parse_codex_usage(transcript: str) -> dict[str, int] | None:
    """Sum completed turns in JSONL, ignoring unrelated output and partial turns."""
    turns = []
    for line in transcript.splitlines():
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(event, dict) or event.get("type") != "turn.completed":
            continue
        usage = event.get("usage")
        if not isinstance(usage, dict):
            continue
        inp, out = usage.get("input_tokens"), usage.get("output_tokens")
        if any(isinstance(x, bool) or not isinstance(x, int) or x < 0 for x in (inp, out)):
            continue
        cached = usage.get("cached_input_tokens", 0)
        if isinstance(cached, bool) or not isinstance(cached, int) or cached < 0:
            cached = 0
        turns.append(dict(prompt_tokens=inp, cached_prompt_tokens=cached,
                          completion_tokens=out, total_tokens=inp + out))
    return {key: sum(turn[key] for turn in turns) for key in TOKEN_FIELDS} if turns else None


def record_codex_invocation(audit_dir: Path, status: dict[str, Any], transcript: str,
                            *, started_at: float) -> dict[str, Any]:
    event = {"schema_version": "1.0", "invocation_id": uuid.uuid4().hex,
             "started_at": started_at, "finished_at": time.time(),
             "role": status.get("role"), "model": status.get("model"),
             "ok": bool(status.get("ok")), "duration_s": status.get("duration_s"),
             "usage": parse_codex_usage(transcript), "cost_usd": None,
             "usage_complete": bool(status.get("ok"))}
    case_audit = next((path for path in (audit_dir, *audit_dir.parents) if path.name == "audit"), audit_dir)
    directory = case_audit / "codex_usage_events"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{event['invocation_id']}.json"
    staging = target.with_suffix(".tmp")
    staging.write_text(json.dumps(event, ensure_ascii=False, indent=2), encoding="utf-8")
    staging.replace(target)
    return event


def summarize_codex_usage(audit_dir: Path, *, since: float | None = None) -> dict[str, Any]:
    events: dict[str, dict[str, Any]] = {}
    for path in audit_dir.rglob("codex_usage_events/*.json") if audit_dir.exists() else []:
        try:
            event = json.loads(path.read_text(encoding="utf-8"))
            if since is not None and float(event["started_at"]) < since:
                continue
            events[str(event["invocation_id"])] = event
        except (OSError, ValueError, KeyError, TypeError):
            continue
    # Pre-ledger transcripts establish that work occurred, but cannot establish
    # complete historical usage. Do not show a historical Codex case as 0 tokens.
    if since is None and audit_dir.exists():
        for path in audit_dir.rglob("*_transcript.txt"):
            status_path = path.with_name(path.name.removesuffix("_transcript.txt") + ".json")
            try:
                status = json.loads(status_path.read_text(encoding="utf-8"))
                if status.get("backend") == "codex" and status.get("role") and not status.get("cost_event"):
                    key = "legacy:" + path.relative_to(audit_dir).as_posix()
                    events[key] = {"role": status["role"], "usage": None, "usage_complete": False}
            except (OSError, ValueError, TypeError):
                continue
    known = [event for event in events.values() if isinstance(event.get("usage"), dict)]
    complete = len(known) == len(events) and all(event.get("usage_complete", True) for event in events.values())
    missing = sum(not isinstance(event.get("usage"), dict) or not event.get("usage_complete", True) for event in events.values())
    return {"llm_calls": len(events), "usage_complete": complete,
            "calls_with_usage": len(known), "calls_missing_usage": missing,
            "observed_tokens": {key: sum(event["usage"].get(key, 0) for event in known) for key in TOKEN_FIELDS},
            **{key: sum(event["usage"].get(key, 0) for event in known) if complete else None for key in TOKEN_FIELDS},
            "cost_usd": None, "currency_note": "No token price is inferred for account-based Codex usage.",
            "by_role": {role: sum(event.get("role") == role for event in events.values())
                        for role in sorted({str(event.get("role") or "unknown") for event in events.values()})}}


def persist_pipeline_cost(output_dir: Path, run_cost: dict[str, Any], *, run_id: str, started_at: float) -> None:
    """Keep each invocation's API/time deltas; publish both latest and cumulative."""
    from .outputs import write_json
    directory = output_dir / "audit" / "pipeline_cost_events"
    directory.mkdir(parents=True, exist_ok=True)
    event = {"run_id": run_id, "started_at": started_at, "finished_at": time.time(),
             "wall_clock_s": run_cost.get("wall_clock_s"),
             "llm_api_totals": run_cost.get("llm_api_totals", run_cost.get("totals", {}))}
    write_json(directory / f"{run_id}.json", event)
    events = []
    for path in directory.glob("*.json"):
        try:
            events.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    cumulative_codex = summarize_codex_usage(output_dir / "audit")
    cumulative_api = {key: sum(int(item.get("llm_api_totals", {}).get(key) or 0) for item in events)
                      for key in ("llm_calls", "prompt_tokens", "completion_tokens", "total_tokens")}
    run_cost["cumulative"] = {"pipeline_invocations": len(events),
        "wall_clock_s": round(sum(float(item.get("wall_clock_s") or 0) for item in events), 3),
        "llm_api_totals": cumulative_api, "codex": cumulative_codex,
        "totals": {key: cumulative_api[key] + cumulative_codex[key] if cumulative_codex.get(key) is not None else None
                   for key in cumulative_api}}
    run_cost["run_id"] = run_id
    run_cost["started_at"] = started_at
    write_json(output_dir / "run_cost.json", run_cost)
