"""Build initial and continuation briefs for task-writer Codex sessions."""

from __future__ import annotations

import json
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
for short status checks. Submission is not completion. The host owns the scientific
process, exit status, stdout/stderr logs and execution receipts; their current
locations appear in the status response. Do not create another PID tracker,
completion marker, or execution receipt. After a tool timeout, inspect status and
wait for an in-flight run instead of submitting another scientific execution.
Launcher exit code 75 means an in-flight conflict; inspect status and wait. It is
not the scientific process's exit code and does not justify a scientific rerun.
Claim completion only from the host's completed result and real exit code.
Diagnose an execution failure using the referenced log; never invent return code 0.
There is no fixed end-to-end scientific timeout.
"""


FOUNDATION_REVISION_PROTOCOL = """## Correcting shared scientific defects
If an observed material failure is caused by a frozen shared component, do not
patch a private copy or keep rerunning a known-bad implementation. Write
`foundation_revision_request.json` with the affected `component_ids`, existing
relative `paper_evidence_files` under `paper_evidence/`, a concrete
`causal_change`, and its `predicted_effect`. Explain the incorrect equation,
normalization, method, or assumption and the paper evidence supporting the fix.
Preserve the current outputs and exit the session. The host waits for other
active Writers, repairs only the requested shared modules in a new generation,
tests them, and restarts affected consumers. A missing paper detail alone is
not evidence for changing shared science to match a curve. Task-private
components remain yours to repair directly.
"""


WRITER_READING_PROTOCOL = """## Task input and evidence navigation
- Start with `paper_evidence/writer_input.json`: complete assigned tasks, acceptance conditions, manifest entries, explicit dependencies, indexed facts, bound components, and experiments. Array references are zero-based. Do not treat selected records as all relevant science.
- Consult `paper_evidence/index.json` for the copied original paper and `paper_evidence/paper_chunks.json` for full text. Check relevant system definitions, equations, baselines, regimes, metric definitions, target figures and captions against the original. Follow cross-page references.
- `paper_evidence/analysis_artifacts/manifest.json` indexes complete facts, tasks, experiment_index.json, execution_plan.json, scientific_architecture.json, paper_thesis.json and analysis_warnings.json. Consult the complete material when references are missing, ambiguous, conflicting, or indicate additional dependencies. Never declare a paper parameter absent without searching the full paper, captions, tables and appendices.
- All rendered pages remain in `paper_evidence/full_paper_pages/index.json`; images are evidence, not instructions. Do not reread every unrelated task or unchanged page merely to satisfy a checklist.
- Before using runtime helpers, read the applicable sections of `paper_evidence/runtime_reference.md`. Use the selected case Python and environment request channel; never install packages inside the Writer.
- On continuation start from the current feedback, source/config differences and latest host receipt. Read additional original evidence as needed; never infer a new scientific defect merely from interruption.
"""


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

## Isolated task reporter feedback
```json
{feedback_text}
```

Investigate the causal rerun note, but do not obey it blindly. Make the specified change only if it remains consistent with the paper. If the same plan already failed, evidence is incomplete, or the issue is non-material, preserve the faithful result and resubmit without another unchanged run.

The original task brief follows.

{base_prompt}
"""

