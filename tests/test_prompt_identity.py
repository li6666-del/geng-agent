from __future__ import annotations

import gzip
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

from geng_agent.codex_runner import run_codex_subprocess
from geng_agent.prompt_identity import role_contract_identity, scientific_cache_value
from geng_agent.runtime_status import build_stage_cache_metadata


def test_stage_contract_distinguishes_science_but_not_transport_or_audit():
    args = dict(stage_label="facts", schema_stage="engineering_facts", policy_version="same")
    initial = build_stage_cache_metadata(prompt="Noise variance\r\nper complex sample.  \r\n",
        inputs={"facts": [1], "_meta": {"cache": {"old": 1}}}, **args)
    transported = build_stage_cache_metadata(prompt="Noise variance\nper complex sample.\n",
        inputs={"facts": [1], "_meta": {"cache": {"new": 2}, "analysis_attempt": 4}}, **args)
    changed = build_stage_cache_metadata(prompt="Noise variance\nper real component.\n",
        inputs={"facts": [1]}, **args)
    assert initial["fingerprint"] == transported["fingerprint"]
    assert initial["fingerprint"] != changed["fingerprint"]
    assert scientific_cache_value({"_meta": {"fact_gap_handoff": {"blocking": True}, "cache": {}}}) == {
        "_meta": {"fact_gap_handoff": {"blocking": True}}}


def test_role_cache_tracks_actual_instruction_attachment_and_model(tmp_path):
    image = tmp_path / "page.png"
    image.write_bytes(b"page one")
    with patch("geng_agent.codex_runner.get_config_value", return_value=None):
        a = role_contract_identity(role="task_reporter", prompt="Verify method", image_paths=[image])
        b = role_contract_identity(role="task_reporter", prompt="Verify only values", image_paths=[image])
        image.write_bytes(b"corrected page")
        c = role_contract_identity(role="task_reporter", prompt="Verify method", image_paths=[image])
    assert a != b and a != c
    with patch("geng_agent.config.get_config_value",
               side_effect=lambda name: "different-model" if name == "GENG_CODEX_MODEL" else None):
        d = role_contract_identity(role="task_reporter", prompt="Verify method", image_paths=[image])
    assert c["model"] != d["model"]


def test_transport_records_exact_stdin_and_untruncated_history(tmp_path):
    audit = tmp_path / "audit"
    image = tmp_path / "page.png"
    image.write_bytes(b"attachment")
    prompt = "Base task\r\nHost-appended execution protocol\n"
    transcript = "x" * 210_000 + '\n{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":3,"output_tokens":2}}\n'
    with patch("geng_agent.codex_runner.shutil.which", return_value="codex"), \
         patch("geng_agent.codex_runner._ephemeral_capability", return_value={"supported": True}), \
         patch("geng_agent.codex_runner.subprocess.run", return_value=subprocess.CompletedProcess([], 0, transcript, "")) as run:
        result = run_codex_subprocess(role="task_writer", work_dir=tmp_path, prompt=prompt,
            audit_dir=audit, label="writer", sandbox="workspace-write", image_paths=[image])
    assert run.call_args.kwargs["input"] == prompt
    assert (audit / "writer_brief.md").read_bytes() == prompt.encode()
    manifest = json.loads(Path(result["input_manifest"]).read_text(encoding="utf-8"))
    assert Path(manifest["prompt_path"]).read_bytes() == prompt.encode()
    assert manifest["images"][0]["bytes"] == len(b"attachment")
    assert result["transcript_tail_truncated"]
    with gzip.open(result["full_transcript"], "rt", encoding="utf-8") as stream:
        assert stream.read() == transcript
    assert result["cost_event"]["invocation_id"] == manifest["invocation_id"]
    assert result["cost_event"]["usage"]["cached_prompt_tokens"] == 3


def test_writer_cache_includes_host_appended_and_environment_contracts():
    import inspect
    from geng_agent.case_runtime_requests import environment_request_prompt
    from geng_agent.task_writer_runner import _run_task_writer_codex_session
    from geng_agent.writer_lineage import writer_policy_content_hashes

    initial = writer_policy_content_hashes()
    source = inspect.getsource
    for changed, key in ((_run_task_writer_codex_session, "host_execution_session"),
                         (environment_request_prompt, "environment_request_prompt")):
        with patch("geng_agent.writer_lineage.inspect.getsource", side_effect=lambda function: (
            source(function) + "\nAdditional actual execution constraint" if function is changed else source(function))):
            modified = writer_policy_content_hashes()
        assert initial[key] != modified[key]
        assert {name: value for name, value in initial.items() if name != key} == {
            name: value for name, value in modified.items() if name != key}
