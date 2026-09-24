from __future__ import annotations

import json
import os
import sys
import textwrap
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from geng_agent.agentic_analysis import run_codex_json_stage
from geng_agent.llm import LLMImage
from geng_agent.pipeline_helpers import _aggregate_validation_issues
from geng_agent.tasks_normalize import finalize_repro_tasks


def _command_for(script: Path) -> str:
    return f'"{sys.executable}" "{script}"'


def _write_analysis_script(temp: Path, body: str) -> str:
    script = temp / "fake_codex_analysis.py"
    capability_preamble = (
        "import sys\n"
        "if sys.argv[1:] == ['exec', '--help']:\n"
        "    print('--ephemeral')\n"
        "    raise SystemExit(0)\n"
    )
    script.write_text(capability_preamble + textwrap.dedent(body), encoding="utf-8")
    return _command_for(script)


def test_complete_owner_candidate_is_returned_without_scientific_repair(tmp_path, monkeypatch):
    from unittest.mock import Mock
    candidate = {"repro_tasks": [{"task_id": "t", "target": "scientific goal", "unknown_detail": {"all_samples": 300}}]}
    message = tmp_path / "message.json"
    message.write_text(json.dumps(candidate), encoding="utf-8")
    worker = Mock(return_value={"ok": True, "last_message_path": str(message)})
    monkeypatch.setattr("geng_agent.agentic_analysis.run_codex_subprocess", worker)
    parsed = run_codex_json_stage(prompt="scientific task", stage_label="tasks", schema_stage="repro_tasks",
        output_dir=tmp_path, audit_dir=tmp_path / "audit", max_attempts=8)
    assert parsed["repro_tasks"] == candidate["repro_tasks"]
    assert parsed["_meta"]["host_observations"] == []
    audit = json.loads((tmp_path / "audit" / "handoff_tasks_attempt_1.json").read_text(encoding="utf-8"))
    assert audit["content_validation_performed"] is False
    assert worker.call_count == 1
    assert "Trusted structural schema" not in worker.call_args.kwargs["prompt"]
