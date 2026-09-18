"""Run four small live model sessions: two Writers and two Reporters.

This diagnoses model-session concurrency and tool execution, not scientific
reproduction capability or the provider's internal compute parallelism. Each
session executes one prewritten standard-library script in its own workspace.
Credentials are read by the normal project configuration; never pass a key on
the command line. This opt-in command makes at most four model session calls.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextvars import copy_context
from datetime import datetime
import gzip
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from uuid import uuid4


CHECKOUT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CHECKOUT))

from geng_agent.agent_activity import agent_activity_scope
from geng_agent.codex_runner import run_codex_subprocess
from geng_agent.config import get_config_value
from geng_agent.model_config import CodexModelConfig, load_model_config, model_config_scope
from geng_agent.outputs import write_json


ROLES = ("task_writer", "task_writer", "task_reporter", "task_reporter")
SCRIPT_NAME = "execution_probe.py"
RESULT_NAME = "execution_result.json"


def prepare_output(path: Path) -> Path:
    if path.is_symlink():
        raise ValueError("The output directory must not be a symlink.")
    output = path.expanduser().resolve()
    if output == CHECKOUT or CHECKOUT in output.parents:
        raise ValueError("Probe outputs must be outside the project checkout.")
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError("The output directory already exists and is not empty; choose a new directory.")
    output.mkdir(parents=True, exist_ok=True)
    return output


def _source(token: str) -> str:
    return (
        "# Prewritten standard-library concurrency diagnostic; no scientific experiment.\n"
        "import json, os, time\nfrom pathlib import Path\n"
        "started = time.time_ns()\ntime.sleep(1.0)\n"
        f"result = {{'token': {token!r}, 'pid': os.getpid(), 'started_ns': started, "
        "'finished_ns': time.time_ns()}\n"
        f"Path({RESULT_NAME!r}).write_text(json.dumps(result), encoding='utf-8')\n"
        f"print({token!r}, flush=True)\n"
    )


def _sha256(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def _executed_command(status: dict, token: str) -> bool:
    transcript = Path(str(status.get("full_transcript") or status.get("transcript") or ""))
    if not transcript.is_file():
        return False
    opener = gzip.open if transcript.suffix == ".gz" else open
    try:
        with opener(transcript, "rt", encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                item = event.get("item") if isinstance(event, dict) else None
                if (isinstance(item, dict) and event.get("type") == "item.completed"
                        and item.get("type") == "command_execution" and item.get("exit_code") == 0
                        and SCRIPT_NAME in str(item.get("command") or "")
                        and token in str(item.get("aggregated_output") or "")):
                    return True
    except (OSError, EOFError):
        return False
    return False


def _run_session(output: Path, index: int, role: str, config: CodexModelConfig) -> dict:
    label = f"{index:02d}_{role}"
    workspace = output / "workspaces" / label
    workspace.mkdir(parents=True)
    token = "CONCURRENCY_PROBE_" + uuid4().hex
    script = workspace / SCRIPT_NAME
    script.write_text(_source(token), encoding="utf-8")
    expected_hash = _sha256(script)
    result = {"label": label, "role": role, "ok": False, "checks": {},
              "workspace": str(workspace), "source_sha256": expected_hash}
    try:
        status = run_codex_subprocess(
            role=role, work_dir=workspace,
            prompt=(
                "This is a tiny model-session concurrency diagnostic, not a paper reproduction. "
                f"Use your shell tool to execute the existing {SCRIPT_NAME} exactly once with this Python "
                f"interpreter: {sys.executable}. Use the quoting and invocation syntax required by your shell. "
                "The script only uses the standard library, waits one second, writes execution_result.json "
                "and prints a marker. Do not edit any file, install anything, read other directories, "
                "search the web, or start another agent. If shell execution is blocked, report the error "
                "and stop; do not substitute file editing or retry sandbox setup. After execution, "
                "return the printed marker and stop."
            ),
            audit_dir=output / "audit" / label, label="probe", sandbox="workspace-write",
            extra_env={"GENG_PYTHON": sys.executable, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        observed = {}
        try:
            observed = json.loads((workspace / RESULT_NAME).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        if not isinstance(observed, dict):
            observed = {}
        valid_times = all(isinstance(observed.get(key), int) and not isinstance(observed[key], bool)
                          for key in ("pid", "started_ns", "finished_ns"))
        checks = {
            "model_session_succeeded": status.get("ok") is True,
            "same_model_configuration": status.get("model_config") == config.identity(),
            "expected_result_token": observed.get("token") == token,
            "script_unchanged": _sha256(script) == expected_hash,
            "successful_command_observed": _executed_command(status, token),
            "execution_process_recorded": bool(valid_times and observed["pid"] > 0
                                                and observed["finished_ns"] > observed["started_ns"]),
        }
        result.update(ok=all(checks.values()), checks=checks,
                      invocation_id=status.get("invocation_id"), duration_s=status.get("duration_s"),
                      returncode=status.get("returncode"), error_kind=status.get("error_kind"),
                      execution={key: observed.get(key) for key in ("pid", "started_ns", "finished_ns")})
    except Exception as exc:
        # Exception details may contain provider-supplied text. The normal
        # transport audit holds redacted diagnostics; the summary keeps types.
        result["error_kind"] = type(exc).__name__
    return result


def _overlap_seconds(intervals: list[tuple[float, float]]) -> float:
    return max(0.0, min(end for _, end in intervals) - max(start for start, _ in intervals)) if intervals else 0.0


def run_probe(config: CodexModelConfig, output: Path) -> dict:
    summary = {
        "diagnostic": "live_model_session_concurrency", "status": "running",
        "scope": "Model sessions and local tool execution only; not scientific capability or provider internal compute parallelism.",
        "planned_sessions": len(ROLES), "started_at": time.time(),
        "model": {"provider": config.provider, "model": config.model,
                  "reasoning_effort": config.reasoning_effort, "managed": config.managed},
        "sessions": [], "checks": {},
    }
    write_json(output / "summary.json", summary)
    with model_config_scope(config), agent_activity_scope(output / "audit") as activity:
        with ThreadPoolExecutor(max_workers=len(ROLES)) as executor:
            futures = [executor.submit(copy_context().run, _run_session, output, index, role, config)
                       for index, role in enumerate(ROLES, start=1)]
            for future in as_completed(futures):
                summary["sessions"].append(future.result())
                summary["sessions"].sort(key=lambda item: item["label"])
                write_json(output / "summary.json", summary)
    # Activity timestamps cover actual transport invocations, excluding initial
    # capability probing and post-call transcript/usage file publication.
    intervals = []
    for session in summary["sessions"]:
        observation = activity.document["sessions"].get(session.get("invocation_id"), {})
        session["started_at"] = observation.get("started_at")
        session["finished_at"] = observation.get("finished_at")
        if session["started_at"] and session["finished_at"]:
            intervals.append(tuple(datetime.fromisoformat(session[key]).timestamp()
                                   for key in ("started_at", "finished_at")))
    overlap = _overlap_seconds(intervals)
    peak = dict(activity.document["peak"])
    summary["checks"] = {
        "four_sessions_completed": len(summary["sessions"]) == len(ROLES),
        "all_session_checks_passed": all(session["ok"] for session in summary["sessions"]),
        "four_transport_intervals_observed": len(intervals) == len(ROLES),
        "all_four_sessions_overlapped": len(intervals) == len(ROLES) and overlap > 0,
        "both_roles_overlapped": peak["task_writer"] == 2 and peak["task_reporter"] == 2 and peak["total"] == 4,
    }
    summary.update(status="completed", passed=all(summary["checks"].values()), peak=peak,
                   common_overlap_s=round(overlap, 6), finished_at=time.time())
    write_json(output / "summary.json", summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="Project model configuration JSON")
    parser.add_argument("--output", type=Path, required=True, help="New or empty directory outside the checkout")
    parser.add_argument("--prompt-key", action="store_true", help="Read the provider key through hidden input for this process only")
    args = parser.parse_args()
    try:
        config = load_model_config(args.config)
        if not config.managed:
            parser.error("An explicit managed model configuration is required.")
        if args.prompt_key:
            if not config.env_key:
                parser.error("The configuration must name a provider credential environment variable.")
            import getpass

            credential = getpass.getpass("Provider API key (hidden, process only): ")
            if credential:
                os.environ[config.env_key] = credential
        if config.env_key and not get_config_value(config.env_key):
            parser.error("The selected provider credential is unavailable in the process or Windows environment.")
        output = prepare_output(args.output)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print("Starting four small model sessions (2 Writer + 2 Reporter); no paper reproduction.", flush=True)
    try:
        summary = run_probe(config, output)
    except KeyboardInterrupt:
        print("Diagnostic interrupted; completed transport evidence remains in the output directory.", flush=True)
        return 130
    except Exception as exc:
        print(f"Diagnostic stopped ({type(exc).__name__}); inspect the output directory.", flush=True)
        return 2
    print(f"Concurrency diagnostic {'passed' if summary['passed'] else 'failed'}; "
          f"successful sessions: {sum(item['ok'] for item in summary['sessions'])}/4; "
          f"peak: {summary['peak']['total']}; shared overlap: {summary['common_overlap_s']} s.", flush=True)
    print("Evidence: " + str(output / "summary.json"), flush=True)
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
