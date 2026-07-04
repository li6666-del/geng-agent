from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
import re
import secrets
import shutil
from pathlib import Path
from typing import Any

from .agentic_project import (
    CODEX_PROJECT_BACKEND,
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
    split_requirement_issues,
    static_scan_repro_project,
    validate_requirements,
)
from .stage_cleanup import _clear_stage_outputs
from .task_scripts import build_tasks_manifest, write_task_scaffolding


TASK_WRITER_STATUSES = {"matched", "explained_gap", "failed"}
DEFAULT_TASK_WRITER_AGENT_CONCURRENCY = 4
MAX_TASK_WRITER_SELF_ITERATIONS = 5


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
    rounds: int = MAX_TASK_WRITER_SELF_ITERATIONS,
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
    rounds = max(1, min(MAX_TASK_WRITER_SELF_ITERATIONS, int(rounds or MAX_TASK_WRITER_SELF_ITERATIONS)))
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

    max_workers = _task_writer_concurrency(len(task_pairs), agent_concurrency, run_repro=run_repro)
    status["agent_concurrency"] = max_workers
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
    blocking_requirement_issues, requirement_warnings = split_requirement_issues(requirement_issues)
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
        requirement_issues=blocking_requirement_issues,
        requirement_warnings=requirement_warnings,
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
                    "writer_error_kind": record.get("writer_error_kind"),
                    "blocked_reason": record.get("blocked_reason"),
                    "warnings": record.get("warnings", []),
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
                    "writer_error_kind": record.get("writer_error_kind"),
                    "blocked_reason": record.get("blocked_reason"),
                    "errors": record.get("errors", []),
                    "warnings": record.get("warnings", []),
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
        run_log=sandbox / "task_agent_runs.jsonl",
        lock_dir=sandbox / ".task_writer_full_locks",
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
- Full runs use the sandbox-local Python guard; do not bypass it.
- Required internal science iterations: keep iterating until `matched`, up to {rounds} cycles maximum.

## Mandatory self-iteration protocol
You are not a one-shot report writer. You are the coder, runner, and reviewer for this task. Work in repeated cycles until the result is either matched, explained by a defensible gap, or genuinely failed.

For each cycle:
1. Implement or revise the task code/config.
2. Run smoke only as a quick sanity check.
3. Run full with `python -m tasks.{module} config.json` when `--run-repro` is enabled.
4. Inspect the local CSV/summary/PNG and compare them with the paper evidence images/text.
5. If the result does not match the paper claim, first assume your implementation, configuration, proxy model, axis scaling, normalization, baseline, seed, or plotting could be wrong. Form a concrete repair hypothesis, modify code or config, and run full again.
6. Create or refresh the paper-side comparison image for this task:
   - Prefer a tight crop of the exact target figure/subfigure from the rendered `paper_page_*.png` evidence.
   - If the exact crop is uncertain, create a small locator image that shows the relevant page/region with a visible red rectangle around the believed target.
   - Do not use an unannotated full `paper_page_*.png` as the final paper comparison image.
7. Record each cycle in `task_agent_result.md`: command, return code, changed files, observed mismatch, repair hypothesis, target-paper-figure crop/locator status, and next decision.

Do not stop after the first imperfect output. A first mismatch should normally trigger at least one code/config repair and rerun. Report `explained_gap` only after you have tried plausible implementation/config/model fixes and can name the remaining gap with evidence. Report `failed` only for a real blocker such as runtime failure, missing essential paper information, timeout, dependency failure, or no valid artifacts. Report `matched` only when local artifacts support the paper trend, scale, ordering, and baseline comparison for this task.

Stopping rule:
- If a cycle reaches `matched`, stop immediately and write the final files.
- If a cycle is not `matched`, continue to the next repair/rerun cycle until cycle {rounds}.
- Do not choose `explained_gap` before the final allowed cycle unless an essential paper detail is provably unavailable and no code/config change could test it.
- At cycle {rounds}, if the result is still not a complete match, write the strongest honest conclusion: `explained_gap` when artifacts exist and the remaining difference is evidenced; `failed` when the task lacks usable artifacts or is blocked.