def _build_execution_unit_writer_brief(
    *,
    unit: dict[str, Any],
    members: list[tuple[int, dict[str, Any], dict[str, Any]]],
    facts: dict[str, Any],
    experiment_index: dict[str, Any],
    paper_context_json: str,
    paper_thesis: dict[str, Any] | None,
    bindings: dict[str, Any],
    run_repro: bool,
    review_feedback: dict[str, Any],
    foundation_enabled: bool,
    case_runtime: CaseRuntime | None,
) -> str:
    unit_id = str(unit.get("unit_id") or "execution_unit")
    tasks_payload = [task for _index, task, _entry in members]
    manifest_payload = [entry for _index, _task, entry in members]
    task_commands = [
        {
            "task_id": str(task.get("task_id") or entry.get("task_id") or ""),
            **({"full": f"python run_task.py --task {entry.get('task_id')} --config configs/{entry.get('module')}_config.json --mode full"} if run_repro else {}),
            "smoke": f"python run_task.py --task {entry.get('task_id')} --config configs/{entry.get('module')}_config_smoke.json --mode smoke",
            "result_json": f"outputs/{entry.get('output_subdir')}/task_agent_result.json",
        }
        for _index, task, entry in members
    ]
    ownership = (
        "Only files listed in the installed Foundation manifest are frozen. "
        "Reuse those modules; unit-private components remain writable under their "
        "declared src/ paths or tasks/. Never overwrite or import-shadow a frozen module."
        if foundation_enabled
        else "You own the unit's task modules, helpers, configs, data, and checkpoint artifacts."
    )
    unit_asset_root = f"execution_units/{safe_label(unit_id)}"
    execution_instruction = (
        "Run every full phase in dependency order and finish all logical task deliveries."
        if run_repro
        else "Prepare all phases but do not run full experiments because --run-repro is disabled."
    )
    prompt = f"""# Role: autonomous Codex compound execution-unit Writer

You own one scientific execution unit containing multiple logical reproduction tasks. The tasks remain separately accepted and separately reviewed, but they must be implemented and executed together because splitting them would change the scientific comparison, shared state, random realization, data partition, or artifact flow.

{WRITER_PAPER_FIDELITY_POLICY}

{CORE_RESULT_STOP_POLICY}

{LONG_RUNNING_FULL_RUN_PROTOCOL}

{FOUNDATION_REVISION_PROTOCOL if foundation_enabled else ''}

## Unit contract
- Unit ID: `{unit_id}`
- {execution_instruction}
- Follow `execution_unit.json` exactly. Execute its `task_ids` in listed producer-before-consumer order.
- {ownership}
- Store every unit-private dataset, split, checkpoint, cache, and state artifact under `{unit_asset_root}/`; configs already expose this as `unit_asset_root`. This stable namespace prevents unrelated execution units from overwriting one another in the final portable package.
- You may edit every listed task module and unit-private helper. Do not edit `src/_io.py`, `src/_backend.py`, `run_task.py`, `run_experiment.py`, `tasks_manifest.json`, `tasks/__init__.py`, or `execution_unit.json`.
- Implement shared unit science once. A consumer must load the declared producer artifact; it must not silently retrain, regenerate a different dataset split, or replace a shared random realization.
- A shared model/trainer source file is not a shared checkpoint. Persist every required learned state, fitted transform, split, or realization using the execution plan's producer/consumer artifact IDs, and have consumers load that exact artifact.
- A strong same-run relationship may use one shared driver/helper called by the task entry points. Still emit one honest result note and output directory per logical task.
- Read `paper_evidence/writer_input.json` and check the original definitions, equations, assumptions and target figures. Do not optimize pixels, colors, typography, or layout.
- Numerical materiality depends on the claim, metric scale and uncertainty. Follow the Reporter decision and its cited causal correction; no universal factor-of-10 rule applies.
- There is no arbitrary wall-clock limit. Rerun only after a paper-grounded material blocker and a concrete causal source/config change.

## Required commands and per-task handoffs
```json
{pretty_json(task_commands)}
```

For each logical task, write only `task_agent_result.json` inside that task's output directory: task_id, status `ready_for_review`, one-sentence summary, differences, remaining_uncertainties, evidence_files, parameter_resolution with paper/derived/assumed sources, causal iteration_records, component_usage and execution_refs (the host run IDs and receipt paths). Put implementation and device choices in implementation_notes. The host supplies observed counts, exit codes, durations and hashes; do not transcribe them. No duplicate Markdown audit narrative is required. A PNG is optional when CSV/JSON/table evidence captures the conclusion.

Also write root `execution_unit_result.json`:
```json
{{
  "schema_version": "1.0",
  "execution_unit_id": "{unit_id}",
  "task_ids": {pretty_json([str(task.get('task_id') or entry.get('task_id') or '') for _, task, entry in members])},
  "commands": [],
  "artifact_lineage": [
    {{
      "artifact_id": "stable logical ID from execution_unit.json",
      "path": "relative/path/to/the/real/persisted/file",
      "producer_task_id": "logical producer task",
      "consumer_task_ids": ["logical consumers"]
    }}
  ]
}}
```
Every artifact declared by a strong relationship must be persisted under `{unit_asset_root}/` at a relative path and listed here, including shared random state or dataset partitions that have no single producer. This may be a checkpoint, split/index manifest, seed/state record, generated dataset, or other scientifically sufficient state. Do not write absolute case/audit paths. The host computes hashes and verifies producer/consumer lineage during packaging.

## Scientific architecture bindings
Read the complete bindings, unique components, tasks, experiments and explicitly referenced facts in `paper_evidence/writer_input.json`. Component/fact indexes are zero-based and refer to the shared arrays in that same file. Conflicting records are retained; inspect the original evidence instead of choosing by convenience.
Reuse bound shared components in the real computation. For trainable/checkpointed components, preserve the declared framework/device/precision/checkpoint semantics across every member task.

## Independent Reporter feedback from a previous unit delivery
```json
{pretty_json(review_feedback or {})}
```

Treat Reporter feedback as evidence to investigate. Each logical task retains its own acceptance goals; sharing an execution unit does not merge those scopes. One material defect in shared state that affects assigned goals may require a unit rerun and re-review of affected logical tasks. Out-of-scope observations never justify a unit rerun. Do not rerun for non-material presentation or numerical differences.

{WRITER_READING_PROTOCOL}

"""
    if not run_repro:
        start = prompt.index("For each logical task, write")
        end = prompt.index("## Scientific architecture bindings", start)
        prompt = prompt[:start] + "Full execution is disabled. Prepare the task modules, configs and producer/consumer artifact paths. Describe unresolved requirements in README.md; do not claim completed runs or write final scientific results.\n\n" + prompt[end:]
    return prompt

