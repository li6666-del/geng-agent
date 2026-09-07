from __future__ import annotations

import gzip
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from tools.prompt_context_benchmark import run_one, tool_counts


def evidence_case():
    evidence = {"task": {"scientific_acceptance": {"metric": "BER"}},
                "facts": {"noise": "per complex symbol"},
                "paper_context": "The noise variance is per complex symbol.",
                "paper_source": {"relative_path": "paper_evidence/source/paper.txt"}}
    return {"case_id": "hidden_expected_case", "origin": "hidden_original_location",
            "expected": {"claim_status": "unsupported", "rerun_allowed": False},
            "files": {
                "inputs/task_report_input.json": json.dumps({"task": evidence["task"], "task_facts": evidence["facts"]}),
                "paper_evidence/source/paper.txt": "Original scientific source, including other regimes.",
                "paper_evidence/task/evidence.json": json.dumps(evidence),
                "paper_evidence/task/context.md": json.dumps(evidence),
                "paper_evidence/index.json": json.dumps({"tasks": [{
                    "task_evidence_json": "paper_evidence/task/evidence.json",
                    "task_context_markdown": "paper_evidence/task/context.md"}]}),
            }}


def test_context_ablation_keeps_science_and_labels_outside_worker(tmp_path):
    case = evidence_case()
    observed = []

    def read_only_worker(**kwargs):
        workspace = kwargs["work_dir"]
        assert kwargs["sandbox"] == "read-only"
        assert case["case_id"] not in str(workspace)
        payload = "\n".join(path.read_text(encoding="utf-8") for path in workspace.rglob("*") if path.is_file())
        assert case["origin"] not in payload
        assert '"expected"' not in payload
        assert kwargs["prompt"].startswith("Frozen scientific instructions\n")
        observed.append((workspace, payload))
        answer = tmp_path / f"answer_{len(observed)}.json"
        answer.write_text(json.dumps({"claim_status": "unsupported", "rerun_allowed": False}), encoding="utf-8")
        return {"last_message_path": str(answer), "duration_s": 1, "cost_event": {"usage": None}}

    with patch("tools.prompt_context_benchmark.run_codex_subprocess", side_effect=read_only_worker):
        full = run_one(tmp_path, case, "candidate", "Frozen scientific instructions")
        reduced = run_one(tmp_path, case, "candidate_deduplicated", "Frozen scientific instructions")
    for workspace, _ in observed:
        assert (workspace / "paper_evidence/source/paper.txt").read_text(encoding="utf-8") == case["files"]["paper_evidence/source/paper.txt"]
        assert (workspace / "inputs/task_report_input.json").read_text(encoding="utf-8") == case["files"]["inputs/task_report_input.json"]
    original = json.loads(case["files"]["paper_evidence/task/evidence.json"])
    deduplicated = json.loads((observed[1][0] / "paper_evidence/task/evidence.json").read_text(encoding="utf-8"))
    assert deduplicated["paper_context"] == original["paper_context"]
    assert deduplicated["paper_source"] == original["paper_source"]
    assert reduced["scientific_prompt_sha256"] == full["scientific_prompt_sha256"]
    # Navigation overhead may exceed the removed text in a tiny fixture;
    # lower context cost is a measured outcome, never a correctness invariant.
    assert reduced["usage"] is None  # An unreported token count is not zero.
    assert reduced["claim_correct"] and reduced["rerun_correct"]


def test_dataset_escape_is_rejected_before_model_call(tmp_path):
    case = evidence_case()
    case["files"] = {"../../../outside.txt": "must not be written"}
    with patch("tools.prompt_context_benchmark.run_codex_subprocess") as model:
        with pytest.raises(ValueError, match="escapes"):
            run_one(tmp_path, case, "candidate", "instructions")
    model.assert_not_called()
    assert not (tmp_path / "outside.txt").exists()


def test_tool_read_cost_counts_completed_items_once(tmp_path):
    path = tmp_path / "transcript.txt.gz"
    events = [
        {"type": "item.started", "item": {"id": "read1", "type": "command_execution"}},
        {"type": "item.completed", "item": {"id": "read1", "type": "command_execution", "aggregated_output": "12345"}},
        {"type": "item.completed", "item": {"id": "read2", "type": "mcp_tool_call", "result": "678"}},
        {"type": "item.completed", "item": {"id": "answer", "type": "agent_message", "text": "not a tool"}},
    ]
    with gzip.open(path, "wt", encoding="utf-8") as stream:
        stream.write("\n".join(json.dumps(event) for event in events))
    assert tool_counts({"full_transcript": str(path)}) == {
        "complete_transcript": True, "tool_calls": 2, "tool_output_characters": 8}
    assert tool_counts({})["tool_calls"] is None
