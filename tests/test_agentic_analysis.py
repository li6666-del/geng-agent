from __future__ import annotations

import json
import os
import sys
import textwrap
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from geng_agent.agentic_analysis import run_codex_json_stage


def _command_for(script: Path) -> str:
    return f'"{sys.executable}" "{script}"'


def _write_analysis_script(temp: Path, body: str) -> str:
    script = temp / "fake_codex_analysis.py"
    script.write_text(textwrap.dedent(body), encoding="utf-8")
    return _command_for(script)


class CodexAnalysisStageTests(unittest.TestCase):
    def test_codex_analysis_stage_returns_validated_json_and_audit(self) -> None:
        with TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            cmd = _write_analysis_script(
                temp,
                r'''
                import json
                import sys
                from pathlib import Path

                args = sys.argv[1:]
                if "--output-schema" in args:
                    raise SystemExit("analysis backend should rely on harness validation, not strict CLI schema")
                out = Path(args[args.index("--output-last-message") + 1])
                doc = {
                    "paper_domain": "communication",
                    "paper_repro_type": "signal_chain",
                    "engineering_facts": [
                        {
                            "type": "channel_model",
                            "name": "AWGN",
                            "value": {},
                            "source": {
                                "source_kind": "text",
                                "chunk_id": "c1",
                                "page": 1,
                                "section": "Simulation",
                                "quote": "AWGN channel",
                                "figure_ref": "",
                            },
                            "confidence": "high",
                            "used_for_reproduction": True,
                        }
                    ],
                    "missing_information": [],
                }
                out.write_text(json.dumps(doc), encoding="utf-8")
                ''',
            )
            old_cmd = os.environ.get("GENG_CODEX_ANALYSIS_CMD")
            os.environ["GENG_CODEX_ANALYSIS_CMD"] = cmd
            try:
                out_dir = temp / "case"
                audit_dir = out_dir / "audit"
                out_dir.mkdir()
                audit_dir.mkdir()
                parsed = run_codex_json_stage(
                    prompt="Extract facts.",
                    stage_label="01_extract_engineering_facts",
                    schema_stage="engineering_facts",
                    output_dir=out_dir,
                    audit_dir=audit_dir,
                    max_attempts=1,
                    timeout=30,
                )
            finally:
                if old_cmd is None:
                    os.environ.pop("GENG_CODEX_ANALYSIS_CMD", None)
                else:
                    os.environ["GENG_CODEX_ANALYSIS_CMD"] = old_cmd

            self.assertEqual(parsed["engineering_facts"][0]["name"], "AWGN")
            self.assertEqual(parsed["_meta"]["analysis_backend"], "codex")
            self.assertTrue((audit_dir / "01_extract_engineering_facts.schema.json").exists())
            self.assertTrue((audit_dir / "raw_01_extract_engineering_facts.txt").exists())
            self.assertTrue((audit_dir / "validation_01_extract_engineering_facts_attempt_1.json").exists())

    def test_codex_analysis_stage_retries_bad_json(self) -> None:
        with TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            cmd = _write_analysis_script(
                temp,
                r'''
                import json
                import sys
                from pathlib import Path

                args = sys.argv[1:]
                out = Path(args[args.index("--output-last-message") + 1])
                counter = Path(__file__).with_name("attempts.txt")
                attempt = int(counter.read_text(encoding="utf-8")) + 1 if counter.exists() else 1
                counter.write_text(str(attempt), encoding="utf-8")
                if attempt == 1:
                    out.write_text("not json", encoding="utf-8")
                else:
                    doc = {
                        "repro_tasks": [
                            {
                                "task_id": "reproduce_fig_1",
                                "target": "BER vs SNR",
                                "metric": "bit_error_rate",
                                "metric_formula": "bit_error_rate = bit_errors / total_bits",
                                "figure_or_claim": "Fig. 1",
                                "expected_artifacts": ["outputs/results.csv", "outputs/fig1.png", "outputs/summary.json"],
                                "output_columns": ["snr_db", "bit_error_rate"],
                                "expected_trend": {
                                    "x_axis": "snr_db",
                                    "y_axis": "bit_error_rate",
                                    "direction": "decreasing",
                                    "reason": "Higher SNR reduces BER.",
                                },
                                "comparison": {"baselines": [], "curve_groups": [], "tolerance": "qualitative trend"},
                                "required_facts": [],
                                "assumptions": [],
                                "risk_if_unreproducible": "Core trend cannot be checked.",
                            }
                        ]
                    }
                    out.write_text(json.dumps(doc), encoding="utf-8")
                ''',
            )
            old_cmd = os.environ.get("GENG_CODEX_ANALYSIS_CMD")
            os.environ["GENG_CODEX_ANALYSIS_CMD"] = cmd
            try:
                out_dir = temp / "case"
                audit_dir = out_dir / "audit"
                out_dir.mkdir()
                audit_dir.mkdir()
                parsed = run_codex_json_stage(
                    prompt="Build tasks.",
                    stage_label="02_build_repro_tasks",
                    schema_stage="repro_tasks",
                    output_dir=out_dir,
                    audit_dir=audit_dir,
                    max_attempts=2,
                    timeout=30,
                )
            finally:
                if old_cmd is None:
                    os.environ.pop("GENG_CODEX_ANALYSIS_CMD", None)
                else:
                    os.environ["GENG_CODEX_ANALYSIS_CMD"] = old_cmd

            self.assertEqual(parsed["repro_tasks"][0]["task_id"], "reproduce_fig_1")
            first_validation = json.loads(
                (audit_dir / "validation_02_build_repro_tasks_attempt_1.json").read_text(encoding="utf-8")
            )
            second_validation = json.loads(
                (audit_dir / "validation_02_build_repro_tasks_attempt_2.json").read_text(encoding="utf-8")
            )
            self.assertFalse(first_validation["ok"])
            self.assertTrue(second_validation["ok"])


if __name__ == "__main__":
    unittest.main()
