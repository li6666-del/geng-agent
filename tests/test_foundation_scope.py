from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from geng_agent.agentic_foundation import (
    _validate_scoped_foundation_revision,
    run_codex_foundation_writer_workflow,
)
from geng_agent.execution_plan import ExecutionPlanError, compile_execution_plan
from geng_agent.foundation_revision import collect_pending_foundation_revisions, validate_foundation_revision_request
from geng_agent.foundation_scope import derive_foundation_scope, scoped_foundation_architecture
from geng_agent.foundation_snapshot import foundation_snapshot_hash
from geng_agent.foundation_snapshot_delivery import (
    foundation_private_source_paths,
    foundation_violations,
    install_foundation_snapshot,
    restore_foundation_snapshot,
)
from geng_agent.task_writer_execution_binding import _task_execution_binding_from_architecture
from geng_agent.task_writer_packaging import _writer_package_files
from geng_agent.outputs import write_json


def _component(name: str, dependencies: tuple[str, ...] = ()) -> dict:
    return {
        "id": name,
        "module": f"src/{name}.py",
        "callable": "compute",
        "depends_on": list(dependencies),
        "execution": {"shared_implementation": False},
    }


def _architecture() -> dict:
    return {
        "schema_version": "1.1",
        "components": [
            _component("noise"),
            _component("channel", ("noise",)),
            _component("ber", ("channel",)),
            _component("capacity", ("channel",)),
            _component("table"),
        ],
        "bindings": [
            {"task_id": "a", "experiment_id": "ber", "components": ["ber"]},
            {"task_id": "a", "experiment_id": "capacity", "components": ["capacity"]},
            {"task_id": "b", "experiment_id": "sweep", "components": ["channel"]},
            {"task_id": "c", "experiment_id": "table", "components": ["table"]},
        ],
    }


def _plan() -> dict:
    return compile_execution_plan({"repro_tasks": [{"task_id": key} for key in "abc"]})


def _bundle(root: Path) -> dict:
    snapshot = root / "snapshot"
    source = snapshot / "src"
    source.mkdir(parents=True)
    (source / "__init__.py").write_text("", encoding="utf-8")
    (source / "noise.py").write_text("def compute(snr):\n    return 1.0 / snr\n", encoding="utf-8")
    (source / "channel.py").write_text("from .noise import compute\n", encoding="utf-8")
    files = [
        {
            "path": file.relative_to(snapshot).as_posix(),
            "bytes": file.stat().st_size,
            "sha256": hashlib.sha256(file.read_bytes()).hexdigest(),
        }
        for file in sorted(source.glob("*.py"))
    ]
    manifest = {
        "schema_version": "1.0", "workflow_version": "2", "contract_version": "1",
        "input_hash": "a" * 64, "analysis_snapshot_hash": "b" * 64,
        "snapshot_hash": foundation_snapshot_hash(files),
        "files": files, "frozen_files": files,
        "required_modules": ["src/channel.py", "src/noise.py"],
        "validation": {"tests_passed": True, "local_imports_resolve": True},
        "scope": derive_foundation_scope(_architecture(), _plan()),
    }
    return {"manifest": manifest, "snapshot_hash": manifest["snapshot_hash"], "snapshot_dir": str(snapshot)}


def test_scope_uses_all_experiments_and_transitive_cross_unit_dependencies() -> None:
    architecture = _architecture()
    scoped = scoped_foundation_architecture(architecture, _plan())
    scope = scoped["_foundation_scope"]
    assert scope["component_ids"] == ["channel", "noise"]
    assert scope["private_component_ids"] == ["ber", "capacity", "table"]
    assert scope["component_task_ids"]["noise"] == ["a", "b"]
    assert scoped["bindings"] == architecture["bindings"]
    binding = _task_execution_binding_from_architecture(architecture, "a", _plan())
    assert binding["experiment_ids"] == ["ber", "capacity"]
    assert {item["component_id"] for item in binding["components"]} == {"ber", "capacity", "channel", "noise"}
    assert next(item for item in binding["components"] if item["component_id"] == "ber")["ownership"] == "execution_unit"


