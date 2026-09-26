"""Build initial and continuation briefs for task-writer Codex sessions."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .case_runtime import CaseRuntime
from .json_utils import pretty_json
from .paper_evidence import facts_for_task, safe_label
from .scientific_materiality import CORE_RESULT_STOP_POLICY
from .task_writer_contracts import WRITER_PAPER_FIDELITY_POLICY
from .task_writer_units import _public_execution_unit


LONG_RUNNING_FULL_RUN_PROTOCOL = """## Host-owned scientific execution
Use the provided `run_task.py` launcher and selected case Python. For a long run,
add `--submit` to return promptly, then use `python run_task.py --task TASK_ID --status`
for short status checks. Every smoke/full launch must add `--device cpu` when its expensive computation is
CPU-only or `--device gpu` when it uses CUDA. Independent GPU tasks run concurrently;
the host does not reserve the GPU or wait for other tasks to release it.
The Writer shell intentionally hides CUDA;
`torch.cuda.is_available()` there does not describe the host GPU. Inspect
`nvidia-smi` and use a `--device gpu` smoke launch to test the real GPU.
Keep submitted source, config and inputs unchanged until the run completes. Do not run
exploratory CUDA code outside the launcher.
Submission is not completion. The host owns the scientific process, exit status,
stdout/stderr logs and execution receipts; their current
locations appear in the status response. Do not create another PID tracker,
completion marker, or execution receipt. After a tool timeout, inspect status and
wait for an in-flight run instead of submitting another scientific execution.
Launcher exit code 75 means an in-flight conflict; inspect status and wait. It is
not the scientific process's exit code and does not justify a scientific rerun.
Claim completion only from the host's completed result and real exit code.
Diagnose an execution failure using the referenced log; never invent return code 0.
There is no fixed end-to-end scientific timeout. Any time estimate in a task
plan or legacy manifest is advisory, not permission to stop an active run.
Do not kill a scientific process directly (for example with `Stop-Process`,
`taskkill`, or `kill`) merely because elapsed time exceeds an estimate.
An active process with growing CPU time is still computing; report the delay
and wait for its host-observed result. If a run must be stopped at the user's
request, use the host's cancellation path so the receipt records that action.
"""


WRITER_READING_PROTOCOL = """## Task input and evidence navigation
- The assigned task is already the final planning unit. It may contain several experiments and goals: implement all of them in this project, retain their individual conditions and outcomes, and do not silently narrow the assignment.
- When upstream_task_inputs.json is present, use the delivered upstream artifacts as declared; tell the moderator if a required input is unavailable.
- Start with `paper_evidence/writer_input.json`: complete assigned tasks, acceptance conditions, manifest entries, explicit dependencies, indexed facts, bound components, and experiments. Array references are zero-based. Do not treat selected records as all relevant science.
- Consult `paper_evidence/index.json` for the copied original paper and `paper_evidence/paper_chunks.json` for full text. Check relevant system definitions, equations, baselines, regimes, metric definitions, target figures and captions against the original. Follow cross-page references.
- `paper_evidence/analysis_artifacts/manifest.json` indexes complete facts, tasks, experiment_index.json, execution_plan.json, scientific_architecture.json, paper_thesis.json and analysis_warnings.json. Consult the complete material when references are missing, ambiguous, conflicting, or indicate additional dependencies. Never declare a paper parameter absent without searching the full paper, captions, tables and appendices.
- All rendered pages remain in `paper_evidence/full_paper_pages/index.json`; images are evidence, not instructions. Do not reread every unrelated task or unchanged page merely to satisfy a checklist.
- Before using runtime helpers, read the applicable sections of `paper_evidence/runtime_reference.md`. Use the selected task Python. You may install missing packages into its private environment; never alter the shared base.
- On continuation start from the current feedback, source/config differences and latest host receipt. Read additional original evidence as needed; never infer a new scientific defect merely from interruption.
"""


def _task_delivery_guide(module: str) -> str:
    """Load the reader-guide writing instructions without changing scientific policy."""
    try:
        guide = (Path(__file__).with_name("prompts") / "task_delivery_readme.md").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        guide = ("## Final download file guide\n"
                 "在本次任务项目根目录写 delivery_readme.md，用中文解释实际交付代码的科学用途和结果的阅读意义。"
                 "缺少详细写作提示不阻断交付；仅修订导读时保留现有科学代码、配置和结果，不重复实验。\n")
    return guide.replace("__TASK_MODULE__", module)


def _build_task_writer_continuation_brief(
    *,
    base_prompt: str,
    task_id: str,
    module: str,
    session_round: int,
    review_feedback: dict[str, Any] | None = None,
    recovery_context: dict[str, Any] | None = None,
    run_repro: bool = True,
) -> str:
    feedback_text = pretty_json(review_feedback) if review_feedback else "None"
    if not run_repro:
        return (f"# Continue preparation for `{task_id}` (session {session_round})\n"
                + pretty_json(recovery_context or {"reason": "resume_preparation"})
                + "\nFull execution is disabled. Continue existing implementation and configs without claiming scientific completion.\n\n"
                + base_prompt)
    return f"""# Mandatory continuation: session {session_round}

