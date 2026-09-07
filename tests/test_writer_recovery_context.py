from __future__ import annotations

import json
import sys
import threading
import time
import pytest
from types import SimpleNamespace
from pathlib import Path

from geng_agent import agentic_foundation as foundation
from geng_agent.execution_receipts import ExecutionBroker, file_hash
from geng_agent.task_writer_prompts import (_build_task_writer_brief,
    _build_task_writer_continuation_brief, _task_experiment_index)
from geng_agent.writer_recovery import localize_writer_feedback, writer_recovery_context, archive_satisfied_environment_request
from geng_agent import execution_client


def test_foundation_repair_restores_implementation_and_actual_validation_failure(monkeypatch, tmp_path):
    audit, output = tmp_path / 'audit', tmp_path / 'output'
    audit.mkdir(); output.mkdir()
    delivery = {'trusted_changed': []}
    validation = {'ok': False, 'issues': [{'file': 'src/model.py', 'message': 'wrong channel normalization'}],
                  'tests': {'returncode': 1, 'stderr': 'test_unit_power: AssertionError'}}
    for name, value in {
        '_collect_writer_analysis_artifacts': {'scientific_architecture.json': tmp_path / 'architecture.json'},
        '_missing_required_analysis_artifacts': [], '_analysis_snapshot_hash': 'analysis',
        '_foundation_input_hash': 'input', '_load_cached_foundation': None,
        'load_foundation_writer_delivery': delivery, '_load_foundation_validation_record': validation,
        '_required_foundation_modules': {'src/model.py'}, 'persist_foundation_writer_delivery': delivery,
        '_finalize_foundation_delivery': {'repaired': True}, '_write_paper_evidence_bundle': None,
    }.items():
        monkeypatch.setattr(foundation, name, lambda *a, _value=value, **kw: _value)
    restored = []
    def restore(*, sandbox, **kw):
        (sandbox / 'src').mkdir(parents=True, exist_ok=True)
        (sandbox / 'src/model.py').write_text('existing_model = 123\n', encoding='utf8')
        (sandbox / 'requirements.txt').write_text('numpy\nmatplotlib\n', encoding='utf8')
        restored.append(sandbox)
    monkeypatch.setattr(foundation, 'restore_foundation_writer_delivery', restore)
    seen = []
    def worker(*, work_dir, prompt, **kw):
        assert (work_dir / 'src/model.py').read_text() == 'existing_model = 123\n'
        assert json.loads((work_dir / 'foundation_validation_feedback.json').read_text()) == validation
        assert 'wrong channel normalization' in prompt and 'test_unit_power: AssertionError' in prompt
        seen.append(prompt)
        return {'ok': True}
    monkeypatch.setattr(foundation, 'run_codex_subprocess', worker)
    result = foundation.run_codex_foundation_writer_workflow(facts={}, tasks={}, experiment_index={},
        scientific_architecture={}, paper={}, paper_path=tmp_path/'paper.pdf', paper_images=[],
        paper_thesis=None, output_dir=output, audit_dir=audit, resume=True)
    assert result == {'repaired': True} and len(seen) == 1 and restored


def test_feedback_references_reuse_identical_local_files_and_preserve_changed_reviewed_bytes(tmp_path):
    reporter, writer = tmp_path/'reporter', tmp_path/'writer'
    for root in (reporter, writer):
        (root/'paper_evidence').mkdir(parents=True)
        (root/'paper_evidence/paper.txt').write_text('paper equation', encoding='utf8')
    (reporter/'inputs/writer_output/source/tasks').mkdir(parents=True)
    (reporter/'inputs/writer_output/source/tasks/t.py').write_text('old()', encoding='utf8')
    (writer/'tasks').mkdir(); (writer/'tasks/t.py').write_text('new()', encoding='utf8')
    (reporter/'paper_evidence/crop.png').write_bytes(b'png evidence')
    original = {'evidence_files': ['inputs/writer_output/source/tasks/t.py'], 'rerun_evidence': {
        'paper_evidence_files': ['paper_evidence/paper.txt','paper_evidence/crop.png']}}
    result = localize_writer_feedback(original, reporter_root=reporter, sandbox=writer, output_subdir='t')
    assert result['rerun_evidence']['paper_evidence_files'][0] == 'paper_evidence/paper.txt'
    reviewed = writer/result['evidence_files'][0]
    assert reviewed.read_text() == 'old()' and (writer/'tasks/t.py').read_text() == 'new()'
    assert (writer/result['rerun_evidence']['paper_evidence_files'][1]).read_bytes() == b'png evidence'
    assert original['evidence_files'] == ['inputs/writer_output/source/tasks/t.py']
    assert result['writer_evidence_references']['inputs/writer_output/source/tasks/t.py']['sha256'] == file_hash(reviewed)


