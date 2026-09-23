from pathlib import Path

from geng_agent import agentic_foundation as foundation
from geng_agent.foundation_snapshot import validate_foundation_snapshot
from geng_agent.supervisor import RunSupervisor


def test_supervisor_can_accept_failed_auxiliary_test_without_faking_its_result(monkeypatch, tmp_path):
    sandbox = tmp_path / "writer"
    source = sandbox / "src" / "model.py"
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")
    # A missing declaration and a failed auxiliary test require context. Neither
    # changes the bytes actually delivered or becomes a host scientific verdict.
    monkeypatch.setattr(foundation, "_run_foundation_tests", lambda *_: {
        "passed": False, "returncode": 1, "stderr": "optional device fixture unavailable"})
    supervisor = RunSupervisor(tmp_path, tmp_path / "audit", {"task": "analytic scalar calculation"})
    observed = []

    def approve(_node, **request):
        observed.append(request)
        return {"action": "approve", "diagnosis": "辅助测试不影响当前解析计算；保留失败记录"}

    monkeypatch.setattr(supervisor, "_request", approve)
    result = supervisor.run_node("foundation", lambda: foundation._finalize_foundation_delivery(
        sandbox=sandbox, snapshot_dir=tmp_path / "snapshot", manifest_path=tmp_path / "manifest.json",
        audit_dir=tmp_path / "audit", scientific_architecture={"schema_version": "1.1",
            "components": [{"id": "scalar", "module": "src/planned.py"}]},
        required_modules={"src/planned.py"}, analysis_hash="a" * 64,
        environment_hash="b" * 64, input_hash="c" * 64, trusted_changed=[], case_runtime=None),
        evidence_roots={"writer": sandbox}, summarize=lambda value: value["manifest"])
    assert not observed  # A usable Foundation handoff does not require model approval.
    validation = result["manifest"]["validation"]
    assert validation["tests_passed"] is False
    assert validation["local_imports_resolve"] is None
    kinds = {item["kind"] for item in validation["observations"]}
    assert {"test_execution_not_passed", "declared_modules_missing", "handoff_unavailable"} <= kinds
    assert validate_foundation_snapshot(result["manifest"], Path(result["snapshot_dir"])) == []
    # Approval never overrides changed physical evidence.
    (Path(result["snapshot_dir"]) / "src/model.py").write_text("VALUE = 2\n", encoding="utf-8")
    assert validate_foundation_snapshot(result["manifest"], Path(result["snapshot_dir"]))


def test_host_does_not_infer_failure_from_python_spelling(monkeypatch, tmp_path):
    source = tmp_path / "src" / "model.py"
    source.parent.mkdir()
    source.write_text("import importlib\n# Diagnostic example: D:/paper/input.csv\n", encoding="utf-8")
    monkeypatch.setattr(foundation, "_run_foundation_tests", lambda *_: {"passed": True, "returncode": 0})
    issues, result = foundation._validate_foundation_delivery(
        sandbox=tmp_path, architecture={"components": [{"module": "src/model.py"}]}, trusted_changed=[])
    assert issues == []
    assert result["decision_authority"] == "supervisor"
    assert result["passed"] is True
    assert not any("forbidden" in str(item) for item in result["observations"])


def test_modified_host_runtime_still_prevents_execution(monkeypatch, tmp_path):
    def must_not_run(*_):
        raise AssertionError("modified trusted runtime must not execute")
    monkeypatch.setattr(foundation, "_run_foundation_tests", must_not_run)
    issues, result = foundation._validate_foundation_delivery(
        sandbox=tmp_path, architecture={}, trusted_changed=["src/_io.py"])
    assert issues and result["skipped"] is True
    assert result["reason"] == "host runtime integrity changed"
