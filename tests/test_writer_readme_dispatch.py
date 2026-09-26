"""Offline Writer dispatch through prompt, handoff, packaging and final ZIP.

Only paper/sandbox setup, the execution broker and external model boundaries are
replaced. No scientific experiment or paid model is run by these tests.
"""

from pathlib import Path
from unittest.mock import MagicMock
from uuid import uuid4
from zipfile import ZipFile

import pytest

from geng_agent import task_recovery, task_writer_runner
from geng_agent.outputs import write_json
from geng_agent.task_writer_packaging import _package_task_directories
from geng_agent.web.delivery import REPORTS, build_delivery


TASK_ID = "qfunc"
UNIT_ID = "unit_qfunc"


def _write(root: Path, relative: str, content: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.mark.parametrize("request_revision", [False, True], ids=["initial", "reporter_revision"])
def test_writer_dispatch_preserves_chinese_guide_through_final_zip(tmp_path, monkeypatch, request_revision):
    case = tmp_path / "case"
    audit = case / "audit"
    task_root = audit / "writers"
    audit.mkdir(parents=True)
    entry = {
        "task_id": TASK_ID,
        "module": TASK_ID,
        "script": f"tasks/{TASK_ID}.py",
        "output_subdir": TASK_ID,
        "config_full": f"configs/{TASK_ID}_config.json",
        "config_smoke": f"configs/{TASK_ID}_config_smoke.json",
    }
    task = {"task_id": TASK_ID, "figure_or_claim": "Q-function approximation error"}

    def prepare_sandbox(*, sandbox, **_kwargs):
        # The scientific paper setup is unrelated to this delivery regression.
        # Input packet writing and both prompt builders remain real.
        sandbox.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(task_writer_runner, "_prepare_task_writer_sandbox", prepare_sandbox)
    broker = MagicMock()
    broker.__enter__.return_value = broker
    broker.session_id = "offlinewriter"
    broker_factory = MagicMock(return_value=broker)
    monkeypatch.setattr(task_writer_runner, "ExecutionBroker", broker_factory)

    prompts = []
    authored_guides = []
    result_versions = []
    repair_marker = "correct_normalization_for_revision"

    def fake_model(**kwargs):
        # This is the external model boundary, after the real session appended
        # its execution instructions. Do not replace the prompt or its builders.
        assert kwargs["role"] == "task_writer"
        assert kwargs["sandbox"] == "workspace-write"
        assert kwargs["extra_env"]["GENG_EXECUTION_BROKER"] == "offlinewriter"
        prompt = kwargs["prompt"]
        for responsibility in (
            "delivery_readme.md",
            "请阅读实际最终代码、配置和结果后自己解释其用途",
            "逐个点名最终交付的文件",
            "坐标轴或关键符号",
            "代码/<相对路径>",
            "复现结果/<原相对路径>",
            "只阅读已有文件并修订文字",
            "代码/configs/qfunc_config.json",
            "代码/requirements.repro.txt",
            "Host-observed execution:",
        ):
            assert responsibility in prompt
        if prompts:
            assert repair_marker in prompt
            assert "Keep `delivery_readme.md` consistent with the latest delivered code and results" in prompt
        prompts.append(prompt)
        version = len(prompts)
        sandbox = Path(kwargs["work_dir"])
        _write(sandbox, f"tasks/{TASK_ID}.py", f"SCALE = {version}\n\ndef main(config_path=None):\n    return 0\n")
        _write(sandbox, "requirements.txt", "numpy\nmatplotlib\n")
        _write(sandbox, "config.json", "{}\n")
        _write(sandbox, "config_smoke.json", '{"smoke": true}\n')
        # Opaque precomputed fixture data substitutes for scientific execution.
        result = f"x,approximation_error\n1,{version / 100}\n".encode("utf-8")
        result_path = sandbox / f"outputs/{TASK_ID}/errors.csv"
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_bytes(result)
        result_versions.append(result)
        guide = (
            f"# Q 函数文件导读 · 第 {version} 版\r\n\r\n"
            "Q(x) 表示标准高斯尾概率，先看误差表。\r\n"
            "[代码](代码/tasks/qfunc.py)：组织指数近似与参考值的误差计算。\r\n"
            "[结果](复现结果/outputs/qfunc/errors.csv)：比较阈值 x 下的概率近似差，"
            "正值表示高估；这份导读不作科研通过判定。\r\n\r\n"
            "```powershell\r\ncd 代码\r\npython -m pip install -r requirements.txt\r\n"
            "python run_experiment.py config.json\r\n```\r\n"
        ).encode("utf-8")
        (sandbox / "delivery_readme.md").write_bytes(guide)
        authored_guides.append(guide)
        write_json(sandbox / f"outputs/{TASK_ID}/task_agent_result.json", {
            "task_id": TASK_ID,
            "status": "ready_for_review",
            "summary": f"Offline model handoff {version}; no science executed by this fixture",
            "local_image_paths": [],
            "delivery_files": [
                {"path": f"tasks/{TASK_ID}.py", "role": "code"},
                {"path": f"outputs/{TASK_ID}/errors.csv", "role": "result"},
            ],
        })
        return {"ok": True}

    monkeypatch.setattr(task_writer_runner, "run_codex_subprocess", fake_model)
    moderation = MagicMock(return_value={
        "action": "revise_writer", "decision_id": "offline-revision",
        "instructions": repair_marker,
    })
    monkeypatch.setattr(task_recovery, "request_moderation", moderation)
    reporter_rounds = []

    def reporter(_index, _task, record, round_no):
        reporter_rounds.append(round_no)
        assert record["result_json"]["summary"].startswith("Offline model handoff")
        verification = {
            "schema_version": "2.0", "task_id": TASK_ID,
            "outcome": "not_reproduced", "host_action": "complete",
            "rerun_reason": "none", "run_valid": True,
        }
        if request_revision and round_no == 1:
            verification.update({
                "host_action": "rerun_writer",
                "rerun_reason": "material_numeric_discrepancy",
                "rerun_evidence": {
                    "rerun_reason": "material_numeric_discrepancy",
                    "contract_item_ids": ["normalization"],
                    "paper_evidence_files": [],
                    "causal_change": repair_marker,
                    "change_targets": [f"tasks/{TASK_ID}.py"],
                    "predicted_effect": "correct the probability normalization",
                },
            })
        return {"ok": True, "task_id": TASK_ID, "workspace": record["sandbox"],
                "task_verification": verification}

    record = task_writer_runner._run_one_task_writer(
        index=1, execution_unit_id=UNIT_ID, reuse_existing=False,
        task=task, manifest_entry=entry, facts={}, experiment_index={},
        paper={"chunks": []}, paper_path=case / "paper.pdf", paper_context_json="",
        paper_images=[], paper_thesis=None, analysis_snapshot_hash="offline-snapshot",
        analysis_artifacts={}, task_root=task_root, audit_dir=audit, run_repro=True,
        task_review_callback=reporter,
    )
    expected_rounds = [1, 2] if request_revision else [1]
    assert reporter_rounds == expected_rounds
    assert len(prompts) == broker_factory.call_count == len(expected_rounds)
    assert moderation.call_count == int(request_revision)
    assert record["writer_session_count"] == len(expected_rounds)
    # Successful guide delivery must not turn a negative scientific verdict
    # into a positive one or request an additional model/scientific run.
    assert record["task_verification"]["outcome"] == "not_reproduced"
    assert record["task_verification"]["host_action"] == "complete"

    project = case / "repro_project"
    _package_task_directories(
        repro_project_dir=project, output_dir=case, audit_dir=audit,
        task_manifest={"version": 1, "tasks": [entry]}, task_records=[record],
        execution_plan={"execution_units": [
            {"unit_id": UNIT_ID, "task_ids": [TASK_ID], "mode": "singleton"},
        ]},
        case_runtime=None, analysis_snapshot_hash="offline-snapshot",
        environment_hash="", require_lineage=False,
    )
    packaged = project / "task_packages/t01_qfunc"
    assert (packaged / "delivery_readme.md").read_bytes() == authored_guides[-1]
    for name in REPORTS:
        (case / name).write_bytes(b"opaque report fixture")

    with ZipFile(build_delivery(case, str(uuid4()))) as archive:
        prefix = "复现交付包/复现任务/t01_qfunc"
        assert archive.read(f"{prefix}/readme.md") == authored_guides[-1]
        assert archive.read(f"{prefix}/复现结果/outputs/qfunc/errors.csv") == result_versions[-1]
        assert archive.read(f"{prefix}/代码/tasks/qfunc.py") == (packaged / "tasks/qfunc.py").read_bytes()
        assert not any("writer_progress" in name or name.endswith("delivery_readme.md")
                       or name.endswith("task_agent_result.json") for name in archive.namelist())
        if request_revision:
            assert authored_guides[0] != archive.read(f"{prefix}/readme.md")


def test_preparation_keeps_reader_guide_and_guide_edits_do_not_change_scientific_policy(monkeypatch):
    from geng_agent.task_writer_prompts import _build_task_writer_brief
    from geng_agent.writer_lineage import writer_policy_content_hashes

    def preparation_prompt():
        return _build_task_writer_brief(index=1, task={"task_id": TASK_ID},
            manifest_entry={"task_id": TASK_ID, "module": TASK_ID, "output_subdir": TASK_ID},
            facts={}, experiment_index={}, paper={}, paper_context_json="", paper_thesis=None,
            run_repro=False)

    before = writer_policy_content_hashes()
    original = preparation_prompt()
    assert "Full execution is disabled" in original
    assert "## Final download file guide" in original
    assert "若尚未执行实验或没有可交付结果，应如实说明" in original
    assert "代码/configs/qfunc_config.json" in original
    read_text = Path.read_text
    marker = "本次仅调整读者说明，不改变科学实验契约。"

    def edited_guide(path, *args, **kwargs):
        text = read_text(path, *args, **kwargs)
        return text + "\n" + marker if path.name == "task_delivery_readme.md" else text

    monkeypatch.setattr(Path, "read_text", edited_guide)
    assert marker in preparation_prompt()
    assert writer_policy_content_hashes() == before