def test_one_compound_unit_needs_no_foundation_and_does_not_launch_writer(tmp_path: Path) -> None:
    tasks = {
        "repro_tasks": [{"task_id": "a"}, {"task_id": "b"}],
        "execution_relationships": [{
            "kind": "checkpoint_flow", "strength": "strong", "task_ids": ["b", "a"],
            "producer_task_id": "a", "consumer_task_ids": ["b"], "artifact_ids": ["trained_model"],
        }],
    }
    architecture = {"components": [_component("model")], "bindings": [
        {"task_id": key, "experiment_id": key, "components": ["model"]} for key in "ab"
    ]}
    with patch("geng_agent.agentic_foundation.run_codex_subprocess") as writer:
        result = run_codex_foundation_writer_workflow(
            facts={}, tasks=tasks, experiment_index={}, scientific_architecture=architecture,
            paper={}, paper_path=tmp_path / "paper.txt", paper_images=[], paper_thesis=None,
            output_dir=tmp_path, audit_dir=tmp_path / "audit",
        )
    assert result is None
    writer.assert_not_called()
    plan = compile_execution_plan(tasks)
    assert plan["execution_units"][0]["task_ids"] == ["a", "b"]
    dependency = plan["execution_units"][0]["dependencies"][0]
    assert dependency["producer_task_id"] == "a"
    assert dependency["consumer_task_id"] == "b"
    assert dependency["artifact_id"] == "trained_model"


@pytest.mark.parametrize("kind", ["checkpoint_flow", "shared_pretraining"])
@pytest.mark.parametrize("strength", ["weak", "strong"])
def test_shared_training_state_cannot_be_replaced_by_undirected_code_reuse(kind: str, strength: str) -> None:
    with pytest.raises(ExecutionPlanError, match="shared trained state"):
        compile_execution_plan({
            "repro_tasks": [{"task_id": "train"}, {"task_id": "evaluate"}],
            "execution_relationships": [{
                "kind": kind, "strength": strength, "task_ids": ["train", "evaluate"],
                "artifact_ids": ["checkpoint"],
            }],
        })