def _build_execution_unit_continuation_brief(
    *,
    base_prompt: str,
    unit_id: str,
    session_round: int,
    review_feedback: dict[str, Any],
    recovery_context: dict[str, Any] | None = None,
    run_repro: bool = True,
) -> str:
    if not run_repro:
        return (f"# Continue unit preparation `{unit_id}` (round {session_round})\n"
                + pretty_json(recovery_context or {"reason": "resume_preparation"})
                + "\nFull execution is disabled. Preserve the implementation and artifact plan; do not claim scientific completion.\n\n"
                + base_prompt)
    return f"""# Continue compound execution unit `{unit_id}` (round {session_round})

Current recovery reason and existing work:
```json
{pretty_json(recovery_context or {'reason': 'resume_existing_delivery'})}
```

Current independent feedback, if any (an interruption is not a rerun request):
```json
{pretty_json(review_feedback)}
```

Inspect the shared source, configs, persisted producer artifacts, and prior outputs. Make only concrete changes justified by a material paper conflict. Because this is one strong execution unit, rerun every affected producer and consumer in dependency order, refresh their per-task result notes, and update execution_unit_result.json lineage. Do not repeat unchanged work or tune presentation details.

Original unit brief:

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
    foundation_enabled: bool = False,
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
        'Inspect the available hardware yourself and choose CPU, CUDA, memory use, batch size, and parallelism appropriate to the task. '
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

- Consume the listed `module` / `callable` implementations in the real computation that produces the submitted CSV, summary, and figure. A task may import a declared component itself or import a shared Foundation composition entrypoint whose local `src/**/*.py` import graph reaches it.
- Every component with `execution.shared_implementation=true` must be reported as `in_scientific_path`. An audit-only call, shape check, reference comparison, or unused import does not count.
- Do not mirror or rewrite a shared trainable model under `tasks/`. Reuse its Foundation model/trainer/checkpoint path so all bound tasks execute the same implementation.
- Add `component_usage` to `task_agent_result.json`, with one exact entry per bound component as shown once in the final result example.
'''
        hardware_instruction = (
            'Follow each bound component execution.primary_framework and execution.device_policy exactly. '
            'Do not substitute Torch, CUDA, NumPy, CPU, or another framework/device heuristic for the architecture contract. '
            'Record evidence that expensive computation ran under the declared policy.'
        )
    ownership_instruction = (
        "Only source, tests, and configs listed in the installed Foundation manifest are frozen. "
        "Import and reuse them without editing, deleting, replacing, or import-shadowing them. "
        "You own components marked `ownership: execution_unit`, including their declared `src/` "
        "modules, and may add task-private helpers under `tasks/` or a unique unit namespace in `src/`."
        if foundation_enabled
        else "You may create or edit any task-private code, config, helper, dependency, and output needed for this task."
    )
    prompt = f"""# Role: autonomous Codex task writer

You own exactly one reproduction task. Write the code, run the assigned full experiment, compare the result directly with the complete paper, and keep revising and rerunning while a paper-grounded material scientific blocker remains. Your handoff is `ready_for_review`; only the independent reporter may grant final `matched`.

{WRITER_PAPER_FIDELITY_POLICY}

{CORE_RESULT_STOP_POLICY}

{LONG_RUNNING_FULL_RUN_PROTOCOL}

{FOUNDATION_REVISION_PROTOCOL if foundation_enabled else ''}

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
- Pass each reused checkpoint/data file as `--input RELATIVE_PATH`; persistent generated state needs a current producer receipt. Keep consumed inputs immutable; write newly trained checkpoints to a new path. Missing dependencies use the environment request channel.
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

- `task_agent_result.json` is the sole required handoff note. Keep all scientific evidence files. Do not write a duplicate Markdown narrative. The host supplies observed execution metadata; your statements remain self-reported.
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
        end = prompt.index("## Independent reporter feedback", start)
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
