from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from geng_agent.case_runtime import CaseRuntime
from geng_agent.execution_plan import compile_execution_plan
from geng_agent.execution_receipts import source_hashes, artifact_hashes
from geng_agent.outputs import write_json
from geng_agent.task_writer_state import _load_task_writer_resume_records
from geng_agent.writer_lineage import build_writer_unit_lineage, foundation_cache_projection
from geng_agent.foundation_scope import scoped_foundation_architecture


def _fixture(root: Path):
    tasks = {"repro_tasks": [
        {"task_id": "a", "required_facts": [{"type": "parameter", "name": "noise"}], "scientific_acceptance": {"core_conclusions": [{"claim_id": "a-trend"}]}},
        {"task_id": "b", "required_facts": [{"type": "parameter", "name": "rate"}], "scientific_acceptance": {"core_conclusions": [{"claim_id": "b-trend"}]}},
    ]}
    plan = compile_execution_plan(tasks)
    pairs = [(task, {"task_id": task["task_id"], "module": task["task_id"], "script": f"tasks/{task['task_id']}.py", "output_subdir": task["task_id"]}) for task in tasks["repro_tasks"]]
    facts = {"engineering_facts": [{"type": "parameter", "name": "noise", "value": 1}, {"type": "parameter", "name": "rate", "value": 2}]}
    architecture = {"schema_version": "1.1", "components": [
        {"id": "solver_a", "module": "src/solver_a.py", "execution": {"primary_framework": "scipy"}},
        {"id": "solver_b", "module": "src/solver_b.py", "execution": {"primary_framework": "standard_library"}},
    ], "bindings": [
        {"task_id": "a", "experiment_id": "exp_a", "components": ["solver_a"]},
        {"task_id": "b", "experiment_id": "exp_b", "components": ["solver_b"]},
    ]}
    architecture_path = root / "scientific_architecture.json"
    write_json(architecture_path, architecture)
    paper = root / "paper.txt"
    paper.write_text("Fixed scientific paper.", encoding="utf-8")
    prefix = root / "runtime"
    metadata = prefix / "Lib/site-packages/scipy-1.dist-info/METADATA"
    metadata.parent.mkdir(parents=True)
    metadata.write_text("Metadata-Version: 2.1\nName: scipy\nVersion: 1.0\nRequires-Dist: numpy>=2\n", encoding="utf-8")
    runtime = CaseRuntime(
        venv_dir=prefix, python_executable=prefix / "python.exe",
        request_path=root / "request.json", lock_path=root / "lock.json", report_path=root / "report.json",
        environment_hash="whole-runtime-v1", manifest={}, report={}, trusted_read_roots=(),
        lock={"interpreter": {"python_full_version": "3.11.9"}, "requirements": [
            {"distribution": "scipy", "requirement": "scipy", "installed_version": "1.0", "import_names": ["scipy"]}
        ], "installed_distributions": [{"distribution": "scipy", "version": "1.0"}, {"distribution": "numpy", "version": "2.1"}]},
    )
    kwargs = dict(task_pairs=pairs, execution_plan=plan, facts=facts, experiment_index={}, paper_path=paper,
                  analysis_artifacts={"scientific_architecture.json": architecture_path}, foundation=None,
                  case_runtime=runtime, task_root=root / "audit/03c_task_writer_sandboxes")
    return kwargs, plan


def test_unrelated_task_fact_and_environment_extension_do_not_invalidate_other_unit(tmp_path: Path) -> None:
    kwargs, plan = _fixture(tmp_path)
    a, b = [plan["task_to_execution_unit"][key] for key in "ab"]
    first = build_writer_unit_lineage(**kwargs)
    kwargs["task_pairs"][0][0]["scientific_acceptance"]["core_conclusions"].append({"claim_id": "a-new"})
    kwargs["facts"]["engineering_facts"][0]["value"] = 3
    changed = build_writer_unit_lineage(**kwargs)
    assert changed[a]["snapshot_hash"] != first[a]["snapshot_hash"]
    assert changed[b]["snapshot_hash"] == first[b]["snapshot_hash"]
    runtime = kwargs["case_runtime"]
    lock = json.loads(json.dumps(runtime.lock))
    lock["installed_distributions"].append({"distribution": "unrelated-paper-library", "version": "4"})
    kwargs["case_runtime"] = replace(runtime, environment_hash="whole-runtime-v2", lock=lock)
    extended = build_writer_unit_lineage(**kwargs)
    assert {key: value["snapshot_hash"] for key, value in changed.items()} == {key: value["snapshot_hash"] for key, value in extended.items()}


