from __future__ import annotations

import json
import os
import sys
import textwrap
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from geng_agent.agentic_analysis import (
    DEFAULT_CODEX_ANALYSIS_TIMEOUT,
    run_codex_json_stage,
)
from geng_agent.llm import LLMImage
from geng_agent.pipeline_helpers import _aggregate_validation_issues
from geng_agent.schemas import ValidationIssue
from geng_agent.tasks_normalize import finalize_repro_tasks


def _command_for(script: Path) -> str:
    return f'"{sys.executable}" "{script}"'


def _write_analysis_script(temp: Path, body: str) -> str:
    script = temp / "fake_codex_analysis.py"
    capability_preamble = (
        "import sys\n"
        "if sys.argv[1:] == ['exec', '--help']:\n"
        "    print('--ephemeral')\n"
        "    raise SystemExit(0)\n"
    )
    script.write_text(capability_preamble + textwrap.dedent(body), encoding="utf-8")
    return _command_for(script)


class CodexAnalysisStageTests(unittest.TestCase):
    def test_default_codex_analysis_timeout_is_1800_seconds(self) -> None:
        self.assertEqual(DEFAULT_CODEX_ANALYSIS_TIMEOUT, 1800.0)

    def test_validation_errors_are_grouped_without_dropping_late_categories(self) -> None:
        issues = [
            ValidationIssue(f"$.quantities[{index}].shape", "must be an array")
            for index in range(74)
        ] + [ValidationIssue("$.bindings[6].components", "field required")]
        grouped = json.loads(_aggregate_validation_issues(issues))
        self.assertEqual(len(grouped), 2)
        self.assertEqual(grouped[0]["path"], "$.quantities[*].shape")
        self.assertEqual(grouped[0]["count"], 74)
        self.assertEqual(grouped[1]["path"], "$.bindings[*].components")

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
            first_brief = (audit_dir / "01_extract_engineering_facts_codex_attempt_1_brief.md").read_text(encoding="utf-8")
            self.assertIn("BEGIN TRUSTED SCHEMA", first_brief)
            self.assertIn('"engineering_facts"', first_brief)
            self.assertTrue((audit_dir / "raw_01_extract_engineering_facts.txt").exists())
            self.assertTrue((audit_dir / "validation_01_extract_engineering_facts_attempt_1.json").exists())

    def test_candidate_normalizer_accepts_soft_handoff_before_schema_validation(self) -> None:
        with TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            cmd = _write_analysis_script(
                temp,
                r"""
                import json
                import sys
                from pathlib import Path

                args = sys.argv[1:]
                out = Path(args[args.index("--output-last-message") + 1])
                counter = Path(__file__).with_name("attempts.txt")
                attempt = int(counter.read_text(encoding="utf-8")) + 1 if counter.exists() else 1
                counter.write_text(str(attempt), encoding="utf-8")
                doc = {
                    "repro_tasks": [
                        {
                            "task_id": "reproduce_fig_1",
                            "target": "BER vs SNR",
                            "metric": "bit_error_rate",
                            "metric_formula": "bit_error_rate = bit_errors / total_bits",
                            "figure_or_claim": "Fig. 1",
                            "expected_artifacts": [
                                "outputs/results.csv",
                                "outputs/fig1.png",
                                "outputs/summary.json",
                            ],
                            "output_columns": ["snr_db", "bit_error_rate"],
                            "expected_trend": {
                                "x_axis": "snr_db",
                                "y_axis": "bit_error_rate",
                                "direction": "decreasing",
                                "reason": "Higher SNR reduces BER.",
                            },
                            "comparison": {
                                "baselines": [],
                                "curve_groups": [],
                                "tolerance": "qualitative trend",
                            },
                            "required_facts": [],
                            "assumptions": [],
                            "risk_if_unreproducible": "Core trend cannot be checked.",
                        }
                    ],
                    "backfill_handoff": {
                        "ready_for_writer": False,
                        "blocking_request_ids": ["reproduce_fig_1:decoder"],
                        "reason": "Decoder settings still define the experiment.",
                    },
                }
                out.write_text(json.dumps(doc), encoding="utf-8")
                """,
            )
            old_cmd = os.environ.get("GENG_CODEX_ANALYSIS_CMD")
            os.environ["GENG_CODEX_ANALYSIS_CMD"] = cmd
            try:
                out_dir = temp / "case"
                audit_dir = out_dir / "audit"
                out_dir.mkdir()
                audit_dir.mkdir()
                parsed = run_codex_json_stage(
                    prompt="Finalize tasks.",
                    stage_label="02c_finalize_repro_tasks",
                    schema_stage="repro_tasks",
                    output_dir=out_dir,
                    audit_dir=audit_dir,
                    max_attempts=2,
                    timeout=30,
                    candidate_normalizer=lambda candidate: finalize_repro_tasks(
                        candidate, {"engineering_facts": []}
                    ),
                )
            finally:
                if old_cmd is None:
                    os.environ.pop("GENG_CODEX_ANALYSIS_CMD", None)
                else:
                    os.environ["GENG_CODEX_ANALYSIS_CMD"] = old_cmd

            handoff = parsed["_meta"]["backfill_handoff"]
            self.assertFalse(handoff["ready_for_writer"])
            self.assertEqual(
                handoff["blocking_request_ids"], ["reproduce_fig_1:decoder"]
            )
            self.assertEqual((temp / "attempts.txt").read_text(encoding="utf-8"), "1")

    def test_scientific_validation_failure_does_not_trigger_format_repair(self) -> None:
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
                out.write_text(json.dumps({
                    "paper_domain": "communication",
                    "paper_repro_type": "signal_chain",
                    "engineering_facts": [],
                    "missing_information": [],
                }), encoding="utf-8")
                ''',
            )
            old_cmd = os.environ.get("GENG_CODEX_ANALYSIS_CMD")
            os.environ["GENG_CODEX_ANALYSIS_CMD"] = cmd
            try:
                out_dir = temp / "case"
                audit_dir = out_dir / "audit"
                out_dir.mkdir()
                audit_dir.mkdir()
                with self.assertRaises(RuntimeError):
                    run_codex_json_stage(
                        prompt="Extract facts.",
                        stage_label="01_science_gate",
                        schema_stage="engineering_facts",
                        output_dir=out_dir,
                        audit_dir=audit_dir,
                        max_attempts=2,
                        timeout=30,
                        extra_validation=lambda _: [ValidationIssue("$.engineering_facts", "scientific conflict")],
                    )
            finally:
                if old_cmd is None:
                    os.environ.pop("GENG_CODEX_ANALYSIS_CMD", None)
                else:
                    os.environ["GENG_CODEX_ANALYSIS_CMD"] = old_cmd

            self.assertEqual((temp / "attempts.txt").read_text(encoding="utf-8"), "1")
            self.assertTrue((audit_dir / "scientific_validation_01_science_gate_attempt_1.json").is_file())

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
                Path(__file__).with_name(f"args_{attempt}.json").write_text(
                    json.dumps(args), encoding="utf-8"
                )
                Path(__file__).with_name(f"prompt_{attempt}.txt").write_text(
                    sys.stdin.read(), encoding="utf-8"
                )
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
                    images=[
                        LLMImage(
                            label="paper_page:1",
                            mime_type="image/png",
                            data_b64="AA==",
                        )
                    ],
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
            first_args = json.loads(
                (temp / "args_1.json").read_text(encoding="utf-8")
            )
            second_args = json.loads(
                (temp / "args_2.json").read_text(encoding="utf-8")
            )
            second_prompt = (temp / "prompt_2.txt").read_text(encoding="utf-8")
            self.assertEqual(first_args.count("--image"), 1)
            self.assertNotIn("--image", second_args)
            self.assertIn("FORMAT REPAIR ONLY", second_prompt)
            self.assertIn("BEGIN UNTRUSTED CANDIDATE", second_prompt)
            self.assertIn("not json", second_prompt)
            self.assertIn("BEGIN TRUSTED SCHEMA", second_prompt)
            self.assertIn('"repro_tasks"', second_prompt)
            self.assertNotIn("Read the complete candidate from", second_prompt)
            self.assertNotIn("raw_02_build_repro_tasks_attempt_1.txt", second_prompt)
            self.assertNotIn("02_build_repro_tasks.schema.json", second_prompt)


if __name__ == "__main__":
    unittest.main()
