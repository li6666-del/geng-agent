"""Offline regressions for final package metadata; no scientific execution."""

from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from geng_agent.agentic_task_writers import (
    _final_package_file_validation,
    _refresh_cached_package_validation,
    run_codex_task_writer_workflow,
)
from geng_agent.agentic_report_editor import run_codex_report_editor_workflow
from geng_agent.outputs import write_json
from geng_agent.task_reporter_context import _task_reporter_input_hash
from tests.test_agentic_report_editor import _workflow_inputs
from tests.test_agentic_task_writers import _delivery, _pair


def _stale_validation() -> dict:
    return {
        "required_files_present": False,
        "missing_files": ["source_inventory.json"],
        "python_compiles": None,
        "host_validation_skipped": True,
        "portable": True,
        "observations": [
            {"code": "declared_package_file_missing", "path": "source_inventory.json"},
            {"code": "independent_observation", "message": "keep me"},
        ],
    }


def _writer_inputs(root: Path) -> tuple[dict, dict]:
    output = root / "case"
    audit = output / "audit"
    paper = root / "paper.pdf"
    paper.write_bytes(b"offline fixture")
    task, _entry = _pair("task_1")
    tasks = {"repro_tasks": [task]}
    write_json(output / "engineering_facts.json", {"engineering_facts": []})
    write_json(output / "repro_tasks.json", tasks)
    write_json(output / "experiment_index.json", {"experiments": []})
    record = _delivery("task_1", audit / "03c_task_writer_sandboxes" / "01_task_1")
    record.update({"index": 1, "writer_session_count": 1})
    return {
        "facts": {"engineering_facts": []}, "tasks": tasks,
        "experiment_index": {"experiments": []}, "paper": {"chunks": []},
        "paper_path": paper, "paper_context_json": "", "paper_images": [],
        "paper_thesis": None, "output_dir": output, "audit_dir": audit,
        "repro_project_dir": output / "repro_project", "run_repro": True,
        "resume": True,
    }, record