def test_transitive_runtime_dependency_change_invalidates_only_its_consumer(tmp_path: Path) -> None:
    kwargs, plan = _fixture(tmp_path)
    first = build_writer_unit_lineage(**kwargs)
    runtime = kwargs["case_runtime"]
    lock = json.loads(json.dumps(runtime.lock))
    lock["installed_distributions"][1]["version"] = "2.2"
    kwargs["case_runtime"] = replace(runtime, lock=lock)
    changed = build_writer_unit_lineage(**kwargs)
    assert changed[plan["task_to_execution_unit"]["a"]]["snapshot_hash"] != first[plan["task_to_execution_unit"]["a"]]["snapshot_hash"]
    assert changed[plan["task_to_execution_unit"]["b"]]["snapshot_hash"] == first[plan["task_to_execution_unit"]["b"]]["snapshot_hash"]


def _write_completed_records(root: Path, kwargs: dict, plan: dict) -> dict[str, str]:
    lineage = build_writer_unit_lineage(**kwargs)
    hashes = {key: value["snapshot_hash"] for key, value in lineage.items()}
    records = []
    for index, task_id in enumerate("ab", 1):
        sandbox = kwargs["task_root"] / f"{index:02d}_{task_id}"
        write_json(sandbox / "paper_evidence/index.json", {"tasks": [{"task_id": task_id}], "analysis_snapshot_hash": hashes[plan["task_to_execution_unit"][task_id]]})
        (sandbox / "tasks").mkdir()
        (sandbox / "tasks" / f"{task_id}.py").write_text("VALUE = 1\n", encoding="utf-8")
        output = sandbox / "outputs" / task_id
        output.mkdir(parents=True)
        (output / "result.csv").write_text("x,y\n1,2\n", encoding="utf-8")
        records.append({"index": index, "task_id": task_id, "sandbox": str(sandbox), "analysis_snapshot_hash": hashes[plan["task_to_execution_unit"][task_id]],
                        "writer_completed": True, "task_writer_status": "ready_for_review"})
        receipt = {"observer": "orchestration_host", "task_id": task_id, "returncode": 0, "mode": "full", "run_id": task_id,
                   "inputs_stable": True, "source_hashes": source_hashes(sandbox), "input_hashes": {}, "output_hashes": artifact_hashes(sandbox, task_id), "finished_at": index}
        write_json(root / "audit/execution_runs" / task_id / "execution_receipt.json", receipt)
    write_json(root / "audit/03c_task_writers_records.json", {"tasks": records})
    return hashes


def test_resume_rechecks_host_receipt_and_preserves_only_changed_unit_for_refresh(tmp_path: Path) -> None:
    kwargs, plan = _fixture(tmp_path)
    hashes = _write_completed_records(tmp_path, kwargs, plan)
    common = dict(audit_dir=tmp_path / "audit", task_pairs=kwargs["task_pairs"], execution_plan=plan,
                  expected_analysis_snapshot_hash="whole-case-changed", expected_snapshot_hashes=hashes)
    current = _load_task_writer_resume_records(**common)
    assert current[1]["writer_completed"] is True
    assert current[2]["writer_completed"] is True
    (kwargs["task_root"] / "01_a/outputs/a/result.csv").write_text("x,y\n1,999\n", encoding="utf-8")
    recovered = _load_task_writer_resume_records(**common)
    assert recovered[1]["runtime_refresh_required"] is True
    assert recovered[2]["writer_completed"] is True
    assert (kwargs["task_root"] / "01_a/tasks/a.py").is_file()


