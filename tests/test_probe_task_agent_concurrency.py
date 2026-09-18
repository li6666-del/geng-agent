"""The opt-in diagnostic itself is tested using a local executable fixture."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

from geng_agent.codex_runner import _clear_ephemeral_capability_cache
from geng_agent.model_config import CodexModelConfig
from tools import probe_task_agent_concurrency as probe


def _profile() -> CodexModelConfig:
    return CodexModelConfig(provider="offline", model="offline-probe-fixture", reasoning_effort="max",
                            base_url="http://127.0.0.1:1", env_key="GENG_PROBE_TEST_KEY", managed=True)


def test_probe_observes_four_local_cli_processes_without_calling_a_provider(tmp_path, monkeypatch):
    fixture = tmp_path / "offline_cli.py"
    fixture.write_text(textwrap.dedent('''
        import json, subprocess, sys
        if sys.argv[1:] == ["exec", "--help"]:
            print("offline fixture --ephemeral --ignore-user-config")
            raise SystemExit(0)
        sys.stdin.read()
        result = subprocess.run([sys.executable, "-B", "execution_probe.py"], capture_output=True, text=True)
        print(json.dumps({"type": "item.completed", "item": {
            "type": "command_execution", "command": "python execution_probe.py",
            "exit_code": result.returncode, "aggregated_output": result.stdout}}))
        raise SystemExit(result.returncode)
    '''), encoding="utf-8")
    monkeypatch.setenv("GENG_CODEX_CMD", f'"{sys.executable}" -B "{fixture}"')
    monkeypatch.setenv("GENG_PROBE_TEST_KEY", "offline-fixture-value")
    output = probe.prepare_output(tmp_path / "diagnostic")
    _clear_ephemeral_capability_cache()
    try:
        summary = probe.run_probe(_profile(), output)
    finally:
        _clear_ephemeral_capability_cache()
    assert summary["passed"], summary
    assert summary["peak"] == {"task_writer": 2, "task_reporter": 2, "total": 4}
    assert summary["common_overlap_s"] > 0
    assert len({item["execution"]["pid"] for item in summary["sessions"]}) == 4
    serialized = (output / "summary.json").read_text(encoding="utf-8")
    assert "offline-fixture-value" not in serialized
    assert "GENG_PROBE_TEST_KEY" not in serialized
    assert json.loads(serialized)["checks"] == summary["checks"]


@pytest.mark.parametrize("defect", ["mutated_source", "wrong_token", "no_command_execution"])
def test_probe_does_not_accept_status_ok_without_execution_evidence(tmp_path, monkeypatch, defect):
    config = _profile()

    def fake_transport(**kwargs):
        root = kwargs["work_dir"]
        result = subprocess.run([sys.executable, "-B", probe.SCRIPT_NAME], cwd=root,
                                capture_output=True, text=True, check=True)
        transcript = root / "local_fixture_transcript.jsonl"
        transcript.write_text(json.dumps({"type": "item.completed", "item": {
            "type": "command_execution" if defect != "no_command_execution" else "agent_message",
            "command": "python execution_probe.py", "exit_code": 0,
            "aggregated_output": result.stdout}}), encoding="utf-8")
        if defect == "mutated_source":
            (root / probe.SCRIPT_NAME).write_text("# changed after execution\n", encoding="utf-8")
        if defect == "wrong_token":
            observation = json.loads((root / probe.RESULT_NAME).read_text(encoding="utf-8"))
            observation["token"] = "wrong"
            (root / probe.RESULT_NAME).write_text(json.dumps(observation), encoding="utf-8")
        return {"ok": True, "model_config": config.identity(), "transcript": str(transcript)}

    monkeypatch.setattr(probe, "run_codex_subprocess", fake_transport)
    result = probe._run_session(tmp_path, 1, "task_writer", config)
    assert not result["ok"]
    expected_check = {"mutated_source": "script_unchanged", "wrong_token": "expected_result_token",
                      "no_command_execution": "successful_command_observed"}[defect]
    assert result["checks"][expected_check] is False


def test_probe_refuses_to_overwrite_nonempty_output(tmp_path):
    preserved = tmp_path / "old-result.txt"
    preserved.write_text("keep", encoding="utf-8")
    with pytest.raises(ValueError, match="not empty"):
        probe.prepare_output(tmp_path)
    assert preserved.read_text(encoding="utf-8") == "keep"


def test_probe_requires_intersection_of_all_sessions():
    assert probe._overlap_seconds([(0, 2), (1, 3), (2.1, 4), (2.2, 5)]) == 0
    assert probe._overlap_seconds([(0, 4), (1, 5), (2, 6), (3, 7)]) == 1
