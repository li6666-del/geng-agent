import json
from pathlib import Path
import sys

import pytest

from geng_agent import agentic_foundation as foundation
from geng_agent.case_runtime import CaseRuntime, EnvironmentRequestRequired
from geng_agent.foundation_snapshot import validate_foundation_snapshot


def _runtime(root: Path, *, extended: bool) -> CaseRuntime:
    requirement = {
        "requirement": "scipy>=1.10", "distribution": "scipy", "installed_version": "1.15.0",
        "applicable": True, "satisfied": True, "version_satisfied": True, "imports_ok": True,
        "import_names": ["scipy"],
    }
    return CaseRuntime(
        venv_dir=Path(sys.prefix), python_executable=Path(sys.executable),
        request_path=root / "request.json", lock_path=root / "lock.json", report_path=root / "report.json",
        environment_hash=("b" if extended else "a") * 64,
        manifest={}, lock={"requirements": [requirement] if extended else []}, report={},
        trusted_read_roots=(Path(sys.prefix),),
    )


@pytest.fixture
def workflow(monkeypatch, tmp_path):
    output = tmp_path / "case"
    output.mkdir()
    audit = output / "audit"
    audit.mkdir()
    for name in ("engineering_facts", "repro_tasks", "experiment_index", "execution_plan", "scientific_architecture"):
        (output / f"{name}.json").write_text("{}", encoding="utf-8")
    paper = tmp_path / "paper.md"
    paper.write_text("Synthetic transport fixture only.", encoding="utf-8")
    writer_calls, test_calls = [], []
    state = {"writer_ok": True, "tests_passed": True, "explicit_request": False,
             "mutate_during_validation": False}

    def writer(**kwargs):
        writer_calls.append(kwargs)
        sandbox = kwargs["work_dir"]
        (sandbox / "src/model.py").write_text("VALUE = 7\n", encoding="utf-8")
        (sandbox / "tests").mkdir(exist_ok=True)
        (sandbox / "tests/test_model.py").write_text(
            "import unittest\nfrom src.model import VALUE\n"
            "class ModelTest(unittest.TestCase):\n"
            "    def test_value(self): self.assertEqual(VALUE, 7)\n", encoding="utf-8")
        (sandbox / "requirements.txt").write_text(
            "" if state["explicit_request"] else "scipy>=1.10\n", encoding="utf-8")
        (sandbox / "foundation_result.json").write_text('{"summary":"completed fixture"}', encoding="utf-8")
        if state["explicit_request"]:
            (sandbox / "environment_request.json").write_text(json.dumps({"requirements": [
                {"requirement": "scipy>=1.10", "import_names": ["scipy"], "reason": "fixture dependency"},
            ]}), encoding="utf-8")
        return {"ok": state["writer_ok"], "error": None if state["writer_ok"] else "interrupted fixture"}

    def observed_test_boundary(**kwargs):
        # Exercise the normal host validation path without executing generated
        # code or invoking a model. Its isolation arguments must remain intact.
        test_calls.append(kwargs)
        assert kwargs["python_executable"] == Path(sys.executable)
        assert kwargs["venv_dir"] == Path(sys.prefix)
        assert kwargs["trusted_runtime_roots"] == (Path(sys.prefix),)
        assert (kwargs["work_dir"] / "src/model.py").read_text() == "VALUE = 7\n"
        if state["mutate_during_validation"]:
            (kwargs["work_dir"] / "src/model.py").write_text("VALUE = 99\n", encoding="utf-8")
        return {"passed": state["tests_passed"], "returncode": 0 if state["tests_passed"] else 1,
                "stderr": "" if state["tests_passed"] else "observed fixture test failure"}

    monkeypatch.setattr(foundation, "run_codex_subprocess", writer)
    monkeypatch.setattr(foundation, "run_python_unittest_subprocess", observed_test_boundary)
    monkeypatch.setattr(foundation, "_write_paper_evidence_bundle", lambda **kwargs: None)
    kwargs = dict(facts={}, tasks={}, experiment_index={},
                  scientific_architecture={"components": [{"id": "model", "module": "src/model.py"}]},
                  paper={}, paper_path=paper, paper_images=[], paper_thesis=None,
                  output_dir=output, audit_dir=audit)

    def run(*, extended=False, resume=True):
        return foundation.run_codex_foundation_writer_workflow(
            **kwargs, case_runtime=_runtime(tmp_path, extended=extended), resume=resume)

    return {"run": run, "state": state, "writers": writer_calls, "tests": test_calls,
            "audit": audit, "output": output, "kwargs": kwargs}


def _pending_record(workflow):
    path, = (workflow["audit"] / "03b_foundation_environment_resume").glob("*.json")
    return path, json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("explicit_request", [False, True])