def test_old_unobserved_delivery_is_not_reused_as_a_verified_full(tmp_path: Path) -> None:
    kwargs, plan = _fixture(tmp_path)
    hashes = _write_completed_records(tmp_path, kwargs, plan)
    (tmp_path / "audit/execution_runs/a/execution_receipt.json").unlink()
    recovered = _load_task_writer_resume_records(
        audit_dir=tmp_path / "audit", task_pairs=kwargs["task_pairs"], execution_plan=plan,
        expected_analysis_snapshot_hash="old-case", expected_snapshot_hashes=hashes,
    )
    assert recovered[1]["runtime_refresh_required"] is True
    assert recovered[2]["writer_completed"] is True


def test_changed_contract_preserves_sandbox_for_targeted_continuation(tmp_path: Path) -> None:
    kwargs, plan = _fixture(tmp_path)
    hashes = _write_completed_records(tmp_path, kwargs, plan)
    hashes[plan["task_to_execution_unit"]["a"]] = "new-a-inputs"
    recovered = _load_task_writer_resume_records(
        audit_dir=tmp_path / "audit", task_pairs=kwargs["task_pairs"], execution_plan=plan,
        expected_analysis_snapshot_hash="new-whole-case", expected_snapshot_hashes=hashes,
    )
    assert recovered[1]["runtime_refresh_required"] is True
    assert recovered[1]["analysis_snapshot_hash"] == "new-a-inputs"
    assert recovered[2]["writer_completed"] is True


def test_absent_fact_references_are_conservative_and_comparison_changes_are_local(tmp_path: Path) -> None:
    kwargs, plan = _fixture(tmp_path)
    a, b = [plan["task_to_execution_unit"][key] for key in "ab"]
    kwargs["task_pairs"][0][0]["required_facts"] = []
    kwargs["task_pairs"][0][0]["figure_or_claim"] = "Fig. 1"
    kwargs["task_pairs"][1][0]["figure_or_claim"] = "Fig. 2"
    kwargs["paper_thesis"] = {"comparisons": [{"figure_ref": "Fig. 1", "expected_ordering": "proposed > baseline"}]}
    first = build_writer_unit_lineage(**kwargs)
    kwargs["facts"]["engineering_facts"].append({"type": "parameter", "name": "unmapped_gain", "value": 2})
    changed = build_writer_unit_lineage(**kwargs)
    assert first[a]["snapshot_hash"] != changed[a]["snapshot_hash"]
    assert first[b]["snapshot_hash"] == changed[b]["snapshot_hash"]
    kwargs["paper_thesis"]["comparisons"][0]["expected_ordering"] = "baseline > proposed"
    compared = build_writer_unit_lineage(**kwargs)
    assert compared[a]["snapshot_hash"] != changed[a]["snapshot_hash"]
    assert compared[b]["snapshot_hash"] == changed[b]["snapshot_hash"]


def test_actual_policy_content_hash_participates_in_unit_key(tmp_path: Path) -> None:
    kwargs, _plan = _fixture(tmp_path)
    with patch("geng_agent.writer_lineage.writer_policy_content_hashes", return_value={"prompt": "original"}):
        first = build_writer_unit_lineage(**kwargs)
    with patch("geng_agent.writer_lineage.writer_policy_content_hashes", return_value={"prompt": "changed"}):
        changed = build_writer_unit_lineage(**kwargs)
    assert all(first[key]["snapshot_hash"] != changed[key]["snapshot_hash"] for key in first)


def test_foundation_source_dependency_change_invalidates_only_consumers(tmp_path: Path) -> None:
    kwargs, plan = _fixture(tmp_path)
    architecture_path = kwargs["analysis_artifacts"]["scientific_architecture.json"]
    architecture = json.loads(architecture_path.read_text())
    architecture["components"].append({"id": "shared_noise", "module": "src/shared_noise.py"})
    architecture["bindings"].append({"task_id": "c", "experiment_id": "exp_c", "components": ["shared_noise"]})
    architecture["components"][0]["depends_on"] = ["shared_noise"]
    write_json(architecture_path, architecture)
    snapshot = tmp_path / "foundation"
    (snapshot / "src").mkdir(parents=True)
    (snapshot / "src/shared_noise.py").write_text("from .variance import scale\n", encoding="utf-8")
    (snapshot / "src/variance.py").write_text("scale = 1\n", encoding="utf-8")
    kwargs["foundation"] = {"snapshot_dir": str(snapshot)}
    before = build_writer_unit_lineage(**kwargs)
    (snapshot / "src/variance.py").write_text("scale = 0.5\n", encoding="utf-8")
    after = build_writer_unit_lineage(**kwargs)
    assert before[plan["task_to_execution_unit"]["a"]]["snapshot_hash"] != after[plan["task_to_execution_unit"]["a"]]["snapshot_hash"]
    assert before[plan["task_to_execution_unit"]["b"]]["snapshot_hash"] == after[plan["task_to_execution_unit"]["b"]]["snapshot_hash"]


