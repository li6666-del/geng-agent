"""Foundation Writer prompt construction and content-addressed resume cache."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .case_runtime import CaseRuntime, environment_request_prompt
from .foundation_architecture import (
    architecture_components as _architecture_components,
    architecture_requires_execution_contracts as _architecture_requires_execution_contracts,
)
from .foundation_snapshot import (
    FOUNDATION_CONTRACT_VERSION,
    path_is_foundation_link,
    validate_foundation_snapshot,
)
from .json_utils import pretty_json
from .scientific_architecture import foundation_module_paths


FOUNDATION_RESULT_STATUS = "ready_for_tasks"
FOUNDATION_LABEL = "03b_foundation_writer"
FOUNDATION_CORE_MODULES = {
    "src/channel.py",
    "src/modulation.py",
    "src/transmitter.py",
    "src/receiver.py",
    "src/metrics.py",
    "src/simulator.py",
    "src/simulation.py",
    "src/algorithms/__init__.py",
    "src/baselines/__init__.py",
}

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
    execution_contract_required = _architecture_requires_execution_contracts(architecture)
    result_template: dict[str, Any] = {
        "status": "ready_for_tasks",
        "summary": "one concise Chinese sentence",
        "tests_command": "python -m unittest discover -s tests -v",
        "tested_invariants": ["invariant ids"],
        "remaining_uncertainties": ["explicit unresolved items only"],
    }
    if execution_contract_required:
        result_template["execution_contracts"] = [
            {
                "component_id": item["component_id"],
                "module": item["module"],
                "callable": item["callable"],
                "execution": item["execution"],
            }
            for item in component_contracts
        ]
        capability_templates: list[dict[str, str]] = []
        for component in _architecture_components(architecture):
            execution = component.get("execution") if isinstance(component.get("execution"), dict) else {}
            capabilities = [
                str(capability)
                for capability in execution.get("required_capabilities", [])
                if str(capability).strip()
            ]
            if execution.get("trainable") is True:
                capabilities.append("training_step")
            if str(execution.get("gradient_mode") or "").strip().casefold() == "required":
                capabilities.append("gradient_flow")
            if str(execution.get("checkpoint_policy") or "").strip().casefold() == "required":
                capabilities.append("checkpoint_roundtrip")
            device_policy = str(execution.get("device_policy") or "").strip().casefold()
            if device_policy == "accelerator_required":
                capabilities.extend(["accelerator_availability", "tensor_device_placement"])
            elif device_policy == "external_runtime":
                capabilities.extend(["runtime_availability", "runtime_invocation"])
            for capability in dict.fromkeys(capabilities):
                capability_templates.append(
                    {
                        "component_id": str(component.get("id") or ""),
                        "module": str(component.get("module") or ""),
                        "callable": str(component.get("callable") or ""),
                        "capability": capability,
                        "test": "tests.test_component.ComponentTests.test_capability",
                        "status": "passed",
                    }
                )
        result_template["capability_tests"] = capability_templates
    execution_result_note = (
        """
6. Because this is scientific architecture schema 1.1 or newer, `foundation_result.json`
   must use the single complete template above. Keep one `execution_contracts`
   record for every component, copy its architecture `execution` object without
   weakening it, and replace every capability `test` placeholder with the real
   delivered test method. Every capability record must retain the component's
   exact `module` and `callable`, and its test must construct/call that public
   component while asserting the relevant state transition.
   A component with `trainable: true` needs evidence of a real parameter update.
   `gradient_mode: required` needs a gradient/back-propagation test, and
   `checkpoint_policy: required` needs a save/load round-trip test.
   `device_policy: accelerator_required` needs tests for accelerator availability
   and actual tensor placement on that accelerator. Every `test` reference must
   identify a discoverable `unittest.TestCase` method actually delivered under
   `tests/test*.py`. `status: passed` is metadata, not proof: the host reruns the
   complete delivered suite and freezes the Foundation only after a real zero-exit
   test outcome. Hard training/gradient/checkpoint/device claims are certified only
   for frameworks in the trusted probe registry; otherwise report
   `environment_extension_required` instead of copying PyTorch method names or
   claiming a pass.
"""
        if _architecture_requires_execution_contracts(architecture)
        else """
6. This is a legacy schema 1.0 architecture. `execution_contracts` and
   `capability_tests` are encouraged when useful but are not required for compatibility.
"""
    )
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

Build the shared scientific foundation for all reproduction tasks. The architecture contract is mandatory and already validated. You own shared source modules and contract tests only; you do not own any figure-specific task, experiment output, report, or runtime result.

## Implementation ownership
{scope_instruction}

## Mandatory inputs
- `paper_evidence/analysis_artifacts/scientific_architecture.json`
- all other finalized artifacts in `paper_evidence/analysis_artifacts/`
- copied source paper and rendered pages under `paper_evidence/`

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
- When no third-party framework is needed, the architecture must use
  `primary_framework: standard_library` or `primary_framework: project_local`;
  an algorithm or component name is not a Python package. Request architecture
  revision if this convention was not followed.
- For `device_policy: external_runtime` (for example MATLAB, Julia, or a custom
  binary), do not add the runtime name as a Python requirement and do not fake a
  Python import. The architecture must declare runtime-availability and invocation-
  interface capabilities, each backed by a delivered unittest. The host must
  resolve the real runtime executable and provide a registered trusted invocation
  adapter. If either is absent, report `environment_extension_required`. A constant
  availability function or identity callable is not evidence. The static validator
  will not launch untrusted external code, but the host will run every delivered
  unittest before freezing the Foundation.
- A NumPy-only, analytic, mock, placeholder, or otherwise non-trainable reference
  is not an implementation of a component that requires training, gradients, or
  checkpoints. Do not replace those requirements with a look-alike interface.
- If an architecture execution contract cannot be implemented in the allowed
  environment, stop and explicitly request an architecture revision. Do not
  downgrade the framework/capability and do not claim `ready_for_tasks`.

## Verification
1. Read the complete scientific architecture; implement each component and exposed binding output. Treat acceptance mappings only as output-routing hints.
2. Implement the required modules, using package `__init__.py` files where needed.
3. Add focused `unittest` tests under `tests/` for dimensions, units/normalization, deterministic seeds, component composition, and applicable cross-task interface invariants. Do not test the paper-result verdict.
4. Run `python -m unittest discover -s tests -v` and fix every failure.
5. Write `foundation_result.json` only after tests pass:
```json
{pretty_json(result_template)}
```
{execution_result_note}

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


def _load_cached_foundation_failure(
    *,
    validation_path: Path,
    expected_input_hash: str,
) -> list[dict[str, Any]] | None:
    """Reuse a failed validation during resume instead of regenerating it."""

    if not validation_path.is_file() or validation_path.is_symlink():
        return None
    try:
        document = json.loads(validation_path.read_text(encoding="utf-8-sig"))
    except Exception:
        return None
    if (
        not isinstance(document, dict)
        or document.get("ok") is not False
        or str(document.get("input_hash") or "") != expected_input_hash
    ):
        return None
    issues = document.get("issues")
    if not isinstance(issues, list) or not issues:
        return None
    normalized = [item for item in issues if isinstance(item, dict)]
    return normalized or None


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
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _required_foundation_modules(architecture: dict[str, Any]) -> set[str]:
    architecture_modules = foundation_module_paths(architecture)
    if isinstance(architecture.get("_foundation_scope"), dict) or _architecture_requires_execution_contracts(architecture):
        return architecture_modules
    return set(FOUNDATION_CORE_MODULES) | architecture_modules
