from __future__ import annotations

import copy
import json
from pathlib import Path
import unittest

from geng_agent.verification_result import normalize_task_verification, writer_revision_allowed


class ScientificCalibrationTests(unittest.TestCase):
    def test_reference_observations_preserve_paired_materiality_decisions(self) -> None:
        root = Path(__file__).parent / "fixtures" / "scientific_calibration"
        inputs = json.loads((root / "cases.json").read_text(encoding="utf-8"))
        labels = json.loads((root / "quality_baseline.json").read_text(encoding="utf-8"))
        cases = {item["case_id"]: item for item in inputs["cases"]}
        for label in labels["tasks"]:
            case = cases[label["case_id"]]
            with self.subTest(case_id=case["case_id"]):
                raw = copy.deepcopy(label["reference_reporter_note"])
                rerun_claim_id = raw.pop("rerun_claim_id", None)
                if rerun_claim_id:
                    raw["rerun_evidence"] = {
                        "rerun_reason": "core_conclusion_failed",
                        "contract_item_ids": [rerun_claim_id],
                        "paper_evidence_files": ["paper_evidence/calibration_excerpt.txt"],
                        "causal_change": case["available_change"],
                        "change_targets": ["tasks/experiment.py"],
                        "predicted_effect": "Remove the evidenced implementation discrepancy and reassess the same core claim.",
                    }
                result = normalize_task_verification(
                    raw, case["task_id"], task=case["task"], run_valid_hint=True
                )
                self.assertEqual(result["outcome"], label["expected_outcome"])
                self.assertEqual(writer_revision_allowed(result, case["task_id"]), label["expected_rerun_allowed"])


if __name__ == "__main__":
    unittest.main()