def test_foundation_key_ignores_private_contract_and_unused_environment_extension(tmp_path: Path) -> None:
    kwargs, plan = _fixture(tmp_path)
    architecture = json.loads(kwargs["analysis_artifacts"]["scientific_architecture.json"].read_text())
    architecture["bindings"].append({"task_id": "c", "experiment_id": "exp_c", "components": ["solver_a"]})
    architecture["components"][0]["basis"] = {"evidence_facts": [{"type": "parameter", "name": "noise"}]}
    def projected():
        return foundation_cache_projection(architecture=scoped_foundation_architecture(architecture, plan),
                                           facts=kwargs["facts"], paper_path=kwargs["paper_path"], case_runtime=kwargs["case_runtime"])
    first = projected()
    architecture["components"][1]["callable"] = "a_new_private_method"
    kwargs["facts"]["engineering_facts"][1]["value"] = 10
    runtime = kwargs["case_runtime"]
    lock = json.loads(json.dumps(runtime.lock))
    lock["installed_distributions"].append({"distribution": "unused-package", "version": "1"})
    kwargs["case_runtime"] = replace(runtime, lock=lock, environment_hash="new-whole-case")
    assert projected() == first
    lock["installed_distributions"][1]["version"] = "3.0"
    assert projected()[2] != first[2]


def test_observed_dynamic_backend_maps_to_distribution_and_invalidates_only_its_consumer(tmp_path: Path) -> None:
    kwargs, plan = _fixture(tmp_path)
    distribution = kwargs["case_runtime"].venv_dir / "Lib/site-packages/scikit_commpy-1.dist-info"
    distribution.mkdir(parents=True)
    metadata = distribution / "METADATA"
    metadata.write_text("Metadata-Version: 2.1\nName: scikit-commpy\nVersion: 1.0\n", encoding="utf-8")
    (distribution / "top_level.txt").write_text("commpy\n", encoding="utf-8")
    # The optional backend is loaded dynamically and is absent from both the
    # architecture and required-dependency graph; only the host saw the import.
    script = kwargs["task_root"] / "01_a/tasks/a.py"
    script.parent.mkdir(parents=True)
    script.write_text("import importlib\ndef run(name): return importlib.import_module(name)\n", encoding="utf-8")
    write_json(tmp_path / "audit/execution_runs/older/execution_receipt.json",
               {"observer": "orchestration_host", "task_id": "a", "finished_at": 1, "observed_import_roots": []})
    write_json(tmp_path / "audit/execution_runs/current/execution_receipt.json",
               {"observer": "orchestration_host", "task_id": "a", "finished_at": 2, "observed_import_roots": ["commpy"]})
    write_json(tmp_path / "audit/execution_runs/untrusted/execution_receipt.json",
               {"observer": "standalone", "task_id": "b", "finished_at": 3, "observed_import_roots": ["commpy"]})
    first = build_writer_unit_lineage(**kwargs)
    a, b = [plan["task_to_execution_unit"][key] for key in "ab"]
    assert first[a]["inputs"]["runtime"]["distributions"]["scikit-commpy"] == "1.0"
    assert "scikit-commpy" not in first[b]["inputs"]["runtime"]["distributions"]
    # Inspect real selected-runtime metadata, so a changed optional package
    # also invalidates stale locks whose version inventory was not refreshed.
    metadata.write_text("Metadata-Version: 2.1\nName: scikit-commpy\nVersion: 2.0\n", encoding="utf-8")
    changed = build_writer_unit_lineage(**kwargs)
    assert first[a]["snapshot_hash"] != changed[a]["snapshot_hash"]
    assert first[b]["snapshot_hash"] == changed[b]["snapshot_hash"]


