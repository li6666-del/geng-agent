"""Independent negative-path checks for observed scientific executions."""
from __future__ import annotations

import json
import py_compile
from pathlib import Path
import sys
import time
from tests.test_execution_sandbox import native_sandbox_temporary_directory as TemporaryDirectory
import unittest
from unittest.mock import patch

from geng_agent.execution_receipts import ExecutionBroker, validate_receipt


class ExecutionReceiptIndependentTests(unittest.TestCase):
    def _project(self, root: Path, body: str):
        project = root / "project"
        (project / "tasks").mkdir(parents=True)
        (project / "tasks" / "__init__.py").write_text("", encoding="utf-8")
        (project / "tasks" / "sample.py").write_text(body, encoding="utf-8")
        (project / "config.json").write_text('{"run_profile":"full"}', encoding="utf-8")
        (project / "config_smoke.json").write_text('{"run_profile":"smoke","smoke":true}', encoding="utf-8")
        (project / "tasks_manifest.json").write_text(json.dumps({"tasks": [{"task_id": "sample",
            "module": "sample", "output_subdir": "sample", "config_full": "config.json",
            "config_smoke": "config_smoke.json"}]}), encoding="utf-8")
        return project, ExecutionBroker(project, root / "audit", Path(sys.executable))

    def test_zero_exit_without_new_artifacts_cannot_certify_old_csv(self):
        with TemporaryDirectory() as temporary:
            project, broker = self._project(Path(temporary), "def main(config):\n    return None\n")
            old = project / "outputs" / "sample"
            old.mkdir(parents=True)
            (old / "result.csv").write_text("value\n123\n", encoding="utf-8")
            receipt = broker.execute({"task_id": "sample", "mode": "full"})
            self.assertFalse(validate_receipt(project, receipt, task_id="sample")["passed"])

    def test_deterministic_rewrite_is_a_valid_new_execution(self):
        with TemporaryDirectory() as temporary:
            project, broker = self._project(Path(temporary), "from pathlib import Path\ndef main(config):\n    Path('outputs/sample/result.csv').write_text('value\\n123\\n')\n")
            first = broker.execute({"task_id": "sample", "mode": "full"})
            self.assertEqual(first["returncode"], 0, first)
            self.assertTrue(validate_receipt(project, first, task_id="sample")["passed"], first)
            second = broker.execute({"task_id": "sample", "mode": "full"})
            self.assertTrue(validate_receipt(project, second, task_id="sample")["passed"], second)
            self.assertNotEqual(first["run_id"], second["run_id"])

    def test_smoke_configuration_cannot_claim_full_evidence(self):
        with TemporaryDirectory() as temporary:
            project, broker = self._project(Path(temporary), "from pathlib import Path\ndef main(config):\n    Path('outputs/sample/result.csv').write_text('smoke only')\n")
            try:
                receipt = broker.execute({"task_id": "sample", "mode": "full", "config": "config_smoke.json"})
            except ValueError:
                return
            self.assertFalse(validate_receipt(project, receipt, task_id="sample")["passed"])

    def test_nonzero_main_return_is_not_a_successful_process(self):
        with TemporaryDirectory() as temporary:
            project, broker = self._project(Path(temporary), "from pathlib import Path\ndef main(config):\n    Path('outputs/sample/result.csv').write_text('partial')\n    return 1\n")
            receipt = broker.execute({"task_id": "sample", "mode": "full"})
            self.assertFalse(validate_receipt(project, receipt, task_id="sample")["passed"])

    def test_new_measurements_are_rejected_but_presentation_does_not_rerun_science(self):
        with TemporaryDirectory() as temporary:
            project, broker = self._project(Path(temporary), "from pathlib import Path\ndef main(config):\n    Path('outputs/sample/raw.csv').write_text('value\\n123\\n')\n")
            receipt = broker.execute({"task_id": "sample", "mode": "full"})
            output = project / "outputs" / "sample"
            (output / "explanation.md").write_text("A report about the measured value.")
            (output / "figure.png").write_bytes(b"presentation bytes")
            checked = validate_receipt(project, receipt, task_id="sample")
            self.assertTrue(checked["passed"], checked)
            self.assertEqual(checked["unobserved_artifacts"], ["outputs/sample/figure.png"])
            (output / "results.csv").write_text("value\n999\n")
            checked = validate_receipt(project, receipt, task_id="sample")
            self.assertFalse(checked["passed"])
            self.assertTrue(any("added outside" in issue for issue in checked["issues"]))

    def test_stderr_capture_does_not_hide_consumed_checkpoint(self):
        with TemporaryDirectory() as temporary:
            project, broker = self._project(Path(temporary), "from pathlib import Path\nimport contextlib,io\ndef main(config):\n    with contextlib.redirect_stderr(io.StringIO()):\n        value=Path('execution_units/model.json').read_text()\n    Path('outputs/sample/result.csv').write_text(value)\n")
            (project / "execution_units").mkdir()
            checkpoint = project / "execution_units" / "model.json"
            checkpoint.write_text('{"weight":1}', encoding="utf-8")
            receipt = broker.execute({"task_id": "sample", "mode": "full"})
            self.assertEqual(receipt["returncode"], 0, receipt)
            checkpoint.write_text('{"weight":2}', encoding="utf-8")
            self.assertFalse(validate_receipt(project, receipt, task_id="sample")["passed"])

    def test_cached_bytecode_does_not_remove_source_binding(self):
        with TemporaryDirectory() as temporary:
            project, broker = self._project(Path(temporary), "from pathlib import Path\ndef main(config):\n    Path('outputs/sample/result.csv').write_text('original')\n")
            source = project / "tasks" / "sample.py"
            py_compile.compile(str(source), doraise=True)
            receipt = broker.execute({"task_id": "sample", "mode": "full"})
            self.assertEqual(receipt["returncode"], 0, receipt)
            source.write_text("def main(config):\n    raise RuntimeError('changed science')\n", encoding="utf-8")
            self.assertFalse(validate_receipt(project, receipt, task_id="sample")["passed"])

    def test_package_change_during_run_cannot_claim_stable_execution(self):
        with TemporaryDirectory() as temporary:
            project, broker = self._project(Path(temporary), "from pathlib import Path\ndef main(config):\n    Path('outputs/sample/result.csv').write_text('completed')\n")
            before = {"ok": True, "sha256": "before", "duration_s": 0.01}
            after = {"ok": True, "sha256": "after", "duration_s": 0.02}
            with patch("geng_agent.execution_receipts.probe_execution_environment", side_effect=[before, after]):
                receipt = broker.execute({"task_id": "sample", "mode": "full"})
            self.assertEqual(receipt["returncode"], 0)
            self.assertFalse(receipt["environment_observation"]["stable"])
            self.assertEqual(receipt["environment_observation"]["probe_duration_s"], 0.03)
            self.assertFalse(validate_receipt(project, receipt, task_id="sample")["passed"])

    def test_cancellation_stops_actual_science_child(self):
        with TemporaryDirectory() as temporary:
            project, broker = self._project(Path(temporary), "from pathlib import Path\nimport time\ndef main(config):\n    Path('outputs/sample/started.json').write_text('{}')\n    time.sleep(60)\n    Path('outputs/sample/finished.csv').write_text('finished')\n")
            with self.assertRaisesRegex(RuntimeError, "cancel current execution"):
                with broker:
                    (broker.queue / "cancel.request.json").write_text(json.dumps({"task_id": "sample", "mode": "full"}))
                    deadline = time.monotonic() + 20
                    while not (project / "outputs/sample/started.json").exists() and time.monotonic() < deadline:
                        time.sleep(0.05)
                    self.assertTrue((project / "outputs/sample/started.json").exists(), "science child did not start")
                    raise RuntimeError("cancel current execution")
            self.assertFalse((project / "outputs/sample/finished.csv").exists())
            self.assertEqual(len(broker.receipts), 1)
            self.assertTrue(broker.receipts[0]["cancelled"])
            self.assertNotEqual(broker.receipts[0]["returncode"], 0)


if __name__ == "__main__":
    unittest.main()