def test_feedback_never_copies_other_role_transcripts_or_traversal(tmp_path):
    reporter, writer = tmp_path/'reporter', tmp_path/'writer'
    reporter.mkdir(); writer.mkdir()
    (reporter/'private.txt').write_text('private transcript')
    result = localize_writer_feedback({'evidence_files':['private.txt','paper_evidence/../private.txt']},
        reporter_root=reporter, sandbox=writer, output_subdir='t')
    assert len(result['unresolved_review_references']) == 2
    assert not list(writer.rglob('private.txt'))


def test_experiment_selection_keeps_complete_late_task_and_related_task():
    index = {'experiments': [{'task_id': 'unrelated', 'science': 'x'*20000},
        {'task_id': 'consumer', 'science': 'late complete scientific statement'},
        {'task_id': 'producer', 'science': 'shared checkpoint semantics'}]}
    selected = _task_experiment_index(index, [{'task_id': 'consumer'}],
        {'relationships': [{'producer_task_id':'producer','consumer_task_id':'consumer'}]})
    assert selected['experiments'] == index['experiments'][1:]


def test_continuation_has_one_policy_and_current_feedback_and_real_recovery_reason(tmp_path):
    base = _build_task_writer_brief(index=1, task={'task_id':'t'}, manifest_entry={'module':'t'},
        facts={}, experiment_index={}, paper={'chunks':[]}, paper_context_json='', paper_thesis=None, run_repro=True)
    context = writer_recovery_context(tmp_path, reason='environment_or_runtime_refresh', task_ids=['t'])
    prompt = _build_task_writer_continuation_brief(base_prompt=base, task_id='t', module='t', session_round=2,
        review_feedback={'current_evidence':'unique causal statement'}, recovery_context=context)
    assert prompt.count('unique causal statement') == 1
    assert prompt.count('## Highest law:') == 1 and prompt.count('## Core-result stopping policy') == 1
    assert 'environment_or_runtime_refresh' in prompt


def test_preparation_prompt_does_not_require_full_or_final_scientific_result():
    prompt = _build_task_writer_brief(index=1, task={'task_id':'t'}, manifest_entry={'module':'t'},
        facts={}, experiment_index={}, paper={'chunks':[]}, paper_context_json='', paper_thesis=None, run_repro=False)
    assert '--mode full' not in prompt and 'Always write the handoff' not in prompt
    assert 'Full execution is disabled' in prompt and '## Preparation handoff' in prompt


def test_host_queue_reports_inflight_and_deduplicates_pending_client_request(tmp_path):
    root = tmp_path/'writer'; root.mkdir()
    (root/'tasks_manifest.json').write_text(json.dumps({'tasks':[{'task_id':'t','module':'t','output_subdir':'t'}]}))
    broker = ExecutionBroker(root,tmp_path/'audit',Path(sys.executable))
    entered, release = threading.Event(), threading.Event()
    calls = []
    def execute(request):
        calls.append(request)
        broker._set_status('t', {'state':'running','run_id':'one','stderr_log':'host/stderr.log'})
        entered.set()
        assert release.wait(5)
        return {'task_id':'t','run_id':'one','returncode':0,
                'inputs_stable':True,'source_hashes':{},'input_hashes':{}}
    broker.execute = execute
    with broker:
        (broker.queue/'first.request.json').write_text(json.dumps({'task_id':'t'}))
        assert entered.wait(5)
        status = json.loads((broker.queue/'status.json').read_text())['tasks']['t']
        assert status['state'] == 'running' and status['stderr_log'] == 'host/stderr.log'
        (broker.queue/'second.request.json').write_text(json.dumps({'task_id':'t'}))
        release.set()
        deadline=time.monotonic()+5
        while not (broker.queue/'second.result.json').exists() and time.monotonic()<deadline:
            time.sleep(.01)
        assert json.loads((broker.queue/'second.result.json').read_text())['run_id'] == 'one'
    assert len(calls) == 1


def test_client_submit_status_and_inflight_retry_do_not_duplicate_request(monkeypatch, tmp_path, capsys):
    (tmp_path/'tasks_manifest.json').write_text(json.dumps({'tasks':[{'task_id':'t','module':'t'}]}))
    queue=tmp_path/'.geng_execution'/'session123'; queue.mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(execution_client, '__file__', str(tmp_path/'run_task.py'))
    monkeypatch.setenv('GENG_EXECUTION_BROKER','session123')
    monkeypatch.setattr(sys,'argv',['run_task.py','--task','t','--submit'])
    assert execution_client.main() == 0
    assert json.loads(capsys.readouterr().out)['state'] == 'submitted'
    assert len(list(queue.glob('*.request.json'))) == 1
    (queue/'status.json').write_text(json.dumps({'tasks':{'t':{'state':'running','run_id':'one'}}}))
    assert execution_client.main() == 75
    assert json.loads(capsys.readouterr().out)['state'] == 'in_flight_conflict'
    assert len(list(queue.glob('*.request.json'))) == 1
    monkeypatch.setattr(sys,'argv',['run_task.py','--task','t','--status'])
    assert execution_client.main() == 0
    assert json.loads(capsys.readouterr().out)['state'] == 'running'