## Required final files
- `task_agent_result.md`: Chinese, human-readable comparison report.
- `paper_target_figure.json`: JSON object describing how you located the paper-side figure, with fields such as `target_figure`, `source_page`, `bbox_norm`, `confidence`, `contains_only_target`, `fallback_used`, `reason`, and `paper_image_paths`.
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
- `paper_image_paths` must list your task-specific paper comparison image(s), preferably under `outputs/{output_subdir}/` and relative to the sandbox root. Use the tight crop when confident; otherwise use the red-box locator image. Do not list raw `paper_page_1.png` / `paper_page_2.png` style full-page files, and do not simply rename a full paper page as a crop.
- If `status == "explained_gap"`, `differences`, `possible_causes`, `remaining_uncertainties`, and `evidence_files` must all be non-empty.
- If `status == "matched"`, cite the local CSV/PNG/summary and paper evidence that support the match.
- If `status == "failed"`, explain whether the blocker is runtime, missing paper details, timeout, dependency, or modeling uncertainty.

## Paper target figure image guidance
- You may use Python with Pillow/PyMuPDF/OpenCV-like array logic if available, but keep dependencies within the allowed whitelist.
- Recommended output names: `outputs/{output_subdir}/paper_target_crop.png` for a confident crop, or `outputs/{output_subdir}/paper_target_locator.png` for a red-box fallback.
- The final report is assembled from `outputs/{output_subdir}/`; put the paper-side PNG there so it remains self-contained after host aggregation.
- The paper-side image is for human comparison in the final Word report. Favor readability over showing an entire page.
- Keep original rendered paper pages untouched under `paper_evidence/`; write your derived crop/locator under `outputs/{output_subdir}/`.

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
    result_path, result_path_fallback = _task_result_file_path(sandbox, output_subdir, "task_agent_result.json")
    md_path, md_path_fallback = _task_result_file_path(sandbox, output_subdir, "task_agent_result.md")
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

    warnings: list[str] = []
    if result_path_fallback:
        warnings.append("task_agent_result.json found under outputs/<task>; accepted as fallback")
    if md_path_fallback:
        warnings.append("task_agent_result.md found under outputs/<task>; accepted as fallback")

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
    if run_repro and successful_full_runs and not _run_record_has_required_artifacts(successful_full_runs[-1]):
        warnings.append("trusted full run did not record required CSV/PNG/summary artifacts")
    if run_records and not trusted_records:
        warnings.append("task_agent_runs.jsonl contains no trusted guard records; ignoring it for host validation")

    artifacts = inspect_output_artifacts(sandbox, subdir=output_subdir)
    if run_repro and status in {"matched", "explained_gap"}:
        if not artifacts.get("has_csv"):
            errors.append("missing valid local CSV artifact")
        if not artifacts.get("has_png"):
            errors.append("missing valid local PNG artifact")
        if not artifacts.get("has_summary_json"):
            errors.append("missing valid local summary.json artifact")
        for invalid in artifacts.get("invalid_files", []):
            errors.append(f"invalid artifact: {invalid}")

    paper_locator_path, paper_locator_fallback = _task_result_file_path(sandbox, output_subdir, "paper_target_figure.json")
    paper_locator_doc: dict[str, Any] = {}
    if paper_locator_path.exists():
        try:
            parsed_locator = json.loads(paper_locator_path.read_text(encoding="utf-8"))
            if isinstance(parsed_locator, dict):
                paper_locator_doc = parsed_locator
            else:
                errors.append("paper_target_figure.json must contain an object")
        except Exception as exc:
            errors.append(f"paper_target_figure.json is invalid JSON: {type(exc).__name__}: {exc}")
    elif status in {"matched", "explained_gap"}:
        errors.append("missing paper_target_figure.json")
    if paper_locator_fallback:
        warnings.append("paper_target_figure.json found under outputs/<task>; accepted as fallback")
    if status in {"matched", "explained_gap"} and paper_locator_doc:
        errors.extend(_validate_paper_locator_doc(paper_locator_doc))

    paper_images, paper_image_warnings, paper_image_errors = _task_paper_image_paths(
        sandbox=sandbox,
        output_subdir=output_subdir,
        result_doc=result_doc,
        locator_doc=paper_locator_doc,
    )
    warnings.extend(paper_image_warnings)
    if status in {"matched", "explained_gap"}:
        errors.extend(paper_image_errors)
    local_images = _task_local_image_paths(sandbox, output_subdir)
    if not paper_images:
        if status in {"matched", "explained_gap"}:
            errors.append("missing writer-provided paper target image")
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
        "paper_locator_path": str(paper_locator_path) if paper_locator_path.exists() else None,
        "paper_locator": paper_locator_doc,
        "run_log_path": str(trusted_run_log_path) if trusted_run_log_path.exists() else None,
        "run_records": trusted_records,
        "full_run": full_runs[-1] if full_runs else None,
        "artifacts": artifacts,
        "local_images": local_images,
        "paper_images": paper_images,
        "structural_ok": structural_ok,
        "errors": errors,
        "warnings": warnings,
        "writer_error_kind": writer_status.get("error_kind") if isinstance(writer_status, dict) else None,
        "blocked_reason": writer_status.get("blocked_reason") if isinstance(writer_status, dict) else None,
    }