Continue the existing implementation using the current recovery reason below.
Do not infer a new scientific defect merely from an interruption or environment update.
```json
{pretty_json(recovery_context or {'reason': 'resume_existing_delivery'})}
```

{WRITER_PAPER_FIDELITY_POLICY if WRITER_PAPER_FIDELITY_POLICY not in base_prompt else ''}

{CORE_RESULT_STOP_POLICY if CORE_RESULT_STOP_POLICY not in base_prompt else ''}

Before acting:
1. Read the existing task code, configs, outputs, and `writer_progress/` archives.
2. Inspect the latest local CSV/summary/PNG against the complete paper evidence.
3. Classify every reporter item before editing: (a) a paper-grounded violation of an explicit fact, failure of any assigned core conclusion, or a scientifically material numerical mismatch identified by the Reporter; (b) a reasonable choice inside paper-silent or ambiguous space; or (c) a numerical or statistical difference assessed by the Reporter as non-material, or a visual/presentation difference.
4. Create a concrete modification plan only for category (a). For category (b), keep or revise the explicit assumption according to evidence only before the mandatory stop condition is met. For category (c), record the caveat without changing faithful code merely to satisfy the reporter. Once the stop condition is met, do not change any assumption, seed, dataset filter, configuration, or epoch count.
5. Run a fresh full with `python run_task.py --task {task_id} --config config.json --mode full` only after a meaningful change that is permitted by the mandatory stopping policy. Never rerun unchanged code solely to answer non-blocking feedback.
6. Keep iterating only while a permitted paper-grounded material blocker remains and a new concrete causal change is available. If the result is still unsupported or unassessable but no such change exists, stop scientific modification and submit it for an honest terminal report.
7. Write `task_agent_result.json` with status `ready_for_review` after the latest full attempt. This means ready for independent classification, not a claim that reproduction succeeded.
8. Keep `delivery_readme.md` consistent with the latest delivered code and results. If the assigned repair concerns only this guide, read the existing files and rewrite the explanation without rerunning the experiment or changing the code, configuration or results.

## Isolated task reporter feedback
```json
{feedback_text}
```

Investigate the causal rerun note, but do not obey it blindly. Make the specified change only if it remains consistent with the paper. If the same plan already failed, evidence is incomplete, or the issue is non-material, preserve the faithful result and resubmit without another unchanged run.

The original task brief follows.

