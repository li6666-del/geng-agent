"""Transport-probe checks, with no network or model calls."""
from __future__ import annotations

import io
import json
from pathlib import Path
import re
import sys

import pytest

from tools import probe_model_flow


@pytest.mark.parametrize(
    "file_ok,command_ok,source_changed,expected",
    [(True, True, False, 0), (True, False, False, 4),
     (False, True, False, 4), (True, True, True, 4)],
)
def test_probe_requires_execution_and_file_evidence(
    monkeypatch, tmp_path: Path, file_ok: bool, command_ok: bool,
    source_changed: bool, expected: int,
) -> None:
    config = tmp_path / "models.json"
    config.write_text(json.dumps({"schema_version": 1, "default": "test",
        "profiles": {"test": {"provider": "test", "model": "test-model",
            "base_url": "https://provider.invalid", "env_key": "PROBE_CREDENTIAL"}}}),
        encoding="utf-8")
    output = tmp_path / "probe"
    monkeypatch.setattr(sys, "argv", ["probe", "--config", str(config), "--output", str(output)])
    monkeypatch.setenv("PROBE_CREDENTIAL", "test-only-credential")
    monkeypatch.setenv("GENG_PYTHON", sys.executable)
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")

    def fake_response(request, **kwargs):
        payload = ({"data": [{"id": "test-model"}]} if request.full_url.endswith("/models")
                   else {"output": [{"content": [{"type": "output_text", "text": "CONNECTION_OK"}]}]})
        return io.BytesIO(json.dumps(payload).encode("utf-8"))

    def fake_worker(*, work_dir, audit_dir, **kwargs):
        source = work_dir / "execution_probe.py"
        program = source.read_text(encoding="utf-8")
        marker = re.search(r"EXECUTION_OK_[a-f0-9]+", program).group()
        if source_changed:
            source.write_text(program + "# changed\n", encoding="utf-8")
        if file_ok:
            (work_dir / "connection.json").write_text('{"connected":true}', encoding="utf-8")
        audit_dir.mkdir()
        transcript = audit_dir / "transcript.txt"
        event = {"type": "item.completed", "item": {"type": "command_execution",
            "exit_code": 0 if command_ok else 1, "aggregated_output": marker}}
        transcript.write_text("[]\n" + json.dumps(event), encoding="utf-8")
        return {"ok": True, "transcript": str(transcript)}

    monkeypatch.setattr(probe_model_flow.urllib.request, "urlopen", fake_response)
    monkeypatch.setattr("geng_agent.codex_runner.run_codex_subprocess", fake_worker)
    assert probe_model_flow.main() == expected
    record = json.loads((output / "probe_result.json").read_text(encoding="utf-8"))
    assert record["checks"]["codex"]["ok"] is (expected == 0)
    assert "test-only-credential" not in (output / "probe_result.json").read_text(encoding="utf-8")
