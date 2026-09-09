import hashlib
import json
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

from geng_agent.agentic_report_editor import run_codex_report_editor_workflow, _build_report_editor_brief
from geng_agent.pipeline_report_delivery import generate_docx_reports
from tests.test_agentic_report_editor import _workflow_inputs, PNG_B64
import base64


def test_resume_restores_verified_images_and_keeps_editor_text(tmp_path):
    output = tmp_path / 'case'
    inputs = _workflow_inputs(output)
    workspace = output / 'audit/04a_task_reporters/01_task_1/round_001'
    manifest = []
    for name in ('local_result.png', 'paper_target.png'):
        relative = 'report_assets/task_1/' + name
        source = workspace / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(base64.b64decode(PNG_B64))
        manifest.append({'path': relative, 'sha256': hashlib.sha256(source.read_bytes()).hexdigest()})
        (output / relative).unlink()
    inputs['task_records'][0].update(index=1, task_reporter={
        'workspace': str(workspace), 'asset_manifest': manifest})
    inputs['paper'] = {'source_path': 'paper.pdf', 'chunks': [{'text': 'Original Paper Title'}]}
    inputs['task_records'][0]['result_json']['iteration_records'] = [{'reported_detail': 'writer statement'}]
    comparison = ('# 结果对比\n\n## 任务一\n\n核心事实与假设：固定功率。\n\n'
                  '| 本地结果 | 原文结果 |\n|---|---|\n'
                  '| ![本地图](report_assets/task_1/local_result.png) | ![原图](report_assets/task_1/paper_target.png) |\n\n'
                  '差距：本地SNR为−3.25 dB，原文为0 dB。\n\n人工核查建议：确认参考功率定义。\n')
    report_texts = {'review.md': '# 论文导航\n', 'reproduction_report.md': '# 本地复现报告\n\n参数与运行配置。\n',
                    'result_review.md': comparison}

    def editor(**kwargs):
        editor_root = kwargs['work_dir']
        material = json.loads((editor_root / 'inputs/report_editor_input.json').read_text(encoding='utf-8'))
        assert material['paper']['opening_text_for_title_only'] == 'Original Paper Title'
        assert material['technical_details']['writer_statements'][0]['reported']['iteration_records']
        assert 'iteration_records' not in material['task_packets'][0]
        assert len(kwargs['image_paths']) == 2
        for name, text in report_texts.items():
            (editor_root / name).write_text(text, encoding='utf-8')
        return {'ok': True, 'role': 'report_editor'}

    with patch('geng_agent.agentic_report_editor.run_codex_subprocess', side_effect=editor) as call:
        result = run_codex_report_editor_workflow(**inputs)
        assert result['ok'] and not result['asset_warnings']
        for name, text in report_texts.items():
            assert (output / name).read_text(encoding='utf-8') == text
        inputs['resume'] = True
        assert run_codex_report_editor_workflow(**inputs)['cached']
        assert call.call_count == 1
    word = generate_docx_reports(output_dir=output, result_review_result={'passed': True})
    assert word['result_review_docx']['passed']
    with ZipFile(output / 'result_review.docx') as doc:
        xml = doc.read('word/document.xml').decode('utf-8')
        assert '人工核查建议' in xml and '−3.25 dB' in xml
        assert '<w:drawing>' in xml
        assert '任务终态与核验记录' not in xml


def test_changed_reporter_image_is_not_republished(tmp_path):
    from geng_agent.report_editor_assets import restore_report_assets, _sanitize_task_packet_assets
    output = tmp_path / 'case'
    workspace = output / 'audit/04a_task_reporters/01_task_1/round_001'
    relative = 'report_assets/task_1/local.png'
    for root in (workspace, output):
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        (root / relative).write_bytes(b'changed image')
    manifest = [{'path': relative, 'sha256': hashlib.sha256(b'original image').hexdigest()}]
    record = {'index': 1, 'task_id': 'task_1', 'task_reporter': {'workspace': str(workspace), 'asset_manifest': manifest}}
    warnings = restore_report_assets(task_records=[record], output_dir=output, audit_dir=output / 'audit')
    assert warnings
    packet = {'task_id': 'task_1', 'local_assets': [relative], 'asset_manifest': manifest}
    assert _sanitize_task_packet_assets([packet], output / 'report_assets')
    assert packet['local_assets'] == []


def test_existing_editor_prompt_assigns_concise_comparison_and_detailed_reproduction():
    brief = _build_report_editor_brief(task_count=2)
    assert '# Role: final report editor' in brief
    for requirement in ('核心事实与假设', '本地结果与原文结果对比', '仍存在的差距', '下一步人工核查建议',
                        '不能只取列表第一张', '不复制逐任务大表', '未知保持未知'):
        assert requirement in brief