def test_declined_scientific_repair_is_preserved_without_claiming_a_full_execution(tmp_path: Path) -> None:
    kwargs, plan = _fixture(tmp_path)
    hashes = _write_completed_records(tmp_path, kwargs, plan)
    path = tmp_path / "audit/03c_task_writers_records.json"
    document = json.loads(path.read_text())
    document["tasks"][0].update({"writer_completed": False, "foundation_revision_request": {"request_id": "unresolved"}})
    write_json(path, document)
    (tmp_path / "audit/execution_runs/a/execution_receipt.json").unlink()
    recovered = _load_task_writer_resume_records(
        audit_dir=tmp_path / "audit", task_pairs=kwargs["task_pairs"], execution_plan=plan,
        expected_analysis_snapshot_hash="case", expected_snapshot_hashes=hashes,
        declined_foundation_revision_ids={"unresolved"},
    )
    assert recovered[1]["scientific_stop_reason"] == "foundation_revision_unresolved"
    assert recovered[1]["writer_completed"] is False
    assert not recovered[1].get("runtime_refresh_required")
    assert recovered[2]["writer_completed"] is True


def test_workflow_finalizes_discovered_imports_before_first_resume(tmp_path: Path) -> None:
    from geng_agent.agentic_task_writers import run_codex_task_writer_workflow
    from geng_agent.task_writer_units import _execution_unit_work_items

    kwargs, plan = _fixture(tmp_path)
    tasks = {"repro_tasks": [pair[0] for pair in kwargs["task_pairs"]]}
    for name, document in {"engineering_facts.json": kwargs["facts"], "repro_tasks.json": tasks,
                           "experiment_index.json": {}}.items():
        write_json(tmp_path / name, document)
    observed = {}

    class CompletedUnitBoundary(RuntimeError):
        pass

    def complete_one_unit(**call):
        unit = next(item for item in _execution_unit_work_items(call["task_pairs"], call["execution_plan"])
                    if item["task_ids"] == ["b"])
        sandbox = call["task_root"] / "02_b"
        write_json(sandbox / "paper_evidence/index.json", {"tasks": [{"task_id": "b"}]})
        script = sandbox / "tasks/b.py"
        script.parent.mkdir()
        script.write_text("import importlib\nRESULT = importlib.import_module('numpy').array([1.0])\n", encoding="utf-8")
        write_json(call["audit_dir"] / "execution_runs/b_dynamic_import/execution_receipt.json",
                   {"observer": "orchestration_host", "task_id": "b", "finished_at": 1, "observed_import_roots": ["numpy"]})
        record = {"task_id": "b", "sandbox": str(sandbox)}
        observed["before"] = call["snapshot_hashes"][unit["unit_id"]]
        call["snapshot_finalizer"](unit, [record])
        observed["after"] = record["analysis_snapshot_hash"]
        raise CompletedUnitBoundary()

    with patch("geng_agent.agentic_task_writers._dispatch_task_writers", side_effect=complete_one_unit):
        with pytest.raises(CompletedUnitBoundary):
            run_codex_task_writer_workflow(
                facts=kwargs["facts"], tasks=tasks, experiment_index={}, paper={}, paper_path=kwargs["paper_path"],
                paper_context_json="{}", paper_images=[], paper_thesis=None, output_dir=tmp_path,
                audit_dir=tmp_path / "audit", repro_project_dir=tmp_path / "repro_project",
                run_repro=False, resume=True, case_runtime=kwargs["case_runtime"], execution_plan=plan,
            )
    current = build_writer_unit_lineage(**kwargs)[plan["task_to_execution_unit"]["b"]]["snapshot_hash"]
    assert observed["before"] != observed["after"] == current
    evidence = json.loads((kwargs["task_root"] / "02_b/paper_evidence/index.json").read_text())
    assert evidence["analysis_snapshot_hash"] == current
