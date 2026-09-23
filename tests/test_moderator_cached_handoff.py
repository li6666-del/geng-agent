"""Cached Writer handoffs use the same shared-repair boundary as fresh runs."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from threading import Barrier, Lock

import pytest

from geng_agent import agentic_task_writers as workflow
from geng_agent.execution_plan import compile_execution_plan
from geng_agent.foundation_revision import FoundationRevisionRequired, validate_foundation_revision_request
from geng_agent.foundation_scope import derive_foundation_scope
from geng_agent.foundation_snapshot import foundation_snapshot_hash
from geng_agent.foundation_snapshot_delivery import install_foundation_snapshot
from geng_agent.outputs import write_json


@pytest.fixture
def cached_case(tmp_path, monkeypatch):
    output = tmp_path / "case"
    audit = output / "audit"
    project = output / "repro_project"
    paper = tmp_path / "paper.txt"
    paper.write_text("The noise variance per quadrature is 1/(2*SNR).", encoding="utf-8")
    tasks = {"repro_tasks": [{"task_id": name} for name in ("a", "b")]}
    plan = compile_execution_plan(tasks)
    architecture = {
        "components": [{"id": "shared", "module": "src/shared.py", "callable": "sample"},
                       {"id": "private_b", "module": "tasks/b.py", "callable": "compute"}],
        "bindings": [{"task_id": "a", "components": ["shared"]},
                     {"task_id": "b", "components": ["shared", "private_b"]}],
    }
    for name, document in {"engineering_facts.json": {"engineering_facts": []},
                           "repro_tasks.json": tasks, "experiment_index.json": {"experiments": []},
                           "execution_plan.json": plan, "scientific_architecture.json": architecture}.items():
        write_json(output / name, document)
    snapshot = tmp_path / "snapshot"
    source = snapshot / "src/shared.py"
    source.parent.mkdir(parents=True)
    source.write_text("def sample(): return 1\n", encoding="utf-8")
    files = [{"path": "src/shared.py", "bytes": source.stat().st_size,
              "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}]
    manifest = {"schema_version": "1.0", "workflow_version": "2", "contract_version": "1",
                "input_hash": "a" * 64, "analysis_snapshot_hash": "b" * 64,
                "snapshot_hash": foundation_snapshot_hash(files), "files": files, "frozen_files": files,
                "required_modules": ["src/shared.py"],
                "validation": {"tests_passed": True, "local_imports_resolve": True},
                "scope": derive_foundation_scope(architecture, plan)}
    foundation = {"manifest": manifest, "snapshot_hash": manifest["snapshot_hash"], "snapshot_dir": str(snapshot)}
    install_foundation_snapshot(project, foundation)
    records = []
    reporters = {}
    for index, name in enumerate(("a", "b"), 1):
        sandbox = audit / "03c_task_writer_sandboxes" / f"{index:02d}_{name}"
        install_foundation_snapshot(sandbox, foundation)
        write_json(sandbox / "paper_evidence/analysis_artifacts/scientific_architecture.json", architecture)
        write_json(sandbox / "paper_evidence/analysis_artifacts/execution_plan.json", plan)
        reporter = tmp_path / f"reporter_{name}"
        evidence = reporter / "paper_evidence/source/paper.txt"
        evidence.parent.mkdir(parents=True)
        evidence.write_bytes(paper.read_bytes())
        reporters[name] = reporter
        records.append({"index": index, "task_id": name, "sandbox": str(sandbox),
                        "output_subdir": name, "writer_session_count": 1, "writer_completed": True,
                        "task_writer_status": "ready_for_review", "result_json": {"status": "ready_for_review"}})
    cached = {"manifest": {"_meta": {"backend": "codex", "mode": "task_writers"}},
              "runtime_result": {"passed": True}, "task_records": records, "written_files": [],
              "status": {"cached": True}}
    monkeypatch.setattr(workflow, "_load_cached_task_writer_workflow", lambda **_kwargs: cached)
    monkeypatch.setattr(workflow, "_load_task_writer_resume_records", lambda **_kwargs: {i: r for i, r in enumerate(records, 1)})

    def forbidden(*_args, **_kwargs):
        pytest.fail("Pending Foundation repair must be handed off before clearing, dispatching or packaging.")

    for function in ("_clear_stage_outputs", "_dispatch_task_writers", "_prepare_project_workspace"):
        monkeypatch.setattr(workflow, function, forbidden)

    def run(callback):
        return workflow.run_codex_task_writer_workflow(
            facts={"engineering_facts": []}, tasks=tasks, experiment_index={"experiments": []},
            paper={"chunks": []}, paper_path=paper, paper_context_json="", paper_images=[],
            paper_thesis=None, output_dir=output, audit_dir=audit, repro_project_dir=project,
            run_repro=True, resume=True, task_review_callback=callback, foundation=foundation,
            execution_plan=plan,
        )

    return {"run": run, "records": records, "audit": audit, "reporters": reporters,
            "architecture": architecture, "plan": plan}


def _verification(task_id, target):
    return {"schema_version": "3.0", "task_id": task_id, "outcome": "not_reproduced",
            "host_action": "rerun_writer", "rerun_reason": "core_conclusion_failed", "run_valid": True,
            "decision_reason": "The implemented noise normalization differs from the paper.",
            "core_conclusions": [{"claim_id": "claim_order", "status": "unsupported"}],
            "rerun_evidence": {"rerun_reason": "core_conclusion_failed", "contract_item_ids": ["claim_order"],
                "paper_evidence_files": ["paper_evidence/source/paper.txt"],
                "change_targets": [target], "causal_change": "Use the paper's per-quadrature variance.",
                "predicted_effect": "Match the stated SNR normalization."}}


@pytest.mark.parametrize("mixed", [False, True], ids=["shared_only", "shared_and_private"])
def test_cached_reviews_handoff_shared_repairs_after_all_parallel_reporters(cached_case, mixed, monkeypatch):
    barrier = Barrier(2, timeout=5)
    lock = Lock()
    completed = set()

    def moderate(**kwargs):
        name = kwargs["context"]["task_id"]
        return {"action": "revise_writer" if mixed and name == "b" else "revise_foundation",
                "component_ids": ["shared"], "decision_id": f"repair-{name}",
                "instructions": "Use the paper's per-quadrature variance.",
                "expected_change": "Correct the assigned normalization."}

    monkeypatch.setattr("geng_agent.task_recovery.request_moderation", moderate)

    def callback(_index, task, _record, _round):
        name = task["task_id"]
        barrier.wait()
        with lock:
            completed.add(name)
        return {"ok": True, "workspace": str(cached_case["reporters"][name]),
                "task_verification": _verification(name, "tasks/b.py:compute" if mixed and name == "b" else "src/shared.py:sample")}

    with pytest.raises(FoundationRevisionRequired) as caught:
        cached_case["run"](callback)
    assert completed == {"a", "b"}
    assert caught.value.affected_component_ids == ("shared",)
    checkpoint = json.loads((cached_case["audit"] / "03c_task_writers_records.json").read_text(encoding="utf-8"))
    records = {record["task_id"]: record for record in checkpoint["tasks"]}
    assert set(records) == {"a", "b"}
    assert all(record["task_verification"]["outcome"] == "not_reproduced" for record in records.values())
    assert records["a"]["foundation_revision_request"]["component_ids"] == ["shared"]
    if mixed:
        assert "foundation_revision_request" not in records["b"]
        assert records["b"]["task_verification"] == _verification("b", "tasks/b.py:compute")
        assert checkpoint["dispatch_policy"]["writer_revision_task_ids"] == ["b"]


def test_cached_pending_shared_request_without_callback_is_handed_off(cached_case):
    request = validate_foundation_revision_request(
        {"component_ids": ["shared"], "paper_evidence_files": ["paper_evidence/source/paper.txt"],
         "causal_change": "Use the paper's per-quadrature variance."},
        architecture=cached_case["architecture"], execution_plan=cached_case["plan"],
        evidence_root=cached_case["reporters"]["a"],
    )
    request["evidence_root"] = str(cached_case["reporters"]["a"])
    cached_case["records"][0]["foundation_revision_request"] = deepcopy(request)
    with pytest.raises(FoundationRevisionRequired) as caught:
        cached_case["run"](None)
    assert caught.value.requests == [request]
    checkpoint = json.loads((cached_case["audit"] / "03c_task_writers_records.json").read_text(encoding="utf-8"))
    assert len(checkpoint["tasks"]) == 2
    assert checkpoint["tasks"][0]["foundation_revision_request"] == request
