"""Regression cases from the September Rayleigh run; no models or paper runs."""
from __future__ import annotations

from contextlib import redirect_stderr
import copy
import io
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from geng_agent.verification_result import (normalize_task_verification,
    verification_scientifically_successful, writer_revision_allowed)
from geng_agent.execution_receipts import ExecutionBroker, _io_path
from geng_agent.execution_client import _wait_for_result
from geng_agent.delivery_environment import export_installation
from geng_agent.task_writer_packaging import _freeze_repro_project_package
from geng_agent.report_facts import publish_terminal_facts
from geng_agent.risk_report import _build_run_cost
from geng_agent.progress import ConsoleProgressReporter
from geng_agent.portability_reference_scan import _literal_path_issues
from geng_agent.task_writer_state import _terminalize_rerun_request
from geng_agent.case_environment import subprocess_argv_runner


def decision(**changes):
    result = {
        "schema_version": "3.0", "task_id": "bpsk", "outcome": "reproduced",
        "decision_reason": "The independently checked derivation and measured BER agree.",
        "host_action": "complete", "run_valid": True,
        "core_conclusions": [{"claim_id": "ber", "status": "supported",
            "local_observation": "Agreement within sampling uncertainty", "evidence_files": ["observed.csv"]}],
        "key_numeric_comparisons": [{"target_id": "at_zero_db", "paper_magnitude": .1464,
            "local_magnitude": .1464, "regime": "gamma_bar = 1 under Equation (24)",
            "local_regime": "gamma_bar=1, equivalently average Eb/N0=0 dB under unit channel energy",
            "comparison_status": "comparable", "comparison_reason": "Equivalent under the stated normalization."}],
    }
    result.update(changes)
    return result


class ReporterDecisionTests(unittest.TestCase):
    def test_equivalent_wording_does_not_change_decision(self):
        raw = decision()
        before = copy.deepcopy(raw)
        result = normalize_task_verification(raw, "bpsk", run_valid_hint=True)
        self.assertEqual(result["outcome"], "reproduced")
        self.assertEqual(result["key_numeric_comparisons"][0]["comparison_status"], "comparable")
        self.assertEqual(result["decision_reason"], raw["decision_reason"])
        self.assertTrue(verification_scientifically_successful(result))
        self.assertEqual(raw, before)

    def test_magnitude_does_not_override_reporter_in_either_direction(self):
        for outcome, local in (("not_reproduced", .147), ("reproduced_with_assumptions", 3.0)):
            raw = decision(outcome=outcome)
            raw["key_numeric_comparisons"][0]["local_magnitude"] = local
            self.assertEqual(normalize_task_verification(raw, "bpsk", run_valid_hint=True)["outcome"], outcome)

    def test_missing_handoff_is_not_missing_paper_information(self):
        for raw in ({}, {**decision(), "schema_version": "2.0"}):
            result = normalize_task_verification(raw, "bpsk", run_valid_hint=True)
            self.assertEqual(result["engineering_status"], "handoff_failed")
            self.assertNotEqual(result["outcome"], "inconclusive_missing_information")
            self.assertEqual(result["host_action"], "complete")
            self.assertFalse(writer_revision_allowed(result, "bpsk"))
            self.assertFalse(verification_scientifically_successful(result))

    def test_engineering_failure_preserves_but_does_not_certify_reporter_decision(self):
        for hint in (False, None):
            result = normalize_task_verification(decision(), "bpsk", run_valid_hint=hint)
            self.assertEqual(result["outcome"], "reproduced")
            self.assertFalse(verification_scientifically_successful(result))

    def test_malformed_decision_fields_need_handoff_repair_without_crashing(self):
        cases = [decision(outcome={}), decision(host_action=[]), decision(run_valid="yes")]
        bad_conclusion = decision()
        bad_conclusion["core_conclusions"][0]["status"] = []
        bad_numeric = decision()
        bad_numeric["key_numeric_comparisons"][0]["comparison_status"] = {}
        for raw in [*cases, bad_conclusion, bad_numeric]:
            with self.subTest(raw=raw):
                result = normalize_task_verification(raw, "bpsk", run_valid_hint=True)
                self.assertEqual(result["engineering_status"], "handoff_failed")
                self.assertFalse(verification_scientifically_successful(result))
                self.assertFalse(writer_revision_allowed(result, "bpsk"))

    def test_host_stop_cannot_turn_reporter_pending_action_into_verified_success(self):
        verification = normalize_task_verification(decision(), "bpsk", run_valid_hint=True)
        verification.update(host_action="rerun_writer", reporter_action="rerun_writer",
                            remaining_uncertainties=["Sampling uncertainty remains."])
        stopped = _terminalize_rerun_request(record={}, verification=verification,
            stop_reason="no_progress", uncertainty="The requested correction was not completed.")
        self.assertEqual(stopped["outcome"], "reproduced")
        self.assertEqual(stopped["remaining_uncertainties"], ["Sampling uncertainty remains."])
        self.assertIn("The requested correction was not completed.", stopped["engineering_issues"])
        self.assertFalse(verification_scientifically_successful(stopped))

    def test_material_discrepancy_rerun_does_not_require_tenfold_ratio(self):
        raw = decision(outcome="not_reproduced", host_action="rerun_writer", rerun_evidence={
            "rerun_reason": "material_numeric_discrepancy", "contract_item_ids": ["at_zero_db"],
            "paper_evidence_files": ["paper_evidence/source/paper.pdf"], "causal_change": "Correct the noise variance",
            "change_targets": ["tasks/bpsk.py"], "predicted_effect": "Correct the BER under the same SNR definition"})
        result = normalize_task_verification(raw, "bpsk", run_valid_hint=True)
        self.assertTrue(writer_revision_allowed(result, "bpsk"))


