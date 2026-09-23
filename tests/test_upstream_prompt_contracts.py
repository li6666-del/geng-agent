from __future__ import annotations

import copy
import gzip
import io
import json
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

import pytest

from geng_agent.agentic_analysis import run_codex_json_stage
from geng_agent.analysis_prompt_context import (
    analysis_paper_context, ledger_for_prompt, resolution_for_prompt,
    scientific_prompt_value, tasks_for_backfill,
)
from geng_agent.llm import LLMImage, OpenAICompatibleClient
from geng_agent.pipeline import ReviewPipeline
from tests.test_scientific_architecture import _inputs


IMAGE = LLMImage(label="paper_page:1", mime_type="image/png", data_b64="QQ==")


class SequenceClient:
    def __init__(self, values):
        self.values = iter(values)
        self.calls = []

    def complete(self, prompt, *, system=None, response_format=None):
        self.calls.append((prompt, []))
        return next(self.values)

    def complete_multimodal(self, prompt, *, images, system=None, response_format=None):
        self.calls.append((prompt, images))
        return next(self.values)


def run_candidates(tmp_path, backend, candidates, **kwargs):
    prompts = []
    values = iter(json.dumps(item, ensure_ascii=False) for item in candidates)
    if backend == "llm":
        client = SequenceClient(values)
        result = ReviewPipeline(client=client)._call_validated_json(
            audit_dir=tmp_path, **kwargs,
        )
        return result, client.calls

    def fake_codex(**call):
        prompts.append((call["prompt"], call["image_paths"]))
        output = tmp_path / f"candidate_{len(prompts)}.json"
        output.write_text(next(values), encoding="utf-8")
        return {"ok": True, "last_message_path": str(output)}

    with patch("geng_agent.agentic_analysis.run_codex_subprocess", side_effect=fake_codex):
        result = run_codex_json_stage(output_dir=tmp_path, audit_dir=tmp_path, **kwargs)
    return result, prompts












def test_prompt_projections_preserve_science_and_remove_only_proven_duplicates():
    item = {"request_id": "r1", "field_results": [{"field_id": "variance", "status": "not_found_in_paper", "note": "Looked at Eq. 2"}]}
    resolution = {"resolved": [], "terminal_unresolved": [item], "open": [], "unresolved": [item]}
    row = {"request_id": "r1", "status": "open", "searched_locations": ["Eq. 2"]}
    previous = {**row, "searched_locations": ["Appendix A"]}
    ledger = {"entries": [previous, row], "latest": [row], "round_count": 2}
    original = copy.deepcopy((resolution, ledger))
    reduced = resolution_for_prompt(resolution)
    assert reduced["terminal_unresolved"] == [item]
    assert "unresolved" not in reduced
    reduced_ledger = ledger_for_prompt(ledger)
    assert reduced_ledger["latest"] == [row]
    assert reduced_ledger["previous_search_results"] == [previous]
    assert (resolution, ledger) == original
    scientific = {"normalization": [1, 1], "_meta": {"cache": {"hash": "old"}, "fallback_reason": "No original checkpoint", "fact_gap_handoff": {"terminal_unresolved": [item], "assumption_diagnostics": ["unit unknown"]}}}
    projection = scientific_prompt_value(scientific, resolution_supplied=True)
    assert projection["normalization"] == [1, 1]
    assert projection["_meta"]["fallback_reason"] == "No original checkpoint"
    assert projection["_meta"]["fact_gap_handoff"]["assumption_diagnostics"] == ["unit unknown"]
    assert "cache" not in projection["_meta"]
    assert scientific["_meta"]["cache"] == {"hash": "old"}


def test_backfill_uses_requested_task_science_without_mutating_other_tasks():
    tasks = {"repro_tasks": [{"task_id": "a", "formula_chain": [{"value": "variance/2"}]}, {"task_id": "b", "formula_chain": [{"value": "variance"}]}]}
    projected = tasks_for_backfill(tasks, [{"task_ids": ["a"]}])
    assert projected["repro_tasks"] == [tasks["repro_tasks"][0]]
    assert len(tasks["repro_tasks"]) == 2


def context_document(value):
    return json.loads(value.split("\n", 1)[1].rsplit("\n", 1)[0])


