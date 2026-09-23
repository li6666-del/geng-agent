"""Foundation Writer prompt construction and content-addressed resume cache."""

from __future__ import annotations

import hashlib
import inspect
import json
from pathlib import Path
from typing import Any

from .case_runtime import CaseRuntime, environment_request_prompt
from .foundation_architecture import (
    architecture_components as _architecture_components,
)
from .foundation_snapshot import (
    FOUNDATION_CONTRACT_VERSION,
    path_is_foundation_link,
    validate_foundation_snapshot,
)
from .json_utils import pretty_json
from .scientific_architecture import foundation_module_paths
from .prompt_identity import model_identity, text_identity


FOUNDATION_RESULT_STATUS = "ready_for_tasks"
FOUNDATION_LABEL = "03b_foundation_writer"

def _foundation_brief(
    architecture: dict[str, Any],
    *,
    case_runtime: CaseRuntime | None = None,
) -> str:
    modules = sorted(_required_foundation_modules(architecture))
    component_contracts = [
        {
            "component_id": str(component.get("id") or ""),
            "kind": str(component.get("kind") or ""),
            "module": str(component.get("module") or ""),
            "callable": str(component.get("callable") or ""),
            "execution": component.get("execution") if isinstance(component.get("execution"), dict) else {},
        }
        for component in _architecture_components(architecture)
    ]
    acceptance_output_contracts: list[dict[str, Any]] = []
    raw_bindings = architecture.get("bindings")
    for binding in raw_bindings if isinstance(raw_bindings, list) else []:
        if not isinstance(binding, dict):
            continue
        binding_outputs = {
            str(output_id)
            for output_id in binding.get("outputs", [])
        } if isinstance(binding.get("outputs"), list) else set()
        raw_acceptance = binding.get("acceptance_bindings")
        for acceptance in raw_acceptance if isinstance(raw_acceptance, list) else []:
            if not isinstance(acceptance, dict):
                continue
            criterion_id = str(acceptance.get("criterion_id") or "")
            output_ids = [
                str(output_id)
                for output_id in acceptance.get("output_quantity_ids", [])
                if str(output_id) in binding_outputs
            ] if isinstance(acceptance.get("output_quantity_ids"), list) else []
            if not criterion_id or not output_ids:
                continue
            acceptance_output_contracts.append(
                {
                    "task_id": str(binding.get("task_id") or ""),
                    "criterion_id": criterion_id,
                    "criterion_kind": str(acceptance.get("criterion_kind") or ""),
                    "output_quantity_ids": output_ids,
                }
            )
    result_template = {
        "status": "ready_for_tasks",
        "summary": "简述已实现的共享功能",
        "test_evidence": ["实际测试命令、结果和相关文件；未运行或失败也如实记录"],
        "remaining_uncertainties": ["需要主持人判断的缺口、失败或任务设计矛盾"],
    }
    environment_policy = (
        environment_request_prompt(case_runtime)
        if case_runtime is not None
        else "Use the host Python runtime; never install packages from inside the writer."
    )
    scope = architecture.get("_foundation_scope")
    scope_instruction = (
        "The host has limited this generation to the cross-execution-unit component "
        "dependency closure shown below. The complete copied architecture and all its "
        "task/experiment bindings remain context; only the listed component modules "
        "are yours to implement. Task-private components remain Writer-owned, including "
        "their src/ modules. Do not generate them here. Training code may be shared, "
        "but learned checkpoints, dataset splits, and sampled state belong to the "
        "execution plan's explicit producer/consumer flow; do not produce them as "
        "Foundation artifacts.\n" + pretty_json(scope)
        if isinstance(scope, dict)
        else "Implement only the shared scientific modules listed in this brief."
    )
    return f"""# Role: Foundation Writer

Build the shared scientific foundation for all reproduction tasks. Use the approved architecture and original paper as the implementation brief. Report conflicts to the supervisor; do not silently weaken the task. You own shared source modules and contract tests only; you do not own any figure-specific task, experiment output, report, or runtime result.

## Implementation ownership
{scope_instruction}

## Reading scope
- Start with `paper_evidence/analysis_artifacts/foundation_context.json`: the shared component contract, quantities, invariants and consuming interfaces.
- Follow its evidence references into the canonical facts and original source/pages under `paper_evidence/` as needed to implement those components faithfully.
- The complete architecture and finalized artifacts remain available for resolving an actual ambiguity. Do not repeatedly read all task plans or unrelated private components; read the needed section once and refer back to its path.
- The explicit component interfaces below are an implementation checklist, not a request for another full architecture analysis.

## Required modules
Create every module below and implement the interfaces assigned by the architecture:
```json
{pretty_json(modules)}
```

## Per-component implementation contract
The architecture designer, not the Foundation Writer, has already selected the
technical stack for each component. Follow each `module`, `callable`, and
`execution` object below. Components may intentionally use different frameworks;
a mixed-framework Foundation is valid and must not be flattened into one preferred
stack.
```json
{pretty_json(component_contracts)}
```

## Measurable acceptance output interfaces
The optional mappings below are routing hints from task criterion IDs to shared
quantities. Implement the listed quantity interfaces so Task Writers can measure
them. Do not decide whether a paper conclusion is supported, compute an acceptance
verdict, or restate the task's scientific contract. Unknown or absent mappings do
not create Foundation work.
```json
{pretty_json(acceptance_output_contracts)}
```
## Ownership and safety
- You may create/edit `src/**/*.py` except `src/_io.py` and `src/_backend.py`.
- You may create `tests/**/*.py`, `configs/foundation*.json|yaml`, `requirements.txt`, and `README.foundation.md`.
- Do not create or edit `tasks/`, `outputs/`, reports, task configs, or harness/runtime files.
- Foundation tests verify interfaces, shapes, units, execution capabilities, and reusable scientific mechanics only. Never add tests for paper-claim success, paper-value closeness, plot styling, crop geometry, or pixel similarity; those are downstream observations, not Foundation invariants.
- Do not duplicate paper-explicit channel, normalization, metric, baseline, or shape logic inside separate modules. Implement one shared definition and expose a clear callable interface.
- Keep unresolved paper details explicit in arguments/defaults and comments. Never hard-code target curves or fabricate paper values.
- Declare every real Python dependency in `requirements.txt`; package names are not restricted by a static whitelist.
- {environment_policy}
- Import and use every external `primary_framework` selected by the architecture,
  and declare it in `requirements.txt` or the architecture dependency metadata.
- Framework names describe implementation choices, not Python import requirements. A
  project-local or standard-library implementation does not require another package.
- If a task genuinely needs an external executable or GPU, use the available host
  execution interface and report the actual capability result. Do not substitute a
  constant availability function, fake import, or identity callable for real execution.
- A NumPy-only, analytic, mock, placeholder, or otherwise non-trainable reference
  is not an implementation of a component that requires training, gradients, or
  checkpoints. Do not replace those requirements with a look-alike interface.
- If an architecture execution contract cannot be implemented in the allowed
  environment, stop and explicitly request an architecture revision. Do not
  downgrade the framework/capability and do not claim `ready_for_tasks`.

## Verification
1. Read the scoped Foundation context and relevant paper evidence; implement each owned component and exposed binding output. Treat acceptance mappings only as output-routing hints.
2. Implement the required modules, using package `__init__.py` files where needed.
3. Add focused `unittest` tests under `tests/` for dimensions, units/normalization, deterministic seeds, component composition, and applicable cross-task interface invariants. Do not test the paper-result verdict.
4. Run focused tests and record actual results. Explain any failure or unavailable capability, including its effect on the task. Repeat tests only after a relevant change. The host records its guarded test run and exact file identities; the supervisor decides whether the handoff is sufficient.
5. Write `foundation_result.json` with an honest handoff, including incomplete work:
```json
{pretty_json(result_template)}
```

Do not implement any `tasks/<figure>.py`. Parallel task writers will consume this foundation as a frozen, read-only dependency.
"""