def test_private_src_is_packaged_and_survives_frozen_source_restore(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    sandbox = tmp_path / "writer"
    install_foundation_snapshot(sandbox, bundle)
    private = sandbox / "src" / "ber.py"
    private.write_text("from .channel import compute\ndef ber(snr):\n    return compute(snr) / 2\n", encoding="utf-8")
    assert foundation_violations(sandbox, bundle) == []
    assert foundation_private_source_paths(sandbox, bundle) == ["src/ber.py"]
    assert private in _writer_package_files(sandbox)
    (sandbox / "src" / "noise.py").write_text("WRONG = True\n", encoding="utf-8")
    assert foundation_violations(sandbox, bundle)
    restore_foundation_snapshot(sandbox, bundle)
    assert private.is_file()
    assert foundation_violations(sandbox, bundle) == []


def test_private_package_cannot_shadow_frozen_module(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    sandbox = tmp_path / "writer"
    install_foundation_snapshot(sandbox, bundle)
    shadow = sandbox / "src" / "channel" / "__init__.py"
    shadow.parent.mkdir()
    shadow.write_text("def compute(snr): return 0\n", encoding="utf-8")
    assert any(item["file"] == "src/channel/__init__.py" for item in foundation_violations(sandbox, bundle))
    restore_foundation_snapshot(sandbox, bundle)
    assert not shadow.exists()


def test_scientific_revision_targets_consumers_and_keeps_previous_generation(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    sandbox = tmp_path / "revision"
    install_foundation_snapshot(sandbox, bundle)
    evidence = sandbox / "paper_evidence" / "source" / "paper.txt"
    evidence.parent.mkdir(parents=True)
    evidence.write_text("Complex noise uses variance 1/(2*SNR) per quadrature.", encoding="utf-8")
    request = validate_foundation_revision_request(
        {
            "component_ids": ["noise"], "paper_evidence_files": ["paper_evidence/source/paper.txt"],
            "causal_change": "Divide the per-quadrature noise variance by two as the paper specifies.",
            "predicted_effect": "The complex noise power will match the declared SNR.",
        }, architecture=_architecture(), execution_plan=_plan(), evidence_root=sandbox,
    )
    assert request["affected_task_ids"] == ["a", "b"]
    assert request["module_paths"] == ["src/noise.py"]
    before = (Path(bundle["snapshot_dir"]) / "src/noise.py").read_bytes()
    (sandbox / "src/noise.py").write_text("def compute(snr):\n    return 1.0 / (2 * snr)\n", encoding="utf-8")
    _validate_scoped_foundation_revision(sandbox=sandbox, previous_foundation=bundle, revision_request=request)
    assert (Path(bundle["snapshot_dir"]) / "src/noise.py").read_bytes() == before
    (sandbox / "src/channel.py").write_text("def compute(snr): return 0\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="unrelated source"):
        _validate_scoped_foundation_revision(sandbox=sandbox, previous_foundation=bundle, revision_request=request)


def test_revision_rejects_private_components_and_missing_paper_evidence(tmp_path: Path) -> None:
    request = {"component_ids": ["ber"], "paper_evidence_files": ["paper_evidence/missing.pdf"], "causal_change": "fix"}
    with pytest.raises(ValueError, match="frozen shared"):
        validate_foundation_revision_request(request, architecture=_architecture(), execution_plan=_plan(), evidence_root=tmp_path)
    request["component_ids"] = ["noise"]
    with pytest.raises(FileNotFoundError):
        validate_foundation_revision_request(request, architecture=_architecture(), execution_plan=_plan(), evidence_root=tmp_path)


def test_applied_revision_retires_old_records_but_keeps_new_or_declined_requests() -> None:
    old = {"foundation_revision_request": {"request_id": "fixed"}, "foundation_revision_snapshot_hash": "previous"}
    new = {"foundation_revision_request": {"request_id": "next"}, "foundation_revision_snapshot_hash": "previous"}
    foundation = {"snapshot_hash": "current", "manifest": {"revision": {"request_id": "fixed", "applied_request_ids": ["fixed"]}}}
    assert collect_pending_foundation_revisions([old, new], foundation) == [{"request_id": "next"}]
    assert "foundation_revision_request" not in old
    assert collect_pending_foundation_revisions([new], foundation, {"next"}) == []
    assert new["scientific_stop_reason"] == "foundation_revision_unresolved"
    assert new["writer_completed"] is False


def test_revision_workflow_freezes_new_generation_and_resume_keeps_repair_and_private_source(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    output = tmp_path / "case"
    audit = output / "audit"
    architecture = _architecture()
    tasks = {"repro_tasks": [{"task_id": key} for key in "abc"]}
    for filename, document in {"engineering_facts.json": {}, "repro_tasks.json": tasks,
                               "experiment_index.json": {}, "execution_plan.json": _plan(),
                               "scientific_architecture.json": architecture}.items():
        write_json(output / filename, document)
    paper = tmp_path / "paper.txt"
    paper.write_text("The per-quadrature variance is 1 / (2 * SNR).", encoding="utf-8")
    consumer = audit / "03c_task_writer_sandboxes/01_a"
    install_foundation_snapshot(consumer, bundle)
    private = consumer / "src/ber.py"
    private.write_text("from .noise import compute\ndef ber(snr): return compute(snr)\n", encoding="utf-8")
    evidence = consumer / "paper_evidence/source/paper.txt"
    evidence.parent.mkdir(parents=True)
    evidence.write_bytes(paper.read_bytes())
    request = {"component_ids": ["noise"], "paper_evidence_files": ["paper_evidence/source/paper.txt"],
               "causal_change": "Use half the reciprocal SNR per quadrature."}

    def repair_source(**kwargs):
        sandbox = Path(kwargs["work_dir"])
        (sandbox / "src/noise.py").write_text("def compute(snr):\n    return 1.0 / (2 * snr)\n", encoding="utf-8")
        test = sandbox / "tests/test_noise.py"
        test.parent.mkdir(parents=True)
        test.write_text("import unittest\nfrom src.noise import compute\nclass NoiseTests(unittest.TestCase):\n    def test_quadrature_variance(self):\n        self.assertAlmostEqual(2 * compute(10), 0.1)\n", encoding="utf-8")
        return {"ok": True}

    kwargs = dict(facts={}, tasks=tasks, experiment_index={}, scientific_architecture=architecture,
                  paper={}, paper_path=paper, paper_images=[], paper_thesis=None,
                  output_dir=output, audit_dir=audit, execution_plan=_plan())
    with patch("geng_agent.agentic_foundation.run_codex_subprocess", side_effect=repair_source) as writer:
        revised = run_codex_foundation_writer_workflow(
            **kwargs, revision_request=request, revision_evidence_root=consumer, previous_foundation=bundle,
        )
        # This is a full host validation/freeze, with only the external agent mocked.
        assert revised["manifest"]["validation"]["tests_passed"] is True
        resumed = run_codex_foundation_writer_workflow(**kwargs, resume=True)
        assert writer.call_count == 1
    assert resumed["snapshot_hash"] == revised["snapshot_hash"] != bundle["snapshot_hash"]
    assert resumed["snapshot_dir"] == revised["snapshot_dir"]
    assert "return 1.0 / snr" in (Path(bundle["snapshot_dir"]) / "src/noise.py").read_text()
    restore_foundation_snapshot(consumer, resumed)
    assert private.is_file()
    assert "2 * snr" in (consumer / "src/noise.py").read_text()
