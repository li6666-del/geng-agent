from __future__ import annotations
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from geng_agent.benchmark_quality import assess_quality_baseline
from geng_agent.codex_cost import record_codex_invocation, summarize_codex_usage, persist_pipeline_cost
from geng_agent.risk_report import _build_run_cost
from geng_agent.delivery_environment import export_installation
from geng_agent.environment_rebuild import verify_clean_environment
from geng_agent.portability_inventory import build_source_inventory
from geng_agent.report_facts import publish_terminal_facts


class DeliveryQualityCostTests(unittest.TestCase):
    def test_pipeline_exception_keeps_cost_and_original_error(self):
        from geng_agent.pipeline import ReviewPipeline
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            error = RuntimeError("scientific stage interrupted")
            def fail_after_worker(_pipeline, context, **_kwargs):
                context.mark("start")
                record_codex_invocation(context.audit_dir, {"ok": False, "role": "writer"},
                                        "partial worker without usage", started_at=context.wall_start + 0.01)
                raise error
            with patch("geng_agent.pipeline.run_analysis_flow", side_effect=fail_after_worker):
                with self.assertRaises(RuntimeError) as caught:
                    ReviewPipeline().run(root / "paper.pdf", root / "case")
            self.assertIs(caught.exception, error)
            cost = json.loads((root / "case" / "run_cost.json").read_text())
            self.assertTrue(cost["interrupted_before_terminal_report"])
            self.assertEqual(cost["cumulative"]["codex"]["llm_calls"], 1)
            self.assertIsNone(cost["cumulative"]["totals"]["total_tokens"])

    def test_resume_cost_delta_and_cumulative_survive_stage_cleanup(self):
        from geng_agent.stage_cleanup import _clear_stage_audit
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            nested = root / "audit" / "04a_task_reporters" / "task"
            nested.mkdir(parents=True)
            usage = '{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":2}}'
            record_codex_invocation(nested, {"ok": True, "role": "reporter"}, usage, started_at=1)
            cost = _build_run_cost([{"total_tokens": 40, "llm_calls": 1}, {"total_tokens": 50, "llm_calls": 2}],
                                   total_wall_s=5, by_model={}, audit_dir=root / "audit", codex_since=0)
            persist_pipeline_cost(root, cost, run_id="one", started_at=0)
            _clear_stage_audit(root, "reports")
            second = _build_run_cost([{"total_tokens": 50, "llm_calls": 2}, {"total_tokens": 50, "llm_calls": 2}],
                                     total_wall_s=2, by_model={}, audit_dir=root / "audit", codex_since=2)
            persist_pipeline_cost(root, second, run_id="two", started_at=2)
            self.assertEqual(second["totals"]["total_tokens"], 0)
            self.assertEqual(second["cumulative"]["totals"]["total_tokens"], 22)
            self.assertEqual(second["cumulative"]["wall_clock_s"], 7)

    def test_cost_keeps_repeated_labels_and_missing_usage_unknown(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            status = {"ok": True, "role": "writer", "model": "test"}
            event = '{"type":"turn.completed","usage":{"input_tokens":100,"cached_input_tokens":40,"output_tokens":7}}'
            record_codex_invocation(root, status, event, started_at=1)
            record_codex_invocation(root, status, "no usage", started_at=3)
            total = summarize_codex_usage(root)
            self.assertEqual(total["llm_calls"], 2)
            self.assertIsNone(total["total_tokens"])
            self.assertEqual(total["observed_tokens"]["total_tokens"], 107)
            self.assertEqual(summarize_codex_usage(root, since=2)["llm_calls"], 1)

    def test_legacy_codex_usage_is_unknown_and_pipeline_event_is_idempotent(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            audit = root / "audit"
            audit.mkdir()
            (audit / "old_transcript.txt").write_text("old worker output", encoding="utf-8")
            (audit / "old.json").write_text(json.dumps({"backend": "codex", "role": "writer"}), encoding="utf-8")
            cost = _build_run_cost([], total_wall_s=2, by_model={}, audit_dir=audit, codex_since=3)
            persist_pipeline_cost(root, cost, run_id="same", started_at=3)
            persist_pipeline_cost(root, cost, run_id="same", started_at=3)
            self.assertEqual(cost["cumulative"]["pipeline_invocations"], 1)
            self.assertEqual(cost["cumulative"]["codex"]["llm_calls"], 1)
            self.assertIsNone(cost["cumulative"]["totals"]["total_tokens"])

    def test_delivery_evidence_preserves_original_bytes_and_host_receipt(self):
        from geng_agent.delivery_evidence import package_execution_evidence
        from geng_agent.execution_receipts import file_hash
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            sandbox, project = root / "writer", root / "project"
            for path in (sandbox / "tasks", project / "tasks", project / "outputs" / "a"):
                path.mkdir(parents=True)
            original = sandbox / "tasks" / "a.py"
            original.write_bytes(b"\xef\xbb\xbfVALUE = 1\r\n")
            (project / "tasks" / "a.py").write_bytes(b"VALUE = 1\n")
            receipt = {"observer": "orchestration_host", "task_id": "a", "run_id": "actual-run",
                       "source_hashes": {"tasks/a.py": file_hash(original)}}
            (project / "outputs" / "a" / "execution_receipt.json").write_text('{"run_id":"writer-forged"}')
            package_execution_evidence(project, [{"task_id": "a", "module": "a", "sandbox": str(sandbox),
                                                "host_execution": {"passed": True, "receipt": receipt}}])
            published = json.loads((project / "outputs" / "a" / "execution_receipt.json").read_text())
            self.assertEqual(published, receipt)
            evidence = json.loads((project / "execution_evidence.json").read_text())["tasks"][0]
            self.assertTrue(evidence["all_bytes_available"])
            archived = project / evidence["files"][0]["packaged_path"]
            self.assertEqual(archived.suffix, ".original")
            self.assertEqual(archived.read_bytes(), original.read_bytes())
            original.write_bytes(b"VALUE = 'changed after observation'\n")
            package_execution_evidence(project, [{"task_id": "a", "module": "a", "sandbox": str(sandbox),
                                                "host_execution": {"passed": True, "receipt": receipt}}])
            missing = json.loads((project / "execution_evidence.json").read_text())["tasks"][0]
            self.assertFalse(missing["all_bytes_available"])

    def test_installation_excludes_unrelated_host_packages_and_preserves_cuda(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "requirements.txt").write_text("torch>=2\n", encoding="utf-8")
            (root / "environment.lock.json").write_text(json.dumps({"installed_distributions": [
                {"distribution": "torch", "version": "2.11.0+cu126"},
                {"distribution": "unrelated-secret-tool", "version": "9"}]}), encoding="utf-8")
            export_installation(root)
            pins = (root / "constraints.repro.txt").read_text()
            self.assertIn("torch==2.11.0+cu126", pins)
            self.assertNotIn("unrelated", pins)
            self.assertIn("https://download.pytorch.org/whl/cu126", (root / "requirements.repro.txt").read_text())
            self.assertIn("config_smoke.json", (root / "README.md").read_text())

    def test_installation_warns_only_for_changed_observed_project_dependencies(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "requirements.txt").write_text("numpy\n", encoding="utf-8")
            (root / "environment.lock.json").write_text(json.dumps({"installed_distributions": [
                {"distribution": "numpy", "version": "2.0"}, {"distribution": "unrelated", "version": "99"}]}))
            (root / "execution_receipt.json").write_text(json.dumps({"environment_observation": {"before": {
                "inventory": {"packages": [["numpy", "1.26"], ["unrelated", "1"]]}}}}))
            (root / "execution_evidence.json").write_text(json.dumps({"tasks": [
                {"task_id": "a", "run_id": "original", "receipt": "execution_receipt.json"}]}))
            export_installation(root)
            installation = json.loads((root / "installation.json").read_text())
            mismatch = installation["observed_execution_version_mismatches"][0]
            self.assertEqual(mismatch["task_id"], "a")
            self.assertEqual(mismatch["dependencies"], [
                {"distribution": "numpy", "observed_version": "1.26", "exported_version": "2.0"}])
            evidence = json.loads((root / "execution_evidence.json").read_text())
            self.assertEqual(evidence["installation_version_mismatches"], [mismatch])
            self.assertIn("numpy 1.26 -> 2.0", (root / "README.md").read_text())

    def test_host_facts_cover_omitted_failure_and_are_idempotent(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("review.md", "reproduction_report.md", "result_review.md"):
                (root / name).write_text("# An editor omitted the failed task\n", encoding="utf-8")
            packet = {"task_id": "failed_task", "terminal_outcome": "not_reproduced",
                      "task": {"figure_or_claim": "ranking"},
                      "verification": {"core_conclusions": [{"claim_id": "ranking", "status": "unsupported"}]}}
            publish_terminal_facts(root, [packet])
            self.assertEqual(publish_terminal_facts(root, [packet]), [])
            for path in root.glob("*.md"):
                self.assertIn("未复现", path.read_text(encoding="utf-8"))
                self.assertIn("ranking: unsupported", path.read_text(encoding="utf-8"))

    def test_quality_counts_false_success_without_treating_missing_as_correct(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "quality_baseline.json").write_text(json.dumps({"schema_version": "1.0", "tasks": [
                {"task_id": "bad", "expected_outcome": "not_reproduced", "paper_family": "coding"},
                {"task_id": "missing", "expected_outcome": "reproduced"}]}), encoding="utf-8")
            (root / "quality_results.json").write_text(json.dumps({"tasks": [
                {"task_id": "bad", "outcome": "reproduced"}]}), encoding="utf-8")
            result = assess_quality_baseline(root)
            self.assertEqual(result["false_success"], 1)
            self.assertEqual(result["unassessed"], 1)

    def test_real_clean_venv_smoke_and_cached_environment(self):
        # No package download: this exercises a real isolated venv and project run.
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            (project / "requirements.txt").write_text("# stdlib-only fixture\n", encoding="utf-8")
            (project / "run_experiment.py").write_text(
                "import sys,site\nassert sys.prefix != sys.base_prefix\nassert not site.ENABLE_USER_SITE\nprint('clean smoke')\n", encoding="utf-8")
            (project / "reproducibility_manifest.json").write_text(json.dumps({
                "smoke_command": ["python", "run_experiment.py", "config_smoke.json"]}), encoding="utf-8")
            (project / "config_smoke.json").write_text('{"smoke":true}', encoding="utf-8")
            export_installation(project)
            (project / "source_inventory.json").write_text(json.dumps(build_source_inventory(project)), encoding="utf-8")
            first = verify_clean_environment(project, cache_dir=root / "cache", python_executable=sys.executable)
            self.assertTrue(first["verified"], first)
            self.assertFalse(first["environment_reused"])
            second = verify_clean_environment(project, cache_dir=root / "cache", python_executable=sys.executable)
            self.assertTrue(second["verified"], second)
            self.assertTrue(second["environment_reused"])


if __name__ == "__main__":
    unittest.main()
