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
    _task_execution_binding_issues,
    _write_minimal_shared_project_files,
)
from geng_agent.outputs import write_json


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
            }
        ],
    }


def _write_binding(sandbox: Path, version: str = '1.1') -> None:
    root = sandbox / 'paper_evidence' / 'analysis_artifacts'
    root.mkdir(parents=True, exist_ok=True)
    write_json(root / 'scientific_architecture.json', _architecture(version))


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
        foundation_enabled=True,
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
        self.assertIn('An audit-only call', prompt)
        self.assertIn('Follow each bound component execution.primary_framework', prompt)
        self.assertNotIn('prefer a real Torch CUDA implementation', prompt)

    def test_v10_keeps_legacy_prompt_and_has_no_binding_gate(self) -> None:
        with TemporaryDirectory() as temp:
            sandbox = Path(temp)
            _write_binding(sandbox, version='1.0')
            (sandbox / 'tasks').mkdir()
            (sandbox / 'tasks' / 'fig_1.py').write_text(
                'import torch.nn as nn\nclass Mirror(nn.Module):\n    pass\n',
                encoding='utf-8',
            )
            binding = _load_task_execution_binding(sandbox, 'fig_1')
            issues = _task_execution_binding_issues(
                sandbox=sandbox,
                task_id='fig_1',
                result_doc={},
            )

        self.assertIsNone(binding)
        self.assertEqual(issues, [])
        legacy_prompt = _brief(binding)
        self.assertIn('prefer a real Torch CUDA implementation', legacy_prompt)
        usage_key = json.dumps('component_usage') + ':'
        self.assertNotIn(usage_key, legacy_prompt.split('## Required final files', 1)[1])

    def test_composition_entrypoint_reaches_bound_component_through_relative_import(self) -> None:
        with TemporaryDirectory() as temp:
            sandbox = Path(temp)
            _write_binding(sandbox)
            (sandbox / 'tasks').mkdir()
            (sandbox / 'tasks' / 'fig_1.py').write_text(
                'from src.system import System\n\ndef run():\n    return System().run()\n\nif __name__ == "__main__":\n    run()\n',
                encoding='utf-8',
            )
            (sandbox / 'src' / 'models').mkdir(parents=True)
            (sandbox / 'src' / 'system.py').write_text(
                'from .models.shared import SharedModel\nclass System:\n    def run(self):\n        return SharedModel()\n',
                encoding='utf-8',
            )
            (sandbox / 'src' / 'models' / 'shared.py').write_text(
                'class SharedModel:\n    pass\n',
                encoding='utf-8',
            )

            issues = _task_execution_binding_issues(
                sandbox=sandbox,
                task_id='fig_1',
                result_doc=_result(),
            )

        self.assertEqual(issues, [])

    def test_task_private_framework_head_may_compose_with_shared_trainable_component(self) -> None:
        with TemporaryDirectory() as temp:
            sandbox = Path(temp)
            _write_binding(sandbox)
            (sandbox / 'tasks').mkdir()
            (sandbox / 'tasks' / 'fig_1.py').write_text(
                'from src.models.shared import SharedModel\n'
                'from src import _backend\n'
                'torch = _backend.torch()\n'
                'nn = torch.nn\n'
                'class Mirror(nn.Module):\n    pass\n'
                'def run():\n'
                '    return SharedModel(), Mirror()\n'
                'if __name__ == "__main__":\n'
                '    run()\n',
                encoding='utf-8',
            )
            (sandbox / 'src' / 'models').mkdir(parents=True)
            (sandbox / 'src' / 'models' / 'shared.py').write_text(
                'class SharedModel:\n    pass\n',
                encoding='utf-8',
            )

            issues = _task_execution_binding_issues(
                sandbox=sandbox,
                task_id='fig_1',
                result_doc=_result(),
            )

        self.assertEqual(issues, [])

    def test_orphan_task_helper_import_does_not_satisfy_shared_component_reachability(self) -> None:
        with TemporaryDirectory() as temp:
            sandbox = Path(temp)
            _write_binding(sandbox)
            (sandbox / 'tasks').mkdir()
            (sandbox / 'tasks' / 'fig_1.py').write_text(
                'def run():\n    return 1\n',
                encoding='utf-8',
            )
            (sandbox / 'tasks' / 'orphan.py').write_text(
                'from src.models.shared import SharedModel\n',
                encoding='utf-8',
            )
            (sandbox / 'src' / 'models').mkdir(parents=True)
            (sandbox / 'src' / 'models' / 'shared.py').write_text(
                'class SharedModel:\n    pass\n',
                encoding='utf-8',
            )

            issues = _task_execution_binding_issues(
                sandbox=sandbox,
                task_id='fig_1',
                result_doc=_result(),
            )

        self.assertTrue(
            any(
                'expected module src.models.shared is not reachable' in issue
                for issue in issues
            )
        )

    def test_manifest_entry_reaches_helper_and_foundation_composition(self) -> None:
        with TemporaryDirectory() as temp:
            sandbox = Path(temp)
            _write_binding(sandbox)
            (sandbox / 'tasks').mkdir()
            write_json(
                sandbox / 'tasks_manifest.json',
                {
                    'version': 1,
                    'tasks': [
                        {
                            'task_id': 'fig_1',
                            'module': 'assigned',
                            'script': 'tasks/assigned.py',
                        }
                    ],
                },
            )
            (sandbox / 'tasks' / 'assigned.py').write_text(
                'from tasks.helper import build\n\ndef run():\n    return build()\n\nif __name__ == "__main__":\n    run()\n',
                encoding='utf-8',
            )
            (sandbox / 'tasks' / 'helper.py').write_text(
                'from src.system import System\n\ndef build():\n    return System().run()\n',
                encoding='utf-8',
            )
            (sandbox / 'tasks' / 'fig_1.py').write_text(
                'raise RuntimeError("manifest entry must win over fallback")\n',
                encoding='utf-8',
            )
            (sandbox / 'src' / 'models').mkdir(parents=True)
            (sandbox / 'src' / 'system.py').write_text(
                'from .models.shared import SharedModel\n'
                'class System:\n'
                '    def run(self):\n'
                '        return SharedModel()\n',
                encoding='utf-8',
            )
            (sandbox / 'src' / 'models' / 'shared.py').write_text(
                'class SharedModel:\n    pass\n',
                encoding='utf-8',
            )

            issues = _task_execution_binding_issues(
                sandbox=sandbox,
                task_id='fig_1',
                result_doc=_result(evidence_files=['tasks/helper.py:3']),
            )

        self.assertEqual(issues, [])

    def test_component_usage_evidence_must_exist_and_be_in_task_import_closure(self) -> None:
        with TemporaryDirectory() as temp:
            sandbox = Path(temp)
            _write_binding(sandbox)
            (sandbox / 'tasks').mkdir()
            (sandbox / 'tasks' / 'fig_1.py').write_text(
                'from src.models.shared import SharedModel\n',
                encoding='utf-8',
            )
            (sandbox / 'tasks' / 'orphan.py').write_text(
                'VALUE = 1\n',
                encoding='utf-8',
            )
            (sandbox / 'src' / 'models').mkdir(parents=True)
            (sandbox / 'src' / 'models' / 'shared.py').write_text(
                'class SharedModel:\n    pass\n',
                encoding='utf-8',
            )

            missing_issues = _task_execution_binding_issues(
                sandbox=sandbox,
                task_id='fig_1',
                result_doc=_result(evidence_files=['tasks/missing.py:1']),
            )
            orphan_issues = _task_execution_binding_issues(
                sandbox=sandbox,
                task_id='fig_1',
                result_doc=_result(evidence_files=['tasks/orphan.py:1']),
            )

        self.assertTrue(any('must exist inside the sandbox' in issue for issue in missing_issues))
        self.assertTrue(any('not in the assigned task import closure' in issue for issue in orphan_issues))

    def test_import_only_does_not_prove_declared_callable_participation(self) -> None:
        with TemporaryDirectory() as temp:
            sandbox = Path(temp)
            _write_binding(sandbox)
            (sandbox / 'tasks').mkdir()
            (sandbox / 'tasks' / 'fig_1.py').write_text(
                'from src.models.shared import SharedModel\n'
                'DECLARED_MODEL = SharedModel\n'
                'def main():\n'
                '    return 1\n'
                'if __name__ == "__main__":\n'
                '    main()\n',
                encoding='utf-8',
            )
            (sandbox / 'src' / 'models').mkdir(parents=True)
            (sandbox / 'src' / 'models' / 'shared.py').write_text(
                'class SharedModel:\n    pass\n',
                encoding='utf-8',
            )

            issues = _task_execution_binding_issues(
                sandbox=sandbox,
                task_id='fig_1',
                result_doc=_result(),
            )

        self.assertTrue(
            any(
                'declared callable src.models.shared.SharedModel is imported but not called'
                in issue
                for issue in issues
            )
        )

    def test_dead_entry_helper_call_does_not_prove_scientific_participation(self) -> None:
        with TemporaryDirectory() as temp:
            sandbox = Path(temp)
            _write_binding(sandbox)
            (sandbox / 'tasks').mkdir()
            (sandbox / 'tasks' / 'fig_1.py').write_text(
                'from src.models.shared import SharedModel\n'
                'def fake():\n'
                '    return SharedModel()\n'
                'def main():\n'
                '    return 1\n'
                'if __name__ == "__main__":\n'
                '    main()\n',
                encoding='utf-8',
            )
            (sandbox / 'src' / 'models').mkdir(parents=True)
            (sandbox / 'src' / 'models' / 'shared.py').write_text(
                'class SharedModel:\n    pass\n',
                encoding='utf-8',
            )

            issues = _task_execution_binding_issues(
                sandbox=sandbox,
                task_id='fig_1',
                result_doc=_result(),
            )

        self.assertTrue(
            any(
                'declared callable src.models.shared.SharedModel is imported but not called'
                in issue
                for issue in issues
            )
        )

    def test_import_and_assignment_alias_call_declared_callable(self) -> None:
        with TemporaryDirectory() as temp:
            sandbox = Path(temp)
            _write_binding(sandbox)
            (sandbox / 'tasks').mkdir()
            (sandbox / 'tasks' / 'fig_1.py').write_text(
                'from src.models.shared import SharedModel as ModelFactory\n'
                'def run():\n'
                '    factory = ModelFactory\n'
                '    return factory()\n'
                'if __name__ == "__main__":\n'
                '    run()\n',
                encoding='utf-8',
            )
            (sandbox / 'src' / 'models').mkdir(parents=True)
            (sandbox / 'src' / 'models' / 'shared.py').write_text(
                'class SharedModel:\n    pass\n',
                encoding='utf-8',
            )

            issues = _task_execution_binding_issues(
                sandbox=sandbox,
                task_id='fig_1',
                result_doc=_result(),
            )

        self.assertEqual(issues, [])

    def test_shared_instance_fixture_call_is_accepted(self) -> None:
        with TemporaryDirectory() as temp:
            sandbox = Path(temp)
            _write_binding(sandbox)
            (sandbox / 'tasks').mkdir()
            (sandbox / 'tasks' / 'fig_1.py').write_text(
                'from src.models.shared import SharedModel\n'
                'MODEL_FIXTURE = SharedModel()\n'
                'def run(model=MODEL_FIXTURE):\n'
                '    return model(None)\n'
                'if __name__ == "__main__":\n'
                '    run()\n',
                encoding='utf-8',
            )
            (sandbox / 'src' / 'models').mkdir(parents=True)
            (sandbox / 'src' / 'models' / 'shared.py').write_text(
                'class SharedModel:\n'
                '    def __call__(self, value):\n'
                '        return value\n',
                encoding='utf-8',
            )

            issues = _task_execution_binding_issues(
                sandbox=sandbox,
                task_id='fig_1',
                result_doc=_result(),
            )

        self.assertEqual(issues, [])

    def test_unknown_framework_uses_import_and_evidence_gate_without_model_guessing(self) -> None:
        with TemporaryDirectory() as temp:
            sandbox = Path(temp)
            architecture = _architecture()
            architecture['components'][0]['execution']['primary_framework'] = 'custom_engine'
            root = sandbox / 'paper_evidence' / 'analysis_artifacts'
            root.mkdir(parents=True)
            write_json(root / 'scientific_architecture.json', architecture)
            (sandbox / 'tasks').mkdir()
            (sandbox / 'tasks' / 'fig_1.py').write_text(
                'from src.models.shared import SharedModel\n'
                'import torch.nn as nn\n'
                'class Adapter(nn.Module):\n    pass\n'
                'def run():\n'
                '    return SharedModel(), Adapter()\n'
                'if __name__ == "__main__":\n'
                '    run()\n',
                encoding='utf-8',
            )
            (sandbox / 'src' / 'models').mkdir(parents=True)
            (sandbox / 'src' / 'models' / 'shared.py').write_text(
                'class SharedModel:\n    pass\n',
                encoding='utf-8',
            )

            issues = _task_execution_binding_issues(
                sandbox=sandbox,
                task_id='fig_1',
                result_doc=_result(),
            )

        self.assertEqual(issues, [])

    def test_collection_reports_shared_component_bypassed(self) -> None:
        with TemporaryDirectory() as temp:
            sandbox = Path(temp)
            _write_binding(sandbox)
            (sandbox / 'tasks').mkdir()
            (sandbox / 'tasks' / 'fig_1.py').write_text('def run():\n    return 1\n', encoding='utf-8')
            write_json(sandbox / 'task_agent_result.json', _result(usage='reference_only'))

            record = _collect_task_writer_delivery(
                index=1,
                task={'task_id': 'fig_1'},
                manifest_entry={'task_id': 'fig_1', 'module': 'fig_1', 'output_subdir': 'fig_1'},
                sandbox=sandbox,
                writer_status={'ok': True},
            )

        self.assertFalse(record['writer_completed'])
        self.assertEqual(record['writer_error_kind'], 'shared_component_bypassed')
        self.assertTrue(any(item.startswith('shared_component_bypassed:') for item in record['delivery_blockers']))

    def test_binding_failure_reopens_same_writer_with_concrete_feedback(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            failed = {
                'task_id': 'fig_1',
                'writer_completed': False,
                'task_writer_status': 'failed',
                'writer_error_kind': 'shared_component_bypassed',
                'delivery_blockers': [
                    'shared_component_bypassed: shared_model is reference_only'
                ],
            }
            ready = {
                'task_id': 'fig_1',
                'writer_completed': True,
                'task_writer_status': 'ready_for_review',
                'writer_error_kind': None,
                'delivery_blockers': [],
            }
            with patch(
                'geng_agent.agentic_task_writers._prepare_task_writer_sandbox'
            ), patch(
                'geng_agent.agentic_task_writers._build_task_writer_brief',
                return_value='base',
            ), patch(
                'geng_agent.agentic_task_writers._run_task_writer_codex_session',
                return_value={'ok': True},
            ) as run_session, patch(
                'geng_agent.agentic_task_writers._restore_trusted_files'
            ), patch(
                'geng_agent.agentic_task_writers._collect_task_writer_delivery',
                side_effect=[failed, ready],
            ), patch(
                'geng_agent.agentic_task_writers._archive_nonterminal_writer_delivery'
            ) as archive:
                result = _run_one_task_writer(
                    index=1,
                    reuse_existing=False,
                    task={'task_id': 'fig_1'},
                    manifest_entry={
                        'task_id': 'fig_1',
                        'module': 'fig_1',
                        'output_subdir': 'fig_1',
                    },
                    facts={},
                    experiment_index={},
                    paper={},
                    paper_path=root / 'paper.pdf',
                    paper_context_json='',
                    paper_images=[],
                    paper_thesis=None,
                    analysis_snapshot_hash='snapshot',
                    analysis_artifacts={},
                    task_root=root / 'sandboxes',
                    audit_dir=root,
                    run_repro=True,
                )

        self.assertEqual(run_session.call_count, 2)
        self.assertEqual(archive.call_count, 1)
        self.assertIn('shared_component_bypassed', run_session.call_args_list[1].kwargs['prompt'])
        self.assertEqual(result['task_writer_status'], 'ready_for_review')

    def test_foundation_scaffold_removes_legacy_numpy_and_communication_stubs(self) -> None:
        with TemporaryDirectory() as temp:
            sandbox = Path(temp)
            (sandbox / 'src').mkdir(parents=True)
            (sandbox / 'tasks').mkdir()
            _write_minimal_shared_project_files(
                sandbox,
                {'task_id': 'fig_1'},
                {'task_id': 'fig_1', 'module': 'fig_1'},
                foundation_enabled=True,
            )

            self.assertFalse((sandbox / 'requirements.txt').exists())
            self.assertFalse((sandbox / 'src' / 'channel.py').exists())
            self.assertTrue((sandbox / 'config.json').is_file())
            self.assertTrue((sandbox / 'tasks' / 'fig_1.py').is_file())

    def test_foundation_merge_does_not_reinject_legacy_numpy_requirements(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            sandbox = root / 'sandbox'
            repro = root / 'repro'
            (sandbox / 'tasks').mkdir(parents=True)
            (sandbox / 'tasks' / 'fig_1.py').write_text('VALUE = 1\n', encoding='utf-8')
            (sandbox / 'requirements.txt').write_text('torch\n', encoding='utf-8')
            expected = {
                'README.md',
                'config.json',
                'config_smoke.json',
                'requirements.txt',
                'tasks/fig_1.py',
            }
            with patch(
                'geng_agent.agentic_task_writers.install_foundation_snapshot',
                return_value={'requirements.txt'},
            ):
                _merge_task_writer_deliveries(
                    repro_project_dir=repro,
                    task_manifest={'version': 1, 'tasks': []},
                    expected_paths=expected,
                    task_records=[
                        {
                            'task_id': 'fig_1',
                            'module': 'fig_1',
                            'output_subdir': 'fig_1',
                            'sandbox': str(sandbox),
                        }
                    ],
                    foundation={'manifest': {}},
                )

            requirements = (repro / 'requirements.txt').read_text(encoding='utf-8')
            self.assertEqual(requirements, 'torch\n')


if __name__ == '__main__':
    unittest.main()