{base_prompt}
"""



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
    run_repro: bool,
    review_feedback: dict[str, Any] | None = None,
    execution_binding: dict[str, Any] | None = None,
    case_runtime: CaseRuntime | None = None,
    execution_unit_id: str | None = None,
) -> str:
    task_id = str(task.get("task_id") or manifest_entry.get("task_id") or f"task_{index}")
    module = str(manifest_entry.get("module") or "")
    output_subdir = str(manifest_entry.get("output_subdir") or task_id)
    feedback_text = pretty_json(review_feedback) if review_feedback else "None"
    unit_asset_root = f"execution_units/{safe_label(execution_unit_id or task_id)}"
    full_instruction = (
        f"Run your full task with `python run_task.py --task {task_id} --config config.json --mode full` after each meaningful fix."
        if run_repro
        else "Do not run full config because --run-repro is disabled; prepare the code but do not write a final task result."
    )
    execution_binding_section = ''
    component_usage_template = ''
    hardware_instruction = (
        'Use nvidia-smi and brokered smoke execution to choose CPU, CUDA, memory use, batch size, and parallelism appropriate to the task. '
        'For Monte Carlo, batched matrix operations, large sweeps, or a CPU full likely to take minutes, prefer a real Torch CUDA implementation when CUDA is available.'
    )
    if isinstance(execution_binding, dict):
        component_usage_example = [
            {
                'component_id': str(component.get('component_id') or ''),
                'module': str(component.get('module') or ''),
                'callable': str(component.get('callable') or ''),
                'usage': 'in_scientific_path',
                'evidence_files': [f'tasks/{module}.py:line'],
            }
            for component in execution_binding.get('components', [])
            if isinstance(component, dict)
        ]
        component_usage_key = json.dumps('component_usage')
        component_usage_template = (
            f'  {component_usage_key}: {pretty_json(component_usage_example)},'
        )
        execution_binding_section = f'''## Mandatory scientific execution binding (architecture 1.1)
Read this task's binding and indexed component contracts in `paper_evidence/writer_input.json`. Each component is stored once; no contract is summarized or truncated.

- Consume the listed `module` / `callable` implementations in the real computation that produces the submitted CSV, summary, and figure. A task may import a declared component itself or implement an appropriate composition in this task project.
- Explain which implementations actually produced the submitted results. The architecture is guidance; record necessary corrections against the paper.
- Keep scientifically required training, evaluation and ablation state consistent within this task. Preserve checkpoints needed to rerun or explain each experiment.
- Add `component_usage` to `task_agent_result.json`, with one exact entry per bound component as shown once in the final result example.
'''
        hardware_instruction = (
            'Follow each bound component execution.primary_framework and execution.device_policy exactly. '
            'Do not substitute Torch, CUDA, NumPy, CPU, or another framework/device heuristic for the architecture contract. '
            'Record evidence that expensive computation ran under the declared policy.'
        )
    ownership_instruction = "You own this complete task project, including all src/ modules, tests, configs, dependencies and results. Repair them directly when the paper and observations justify it."
    prompt = f"""# Role: autonomous Codex task writer

You own exactly one reproduction task. Write the code, run the assigned full experiment, compare the result directly with the complete paper, and keep revising and rerunning while a paper-grounded material scientific blocker remains. Your handoff is `ready_for_review`; only the independent reporter may grant final `matched`.

{WRITER_PAPER_FIDELITY_POLICY}

{CORE_RESULT_STOP_POLICY}

{LONG_RUNNING_FULL_RUN_PROTOCOL}


## Ownership
- Assigned task_id: `{task_id}`
- Assigned module: `tasks.{module}`
- Output directory: `outputs/{output_subdir}/`
- Store task-private datasets, splits, checkpoints, caches, and persistent state under `{unit_asset_root}/`; both configs expose this path as `unit_asset_root` so the final package cannot collide with another execution unit.
- You own the task-private portion of this isolated sandbox. {ownership_instruction}
- Do not edit `src/_io.py`, `src/_backend.py`, `run_task.py`, `run_experiment.py`, `tasks_manifest.json`, `tasks/__init__.py`, or any other task module.
- Read your task packet binding and preserve its shared shapes, units, normalization, component identities, and invariants. Complete architecture is accessible through the analysis library when further context is needed.
- {full_instruction}
- You may run smoke with `python run_task.py --task {task_id} --config config_smoke.json --mode smoke`.
- Use the selected case Python. The trusted launcher observes one actual scientific process with the existing filesystem/environment isolation and records its exit and consumed inputs. You still choose the scientific implementation, hardware usage and experiment settings. Do not request an unobserved full via a raw module command.
- Pass each reused checkpoint/data file as `--input RELATIVE_PATH`; persistent generated state needs a current producer receipt. Keep consumed inputs immutable; write newly trained checkpoints to a new path. Install missing dependencies into your selected private task Python and record what you added.
- {hardware_instruction}
- Calling `_backend.select_backend()` is not GPU acceleration by itself. If CUDA is selected, the expensive computation must actually run on CUDA tensors. If CPU is selected despite available CUDA, record a concrete task-specific reason.
- There is no arbitrary wall-clock cycle limit, but the mandatory core-result stopping policy is a hard upper boundary on scientific iteration. External process failures are handled by the host.

