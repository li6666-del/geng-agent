from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
import secrets
import shutil
from pathlib import Path
from typing import Any

from .agentic_project import (
    CODEX_PROJECT_BACKEND,
    PAPER_EVIDENCE_DIR,
    _clear_result_review_outputs,
    _load_cached_agentic_project,
    _manifest_from_project,
    _manifest_disk_paths,
    _prepare_project_workspace,
    _prune_unexpected_files,
    _render_writer_python_cmd_wrapper,
    _render_writer_python_sh_wrapper,
    _resolve_writer_real_python,
    _restore_trusted_files,
    _write_paper_evidence_bundle,
)
from .codex_runner import run_codex_subprocess
from .config import get_config_value
from .io_runtime import BACKEND_RUNTIME_API_DOC, IO_RUNTIME_API_DOC, inject_io_runtime
from .json_utils import pretty_json
from .llm import LLMClient
from .manifest_utils import expected_generated_paths
from .outputs import inspect_output_artifacts, validate_repro_project, write_json, write_text
from .result_review import facts_for_task, paper_context_for_task, safe_label, thesis_ordering_anchor_for_task
from .schemas import validate_stage
from .security import (
    dependency_policy_prompt_text,
    reconcile_whitelisted_requirements,
    redact_text,
    static_scan_repro_project,
    validate_requirements,
)
from .stage_cleanup import _clear_stage_outputs
from .task_scripts import build_tasks_manifest, write_task_scaffolding


TASK_WRITER_STATUSES = {"matched", "explained_gap", "failed"}
DEFAULT_TASK_WRITER_AGENT_CONCURRENCY = 4


