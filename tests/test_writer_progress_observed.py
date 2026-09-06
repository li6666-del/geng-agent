from pathlib import Path

from geng_agent.outputs import write_json
from geng_agent.task_writer_state import _writer_progress_fingerprint, _rerun_evidence_fingerprint
from geng_agent.task_reporter_context import _task_reporter_input_hash
from geng_agent.task_writer_results import _task_writer_runtime_task_passed
from geng_agent.security import static_scan_repro_project
from geng_agent.task_scripts import write_task_scaffolding


def test_same_function_can_receive_two_real_corrections_without_rewording_request(tmp_path):
    (tmp_path / "tasks").mkdir()
    (tmp_path / "outputs" / "t").mkdir(parents=True)
    source = tmp_path / "tasks" / "t.py"
    source.write_text("def estimate(x):\n    return x + 1\n", encoding="utf-8")
    (tmp_path / "outputs" / "t" / "results.csv").write_text("x,y\n0,1\n", encoding="utf-8")
    evidence = {"rerun_reason": "core_conclusion_failed", "contract_item_ids": ["ordering"], "change_targets": ["tasks/t.py:estimate"]}
    first = _rerun_evidence_fingerprint(evidence, _writer_progress_fingerprint(tmp_path))
    source.write_text("def estimate(x):\n    # explain the first implementation\n    return x + 1\n", encoding="utf-8")
    assert first == _rerun_evidence_fingerprint(evidence, _writer_progress_fingerprint(tmp_path))
    source.write_text("def estimate(x):\n    return 2 * x + 1\n", encoding="utf-8")
    assert first != _rerun_evidence_fingerprint(evidence, _writer_progress_fingerprint(tmp_path))


def test_host_execution_verdict_participates_in_reporter_cache(tmp_path):
    paper = tmp_path / "paper.md"
    paper.write_text("A beats B", encoding="utf-8")
    record = {"task_id": "t", "sandbox": str(tmp_path), "host_execution": {"passed": True, "run_id": "one"}}
    def snapshot():
        return _task_reporter_input_hash(task={"task_id": "t"}, task_record=record,
            paper_path=paper, facts={}, experiment_index={}, paper_thesis=None, figure_candidates=[])
    before = snapshot()
    record["host_execution"] = {"passed": False, "run_id": "one", "issues": ["input changed"]}
    assert snapshot() != before
    record.update(writer_completed=True, task_writer_status="ready_for_review",
                  task_verification={"run_valid": True, "outcome": "reproduced"})
    assert _task_writer_runtime_task_passed(record) is False


def test_only_exact_host_launcher_is_exempt_from_static_scan(tmp_path):
    write_task_scaffolding(tmp_path, {"tasks": []})
    assert static_scan_repro_project(tmp_path) == []
    launcher = tmp_path / "run_task.py"
    launcher.write_text(launcher.read_text(encoding="utf-8") + "\nimport subprocess\nsubprocess.run(['bad'])\n", encoding="utf-8")
    assert static_scan_repro_project(tmp_path)
