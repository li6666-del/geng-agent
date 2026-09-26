"""Load assigned scientific component metadata for Writer context."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .task_writer_files import _read_optional_json_object
from .task_writer_support import PAPER_EVIDENCE_DIR
from .task_components import task_component_ids
from .architecture_protocol import architecture_runtime_view


def _load_task_execution_binding(sandbox: Path, task_id: str) -> dict[str, Any] | None:
    '''Load the task-scoped scientific execution contract from its sandbox copy.'''

    architecture = _read_optional_json_object(
        sandbox
        / PAPER_EVIDENCE_DIR
        / 'analysis_artifacts'
        / 'scientific_architecture.json'
    )
    execution_plan = _read_optional_json_object(
        sandbox / PAPER_EVIDENCE_DIR / 'analysis_artifacts' / 'execution_plan.json'
    )
    return _task_execution_binding_from_architecture(architecture, task_id, execution_plan)

def _task_execution_binding_from_architecture(
    architecture: Any,
    task_id: str,
    execution_plan: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    '''Resolve explicit task addresses regardless of descriptive schema version.'''

    if not isinstance(architecture, dict):
        return None
    architecture = architecture_runtime_view(architecture)
    raw_components = architecture.get('components')
    components_by_id = {
        str(item.get('id')): item
        for item in raw_components
        if isinstance(item, dict) and str(item.get('id') or '')
    } if isinstance(raw_components, list) else {}
    raw_bindings = architecture.get('bindings')
    bindings = [
        item for item in raw_bindings
        if isinstance(item, dict) and str(item.get('task_id') or '') == str(task_id)
    ] if isinstance(raw_bindings, list) else []
    binding = bindings[0] if bindings else None
    component_map = task_component_ids(architecture)
    configuration_issues: list[str] = []
    bound_components: list[dict[str, Any]] = []
    raw_groups = architecture.get('consistency_groups')
    consistency_groups = [
        str(group.get('id') or '')
        for group in (raw_groups if isinstance(raw_groups, list) else [])
        if isinstance(group, dict)
        and str(group.get('id') or '')
        and str(task_id) in {
            str(item)
            for item in group.get('task_ids', [])
        }
    ]
    if not isinstance(binding, dict):
        configuration_issues.append(f'no scientific architecture binding exists for task {task_id}')
    else:
        component_ids: list[str] = []
        for item in bindings:
            if not isinstance(item.get('components'), list):
                configuration_issues.append('binding.components must be a list of component IDs')
                continue
            component_ids.extend(str(value) for value in item['components'])
        component_ids = list(dict.fromkeys([
            *component_ids,
            *component_map.get(str(task_id), []),
        ]))
        for raw_component_id in component_ids:
            component_id = str(raw_component_id or '')
            component = components_by_id.get(component_id)
            if not isinstance(component, dict):
                label = component_id or '<empty>'
                configuration_issues.append(f'binding refers to unknown component {label}')
                continue
            execution = component.get('execution')
            bound_components.append(
                {
                    'component_id': component_id,
                    'module': str(component.get('module') or ''),
                    'callable': str(component.get('callable') or ''),
                    'execution': dict(execution) if isinstance(execution, dict) else {},
                    'ownership': 'task',
                }
            )
    return {
        'schema_version': '1.1',
        'task_id': str(task_id),
        'experiment_id': str(binding.get('experiment_id') or '') if isinstance(binding, dict) else '',
        'experiment_ids': list(dict.fromkeys(str(item.get('experiment_id') or '') for item in bindings)),
        'bindings': [dict(item) for item in bindings],
        'consistency_group': str(binding.get('consistency_group') or '') if isinstance(binding, dict) else '',
        'consistency_groups': consistency_groups,
        'components': bound_components,
        'configuration_issues': configuration_issues,
    }