def run_codex_task_writer_workflow(
    *,
    facts: dict[str, Any],
    tasks: dict[str, Any],
    experiment_index: dict[str, Any],
    paper: dict[str, Any],
    paper_path: Path,
    paper_context_json: str,
    paper_thesis: dict[str, Any] | None,
    output_dir: Path,
    audit_dir: Path,
    repro_project_dir: Path,
    client: LLMClient,
    prompt_book: Any,
    system_message: str,
    run_repro: bool,
    result_review: bool,
    rounds: int = 8,
    timeout: float = 1800.0,
    run_timeout: float = 120.0,
    resume: bool = True,
    agent_concurrency: int | None = None,
) -> dict[str, Any]:
    """Third-round autonomous per-task Codex writer workflow.

    Each task gets an isolated sandbox and one Codex writer that owns code,
    full execution, and task-level paper comparison. The host does not run a
    separate reviewer and does not repeat the full run after merging.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    audit_dir.mkdir(parents=True, exist_ok=True)
    rounds = max(1, int(rounds or 1))
    timeout = float(timeout or 1800.0)

    cached = _load_cached_agentic_project(
        output_dir=output_dir,
        repro_project_dir=repro_project_dir,
        run_repro=run_repro,
        result_review=result_review,
    )
    if resume and cached is not None:
        write_json(audit_dir / "03c_task_writers_resume.json", {"ok": True, "source": "cached artifacts"})
        return cached

    _clear_stage_outputs(output_dir, "manifest")
    _clear_result_review_outputs(output_dir)
    task_manifest = build_tasks_manifest(tasks)
    task_items = [task for task in tasks.get("repro_tasks", []) if isinstance(task, dict)]
    manifest_entries = [entry for entry in task_manifest.get("tasks", []) if isinstance(entry, dict)]
    task_pairs = list(zip(task_items, manifest_entries))
    expected_paths = expected_generated_paths([item["script"] for item in manifest_entries])

    task_root = audit_dir / "03c_task_writer_sandboxes"
    if task_root.exists():
        shutil.rmtree(task_root)
    task_root.mkdir(parents=True, exist_ok=True)
    lock_dir = audit_dir / "03c_task_writer_full_locks"
    if lock_dir.exists():
        shutil.rmtree(lock_dir)
    lock_dir.mkdir(parents=True, exist_ok=True)

    status: dict[str, Any] = {
        "backend": CODEX_PROJECT_BACKEND,
        "mode": "task_writers",
        "rounds_requested": rounds,
        "run_repro": bool(run_repro),
        "result_review": bool(result_review),
        "task_count": len(task_pairs),
        "rounds": [],
    }
    write_json(audit_dir / "03c_task_writers_start.json", status)

    max_workers = _task_writer_concurrency(len(task_pairs), agent_concurrency)
    task_records: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_index = {
            executor.submit(
                _run_one_task_writer,
                index=index,
                task=task,
                manifest_entry=manifest_entry,
                facts=facts,
                experiment_index=experiment_index,
                paper=paper,
                paper_path=paper_path,
                paper_context_json=paper_context_json,
                paper_thesis=paper_thesis,
                task_root=task_root,
                audit_dir=audit_dir,
                lock_dir=lock_dir,
                rounds=rounds,
                timeout=timeout,
                run_timeout=run_timeout,
                run_repro=run_repro,
            ): index
            for index, (task, manifest_entry) in enumerate(task_pairs, start=1)
        }
        by_index: dict[int, dict[str, Any]] = {}
        for future in as_completed(future_to_index):
            index = future_to_index[future]
            try:
                by_index[index] = future.result()
            except Exception as exc:
                task, manifest_entry = task_pairs[index - 1]
                task_id = str(task.get("task_id") or manifest_entry.get("task_id") or f"task_{index}")
                by_index[index] = _failed_task_record(
                    index=index,
                    task_id=task_id,
                    module=str(manifest_entry.get("module") or ""),
                    error=f"{type(exc).__name__}: {exc}",
                )
        task_records = [by_index[index] for index in range(1, len(task_pairs) + 1)]

    _prepare_project_workspace(repro_project_dir, task_manifest)
    expected_paths = _merge_task_writer_deliveries(
        repro_project_dir=repro_project_dir,
        task_manifest=task_manifest,
        expected_paths=set(expected_paths),
        task_records=task_records,
    )
    _restore_trusted_files(repro_project_dir, task_manifest)
    final_task_manifest = _task_manifest_with_configs(task_manifest)
    write_json(repro_project_dir / "tasks_manifest.json", final_task_manifest)
    reconcile_whitelisted_requirements(repro_project_dir)
    validation = validate_repro_project(repro_project_dir)
    requirement_issues = validate_requirements(repro_project_dir)
    security_issues = static_scan_repro_project(repro_project_dir)
    manifest = _manifest_from_project(
        repro_project_dir=repro_project_dir,
        expected_paths=expected_paths,
        task_manifest=final_task_manifest,
        round_no=1,
    )
    manifest["_meta"]["mode"] = "task_writers"
    manifest_issues = validate_stage("repro_project_manifest", manifest, required_files=expected_paths)
    write_json(
        audit_dir / "03c_task_writers_manifest_validation.json",
        {"ok": not manifest_issues, "errors": [issue.as_dict() for issue in manifest_issues]},
    )
    write_json(output_dir / "repro_project_manifest.json", manifest)

    runtime_result = _task_writer_runtime_result(
        task_records=task_records,
        validation=validation,
        manifest_issues=[issue.as_dict() for issue in manifest_issues],
        requirement_issues=requirement_issues,
        security_issues=security_issues,
    )
    write_json(output_dir / "runtime_result.json", runtime_result)

    review_doc: dict[str, Any] | None = None
    if result_review:
        markdown = _render_task_writer_result_review(task_records)
        alignment_summary = _task_writer_alignment_summary(task_records)
        write_text(output_dir / "result_review.md", markdown)
        review_doc = {
            "_meta": {"markdown_review": True, "mode": "task_writer_self_review"},
            "markdown": markdown,
            **alignment_summary,
            "task_writer_reviews": [_compact_task_writer_review(record) for record in task_records],
        }
        result_review_result = {
            "enabled": True,
            "passed": True,
            "mode": "codex_task_writer_self_review",
            "result_review_markdown_path": str(output_dir / "result_review.md"),
            **alignment_summary,
            "task_count": len(task_records),
            "task_statuses": [
                {
                    "task_id": record.get("task_id"),
                    "status": record.get("task_writer_status"),
                    "structural_ok": record.get("structural_ok"),
                }
                for record in task_records
            ],
            "note": "No independent reviewer was launched; task writers performed their own comparisons.",
        }
    else:
        result_review_result = {
            "enabled": False,
            "passed": None,
            "reason": "result review disabled by --no-result-review",
            "mode": "codex_task_writer_self_review",
        }

    write_json(audit_dir / "03c_task_writers_records.json", {"tasks": task_records})
    status.update(
        {
            "rounds_run": 1,
            "best_round": 1,
            "stop_class": _task_writer_stop_class(task_records),
            "stopped_reason": _task_writer_stopped_reason(task_records),
            "validation": validation,
            "manifest_errors": [issue.as_dict() for issue in manifest_issues],
            "runtime": {
                "passed": runtime_result.get("passed"),
                "coverage": runtime_result.get("coverage"),
            },
            "result_review": {
                "enabled": result_review_result.get("enabled"),
                "passed": result_review_result.get("passed"),
                "mode": result_review_result.get("mode"),
            },
            "tasks": [
                {
                    "task_id": record.get("task_id"),
                    "status": record.get("task_writer_status"),
                    "structural_ok": record.get("structural_ok"),
                    "errors": record.get("errors", []),
                }
                for record in task_records
            ],
        }
    )
    write_json(audit_dir / "03c_task_writers_status.json", status)
    return {
        "manifest": manifest,
        "runtime_result": runtime_result,
        "result_review_result": result_review_result,
        "result_review_doc": review_doc,
        "written_files": [str(path) for path in _manifest_disk_paths(manifest, repro_project_dir)],
        "status": status,
    }


def _run_one_task_writer(
    *,
    index: int,
    task: dict[str, Any],
    manifest_entry: dict[str, Any],
    facts: dict[str, Any],
    experiment_index: dict[str, Any],
    paper: dict[str, Any],
    paper_path: Path,
    paper_context_json: str,
    paper_thesis: dict[str, Any] | None,
    task_root: Path,
    audit_dir: Path,
    lock_dir: Path,
    rounds: int,
    timeout: float,
    run_timeout: float,
    run_repro: bool,
) -> dict[str, Any]:
    task_id = str(task.get("task_id") or manifest_entry.get("task_id") or f"task_{index}")
    module = str(manifest_entry.get("module") or safe_label(task_id))
    label = f"03c_task_writer_{index:02d}_{safe_label(task_id)}"
    sandbox = task_root / f"{index:02d}_{safe_label(task_id)}"
    _prepare_task_writer_sandbox(
        sandbox=sandbox,
        task=task,
        manifest_entry=manifest_entry,
        paper=paper,
        paper_path=paper_path,
        facts=facts,
        paper_thesis=paper_thesis,
    )
    prompt = _build_task_writer_brief(
        index=index,
        task=task,
        manifest_entry=manifest_entry,
        facts=facts,
        experiment_index=experiment_index,
        paper=paper,
        paper_context_json=paper_context_json,
        paper_thesis=paper_thesis,
        rounds=rounds,
        run_timeout=run_timeout,
        run_repro=run_repro,
    )
    write_text(audit_dir / f"{label}_brief.md", prompt)
    guard = _prepare_task_writer_python_guard(
        audit_dir=audit_dir,
        label=label,
        module=module,
        output_subdir=str(manifest_entry.get("output_subdir") or task_id),
        run_log=audit_dir / f"{label}_runs.jsonl",
        lock_dir=lock_dir,
        allow_full=run_repro,
        run_timeout=run_timeout,
    )
    writer_status = run_codex_subprocess(
        role="task_writer",
        work_dir=sandbox,
        prompt=prompt,
        audit_dir=audit_dir,
        label=label,
        sandbox="workspace-write",
        timeout=timeout,
        command_override=get_config_value("GENG_CODEX_TASK_WRITER_CMD") or get_config_value("GENG_CODEX_WRITER_CMD"),
        extra_env=guard["env"],
        path_prepend=[guard["bin_dir"]],
    )
    _restore_trusted_files(sandbox, {"version": 1, "tasks": [manifest_entry]})
    return _validate_task_writer_delivery(
        index=index,
        task=task,
        manifest_entry=manifest_entry,
        sandbox=sandbox,
        writer_status=writer_status,
        run_repro=run_repro,
        trusted_run_log_path=Path(str(guard["run_log"])),
        guard_token=str(guard["guard_token"]),
    )


def _prepare_task_writer_sandbox(
    *,
    sandbox: Path,
    task: dict[str, Any],
    manifest_entry: dict[str, Any],
    paper: dict[str, Any],
    paper_path: Path,
    facts: dict[str, Any],
    paper_thesis: dict[str, Any] | None,
) -> None:
    if sandbox.exists():
        shutil.rmtree(sandbox)
    sandbox.mkdir(parents=True, exist_ok=True)
    single_manifest = {"version": 1, "tasks": [manifest_entry]}
    inject_io_runtime(sandbox)
    write_task_scaffolding(sandbox, single_manifest)
    _write_minimal_shared_project_files(sandbox, task, manifest_entry)
    _write_paper_evidence_bundle(
        repro_project_dir=sandbox,
        paper_path=paper_path,
        paper=paper,
        facts=facts,
        tasks={"repro_tasks": [task]},
        paper_thesis=paper_thesis,
    )


def _write_minimal_shared_project_files(sandbox: Path, task: dict[str, Any], manifest_entry: dict[str, Any]) -> None:
    task_id = str(task.get("task_id") or manifest_entry.get("task_id") or "task")
    module = str(manifest_entry.get("module") or "task")
    write_text(sandbox / "README.md", f"# Task writer sandbox\n\nTask: `{task_id}`\n")
    write_text(sandbox / "requirements.txt", "numpy\nmatplotlib\n")
    write_json(sandbox / "config.json", {"run_profile": "full", "task_id": task_id, "seed": 1})
    write_json(sandbox / "config_smoke.json", {"run_profile": "smoke", "task_id": task_id, "seed": 1, "smoke": True})
    for name in ("channel.py", "modulation.py", "metrics.py", "simulation.py"):
        write_text(sandbox / "src" / name, '"""Task-private workflow stub; prefer tasks/<module>_lib.py."""\n')
    task_script = sandbox / "tasks" / f"{module}.py"
    if not task_script.exists():
        write_text(
            task_script,
            "\n".join(
                [
                    "from __future__ import annotations",
                    "",
                    "def main(config_path=None) -> int:",
                    "    raise RuntimeError('task writer did not implement this task yet')",
                    "",
                    "if __name__ == '__main__':",
                    "    raise SystemExit(main())",
                    "",
                ]
            ),
        )


def _build_task_writer_brief(
    *,
    index: int,
    task: dict[str, Any],
    manifest_entry: dict[str, Any],
    facts: dict[str, Any],
    experiment_index: dict[str, Any],
    paper: dict[str, Any],
    paper_context_json: str,
    paper_thesis: dict[str, Any] | None,
    rounds: int,
    run_timeout: float,
    run_repro: bool,
) -> str:
    task_id = str(task.get("task_id") or manifest_entry.get("task_id") or f"task_{index}")
    module = str(manifest_entry.get("module") or "")
    output_subdir = str(manifest_entry.get("output_subdir") or task_id)
    task_context = paper_context_for_task(paper=paper, facts=facts, task=task)
    task_facts = facts_for_task(facts, task)
    ordering_anchor = thesis_ordering_anchor_for_task(paper_thesis, task)
    full_instruction = (
        f"Run your full task with `python -m tasks.{module} config.json` after each meaningful fix."
        if run_repro
        else "Do not run full config because --run-repro is disabled; produce code and mark the result failed/skipped."
    )
    return f"""# Role: autonomous Codex task writer