def _task_result_file_path(sandbox: Path, output_subdir: str, filename: str) -> tuple[Path, bool]:
    root_path = sandbox / filename
    if root_path.exists():
        return root_path, False
    output_path = sandbox / "outputs" / output_subdir / filename
    if output_path.exists():
        return output_path, True
    return root_path, False


def _non_empty_list(value: Any) -> bool:
    return isinstance(value, list) and any(str(item).strip() for item in value)


def _validate_paper_locator_doc(locator_doc: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for key in ("target_figure", "confidence", "reason"):
        if not isinstance(locator_doc.get(key), str) or not str(locator_doc.get(key)).strip():
            errors.append(f"paper_target_figure.json requires non-empty {key}")
    source_page = locator_doc.get("source_page")
    if isinstance(source_page, bool) or not isinstance(source_page, (int, str)) or not str(source_page).strip():
        errors.append("paper_target_figure.json requires source_page")
    if not isinstance(locator_doc.get("fallback_used"), bool):
        errors.append("paper_target_figure.json requires boolean fallback_used")
    if not isinstance(locator_doc.get("contains_only_target"), bool):
        errors.append("paper_target_figure.json requires boolean contains_only_target")
    if not _non_empty_list(locator_doc.get("paper_image_paths")) and not any(
        isinstance(locator_doc.get(key), str) and str(locator_doc.get(key)).strip()
        for key in ("crop_path", "locator_path", "image_path")
    ):
        errors.append("paper_target_figure.json requires paper_image_paths or a crop/locator/image path")
    bbox = locator_doc.get("bbox_norm")
    if bbox is not None:
        if not isinstance(bbox, list) or len(bbox) != 4:
            errors.append("paper_target_figure.json bbox_norm must be a list of four numbers")
        else:
            for value in bbox:
                if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0 or value > 1:
                    errors.append("paper_target_figure.json bbox_norm values must be numbers in [0, 1]")
                    break
    return errors


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


def _task_paper_image_paths(
    *,
    sandbox: Path,
    output_subdir: str,
    result_doc: dict[str, Any],
    locator_doc: dict[str, Any],
) -> tuple[list[str], list[str], list[str]]:
    warnings: list[str] = []
    errors: list[str] = []
    raw_paths: list[str] = []

    raw_paths.extend(_string_list(result_doc.get("paper_image_paths")))
    if not raw_paths:
        raw_paths.extend(_string_list(locator_doc.get("paper_image_paths")))
    for key in ("crop_path", "locator_path", "image_path"):
        value = locator_doc.get(key)
        if isinstance(value, str) and value.strip():
            raw_paths.append(value)

    if not raw_paths:
        return [], warnings, ["task_agent_result.json paper_image_paths is empty"]

    resolved_paths: list[str] = []
    seen: set[str] = set()
    raw_page_hashes = _raw_rendered_paper_page_hashes(sandbox)
    for raw in raw_paths:
        path = _resolve_writer_declared_path(sandbox=sandbox, output_subdir=output_subdir, raw_path=raw)
        if path is None:
            errors.append(f"paper image path does not exist: {raw}")
            continue
        if not _path_is_inside(path, sandbox):
            errors.append(f"paper image path must stay inside task sandbox: {raw}")
            continue
        if _is_raw_rendered_paper_page(path):
            errors.append(f"paper image path must be a writer-created crop or locator, not raw page: {raw}")
            continue
        if not _looks_like_png(path):
            errors.append(f"paper image path is not a valid PNG: {raw}")
            continue
        digest = _file_sha256(path)
        if digest and digest in raw_page_hashes:
            errors.append(f"paper image path appears to be an unmodified rendered paper page: {raw}")
            continue
        normalized = _copy_paper_image_to_output_dir(sandbox=sandbox, output_subdir=output_subdir, path=path)
        if normalized != path:
            warnings.append(f"paper image copied into outputs/{output_subdir}: {path.name}")
            path = normalized
        key = str(path.resolve()).lower()
        if key in seen:
            continue
        seen.add(key)
        resolved_paths.append(str(path.resolve()))

    if not resolved_paths and raw_paths:
        warnings.append("writer declared paper_image_paths but none were usable")
    return resolved_paths, warnings, errors


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _resolve_writer_declared_path(*, sandbox: Path, output_subdir: str, raw_path: str) -> Path | None:
    raw = str(raw_path).strip().strip('"')
    if not raw:
        return None
    candidate = Path(raw)
    candidates: list[Path] = []
    if candidate.is_absolute():
        candidates.append(candidate)
    else:
        candidates.extend([sandbox / candidate, sandbox / "outputs" / output_subdir / candidate])
        if candidate.parent == Path("."):
            candidates.append(sandbox / "outputs" / output_subdir / candidate.name)
    for path in candidates:
        if path.exists() and path.is_file():
            return path
    return None


def _path_is_inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _is_raw_rendered_paper_page(path: Path) -> bool:
    return bool(re.fullmatch(r"paper_page_\d+\.png", path.name))


def _raw_rendered_paper_page_hashes(sandbox: Path) -> set[str]:
    hashes: set[str] = set()
    evidence_root = sandbox / "paper_evidence"
    if not evidence_root.exists():
        return hashes
    for path in evidence_root.rglob("paper_page_*.png"):
        if path.is_file() and _is_raw_rendered_paper_page(path):
            digest = _file_sha256(path)
            if digest:
                hashes.add(digest)
    return hashes


def _file_sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _looks_like_png(path: Path) -> bool:
    try:
        return path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    except OSError:
        return False


def _copy_paper_image_to_output_dir(*, sandbox: Path, output_subdir: str, path: Path) -> Path:
    output_dir = sandbox / "outputs" / output_subdir
    try:
        path.resolve().relative_to(output_dir.resolve())
        return path
    except ValueError:
        pass
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = safe_label(path.stem) or "paper_target_image"
    target = output_dir / f"{stem}{path.suffix or '.png'}"
    if target.exists() and target.resolve() != path.resolve():
        for index in range(2, 1000):
            candidate = output_dir / f"{stem}_{index}{path.suffix or '.png'}"
            if not candidate.exists():
                target = candidate
                break
    if target.resolve() != path.resolve():
        shutil.copy2(path, target)
    return target


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
        for name in ("task_agent_result.json", "task_agent_result.md", "paper_target_figure.json"):
            source, _ = _task_result_file_path(sandbox, output_subdir, name)
            if source.exists():
                shutil.copy2(source, result_dir / name)
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
        "Each task delivered its own code, artifacts, and self-review before host aggregation.\n",
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
    requirement_warnings: list[dict[str, Any]],
    security_issues: list[dict[str, Any]],
) -> dict[str, Any]:
    passed = sum(1 for record in task_records if _task_writer_runtime_task_passed(record))
    delivered = sum(1 for record in task_records if record.get("structural_ok"))
    total = len(task_records)
    valid_task_ids = [str(record.get("task_id")) for record in task_records if _task_writer_runtime_task_passed(record)]
    valid_csv_files: list[str] = []
    valid_png_files: list[str] = []
    valid_summary_json_files: list[str] = []
    for record in task_records:
        if not _task_writer_runtime_task_passed(record):
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
        "deliveries_passed": delivered,
        "delivery_coverage": f"{delivered}/{total}",
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
                "passed": _task_writer_runtime_task_passed(record),
                "delivery_ok": bool(record.get("structural_ok")),
                "task_writer_status": record.get("task_writer_status"),
                "writer_error_kind": record.get("writer_error_kind"),
                "blocked_reason": record.get("blocked_reason"),
                "full_run": record.get("full_run"),
                "artifacts": record.get("artifacts"),
                "errors": record.get("errors", []),
                "warnings": record.get("warnings", []),
            }
            for record in task_records
        ],
        "validation": validation,
        "manifest_issues": manifest_issues,
        "requirements_issues": requirement_issues,
        "requirements_warnings": requirement_warnings,
        "security_issues": security_issues,
    }