def test_targeted_context_recovers_appendix_omitted_by_initial_budget(tmp_path):
    paper = {"chunks": [
        {"chunk_id": "c1", "page": 1, "text": "simulation experiment result figure table baseline parameter snr channel " + "x" * 170},
        {"chunk_id": "c2", "page": 2, "text": "Appendix Z: complex quadrature variance is sigma squared divided by two."},
    ]}
    kwargs = dict(paper=paper, figure_summary={}, paper_path=tmp_path / "paper.pdf", chunks_path=tmp_path / "paper_chunks.json", images=[], backend="llm", max_chars=250)
    first = context_document(analysis_paper_context(**kwargs))
    assert first["evidence_inventory"]["omitted_loaded_chunk_ids"] == ["c2"]
    targeted = context_document(analysis_paper_context(**kwargs, requests=[{"name": "quadrature variance", "search_targets": ["Appendix Z"], "required_fields": []}]))
    assert "c2" in targeted["evidence_inventory"]["supplied_chunk_ids"]
    assert not targeted["evidence_inventory"]["local_sources_readable_by_worker"]
    assert targeted["evidence_inventory"]["supplied_image_labels"] == []
    assert targeted["paper_chunks"][0]["text"].startswith("Appendix Z")


def test_multimodal_format_fallback_preserves_exact_images_and_audits_each_http_body(tmp_path):
    client = OpenAICompatibleClient(api_key="secret-never-log", base_url="https://example.invalid/v1", model="fixture")
    calls = []

    def fake_urlopen(request, **kwargs):
        payload = json.loads(request.data)
        calls.append(payload)
        if len(calls) == 1:
            raise HTTPError(request.full_url, 400, "Bad Request", {}, io.BytesIO(b'json_schema response_format unsupported'))
        return io.BytesIO(json.dumps({"choices": [{"message": {"content": '{"ok":true}'}}]}).encode())

    with patch("geng_agent.llm.urllib.request.urlopen", side_effect=fake_urlopen), client.audit_requests(tmp_path, "analysis"):
        assert client.complete_multimodal("prompt", images=[IMAGE], system="system evidence rules", response_format={"type": "json_schema"}) == '{"ok":true}'
    assert len(calls) == 2
    assert calls[0]["messages"] == calls[1]["messages"]
    assert calls[1]["response_format"] == {"type": "json_object"}
    assert calls[1]["messages"][1]["content"][-1]["image_url"]["url"] == "data:image/png;base64,QQ=="
    records = [gzip.decompress(path.read_bytes()) for path in tmp_path.glob("*.json.gz")]
    assert len(records) == 2
    assert all(b"secret-never-log" not in record for record in records)
    assert sorted(json.dumps(json.loads(record), sort_keys=True) for record in records) == sorted(json.dumps(call, sort_keys=True) for call in calls)


def test_unrelated_http_400_does_not_trigger_format_or_vision_fallback():
    client = OpenAICompatibleClient(api_key="unused", base_url="https://example.invalid", model="fixture")
    with patch("geng_agent.llm.urllib.request.urlopen", side_effect=HTTPError("url", 400, "Bad Request", {}, io.BytesIO(b"unsupported image input"))) as request:
        with pytest.raises(RuntimeError, match="unsupported image"):
            client.complete_multimodal("prompt", images=[IMAGE], response_format={"type": "json_schema"})
    assert request.call_count == 1


@pytest.mark.parametrize("vision_method", [False, True])
def test_known_vision_unavailability_continues_with_explicit_text_evidence_and_audit(tmp_path, vision_method):
    class TextClient:
        def __init__(self):
            self.calls = []

        def complete(self, prompt, **kwargs):
            self.calls.append(prompt)
            return '{"paper_domain":"communication","paper_repro_type":"other","engineering_facts":[],"missing_information":[]}'

    class UnsupportedVisionClient(TextClient):
        def complete_multimodal(self, prompt, **kwargs):
            raise RuntimeError("LLM request failed: HTTP 400: unsupported image input")

    client = UnsupportedVisionClient() if vision_method else TextClient()
    original = "CRITICAL TEXT: complex quadrature noise variance is N0/2. " + "equation context " * 1000
    result = ReviewPipeline(client=client)._call_validated_json(
        prompt=original, stage_label="facts", schema_stage="engineering_facts", audit_dir=tmp_path,
        max_attempts=1, images=[IMAGE],
    )
    assert len(client.calls) == 1
    actual = client.calls[0]
    assert original in actual
    assert "were NOT delivered" in actual
    assert "paper_page:1" in actual
    assert "not_found_in_paper" in actual
    assert result["_meta"]["evidence_visibility"]["mode"] == "text_only_downgrade"
    manifest = json.loads((tmp_path / "facts_llm_attempt_1_input.json").read_text(encoding="utf-8"))
    assert manifest["images"] == []
    assert manifest["visibility"]["omitted_image_labels"] == ["paper_page:1"]
    assert Path(manifest["prompt_path"]).read_text(encoding="utf-8") == actual