def test_environment_extension_revalidates_completed_delivery_without_a_second_writer(workflow, explicit_request):
    workflow["state"]["explicit_request"] = explicit_request
    with pytest.raises(EnvironmentRequestRequired):
        workflow["run"](resume=False)
    checkpoint, pending = _pending_record(workflow)
    assert pending["state"] == "awaiting_environment" and pending["validation"] is None
    assert len(workflow["writers"]) == 1 and workflow["tests"] == []
    assert not (workflow["output"] / "foundation_manifest.json").exists()
    saved = workflow["audit"] / "03b_foundation_writer_deliveries" / pending["writer_input_hash"]
    saved_source = (saved / "src/model.py").read_bytes()
    assert (saved / "src/model.py").read_text() == "VALUE = 7\n"

    # A still-unsatisfied environment is retried by its owner; it does not erase
    # the completed source, run tests early, or ask the Writer to regenerate it.
    with pytest.raises(EnvironmentRequestRequired):
        workflow["run"]()
    assert len(workflow["writers"]) == 1 and workflow["tests"] == []

    result = workflow["run"](extended=True)
    assert len(workflow["writers"]) == 1 and len(workflow["tests"]) == 1
    assert result["manifest"]["input_hash"] != pending["writer_input_hash"]
    assert result["manifest"]["environment_lock_hash"] == "b" * 64
    assert result["manifest"]["validation"]["tests_passed"] is True
    assert (Path(result["snapshot_dir"]) / "src/model.py").read_bytes() == saved_source
    assert validate_foundation_snapshot(result["manifest"], Path(result["snapshot_dir"])) == []
    assert json.loads(checkpoint.read_text())["state"] == "validated"


def test_environment_resume_records_the_actual_failed_test_result(workflow):
    with pytest.raises(EnvironmentRequestRequired):
        workflow["run"]()
    workflow["state"]["tests_passed"] = False
    result = workflow["run"](extended=True)
    validation = result["manifest"]["validation"]
    assert validation["tests_passed"] is False
    assert validation["tests"]["returncode"] == 1
    assert validation["tests"]["stderr"] == "observed fixture test failure"
    assert len(workflow["writers"]) == 1


@pytest.mark.parametrize("relative", ["src/model.py", "requirements.txt"])
def test_changed_saved_source_or_requirements_cannot_be_reused(workflow, relative):
    with pytest.raises(EnvironmentRequestRequired):
        workflow["run"]()
    _, pending = _pending_record(workflow)
    saved = workflow["audit"] / "03b_foundation_writer_deliveries" / pending["writer_input_hash"]
    (saved / relative).write_text("changed\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="delivery changed before environment recovery"):
        workflow["run"](extended=True)
    assert len(workflow["writers"]) == 1 and workflow["tests"] == []
    assert (saved / relative).read_text() == "changed\n"
    assert not (workflow["output"] / "foundation_manifest.json").exists()


def test_changed_scientific_input_does_not_adopt_old_environment_handoff(workflow):
    with pytest.raises(EnvironmentRequestRequired):
        workflow["run"]()
    workflow["kwargs"]["scientific_architecture"]["components"][0]["contract"] = "new requirement"
    workflow["run"](extended=True)
    assert len(workflow["writers"]) == 2
    assert len(workflow["tests"]) == 1


def test_completed_supervisor_repair_is_reused_after_environment_extension(workflow):
    workflow["kwargs"]["recovery_instructions"] = {"instructions": "repair the declared shared function"}
    with pytest.raises(EnvironmentRequestRequired):
        workflow["run"]()
    result = workflow["run"](extended=True)
    assert len(workflow["writers"]) == 1 and len(workflow["tests"]) == 1
    assert result["manifest"]["validation"]["tests_passed"] is True


def test_new_supervisor_repair_does_not_adopt_previous_repair_handoff(workflow):
    workflow["kwargs"]["recovery_instructions"] = {"instructions": "repair first issue"}
    with pytest.raises(EnvironmentRequestRequired):
        workflow["run"]()
    workflow["kwargs"]["recovery_instructions"] = {"instructions": "repair a different issue"}
    workflow["run"](extended=True)
    assert len(workflow["writers"]) == 2 and len(workflow["tests"]) == 1


def test_failed_writer_does_not_publish_an_environment_reusable_delivery(workflow):
    workflow["state"]["writer_ok"] = False
    with pytest.raises(RuntimeError, match="foundation writer failed: interrupted fixture"):
        workflow["run"]()
    assert not (workflow["audit"] / "03b_foundation_environment_resume").exists()
    assert not (workflow["audit"] / "03b_foundation_writer_deliveries").exists()
    assert workflow["tests"] == []


def test_environment_resume_keeps_delivery_immutability_checks(workflow):
    with pytest.raises(EnvironmentRequestRequired):
        workflow["run"]()
    workflow["state"]["mutate_during_validation"] = True
    with pytest.raises(RuntimeError, match="tests changed files eligible for freezing"):
        workflow["run"](extended=True)
    assert len(workflow["writers"]) == 1 and len(workflow["tests"]) == 1
    assert _pending_record(workflow)[1]["state"] == "awaiting_environment"
    assert not (workflow["output"] / "foundation_manifest.json").exists()