class ExecutionDeliveryTests(unittest.TestCase):
    def test_isolated_environment_probe_preserves_unicode_output(self):
        result = subprocess_argv_runner([sys.executable, "-I", "-c", "print('通信论文')"], timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "通信论文")

    def test_long_unicode_result_path_uses_short_temporary_and_extended_io(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            broker = ExecutionBroker(root, root / "audit", Path(sys.executable))
            parent = root / ("通信实验" * 15) / ("结果回传" * 15) / ("参数空间" * 15)
            _io_path(parent).mkdir(parents=True)
            target = parent / ("a" * 32 + ".result.json")
            broker._publish_response(target, {"returncode": 0})
            self.assertEqual(json.loads(_io_path(target).read_text(encoding="utf-8")), {"returncode": 0})
            self.assertFalse(list(_io_path(parent).glob("*.tmp")))
            _io_path(target).unlink()
            for directory in (parent, parent.parent, parent.parent.parent):
                directory.resolve().relative_to(root.resolve())
                _io_path(directory).rmdir()

    def test_delivery_failure_recovers_same_receipt_without_execute(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            broker = ExecutionBroker(root, root / "audit", Path(sys.executable))
            broker.queue.mkdir(parents=True)
            original = broker._publish_response
            receipt = {"observer": "orchestration_host", "task_id": "bpsk", "run_id": "actual", "returncode": 0}
            def fail_result(path, value):
                if path.name.endswith(".result.json"):
                    raise FileNotFoundError("transport path failure")
                original(path, value)
            with patch.object(broker, "_publish_response", side_effect=fail_result), patch.object(broker, "execute") as execute:
                broker._deliver_receipt("bpsk", "request1", broker.queue / "request1.result.json", receipt)
                result = _wait_for_result(broker.queue, "request1", "bpsk", heartbeat_timeout=.01)
            self.assertEqual(result, receipt)
            self.assertEqual(broker.task_status["bpsk"]["state"], "completed")
            self.assertEqual(broker.task_status["bpsk"]["transport_state"], "failed")
            execute.assert_not_called()

    def test_stopped_host_and_unrelated_completion_do_not_hang_or_pass(self):
        with TemporaryDirectory() as tmp:
            queue = Path(tmp)
            (queue / "status.json").write_text(json.dumps({"broker_state": "stopped", "requests": {
                "other": {"state": "completed", "request_id": "other", "task_id": "bpsk", "receipt": {"returncode": 0}}}}))
            self.assertEqual(_wait_for_result(queue, "wanted", "bpsk")["error_kind"], "host_unavailable")
            (queue / "status.json").write_text(json.dumps({"updated_at": time.time() - 100, "broker_state": "running"}))
            self.assertEqual(_wait_for_result(queue, "wanted", "bpsk", heartbeat_timeout=.01)["error_kind"], "host_unavailable")


class PackagingTests(unittest.TestCase):
    def test_namespace_prefix_is_portable_but_concrete_windows_paths_are_not(self):
        for value in ("\\\\?\\", "\\\\?\\UNC\\"):
            self.assertEqual(_literal_path_issues("run_task.py", value, location="literal"), [])
        for value in ("\\\\?\\C:\\case\\input.csv", "\\\\?\\UNC\\server\\share\\input.csv"):
            self.assertTrue(_literal_path_issues("run_task.py", value, location="literal"))

    def test_final_freeze_preserves_dependency_closure_and_only_reuses_matching_validation(self):
        with TemporaryDirectory() as tmp:
            output = Path(tmp)
            project = output / "project"
            project.mkdir()
            (project / "requirements.txt").write_text("parentlib\n")
            (project / "environment.lock.json").write_text("{}")
            (project / "tasks_manifest.json").write_text('{"tasks":[]}')
            (project / "config.json").write_text('{}')
            (project / "run_experiment.py").write_text('print("smoke")')
            metadata = [{"distribution": "parentlib", "version": "1.0", "requires": ["childlib>=2"]},
                        {"distribution": "childlib", "version": "2.1", "requires": []}]
            with patch("geng_agent.delivery_environment.subprocess.run") as probe:
                probe.return_value.stdout = json.dumps(metadata)
                export_installation(project, python_executable=Path(sys.executable))
            original = (project / "constraints.repro.txt").read_bytes()
            self.assertIn(b"childlib==2.1", original)
            kwargs = dict(repro_project_dir=project, output_dir=output, task_manifest={"tasks": []},
                expected_paths=set(), analysis_snapshot_hash="a", foundation_snapshot_hash="f", environment_hash="e")
            with patch("geng_agent.task_writer_packaging.validate_repro_project_portability", return_value={"portable": True}), \
                 patch("geng_agent.task_writer_packaging._manifest_from_project", side_effect=lambda **k: {"_meta": {}}), \
                 patch("geng_agent.environment_rebuild.verify_clean_environment", return_value={"verified": True}):
                _freeze_repro_project_package(**kwargs, run_smoke=True, audit_path=output / "audit/03c_project_portability.json")
                (project / "config.json").write_text('{"scientific_outcomes":{"bpsk":"reproduced"}}')
                _, final = _freeze_repro_project_package(**kwargs, run_smoke=False, audit_path=output / "audit/final.json")
                self.assertTrue(final["clean_environment"]["verified"])
                self.assertEqual((project / "constraints.repro.txt").read_bytes(), original)
                (project / "run_experiment.py").write_text('raise RuntimeError("changed")')
                _, changed = _freeze_repro_project_package(**kwargs, run_smoke=False, audit_path=output / "audit/final.json")
                self.assertFalse(changed["clean_environment"]["verified"])


class ReportAndCostTests(unittest.TestCase):
    def test_report_keeps_direct_reason_and_local_image_without_paper_crop(self):
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            asset = workspace / "report_assets/bpsk/local.png"
            asset.parent.mkdir(parents=True)
            asset.write_bytes(b"image fixture")
            for name in ("review.md", "result_review.md", "reproduction_report.md"):
                (workspace / name).write_text("Existing explanation\n")
            packets = [{"task_id": "bpsk", "verification": {**decision(), "engineering_status": "verified"},
                        "local_assets": ["report_assets/bpsk/local.png"], "paper_assets": []}]
            publish_terminal_facts(workspace, packets)
            report = (workspace / "result_review.md").read_text(encoding="utf-8")
            self.assertEqual(asset.read_bytes(), b"image fixture")
            self.assertIn(decision()["decision_reason"], report)
            self.assertIn("![bpsk 本地结果展示](report_assets/bpsk/local.png)", report)
            publish_terminal_facts(workspace, packets)
            self.assertEqual(report, (workspace / "result_review.md").read_text(encoding="utf-8"))

    def test_stage_cost_includes_codex_and_unknown_usage_remains_unknown(self):
        with TemporaryDirectory() as tmp:
            audit = Path(tmp)
            directory = audit / "codex_usage_events"
            directory.mkdir()
            event = {"invocation_id": "1", "role": "task_writer", "started_at": 101, "finished_at": 105,
                     "usage_complete": True, "usage": {"prompt_tokens": 30, "completion_tokens": 5, "total_tokens": 35}}
            (directory / "1.json").write_text(json.dumps(event))
            marks = [{"stage": "start", "elapsed_s": 0}, {"stage": "generation", "elapsed_s": 10}]
            result = _build_run_cost(marks, total_wall_s=10, by_model={}, audit_dir=audit, codex_since=100)
            self.assertEqual(result["by_stage"][0]["total_tokens"], 35)
            event["usage"] = None
            (directory / "1.json").write_text(json.dumps(event))
            result = _build_run_cost(marks, total_wall_s=10, by_model={}, audit_dir=audit, codex_since=100)
            self.assertIsNone(result["by_stage"][0]["total_tokens"])

    def test_cli_progress_is_visible_on_stderr(self):
        output = io.StringIO()
        with redirect_stderr(output):
            ConsoleProgressReporter().emit("step.started", phase="task_reproduction", step="foundation")
        self.assertIn("step.started foundation", output.getvalue())