You own exactly one reproduction task. There is no separate reviewer. You must write the code, run your assigned task, compare the output to the paper evidence, revise if needed, then leave a final human-readable comparison for the host.

## Hard boundaries
- Assigned task_id: `{task_id}`
- Assigned module: `tasks.{module}`
- Output directory: `outputs/{output_subdir}/`
- You may edit only: `README.md`, `requirements.txt`, `config.json`, `config_smoke.json`, `tasks/{module}.py`, and optional `tasks/{module}_lib.py`.
- Do not edit `src/_io.py`, `src/_backend.py`, `run_experiment.py`, `tasks_manifest.json`, `tasks/__init__.py`, or any other task module.
- Do not run `python run_experiment.py config.json`; the Python guard rejects dispatcher full runs and other task modules.
- {full_instruction}
- You may run smoke with `python -m tasks.{module} config_smoke.json`.
- Full runs may wait on a host semaphore; do not bypass the Python guard.
- Max internal science iterations: {rounds}. Stop earlier when you reach a stable conclusion.

## Required final files
- `task_agent_result.md`: Chinese, human-readable comparison report.
- `task_agent_result.json`: strict JSON object with:
```json
{{
  "task_id": "{task_id}",
  "status": "matched|explained_gap|failed",
  "summary": "one Chinese sentence",
  "differences": [],
  "possible_causes": [],
  "remaining_uncertainties": [],
  "evidence_files": [],
  "local_image_paths": [],
  "paper_image_paths": []
}}
```
- If `status == "explained_gap"`, `differences`, `possible_causes`, `remaining_uncertainties`, and `evidence_files` must all be non-empty.
- If `status == "matched"`, cite the local CSV/PNG/summary and paper evidence that support the match.
- If `status == "failed"`, explain whether the blocker is runtime, missing paper details, timeout, dependency, or modeling uncertainty.