{execution_binding_section}

## Paper-faithful core objective
Use `task.scientific_acceptance` as a short navigation list, not a format gate. Recover any missing intended claim from the task and paper. Prioritize paper-explicit models, equations, algorithms, baselines, regimes, axes, and statistics, then decide whether the scientific conclusion is supported. Do not spend runs reproducing pixels, typography, colors, crop boundaries, private code identity, or other presentation details.

The core conclusion is normally a method identity, comparison direction, ordering, trend, crossing/threshold region, scaling behavior, gain/loss region, mechanism, or an explicitly claimed absolute level. Numerical agreement must be judged against the claim, natural metric scale and statistical uncertainty; a magnitude ratio is diagnostic only.

## Self-iteration protocol
You are the coder, runner, and first reviewer. You should compare and improve your own implementation, but another full run must have a scientific reason and a concrete causal change.

For each cycle:
1. Read the task packet and check the relevant original paper definitions, equations, conditions and results. On continuation inspect current feedback and changed evidence; reuse unchanged work rather than rereading every artifact by default.
2. Search the complete paper before filling a missing parameter. If still absent, make and disclose a scientifically plausible assumption; do not relabel it as a paper fact.
3. Implement the paper-faithful task, run smoke when useful, then run full with `python run_task.py --task {task_id} --config config.json --mode full`.
4. Compare explicit scientific facts, each core conclusion, and Task-Designer key numeric targets. Record material and non-material differences separately.
5. Rerun only for `invalid_run`, `core_conclusion_failed`, or `material_numeric_discrepancy`, and only after recording paper evidence, the specific code/config change, its target, and predicted effect.
6. Stop changing the science immediately when the Reporter concludes that the evidence supports the assigned conclusions. Also stop when a valid faithful result remains unsupported or unassessable but there is no new evidence-based causal change. In that case, hand the result to the Reporter; do not loop forever or tune toward the picture.
7. Never rerun unchanged code. A repeated ineffective plan, seed fishing, broad hyperparameter sweep, extra epochs without a causal hypothesis, or report-only change is not progress.

Your normal handoff is always `ready_for_review` after the latest full and honest comparison. It does not assert final success: the independent Reporter may classify it as reproduced, reproduced with assumptions, inconclusive, or not reproduced. Only the Reporter plus host may request another Writer run, and only with a complete causal rerun note.
## Required final files
Always write the handoff after the latest full attempt, including when the run failed or the scientific result remains unsupported. Structure is intentionally small; missing optional prose or images must not trigger another scientific run.

- `task_agent_result.json` is the machine handoff note. Keep all scientific evidence files. Do not write a duplicate scientific-review narrative; the separate `delivery_readme.md` below is a reader's guide to the delivered code and results. The host supplies observed execution metadata; your statements remain self-reported.
- `delivery_readme.md` is part of every task's user delivery. Write the Chinese explanation yourself in this same session, even when the scientific result remains unsupported. Explain the actual available files; do not claim results that were not produced. The optional nature of `delivery_files` metadata does not make this guide optional.
- Write the following concise JSON:
```json
{{
{component_usage_template}
  "task_id": "{task_id}",
  "status": "ready_for_review",
  "summary": "one Chinese sentence",
  "differences": ["material scientific differences"],
  "remaining_uncertainties": [],
  "evidence_files": [],
  "local_image_paths": [],
  "delivery_files": [
    {{"path": "tasks/{module}.py", "role": "code", "description": "用中文简述此文件实现的任务算法与实验"}},
    {{"path": "outputs/{output_subdir}/results.csv", "role": "result", "description": "用中文简述此结果文件包含的指标、变量或比较对象；仅列出实际存在的文件"}}
  ],
  "parameter_resolution": [
    {{"name": "parameter", "value": "value", "source": "paper|derived|assumed", "evidence": "page/equation or rationale"}}
  ],
  "iteration_records": [
    {{
      "full_run_index": 1,
      "scientific_reason": "initial_run|invalid_run|core_conclusion_failed|material_numeric_discrepancy",
      "comparison": ["local observation versus the paper conclusion"],
      "causal_change": "specific change made before this run, or empty for initial run",
      "outcome": "supported|unsupported|unassessable|invalid"
    }}
  ],
  "execution_refs": [{{"run_id": "host-issued ID", "receipt_path": "path returned by the launcher"}}],
  "implementation_notes": {{
    "method": "brief implementation choices not already described in the task",
    "backend_choice_reason": "task-specific reason",
    "actual_compute_device_evidence": "source/output evidence for where expensive computation ran"
  }}
}}
```
A readable PNG is useful for a figure task but optional when structured CSV/JSON/table/text evidence represents the result. Do not manufacture an image merely to pass a gate.