def _load_cached_foundation(
    *,
    manifest_path: Path,
    snapshot_dir: Path,
    expected_input_hash: str,
    expected_required_modules: set[str] | None = None,
) -> dict[str, Any] | None:
    try:
        if path_is_foundation_link(manifest_path) or path_is_foundation_link(manifest_path.parent):
            return None
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except Exception:
        return None
    if validate_foundation_snapshot(
        manifest,
        snapshot_dir,
        expected_input_hash=expected_input_hash,
        expected_required_modules=expected_required_modules,
    ):
        return None
    return {
        "manifest": manifest,
        "manifest_path": str(manifest_path),
        "snapshot_dir": str(snapshot_dir),
        "snapshot_hash": str(manifest.get("snapshot_hash") or ""),
    }



def _foundation_input_hash(
    analysis_hash: str,
    architecture: dict[str, Any],
    *,
    environment_hash: str = "host-runtime",
) -> str:
    payload = {
        "analysis_snapshot_hash": analysis_hash,
        "contract_version": FOUNDATION_CONTRACT_VERSION,
        "architecture": architecture,
        "environment_lock_hash": environment_hash,
        "role": "foundation_writer",
        "model_identity": model_identity("foundation_writer"),
        "prompt_contract": text_identity(inspect.getsource(_foundation_brief)),
        "environment_request_contract": text_identity(inspect.getsource(environment_request_prompt)),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _required_foundation_modules(architecture: dict[str, Any]) -> set[str]:
    """Only the planned shared modules create work, regardless of schema label."""
    return foundation_module_paths(architecture)