def _task_writer_runtime_task_passed(record: dict[str, Any]) -> bool:
    return bool(record.get("structural_ok")) and str(record.get("task_writer_status") or "") in {
        "matched",
        "explained_gap",
    }


def _task_writer_alignment_summary(task_records: list[dict[str, Any]]) -> dict[str, Any]:
    if not task_records:
        return {
            "overall_alignment": "inconclusive",
            "overall_result_credibility": "low",
            "overall_summary": "没有可审查的复现任务。",
        }
    if any(_task_writer_blocked_by_codex(record) for record in task_records):
        return {
            "overall_alignment": "inconclusive",
            "overall_result_credibility": "low",
            "overall_summary": "至少一个 Codex task writer 因额度或限流被阻塞，不能把缺失任务视为科学复现失败。",
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
    appendix_sections: list[str] = []
    for index, record in enumerate(task_records, start=1):
        task_id = str(record.get("task_id") or f"task_{index}")
        result = record.get("result_json") if isinstance(record.get("result_json"), dict) else {}
        lines = [f"## {index}. {task_id}", ""]
        lines.extend(
            [
                f"**Writer 结论：** `{record.get('task_writer_status', 'failed')}`",
                f"**主持人结构验收：** {'通过' if record.get('structural_ok') else '失败'}",
            ]
        )
        if record.get("errors"):
            lines.append("**结构问题：** " + "；".join(str(item) for item in record.get("errors", [])))
        if record.get("blocked_reason"):
            lines.append(f"**执行阻塞：** {record.get('blocked_reason')}")
        lines.append("")

        lines.extend(_image_comparison_markdown(record=record, result=result))
        lines.extend(["### 简短审查结论", ""])
        lines.append(str(result.get("summary") or "Writer 未提供简短结论。"))
        lines.append("")
        lines.extend(_result_list_section("关键差异", result.get("differences"), default="未报告明显差异。"))
        lines.extend(_result_list_section("可能原因", result.get("possible_causes"), default="未报告可能原因。"))
        lines.extend(_result_list_section("剩余不确定性", result.get("remaining_uncertainties"), default="未报告剩余不确定性。"))
        lines.extend(_result_list_section("证据文件", result.get("evidence_files"), default="未列出证据文件。"))
        lines.extend(["", f"完整 writer 自审原文见附录 A{index}。"])

        md = _read_optional_text(record.get("result_markdown_path"))
        appendix_lines = [f"### A{index}. {task_id}", ""]
        if md:
            appendix_lines.append(md.strip())
        else:
            appendix_lines.extend(_fallback_writer_review_lines(result))
        appendix_sections.append("\n".join(appendix_lines).strip() + "\n")
        sections.append("\n".join(lines).strip() + "\n")

    if appendix_sections:
        sections.append("## 附录：Writer 自审原文\n")
        sections.extend(appendix_sections)
    return "\n".join(sections).strip() + "\n"


def _image_comparison_markdown(*, record: dict[str, Any], result: dict[str, Any]) -> list[str]:
    local_images = [str(path) for path in record.get("local_images", []) or [] if str(path).strip()]
    paper_images = [str(path) for path in record.get("paper_images", []) or [] if str(path).strip()]
    figure_label = _human_figure_label_from_record(record, result)
    paper_caption = "论文原图" if not figure_label else f"论文原图：{figure_label}"
    lines = ["### 图像对比", ""]
    if local_images and paper_images:
        lines.extend(["| 本地复现图 | 论文原图 |", "|---|---|"])
        row_count = max(len(local_images), len(paper_images))
        for row_index in range(row_count):
            local_cell = _markdown_image_cell(
                local_images[row_index] if row_index < len(local_images) else "",
                "本地复现图" if row_count == 1 else f"本地复现图 {row_index + 1}",
            )
            paper_cell = _markdown_image_cell(
                paper_images[row_index] if row_index < len(paper_images) else "",
                paper_caption if row_count == 1 else f"{paper_caption} {row_index + 1}",
            )
            lines.append(f"| {local_cell} | {paper_cell} |")
        lines.append("")
        return lines

    single_images = [("本地复现图", path) for path in local_images] or [(paper_caption, path) for path in paper_images]
    if not single_images:
        lines.extend(["无可用图片。", ""])
        return lines
    for caption, path in single_images:
        lines.append(_markdown_image_cell(path, caption))
        lines.append("")
    return lines


def _markdown_image_cell(path: str, caption: str) -> str:
    if not path:
        return "无可用图片"
    return f"![{caption}]({path})"


def _result_list_section(title: str, values: Any, *, default: str) -> list[str]:
    lines = [f"### {title}", ""]
    if isinstance(values, list):
        items = [str(item) for item in values if str(item).strip()]
    elif values:
        items = [str(values)]
    else:
        items = []
    if not items:
        items = [default]
    lines.extend(f"- {item}" for item in items)
    lines.append("")
    return lines


def _human_figure_label(task_id: str) -> str:
    match = re.search(r"fig(?:ure)?[._\s:-]*([0-9]+)[._\s:-]*\(?([a-z])?\)?\b", task_id, re.I)
    if not match:
        return ""
    number = match.group(1)
    letter = match.group(2)
    return f"Fig. {number}({letter.lower()})" if letter else f"Fig. {number}"


def _human_figure_label_from_record(record: dict[str, Any], result: dict[str, Any]) -> str:
    candidates = [
        str(record.get("task_id") or ""),
        str(result.get("task_id") or ""),
        str(result.get("summary") or ""),
    ]
    for candidate in candidates:
        label = _human_figure_label(candidate)
        if label:
            return label
    return ""


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
        "warnings": record.get("warnings", []),
        "writer_error_kind": record.get("writer_error_kind"),
        "blocked_reason": record.get("blocked_reason"),
    }