## Trusted runtime APIs
{IO_RUNTIME_API_DOC}

{BACKEND_RUNTIME_API_DOC}

## Dependency policy
{dependency_policy_prompt_text()}

## Task evidence files
- `paper_evidence/index.json`
- `paper_evidence/01_{safe_label(task_id)}/evidence.json`
- `paper_evidence/01_{safe_label(task_id)}/context.md`

## Task JSON
```json
{pretty_json(task)}
```

## Manifest entry
```json
{pretty_json(manifest_entry)}
```

## Relevant facts
```json
{pretty_json(task_facts)}
```

## Paper thesis / ordering anchor
{ordering_anchor or "None"}

## Task paper context
{task_context[:12000]}

## Full paper context excerpt
{paper_context_json[:8000]}

## Experiment index
```json
{pretty_json(experiment_index)[:8000]}
```
"""


def _prepare_task_writer_python_guard(
    *,
    audit_dir: Path,
    label: str,
    module: str,
    output_subdir: str,
    run_log: Path,
    lock_dir: Path,
    allow_full: bool,
    run_timeout: float,
) -> dict[str, Any]:
    bin_dir = audit_dir / f"{label}_python_guard"
    if bin_dir.exists():
        shutil.rmtree(bin_dir)
    bin_dir.mkdir(parents=True, exist_ok=True)
    real_python = _resolve_writer_real_python()
    guard_token = secrets.token_hex(16)
    guard_script = bin_dir / "task_writer_python_guard.py"
    write_text(guard_script, _render_task_writer_python_guard(real_python, guard_token, run_timeout))
    for name in ("python", "python3", "py"):
        if os.name == "nt":
            write_text(bin_dir / f"{name}.cmd", _render_writer_python_cmd_wrapper(guard_script, real_python))
        else:
            wrapper = bin_dir / name
            write_text(wrapper, _render_writer_python_sh_wrapper(guard_script, real_python))
            wrapper.chmod(0o755)
    shim_python = bin_dir / ("python.cmd" if os.name == "nt" else "python")
    env = {
        "GENG_WRITER_SELFTEST_MODE": "task_writer_full",
        "GENG_TASK_WRITER_MODULE": module,
        "GENG_TASK_WRITER_OUTPUT_SUBDIR": output_subdir,
        "GENG_TASK_WRITER_RUN_LOG": str(run_log),
        "GENG_TASK_WRITER_LOCK_DIR": str(lock_dir),
        "GENG_TASK_WRITER_ALLOW_FULL": "1" if allow_full else "0",
        "PYTHON": str(shim_python),
        "GENG_PYTHON": str(shim_python),
    }
    return {
        "bin_dir": bin_dir,
        "env": env,
        "real_python": real_python,
        "shim_python": shim_python,
        "run_log": run_log,
        "guard_token": guard_token,
    }


def _render_task_writer_python_guard(real_python: str, guard_token: str, run_timeout: float) -> str:
    return f'''from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time

REAL_PYTHON = {real_python!r}
GUARD_TOKEN = {guard_token!r}
RUN_TIMEOUT_S = {float(run_timeout or 0.0)!r}
MODULE = os.environ.get("GENG_TASK_WRITER_MODULE", "")
OUTPUT_SUBDIR = os.environ.get("GENG_TASK_WRITER_OUTPUT_SUBDIR", "")
RUN_LOG = Path(os.environ.get("GENG_TASK_WRITER_RUN_LOG", "task_agent_runs.jsonl"))
LOCK_DIR = Path(os.environ.get("GENG_TASK_WRITER_LOCK_DIR", ".task_writer_locks"))
ALLOW_FULL = os.environ.get("GENG_TASK_WRITER_ALLOW_FULL") == "1"


def _base(value: str) -> str:
    return os.path.basename(str(value).replace("\\\\", "/")).lower()


def _strip_py_launcher_version(args: list[str]) -> list[str]:
    if args and args[0].startswith("-"):
        version = args[0][1:]
        if version and all(ch.isdigit() or ch == "." for ch in version):
            return args[1:]
    return args


def _parse_allowed(args: list[str]) -> tuple[bool, str, str]:
    args = _strip_py_launcher_version(list(args))
    config = ""
    if len(args) == 3 and args[0] == "-m" and args[1] == f"tasks.{{MODULE}}":
        config = args[2]
    elif len(args) == 2 and _base(args[0]) == f"{{MODULE}}.py":
        config = args[1]
    else:
        return False, "", "only the assigned task module may be executed"
    config_base = _base(config)
    if config_base == "config_smoke.json":
        return True, "smoke", ""
    if config_base == "config.json" and ALLOW_FULL:
        return True, "full", ""
    if config_base == "config.json":
        return False, "", "full config is disabled because --run-repro was not requested"
    return False, "", "only config_smoke.json or config.json are allowed"


def _slot_count() -> int:
    try:
        import torch  # type: ignore
        cuda = bool(torch.cuda.is_available())
    except Exception:
        cuda = False
    env_name = "GENG_TASK_WRITER_GPU_FULL_SLOTS" if cuda else "GENG_TASK_WRITER_CPU_FULL_SLOTS"
    default = "1" if cuda else "2"
    try:
        return max(1, int(os.environ.get(env_name, default)))
    except ValueError:
        return int(default)


def _acquire_full_slot() -> Path | None:
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    slots = _slot_count()
    while True:
        for index in range(slots):
            path = LOCK_DIR / f"full_{{index}}.lock"
            try:
                fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(json.dumps({{"pid": os.getpid(), "time": time.time()}}))
                return path
            except FileExistsError:
                try:
                    if time.time() - path.stat().st_mtime > 6 * 3600:
                        path.unlink()
                except OSError:
                    pass
        time.sleep(1.0)


def _artifact_snapshot() -> dict:
    root = Path("outputs") / OUTPUT_SUBDIR
    result = {{"output_subdir": OUTPUT_SUBDIR, "csv_files": [], "png_files": [], "summary_json_files": []}}
    if not root.exists():
        return result
    result["csv_files"] = sorted(str(path.as_posix()) for path in root.glob("*.csv"))
    result["png_files"] = sorted(str(path.as_posix()) for path in root.glob("*.png"))
    result["summary_json_files"] = sorted(str(path.as_posix()) for path in root.glob("summary*.json"))
    return result


def _append_run_log(record: dict) -> None:
    RUN_LOG.parent.mkdir(parents=True, exist_ok=True)
    with RUN_LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\\n")


def main() -> int:
    args = sys.argv[1:]
    allowed, profile, reason = _parse_allowed(args)
    if not allowed:
        print(
            "geng-agent task-writer python guard: " + reason + ". "
            f"Allowed commands: python -m tasks.{{MODULE}} config_smoke.json and "
            f"python -m tasks.{{MODULE}} config.json.",
            file=sys.stderr,
        )
        return 97
    lock = _acquire_full_slot() if profile == "full" else None
    started = time.time()
    returncode = 1
    timed_out = False
    try:
        timeout = RUN_TIMEOUT_S if RUN_TIMEOUT_S > 0 else None
        completed = subprocess.run([REAL_PYTHON, *args], check=False, timeout=timeout)
        returncode = int(completed.returncode)
    except subprocess.TimeoutExpired:
        timed_out = True
        returncode = 124
    finally:
        duration = time.time() - started
        record = {{
            "guard": "geng_task_writer_python_guard_v1",
            "guard_token": GUARD_TOKEN,
            "task_module": MODULE,
            "output_subdir": OUTPUT_SUBDIR,
            "command": [REAL_PYTHON, *args],
            "profile": profile,
            "config": args[-1] if args else "",
            "returncode": returncode,
            "timed_out": timed_out,
            "duration_s": round(duration, 3),
            "artifacts": _artifact_snapshot(),
        }}
        _append_run_log(record)
        if lock is not None:
            try:
                lock.unlink()
            except OSError:
                pass
    return returncode


if __name__ == "__main__":
    raise SystemExit(main())
'''


def _validate_task_writer_delivery(
    *,
    index: int,
    task: dict[str, Any],
    manifest_entry: dict[str, Any],
    sandbox: Path,
    writer_status: dict[str, Any],
    run_repro: bool,
    trusted_run_log_path: Path,
    guard_token: str,
) -> dict[str, Any]:
    task_id = str(task.get("task_id") or manifest_entry.get("task_id") or f"task_{index}")
    module = str(manifest_entry.get("module") or "")
    output_subdir = str(manifest_entry.get("output_subdir") or task_id)
    errors: list[str] = []
    result_path = sandbox / "task_agent_result.json"
    md_path = sandbox / "task_agent_result.md"
    result_doc: dict[str, Any] = {}
    if result_path.exists():
        try:
            parsed = json.loads(result_path.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                result_doc = parsed
            else:
                errors.append("task_agent_result.json must contain an object")
        except Exception as exc:
            errors.append(f"task_agent_result.json is invalid JSON: {type(exc).__name__}: {exc}")
    else:
        errors.append("missing task_agent_result.json")
    if not md_path.exists() or len(md_path.read_text(encoding="utf-8", errors="replace").strip()) < 40:
        errors.append("missing or too-short task_agent_result.md")

    status = str(result_doc.get("status") or "failed")
    if status not in TASK_WRITER_STATUSES:
        errors.append(f"invalid task writer status: {status}")
        status = "failed"
    if str(result_doc.get("task_id") or task_id) != task_id:
        errors.append("task_agent_result.json task_id does not match assigned task")

    if status == "explained_gap":
        for key in ("differences", "possible_causes", "remaining_uncertainties", "evidence_files"):
            if not _non_empty_list(result_doc.get(key)):
                errors.append(f"explained_gap requires non-empty {key}")
    if status == "matched" and not _non_empty_list(result_doc.get("evidence_files")):
        errors.append("matched requires non-empty evidence_files")

    script_path = sandbox / "tasks" / f"{module}.py"
    if not script_path.exists() or not script_path.is_file():
        errors.append(f"missing assigned task script: tasks/{module}.py")

    run_records = _read_jsonl(trusted_run_log_path)
    trusted_records = [item for item in run_records if _is_trusted_guard_record(item, guard_token, module, output_subdir)]
    full_runs = [item for item in trusted_records if item.get("profile") == "full"]
    successful_full_runs = [item for item in full_runs if item.get("returncode") == 0]
    full_success = bool(successful_full_runs)
    if run_repro and not full_success:
        errors.append("missing successful assigned-task full run in task_agent_runs.jsonl")
    if run_repro and successful_full_runs and not _run_record_has_required_artifacts(successful_full_runs[-1]):
        errors.append("trusted full run did not record required CSV/PNG/summary artifacts")
    if run_records and not trusted_records:
        errors.append("task_agent_runs.jsonl contains no trusted guard records")

    artifacts = inspect_output_artifacts(sandbox, subdir=output_subdir)
    if run_repro and full_success:
        if not artifacts.get("has_csv"):
            errors.append("missing valid local CSV artifact")
        if not artifacts.get("has_png"):
            errors.append("missing valid local PNG artifact")
        if not artifacts.get("has_summary_json"):
            errors.append("missing valid local summary.json artifact")
        for invalid in artifacts.get("invalid_files", []):
            errors.append(f"invalid artifact: {invalid}")

    paper_images = _task_paper_image_paths(sandbox)
    local_images = _task_local_image_paths(sandbox, output_subdir)
    if not paper_images:
        errors.append("missing rendered paper page evidence image")
    if run_repro and not local_images:
        errors.append("missing local output image")

    structural_ok = not errors
    return {
        "index": index,
        "task_id": task_id,
        "module": module,
        "output_subdir": output_subdir,
        "sandbox": str(sandbox),
        "writer_status": writer_status,
        "task_writer_status": status,
        "result_json": result_doc,
        "result_json_path": str(result_path) if result_path.exists() else None,
        "result_markdown_path": str(md_path) if md_path.exists() else None,
        "run_log_path": str(trusted_run_log_path) if trusted_run_log_path.exists() else None,
        "run_records": trusted_records,
        "full_run": full_runs[-1] if full_runs else None,
        "artifacts": artifacts,
        "local_images": local_images,
        "paper_images": paper_images,
        "structural_ok": structural_ok,
        "errors": errors,
    }


def _non_empty_list(value: Any) -> bool:
    return isinstance(value, list) and any(str(item).strip() for item in value)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            records.append(parsed)
    return records


def _is_trusted_guard_record(record: dict[str, Any], guard_token: str, module: str, output_subdir: str) -> bool:
    return (
        record.get("guard") == "geng_task_writer_python_guard_v1"
        and record.get("guard_token") == guard_token
        and record.get("task_module") == module
        and record.get("output_subdir") == output_subdir
    )


def _run_record_has_required_artifacts(record: dict[str, Any]) -> bool:
    artifacts = record.get("artifacts") if isinstance(record.get("artifacts"), dict) else {}
    csv_files = artifacts.get("csv_files") if isinstance(artifacts.get("csv_files"), list) else []
    png_files = artifacts.get("png_files") if isinstance(artifacts.get("png_files"), list) else []
    summary_files = artifacts.get("summary_json_files") if isinstance(artifacts.get("summary_json_files"), list) else []
    return bool(csv_files and png_files and summary_files)


def _task_paper_image_paths(sandbox: Path) -> list[str]:
    evidence_root = sandbox / PAPER_EVIDENCE_DIR
    if not evidence_root.exists():
        return []
    return [str(path.resolve()) for path in sorted(evidence_root.rglob("paper_page_*.png")) if path.is_file()]


def _task_local_image_paths(sandbox: Path, output_subdir: str) -> list[str]:
    output_dir = sandbox / "outputs" / output_subdir
    if not output_dir.exists():
        return []
    return [str(path.resolve()) for path in sorted(output_dir.glob("*.png")) if path.is_file()]


def _merge_task_writer_deliveries(
    *,
    repro_project_dir: Path,
    task_manifest: dict[str, Any],
    expected_paths: set[str],
    task_records: list[dict[str, Any]],
) -> set[str]:
    _write_final_shared_project_files(repro_project_dir, task_records)
    configs_dir = repro_project_dir / "configs"
    configs_dir.mkdir(parents=True, exist_ok=True)
    combined_requirements: list[str] = ["numpy", "matplotlib"]
    for record in task_records:
        sandbox = Path(str(record.get("sandbox") or ""))
        module = str(record.get("module") or "")
        output_subdir = str(record.get("output_subdir") or record.get("task_id") or "")
        if not sandbox.exists():
            continue
        script_source = sandbox / "tasks" / f"{module}.py"
        script_target = repro_project_dir / "tasks" / f"{module}.py"
        if script_source.exists():
            shutil.copy2(script_source, script_target)
        lib_source = sandbox / "tasks" / f"{module}_lib.py"
        if lib_source.exists():
            lib_target = repro_project_dir / "tasks" / f"{module}_lib.py"
            shutil.copy2(lib_source, lib_target)
            expected_paths.add(f"tasks/{module}_lib.py")
        for config_name, target_name in (
            ("config.json", f"{module}_config.json"),
            ("config_smoke.json", f"{module}_config_smoke.json"),
        ):
            source = sandbox / config_name
            if source.exists():
                target = configs_dir / target_name
                shutil.copy2(source, target)
                expected_paths.add(f"configs/{target_name}")
        source_output = sandbox / "outputs" / output_subdir
        if source_output.exists():
            target_output = repro_project_dir / "outputs" / output_subdir
            if target_output.exists():
                shutil.rmtree(target_output)
            shutil.copytree(source_output, target_output)
        result_dir = repro_project_dir / "outputs" / output_subdir
        result_dir.mkdir(parents=True, exist_ok=True)
        for name in ("task_agent_result.json", "task_agent_result.md"):
            source = sandbox / name
            if source.exists():
                shutil.copy2(source, result_dir / name)
        run_log_path = record.get("run_log_path")
        if run_log_path:
            source = Path(str(run_log_path))
            if source.exists():
                shutil.copy2(source, result_dir / "task_agent_runs.jsonl")
        req_path = sandbox / "requirements.txt"
        if req_path.exists():
            combined_requirements.extend(_read_requirement_names(req_path))
    write_text(repro_project_dir / "requirements.txt", _format_requirements(combined_requirements))
    _prune_unexpected_files(repro_project_dir, expected_paths)
    return expected_paths


def _write_final_shared_project_files(repro_project_dir: Path, task_records: list[dict[str, Any]]) -> None:
    write_text(
        repro_project_dir / "README.md",
        "# Task-writer reproduction project\n\n"
        "This project was assembled from autonomous per-task Codex writer sandboxes. "
        "Each task ran and self-reviewed its own full reproduction before host aggregation.\n",
    )
    write_json(
        repro_project_dir / "config.json",
        {
            "run_profile": "full",
            "task_writer_mode": True,
            "task_statuses": {str(r.get("task_id")): r.get("task_writer_status") for r in task_records},
        },
    )
    write_json(repro_project_dir / "config_smoke.json", {"run_profile": "smoke", "task_writer_mode": True, "smoke": True})
    for name in ("channel.py", "modulation.py", "metrics.py", "simulation.py"):
        write_text(
            repro_project_dir / "src" / name,
            '"""Shared placeholder for task-writer mode; task-specific logic lives in tasks/*.py."""\n',
        )


def _task_manifest_with_configs(task_manifest: dict[str, Any]) -> dict[str, Any]:
    manifest = json.loads(json.dumps(task_manifest))
    for entry in manifest.get("tasks", []):
        if not isinstance(entry, dict):
            continue
        module = str(entry.get("module") or "")
        if module:
            entry["config_full"] = f"configs/{module}_config.json"
            entry["config_smoke"] = f"configs/{module}_config_smoke.json"
    return manifest


def _read_requirement_names(path: Path) -> list[str]:
    names: list[str] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        names.append(line)
    return names


def _format_requirements(requirements: list[str]) -> str:
    seen: set[str] = set()
    lines: list[str] = []
    for item in requirements:
        key = item.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        lines.append(item.strip())
    return "\n".join(lines) + ("\n" if lines else "")


def _task_writer_runtime_result(
    *,
    task_records: list[dict[str, Any]],
    validation: dict[str, Any],
    manifest_issues: list[dict[str, Any]],
    requirement_issues: list[dict[str, Any]],
    security_issues: list[dict[str, Any]],
) -> dict[str, Any]:
    passed = sum(1 for record in task_records if record.get("structural_ok"))
    total = len(task_records)
    valid_task_ids = [str(record.get("task_id")) for record in task_records if record.get("structural_ok")]
    valid_csv_files: list[str] = []
    valid_png_files: list[str] = []
    valid_summary_json_files: list[str] = []
    for record in task_records:
        if not record.get("structural_ok"):
            continue
        artifacts = record.get("artifacts") if isinstance(record.get("artifacts"), dict) else {}
        output_subdir = str(record.get("output_subdir") or record.get("task_id") or "")
        csv_files = artifacts.get("csv_files") if isinstance(artifacts.get("csv_files"), list) else []
        png_files = artifacts.get("png_files") if isinstance(artifacts.get("png_files"), list) else []
        summary_files = artifacts.get("summary_json_files") if isinstance(artifacts.get("summary_json_files"), list) else []
        valid_csv_files.extend(f"{output_subdir}/{item}" for item in csv_files if isinstance(item, str))
        valid_png_files.extend(f"{output_subdir}/{item}" for item in png_files if isinstance(item, str))
        valid_summary_json_files.extend(
            f"{output_subdir}/{item}" for item in summary_files if isinstance(item, str)
        )
    all_checks_passed = (
        total > 0
        and passed == total
        and validation.get("required_files_present")
        and validation.get("python_compiles")
        and not manifest_issues
        and not requirement_issues
        and not security_issues
    )
    return {
        "enabled": True,
        "passed": bool(all_checks_passed),
        "run_profile": "task_writer_full",
        "repair_backend": "codex_task_writers",
        "per_task_orchestration": True,
        "host_repeated_full": False,
        "tasks_total": total,
        "tasks_passed": passed,
        "coverage": f"{passed}/{total}",
        "partial_success": {
            "has_partial_output": bool(0 < passed < total),
            "valid_task_ids": valid_task_ids,
            "valid_csv_files": valid_csv_files,
            "valid_png_files": valid_png_files,
            "valid_summary_json_files": valid_summary_json_files,
        },
        "per_task": [
            {
                "task_id": record.get("task_id"),
                "module": record.get("module"),
                "passed": bool(record.get("structural_ok")),
                "task_writer_status": record.get("task_writer_status"),
                "full_run": record.get("full_run"),
                "artifacts": record.get("artifacts"),
                "errors": record.get("errors", []),
            }
            for record in task_records
        ],
        "validation": validation,
        "manifest_issues": manifest_issues,
        "requirements_issues": requirement_issues,
        "security_issues": security_issues,
    }


def _task_writer_alignment_summary(task_records: list[dict[str, Any]]) -> dict[str, Any]:
    if not task_records:
        return {
            "overall_alignment": "inconclusive",
            "overall_result_credibility": "low",
            "overall_summary": "没有可审查的复现任务。",
        }
    if any(not record.get("structural_ok") for record in task_records):
        return {
            "overall_alignment": "inconclusive",
            "overall_result_credibility": "low",
            "overall_summary": "部分任务未通过主持人结构验收，不能给出强复现结论。",
        }
    statuses = {str(record.get("task_writer_status") or "failed") for record in task_records}
    if statuses == {"matched"}:
        return {
            "overall_alignment": "match",
            "overall_result_credibility": "medium",
            "overall_summary": "所有任务均由自治 writer 报告为 matched，并通过主持人结构验收。",
        }
    if statuses <= {"matched", "explained_gap"} and "explained_gap" in statuses:
        return {
            "overall_alignment": "partial_match",
            "overall_result_credibility": "medium",
            "overall_summary": "任务均完成结构验收，但至少一个任务只解释了剩余差异。",
        }
    return {
        "overall_alignment": "inconclusive",
        "overall_result_credibility": "low",
        "overall_summary": "至少一个任务报告 failed，当前结果只能作为失败或待诊断证据。",
    }


def _render_task_writer_result_review(task_records: list[dict[str, Any]]) -> str:
    sections: list[str] = []
    for index, record in enumerate(task_records, start=1):
        task_id = str(record.get("task_id") or f"task_{index}")
        result = record.get("result_json") if isinstance(record.get("result_json"), dict) else {}
        md = _read_optional_text(record.get("result_markdown_path"))
        lines = [f"## {index}. {task_id}", ""]
        lines.extend(
            [
                f"- Writer 结论：`{record.get('task_writer_status', 'failed')}`",
                f"- 主持人结构验收：{'通过' if record.get('structural_ok') else '失败'}",
            ]
        )
        if record.get("errors"):
            lines.append("- 结构问题：" + "；".join(str(item) for item in record.get("errors", [])))
        lines.append("")
        lines.extend(_image_markdown_group("本地复现图", record.get("local_images", [])))
        lines.extend(_image_markdown_group("论文原图", record.get("paper_images", [])))
        lines.extend(["### Writer 自审正文", ""])
        if md:
            lines.append(md.strip())
        else:
            lines.extend(_fallback_writer_review_lines(result))
        sections.append("\n".join(lines).strip() + "\n")
    return "\n".join(sections).strip() + "\n"


def _image_markdown_group(title: str, paths: Any) -> list[str]:
    lines = [f"### {title}", ""]
    items = [str(path) for path in paths or [] if str(path).strip()]
    if not items:
        lines.extend(["无可用图片。", ""])
        return lines
    for path in items:
        caption = f"{title}: {Path(path).name}"
        lines.append(f"![{caption}]({path})")
        lines.append("")
    return lines


def _fallback_writer_review_lines(result: dict[str, Any]) -> list[str]:
    lines = [str(result.get("summary") or "Writer 未提供可读正文。"), ""]
    for title, key in (
        ("差异", "differences"),
        ("可能原因", "possible_causes"),
        ("仍不确定的信息", "remaining_uncertainties"),
        ("证据文件", "evidence_files"),
    ):
        lines.extend([f"#### {title}", ""])
        values = result.get(key)
        if isinstance(values, list) and values:
            lines.extend(f"- {item}" for item in values)
        else:
            lines.append("- 未列出")
        lines.append("")
    return lines


def _read_optional_text(path_text: Any) -> str:
    if not path_text:
        return ""
    path = Path(str(path_text))
    if not path.exists() or not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def _compact_task_writer_review(record: dict[str, Any]) -> dict[str, Any]:
    result = record.get("result_json") if isinstance(record.get("result_json"), dict) else {}
    return {
        "task_id": record.get("task_id"),
        "task_writer_status": record.get("task_writer_status"),
        "structural_ok": record.get("structural_ok"),
        "summary": result.get("summary"),
        "differences": result.get("differences", []),
        "possible_causes": result.get("possible_causes", []),
        "remaining_uncertainties": result.get("remaining_uncertainties", []),
        "evidence_files": result.get("evidence_files", []),
        "errors": record.get("errors", []),
    }


def _task_writer_concurrency(task_count: int, requested: int | None) -> int:
    if task_count <= 0:
        return 1
    if requested is not None:
        return max(1, min(task_count, int(requested)))
    raw = os.environ.get("GENG_CODEX_TASK_WRITER_CONCURRENCY")
    if raw:
        try:
            return max(1, min(task_count, int(raw)))
        except ValueError:
            pass
    return min(task_count, DEFAULT_TASK_WRITER_AGENT_CONCURRENCY)


def _task_writer_stop_class(task_records: list[dict[str, Any]]) -> str:
    if not task_records:
        return "no_tasks"
    if any(not record.get("structural_ok") for record in task_records):
        return "structural_failures"
    if any(record.get("task_writer_status") == "failed" for record in task_records):
        return "task_failures_reported"
    if any(record.get("task_writer_status") == "explained_gap" for record in task_records):
        return "explained_gaps"
    return "matched"


def _task_writer_stopped_reason(task_records: list[dict[str, Any]]) -> str:
    stop_class = _task_writer_stop_class(task_records)
    return {
        "no_tasks": "no reproduction tasks were available",
        "structural_failures": "one or more task writers did not satisfy the delivery contract",
        "task_failures_reported": "one or more task writers reported failed",
        "explained_gaps": "all structurally valid task writers either matched or explained remaining gaps",
        "matched": "all task writers reported matched and passed structural validation",
    }.get(stop_class, stop_class)


def _failed_task_record(*, index: int, task_id: str, module: str, error: str) -> dict[str, Any]:
    return {
        "index": index,
        "task_id": task_id,
        "module": module,
        "task_writer_status": "failed",
        "structural_ok": False,
        "errors": [redact_text(error)[:1000]],
        "writer_status": {"ok": False, "error": redact_text(error)[:1000]},
        "result_json": {"task_id": task_id, "status": "failed", "summary": redact_text(error)[:500]},
        "run_records": [],
        "local_images": [],
        "paper_images": [],
    }
