import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from geng_agent.agentic_task_writers import (
    _build_task_writer_brief,
    _collect_task_writer_delivery,
    _load_task_execution_binding,
    _merge_task_writer_deliveries,
    _run_one_task_writer,
    _write_minimal_shared_project_files,
)
from geng_agent.outputs import write_json
from geng_agent.task_writer_execution_binding import _task_execution_binding_from_architecture


def _architecture(version: str = '1.1') -> dict:
    return {
        'schema_version': version,
        'workflow_version': '2',
        'quantities': [],
        'components': [
            {
                'id': 'shared_model',
                'kind': 'model',
                'module': 'src/models/shared.py',
                'callable': 'SharedModel',
                'inputs': [],
                'outputs': [],
                'parameters': [],
                'depends_on': [],
                'execution': {
                    'execution_kind': 'train_and_infer',
                    'primary_framework': 'PyTorch',
                    'supporting_libraries': [],
                    'device_policy': 'accelerator_preferred',
                    'precision': 'float32',
                    'trainable': True,
                    'gradient_mode': 'required',
                    'checkpoint_policy': 'required',
                    'shared_implementation': True,
                    'required_capabilities': ['autograd'],
                    'rationale': 'one model must serve every bound task',
                },
            }
        ],
        'bindings': [
            {
                'task_id': 'fig_1',
                'experiment_id': 'exp_1',
                'consistency_group': 'group_1',
                'components': ['shared_model'],
                'allowed_overrides': [],
                'overrides': {},
                'outputs': [],
                'acceptance_bindings': [],
            }
        ],
    }


def _write_binding(sandbox: Path, version: str = '1.1') -> None:
    root = sandbox / 'paper_evidence' / 'analysis_artifacts'
    root.mkdir(parents=True, exist_ok=True)
    write_json(root / 'scientific_architecture.json', _architecture(version))


def test_unversioned_address_aliases_keep_task_ownership_and_original_document():
    architecture = _architecture()
    architecture.pop('schema_version')
    component = architecture['components'][0]
    component['component_id'] = component.pop('id')
    first = architecture['bindings'][0]
    first['component_ids'] = first.pop('components')
    architecture['bindings'].append({**first, 'task_id': 'fig_2', 'experiment_id': 'exp_2'})
    original = json.dumps(architecture, ensure_ascii=False)
    binding = _task_execution_binding_from_architecture(architecture, 'fig_1')
    assert binding['configuration_issues'] == []
    assert binding['experiment_ids'] == ['exp_1']
    assert binding['components'][0]['component_id'] == 'shared_model'
    assert binding['components'][0]['ownership'] == 'task'
    assert binding['components'][0]['execution'] == component['execution']
    assert json.dumps(architecture, ensure_ascii=False) == original


def _result(
    *,
    usage: str = 'in_scientific_path',
    evidence_files: list[str] | None = None,
) -> dict:
    return {
        'task_id': 'fig_1',
        'status': 'ready_for_review',
        'summary': 'done',
        'local_image_paths': ['outputs/fig_1/plot.png'],
        'execution_summary': {'full_run_count': 1, 'last_returncode': 0},
        'component_usage': [
            {
                'component_id': 'shared_model',
                'module': 'src/models/shared.py',
                'callable': 'SharedModel',
                'usage': usage,
                'evidence_files': (
                    list(evidence_files)
                    if evidence_files is not None
                    else ['tasks/fig_1.py:1']
                ),
            }
        ],
    }


def _brief(binding: dict | None) -> str:
    return _build_task_writer_brief(
        index=1,
        task={'task_id': 'fig_1', 'figure_or_claim': 'Fig. 1'},
        manifest_entry={'task_id': 'fig_1', 'module': 'fig_1', 'output_subdir': 'fig_1'},
        facts={'engineering_facts': []},
        experiment_index={'experiments': []},
        paper={'chunks': []},
        paper_context_json='',
        paper_thesis=None,
        run_repro=True,
        execution_binding=binding,
    )


class TaskWriterExecutionBindingTests(unittest.TestCase):
    def test_v11_prompt_lists_execution_contract_and_disables_legacy_torch_heuristic(self) -> None:
        with TemporaryDirectory() as temp:
            sandbox = Path(temp)
            _write_binding(sandbox)
            binding = _load_task_execution_binding(sandbox, 'fig_1')

        prompt = _brief(binding)

        self.assertIn('src/models/shared.py', prompt)
        self.assertIn('SharedModel', prompt)
        self.assertIn('primary_framework', prompt)
        self.assertIn('device_policy', prompt)
        self.assertIn('component_usage', prompt)
        final_template = prompt.split('## Required final files', 1)[1]
        usage_key = json.dumps('component_usage') + ':'
        self.assertIn(usage_key, final_template)
        self.assertIn('independent Reporter', prompt)
        self.assertIn('Follow each bound component execution.primary_framework', prompt)
        self.assertNotIn('prefer a real Torch CUDA implementation', prompt)














    def test_foundation_scaffold_removes_legacy_numpy_and_communication_stubs(self) -> None:
        with TemporaryDirectory() as temp:
            sandbox = Path(temp)
            (sandbox / 'src').mkdir(parents=True)
            (sandbox / 'tasks').mkdir()
            _write_minimal_shared_project_files(
                sandbox,
                {'task_id': 'fig_1'},
                {'task_id': 'fig_1', 'module': 'fig_1'},
                )

            self.assertTrue((sandbox / 'requirements.txt').exists())
            self.assertFalse((sandbox / 'src' / 'channel.py').exists())
            self.assertTrue((sandbox / 'config.json').is_file())
            self.assertTrue((sandbox / 'tasks' / 'fig_1.py').is_file())



if __name__ == '__main__':
    unittest.main()