{_task_delivery_guide(module)}
## Independent reporter feedback from a previous delivery
```json
{feedback_text}
```
If feedback is present, investigate every reported difference against the paper's evidence hierarchy. Fix and rerun for a material paper-grounded blocker. Do not alter explicit paper facts, do not blindly obey speculative feedback, and do not rerun unchanged code for an acceptable assumption or non-material caveat.

{WRITER_READING_PROTOCOL}

"""
    if not run_repro:
        prompt = prompt.replace(
            "Write the code, run the assigned full experiment, compare the result directly with the complete paper, and keep revising and rerunning while a paper-grounded material scientific blocker remains. Your handoff is `ready_for_review`; only the independent reporter may grant final `matched`.",
            "Prepare the paper-faithful implementation and configs. Full execution is disabled; do not claim a scientific result.",
        )
        start = prompt.index("## Self-iteration protocol")
        end = prompt.index("## Final download file guide", start)
        prompt = prompt[:start] + "## Preparation handoff\nKeep the implementation and configs ready for a later full run. Describe unresolved requirements in README.md; do not write a final task result.\n\n" + prompt[end:]
    return prompt


def _task_experiment_index(index: dict[str, Any], tasks: list[dict[str, Any]],
                           unit: dict[str, Any] | None = None) -> dict[str, Any]:
    """Select complete matching experiments and explicit related tasks, never text slices."""
    task_ids = {str(task.get('task_id') or '') for task in tasks}
    experiment_ids = {str(task.get('experiment_id') or '') for task in tasks}
    if unit:
        task_ids.update(map(str, unit.get('task_ids', [])))
        for relation in unit.get('relationships', []):
            if isinstance(relation, dict):
                task_ids.update(map(str, relation.get('task_ids', [])))
                task_ids.update(map(str, relation.get('consumer_task_ids', [])))
                task_ids.update(str(relation.get(k) or '') for k in ('producer_task_id', 'consumer_task_id'))
    selected = [experiment for experiment in index.get('experiments', [])
                if isinstance(experiment, dict) and (
                    str(experiment.get('task_id') or '') in task_ids - {''}
                    or str(experiment.get('experiment_id') or '') in experiment_ids - {''}
                    or bool(set(map(str, experiment.get('task_ids', []))) & task_ids))]
    return {"experiments": selected,
            "complete_index": "paper_evidence/analysis_artifacts/experiment_index.json"}


def _unit_fact_navigation(facts: dict[str, Any], tasks: list[dict[str, Any]]) -> dict[str, Any]:
    """Emit shared fact records once while retaining each task's selected evidence."""
    records: list[dict[str, Any]] = []
    by_content: dict[str, int] = {}
    task_indexes: dict[str, list[int]] = {}
    for task in tasks:
        indexes = []
        for fact in facts_for_task(facts, task).get("engineering_facts", []):
            key = json.dumps(fact, ensure_ascii=False, sort_keys=True)
            if key not in by_content:
                by_content[key] = len(records)
                records.append(fact)
            indexes.append(by_content[key])
        task_indexes[str(task.get("task_id") or "")] = indexes
    return {"engineering_facts": records, "task_fact_indexes_zero_based": task_indexes,
            "missing_information": facts.get("missing_information", []),
            "complete_facts": "paper_evidence/analysis_artifacts/engineering_facts.json"}