class FinalPackageValidationTests(unittest.TestCase):
    def test_only_resolved_missing_observations_are_removed(self) -> None:
        with TemporaryDirectory() as temp:
            project = Path(temp)
            (project / "source_inventory.json").write_text("{}", encoding="utf-8")
            old = _stale_validation()
            old["missing_files"].append("still_missing.csv")
            old["observations"].append({
                "code": "declared_package_file_missing", "path": "still_missing.csv",
                "message": "preserve original detail",
            })
            original = deepcopy(old)
            result = _final_package_file_validation(
                repro_project_dir=project,
                expected_paths={"source_inventory.json", "new_missing.json"},
                validation=old,
            )
            self.assertEqual(old, original)
            self.assertFalse(result["required_files_present"])
            self.assertEqual(result["missing_files"], ["new_missing.json", "still_missing.csv"])
            self.assertIn(old["observations"][1], result["observations"])
            self.assertIn(old["observations"][2], result["observations"])
            self.assertNotIn(old["observations"][0], result["observations"])
            self.assertTrue(result["host_validation_skipped"])
            self.assertIsNone(result["python_compiles"])

    def test_directory_or_outside_path_does_not_count_as_a_present_file(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            project = root / "project"
            project.mkdir()
            (project / "directory.csv").mkdir()
            (root / "outside.csv").write_text("0", encoding="utf-8")
            result = _final_package_file_validation(
                repro_project_dir=project,
                expected_paths={"directory.csv", "../outside.csv"}, validation={},
            )
            self.assertFalse(result["required_files_present"])
            self.assertEqual(result["missing_files"], ["../outside.csv", "directory.csv"])

    def test_cached_refresh_synchronizes_metadata_and_preserves_scientific_results(self) -> None:
        with TemporaryDirectory() as temp:
            output = Path(temp)
            audit = output / "audit"
            project = output / "repro_project"
            project.mkdir()
            (project / "source_inventory.json").write_text("{}", encoding="utf-8")
            old = _stale_validation()
            cached = {
                "manifest": {"files": [{"path": "source_inventory.json"}]},
                "runtime_result": {"passed": True, "scientific_outcome_counts": {
                    "reproduced_with_assumptions": 1, "not_reproduced": 1,
                }, "validation": old},
                "status": {"cached": True},
            }
            write_json(audit / "03c_task_writers_status.json", {"stop_class": "terminal", "validation": old})
            portability = {"portable": True, "smoke": {"ran": False},
                           "execution_evidence": {"tasks_with_host_receipts": 2},
                           "issues": [], "warnings": [], "observations": old["observations"]}
            for name in ("03c_project_portability.json", "03c_project_portability_final.json"):
                write_json(audit / name, portability)

            result = _refresh_cached_package_validation(
                cached=cached, repro_project_dir=project, output_dir=output, audit_dir=audit,
            )
            self.assertTrue(result["runtime_result"]["validation"]["required_files_present"])
            self.assertEqual(result["runtime_result"]["scientific_outcome_counts"], {
                "reproduced_with_assumptions": 1, "not_reproduced": 1,
            })
            self.assertTrue(result["runtime_result"]["passed"])
            on_disk = json.loads((output / "runtime_result.json").read_text(encoding="utf-8"))
            status = json.loads((audit / "03c_task_writers_status.json").read_text(encoding="utf-8"))
            self.assertEqual(on_disk, result["runtime_result"])
            self.assertEqual(status["validation"], on_disk["validation"])
            self.assertEqual(status["stop_class"], "terminal")
            for name in ("03c_project_portability.json", "03c_project_portability_final.json"):
                refreshed = json.loads((audit / name).read_text(encoding="utf-8"))
                self.assertEqual(refreshed["observations"], [old["observations"][1]])
                for key in ("portable", "smoke", "execution_evidence", "issues", "warnings"):
                    self.assertEqual(refreshed[key], portability[key])

    def test_both_cached_returns_refresh_without_writer_dispatch_or_model_call(self) -> None:
        for with_reporter in (False, True):
            with self.subTest(with_reporter=with_reporter), TemporaryDirectory() as temp:
                inputs, record = _writer_inputs(Path(temp))
                project = inputs["repro_project_dir"]
                project.mkdir()
                (project / "source_inventory.json").write_text("{}", encoding="utf-8")
                cached = {
                    "manifest": {"_meta": {"backend": "codex", "mode": "task_writers"},
                                 "files": [{"path": "source_inventory.json"}]},
                    "runtime_result": {"passed": True, "validation": _stale_validation()},
                    "task_records": [record], "written_files": [], "status": {"cached": True},
                }
                reporter_calls = []
                reporter_hash_inputs = {
                    "task": inputs["tasks"]["repro_tasks"][0], "task_record": record,
                    "paper_path": inputs["paper_path"], "facts": inputs["facts"],
                    "experiment_index": inputs["experiment_index"], "paper_thesis": None,
                    "figure_candidates": [], "paper": inputs["paper"],
                    "output_dir": inputs["output_dir"],
                }
                reporter_hash_before = _task_reporter_input_hash(**reporter_hash_inputs)

                def cached_reporter(index, task, task_record, round_no):
                    reporter_calls.append(task_record["task_id"])
                    return {"ok": True, "cached": True, "task_id": "task_1", "task_verification": {
                        "schema_version": "2.0", "task_id": "task_1", "outcome": "not_reproduced",
                        "host_action": "complete", "rerun_reason": "none", "run_valid": True,
                    }}

                with patch("geng_agent.agentic_task_writers._load_cached_task_writer_workflow", return_value=cached), \
                     patch("geng_agent.agentic_task_writers._load_task_writer_resume_records", return_value={1: record}), \
                     patch("geng_agent.agentic_task_writers._dispatch_task_writers") as dispatch, \
                     patch("geng_agent.task_writer_runner.run_codex_subprocess", side_effect=AssertionError("Writer called")), \
                     patch("geng_agent.agentic_task_reporters.run_codex_subprocess", side_effect=AssertionError("Reporter called")):
                    result = run_codex_task_writer_workflow(
                        **inputs, task_review_callback=cached_reporter if with_reporter else None,
                    )
                dispatch.assert_not_called()
                self.assertEqual(reporter_calls, ["task_1"] if with_reporter else [])
                self.assertEqual(reporter_hash_before, _task_reporter_input_hash(**reporter_hash_inputs))
                self.assertTrue(result["runtime_result"]["validation"]["required_files_present"])
                saved = json.loads((inputs["output_dir"] / "runtime_result.json").read_text(encoding="utf-8"))
                self.assertEqual(saved["validation"], result["status"]["validation"])
                if with_reporter:
                    self.assertTrue(result["task_records"][0]["task_reporter"]["cached"])
                    self.assertEqual(result["task_records"][0]["task_verification"]["outcome"], "not_reproduced")

    def test_fresh_assembly_checks_files_after_freeze(self) -> None:
        for create_required in (True, False):
            with self.subTest(create_required=create_required), TemporaryDirectory() as temp:
                inputs, record = _writer_inputs(Path(temp))
                inputs["resume"] = False

                def merge(**kwargs):
                    project = kwargs["repro_project_dir"]
                    project.mkdir(parents=True, exist_ok=True)
                    (project / "run_experiment.py").write_text("pass\n", encoding="utf-8")
                    return {"run_experiment.py", "source_inventory.json"}

                def freeze(**kwargs):
                    project = kwargs["repro_project_dir"]
                    self.assertFalse((project / "source_inventory.json").exists())
                    self.assertEqual(kwargs["contextual_findings"], [])
                    if create_required:
                        (project / "source_inventory.json").write_text('{"files": []}', encoding="utf-8")
                    return {"files": [{"path": "run_experiment.py"}]}, {"portable": True, "smoke": {"ran": False}}

                with patch("geng_agent.agentic_task_writers._load_cached_task_writer_workflow", return_value=None), \
                     patch("geng_agent.agentic_task_writers._clear_stage_outputs"), \
                     patch("geng_agent.agentic_task_writers._dispatch_task_writers", return_value=([record], {})), \
                     patch("geng_agent.agentic_task_writers._prepare_project_workspace"), \
                     patch("geng_agent.agentic_task_writers._restore_trusted_files"), \
                     patch("geng_agent.agentic_task_writers._merge_task_writer_deliveries", side_effect=merge), \
                     patch("geng_agent.agentic_task_writers._freeze_repro_project_package", side_effect=freeze):
                    result = run_codex_task_writer_workflow(**inputs)
                validation = result["runtime_result"]["validation"]
                self.assertEqual(validation["required_files_present"], create_required)
                self.assertEqual(validation["missing_files"], [] if create_required else ["source_inventory.json"])

    def test_validation_only_refresh_does_not_invalidate_editor_cache(self) -> None:
        with TemporaryDirectory() as temp:
            output = Path(temp) / "case"
            inputs = _workflow_inputs(output)
            inputs["runtime_result"]["validation"] = _stale_validation()
            portability_path = output / "audit" / "03c_project_portability_final.json"
            portability = {"portable": True, "smoke": {"ran": False},
                           "observations": _stale_validation()["observations"]}
            write_json(portability_path, portability)

            def fake_editor(**kwargs):
                for name in ("reproduction_report.md", "result_review.md"):
                    (kwargs["work_dir"] / name).write_text("# 测试报告\n\n结论保留不变。\n", encoding="utf-8")
                return {"ok": True}

            with patch("geng_agent.agentic_report_editor.run_codex_subprocess", side_effect=fake_editor) as model:
                first = run_codex_report_editor_workflow(**inputs)
                self.assertTrue(first["ok"], first)
                inputs["runtime_result"]["validation"] = {"required_files_present": True, "missing_files": []}
                inputs["risk_report"] = {"findings": []}
                inputs["resume"] = True
                write_json(portability_path, {**portability, "observations": []})
                second = run_codex_report_editor_workflow(**inputs)
            self.assertTrue(second["cached"], second)
            self.assertEqual(model.call_count, 1)
            self.assertEqual(first["input_hash"], second["input_hash"])


if __name__ == "__main__":
    unittest.main()