def test_preparation_continuation_never_reintroduces_full_execution():
    prompt = _build_task_writer_continuation_brief(base_prompt='prepared configs', task_id='t', module='t',
        session_round=2, run_repro=False, recovery_context={'reason':'resume_preparation'})
    assert '--mode full' not in prompt and 'ready_for_review' not in prompt
    assert 'Full execution is disabled' in prompt


@pytest.mark.parametrize('change', ['mode', 'config_path', 'config_content', 'input_checkpoint', 'source'])
def test_different_pending_science_never_receives_previous_success(tmp_path, change):
    root=tmp_path/'writer'; root.mkdir()
    (root/'tasks_manifest.json').write_text(json.dumps({'tasks':[{'task_id':'t','module':'t','output_subdir':'t'}]}))
    (root/'config.json').write_text('{"n": 1}')
    (root/'other.json').write_text('{"n": 2}')
    (root/'checkpoint1.bin').write_bytes(b'first')
    (root/'checkpoint2.bin').write_bytes(b'second')
    (root/'tasks').mkdir(); (root/'tasks/t.py').write_text('VALUE=1')
    broker=ExecutionBroker(root,tmp_path/'audit',Path(sys.executable))
    entered, release=threading.Event(), threading.Event()
    calls=[]
    def execute(request):
        calls.append(request)
        entered.set(); assert release.wait(5)
        return {'task_id':'t','run_id':str(len(calls)),'returncode':0}
    broker.execute=execute
    first={'task_id':'t','mode':'smoke','config':'config.json','inputs':['checkpoint1.bin']}
    second=dict(first)
    with broker:
        (broker.queue/'first.request.json').write_text(json.dumps(first))
        assert entered.wait(5)
        if change=='mode': second['mode']='full'
        elif change=='config_path': second['config']='other.json'
        elif change=='config_content': (root/'config.json').write_text('{"n": 3}')
        elif change=='input_checkpoint': second['inputs']=['checkpoint2.bin']
        elif change=='source': (root/'tasks/t.py').write_text('VALUE=2')
        (broker.queue/'second.request.json').write_text(json.dumps(second))
        release.set(); deadline=time.monotonic()+5
        while not (broker.queue/'second.result.json').exists() and time.monotonic()<deadline: time.sleep(.01)
        assert json.loads((broker.queue/'second.result.json').read_text())['run_id']=='2'
    assert len(calls)==2


def test_resolved_environment_request_is_archived_only_after_package_and_import_proof(tmp_path):
    request={'requirements':[{'requirement':'numpy>=1','import_names':['numpy'],'reason':'FFT'}]}
    path=tmp_path/'environment_request.json'; path.write_text(json.dumps(request))
    (tmp_path/'existing_model.py').write_text('MODEL=42')
    locked={'requirement':'numpy>=1','distribution':'numpy','applicable':True,'installed_version':'2.0',
        'version_satisfied':True,'imports_ok':True,'satisfied':True,'imports':{'numpy':{'ok':False}}}
    runtime=SimpleNamespace(environment_hash='new-lock',lock={'requirements':[locked]})
    assert archive_satisfied_environment_request(tmp_path,runtime) is None
    assert path.exists()
    locked['imports']['numpy']['ok']=True
    archived=archive_satisfied_environment_request(tmp_path,runtime)
    assert not path.exists()
    assert json.loads((tmp_path/archived).read_text()) == {'request':request,'resolved_environment_hash':'new-lock'}
    assert (tmp_path/'existing_model.py').read_text() == 'MODEL=42'


def test_platform_inapplicable_environment_request_is_archived_without_import(tmp_path):
    requirement = 'uvloop; platform_system == "Linux"'
    request = {'requirements': [{'requirement': requirement, 'import_names': ['uvloop'], 'reason': 'conditional acceleration'}]}
    path = tmp_path/'environment_request.json'; path.write_text(json.dumps(request))
    runtime = SimpleNamespace(environment_hash='windows-lock', lock={
        'interpreter': {'marker_environment': {'platform_system': 'Windows'}}, 'requirements': []})
    archived = archive_satisfied_environment_request(tmp_path, runtime)
    assert archived and not path.exists()
    record = json.loads((tmp_path/archived).read_text())
    assert record['request'] == request and len(record['not_applicable_requirements']) == 1