def _task_writer_concurrency(task_count: int, requested: int | None, *, run_repro: bool = False) -> int:
    if task_count <= 0:
        return 1
    if requested is not None:
        base = max(1, min(task_count, int(requested)))
        return min(base, _task_writer_full_slot_cap()) if run_repro else base
    raw = os.environ.get("GENG_CODEX_TASK_WRITER_CONCURRENCY")
    if raw:
        try:
            base = max(1, min(task_count, int(raw)))
            return min(base, _task_writer_full_slot_cap()) if run_repro else base
        except ValueError:
            pass
    base = min(task_count, DEFAULT_TASK_WRITER_AGENT_CONCURRENCY)
    return min(base, _task_writer_full_slot_cap()) if run_repro else base


def _task_writer_full_slot_cap() -> int:
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


def _task_writer_stop_class(task_records: list[dict[str, Any]]) -> str:
    if not task_records:
        return "no_tasks"
    if any(_task_writer_blocked_by_codex(record) for record in task_records):
        return "blocked_by_codex"
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
        "blocked_by_codex": "one or more Codex task writers were blocked by usage limits or rate limits",
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
        "warnings": [],
        "local_images": [],
        "paper_images": [],
    }


def _task_writer_blocked_by_codex(record: dict[str, Any]) -> bool:
    kind = str(record.get("writer_error_kind") or "")
    return kind in {"codex_usage_limit", "codex_rate_limit"}
