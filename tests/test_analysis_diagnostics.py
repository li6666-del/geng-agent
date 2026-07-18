from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from geng_agent.analysis_diagnostics import write_analysis_warnings
from geng_agent.schemas import ValidationIssue


class AnalysisDiagnosticsTests(unittest.TestCase):
    def test_warnings_are_advisory_aggregated_and_reset_by_first_stage(self) -> None:
        with TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            audit_dir = output_dir / "audit"
            audit_dir.mkdir()

            write_analysis_warnings(
                output_dir=output_dir,
                audit_dir=audit_dir,
                stage="01_extract_engineering_facts",
                groups={
                    "fact_source": [
                        ValidationIssue("$.engineering_facts[0]", "bad page")
                    ]
                },
            )
            aggregate = write_analysis_warnings(
                output_dir=output_dir,
                audit_dir=audit_dir,
                stage="02a_build_preliminary_repro_tasks",
                groups={
                    "task_fact_reference": [
                        ValidationIssue("$.repro_tasks[0]", "missing fact")
                    ]
                },
            )

            self.assertTrue(aggregate["advisory_only"])
            self.assertEqual(aggregate["warning_count"], 2)
            self.assertTrue((output_dir / "analysis_warnings.json").is_file())
            self.assertTrue((audit_dir / "analysis_warnings.json").is_file())

            reset = write_analysis_warnings(
                output_dir=output_dir,
                audit_dir=audit_dir,
                stage="01_extract_engineering_facts",
                groups={},
            )
            self.assertEqual(reset["warning_count"], 0)
            persisted = json.loads(
                (audit_dir / "analysis_warnings.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(persisted["warnings"], [])


if __name__ == "__main__":
    unittest.main()