from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from geng_agent.pipeline import (
    ReviewPipeline,
    _assess_partial_success,
    _clear_project_code_files,
    _validate_project_file,
    normalize_repro_project_manifest_candidate,
)
from geng_agent.schemas import validate_stage


PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="

PROJECT_FILE_CONTENTS = {
    "README.md": "Run `python run_experiment.py`.\n",
    "requirements.txt": "numpy\nmatplotlib\n",
    "config.json": "{\"snr_db\": [0, 2, 4], \"assumptions\": []}\n",
    "config_smoke.json": "{\"snr_db\": [0], \"assumptions\": []}\n",
    "run_experiment.py": (
        "from pathlib import Path\n"
        "Path('outputs').mkdir(exist_ok=True)\n"
        "Path('outputs/results.csv').write_text('snr_db,ber\\n0,0.1\\n', encoding='utf-8')\n"
    ),
    "src/channel.py": "def apply_channel(x):\n    return x\n",
    "src/modulation.py": "def modulate(bits):\n    return bits\n",
    "src/metrics.py": "def ber(a, b):\n    return 0.0\n",
    "src/simulation.py": "def run(config):\n    return []\n",
}


def schema_name(response_format: dict | None) -> str | None:
    if not isinstance(response_format, dict):
        return None
    schema = response_format.get("json_schema")
    if not isinstance(schema, dict):
        return None
    name = schema.get("name")
    return name if isinstance(name, str) else None


def target_path_from_prompt(prompt: str) -> str:
    lines = prompt.splitlines()
    for index, line in enumerate(lines[:-1]):
        if line.strip() == "目标文件：":
            return lines[index + 1].strip()
    raise AssertionError("target path not found in prompt")


def project_plan_response() -> str:
    return json.dumps(
        {
            "implementation_strategy": "Small deterministic AWGN BER smoke project.",
            "assumptions": ["Smoke-scale synthetic data."],
            "files": [
                {"path": path, "purpose": f"Generate {path}.", "key_interfaces": []}
                for path in PROJECT_FILE_CONTENTS
            ],
        }
    )


def project_file_response(prompt: str, *, overrides: dict[str, str] | None = None) -> str:
    target = target_path_from_prompt(prompt)
    contents = {**PROJECT_FILE_CONTENTS, **(overrides or {})}
    return json.dumps({"path": target, "content_lines": contents[target].splitlines()})


def project_manifest_response(*, overrides: dict[str, str] | None = None) -> str:
    contents = {**PROJECT_FILE_CONTENTS, **(overrides or {})}
    return json.dumps({"files": [{"path": path, "content": content} for path, content in contents.items()]})


class FakeLLM:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def complete(self, prompt: str, *, system: str | None = None, response_format: dict | None = None) -> str:
        self.calls.append(prompt)
        if len(self.calls) == 1:
            return json.dumps(
                {
                    "paper_domain": "communication",
                    "paper_repro_type": "signal_chain",
                    "engineering_facts": [
                        {
                            "type": "channel_model",
                            "name": "AWGN",
                            "value": {"snr_db": [0, 2, 4]},
                            "source": {"chunk_id": "text_c1", "page": None, "section": "Simulation", "quote": "AWGN"},
                            "confidence": "high",
                            "used_for_reproduction": True,
                        }
                    ],
                    "missing_information": [],
                }
            )
        if len(self.calls) == 2:
            return json.dumps(
                {
                    "repro_tasks": [
                        {
                            "task_id": "reproduce_fig_1",
                            "target": "BER vs SNR",
                            "metric": "bit_error_rate",
                            "metric_formula": "bit_error_rate = bit_errors / total_bits",
                            "figure_or_claim": "Fig. 1",
                            "expected_artifacts": [
                                "outputs/results.csv",
                                "outputs/ber_vs_snr.png",
                                "outputs/summary.json",
                            ],
                            "output_columns": ["snr_db", "bit_error_rate"],
                            "expected_trend": {
                                "x_axis": "snr_db",
                                "y_axis": "bit_error_rate",
                                "direction": "decreasing",
                                "reason": "Higher SNR should reduce BER in AWGN.",
                            },
                            "comparison": {
                                "baselines": ["AWGN reference"],
                                "curve_groups": ["simulated"],
                                "tolerance": "qualitative trend",
                            },
                            "required_facts": [{"type": "channel_model", "name": "AWGN"}],
                            "assumptions": [],
                            "risk_if_unreproducible": "Core curve cannot be checked.",
                        }
                    ]
                }
            )
        if schema_name(response_format) == "repro_project_plan":
            return project_plan_response()
        if schema_name(response_format) == "repro_project_file":
            return project_file_response(prompt)
        return project_manifest_response()


class PipelineTests(unittest.TestCase):
    def test_normalizes_manifest_with_extra_descriptive_fields(self) -> None:
        manifest = {
            "schema_version": "not part of schema",
            "files": [
                {"path": "README.md", "role": "docs", "content": "Run it.\n"},
                {"path": "requirements.txt", "role": "deps", "content": "\n"},
                {"path": "config.json", "role": "config", "content": "{}\n"},
                {"path": "config_smoke.json", "role": "config", "content": "{}\n"},
                {"path": "run_experiment.py", "role": "entry", "content": "print('ok')\n"},
                {"path": "src/channel.py", "role": "module", "content": "\n"},
                {"path": "src/modulation.py", "role": "module", "content": "\n"},
                {"path": "src/metrics.py", "role": "module", "content": "\n"},
                {"path": "src/simulation.py", "role": "module", "content": "\n"},
            ],
            "verdict": "extra prose",
        }

        normalized = normalize_repro_project_manifest_candidate(manifest)

        self.assertFalse(validate_stage("repro_project_manifest", normalized))
        self.assertEqual(set(normalized["files"][0]), {"path", "content"})
        self.assertTrue(normalized["_meta"]["manifest_normalized"])

    def test_code_files_have_no_size_limit(self) -> None:
        # User decision: generated CODE (.py) files carry no line/char cap (a too-tight cap
        # was the main template-fallback trigger). They must still compile.
        for path in ("src/simulation.py", "run_experiment.py", "src/metrics.py", "src/channel.py"):
            issues = _validate_project_file(
                {"path": path, "content_lines": ["print('x')"] * 2000},
                path,
            )
            self.assertFalse(
                any("lines for" in issue.message or "characters for" in issue.message for issue in issues),
                f"{path} should have no size limit",
            )

    def test_non_code_files_keep_size_limit(self) -> None:
        # Non-code files (README / config / requirements) keep their caps.
        issues = _validate_project_file(
            {"path": "README.md", "content_lines": ["x"] * 201},
            "README.md",
        )
        self.assertTrue(any("200 lines" in issue.message for issue in issues))

    def test_clear_project_code_files_removes_orphans_keeps_scratch(self) -> None:
        # Bug B: a template fallback must atomically replace the project; orphan code from an
        # earlier generation must not survive, but outputs/ and repair_logs/ are preserved.
        with TemporaryDirectory() as tmp:
            proj = Path(tmp) / "repro_project"
            (proj / "src").mkdir(parents=True)
            (proj / "src" / "orphan.py").write_text("x = 1\n", encoding="utf-8")
            (proj / "run_experiment.py").write_text("print(1)\n", encoding="utf-8")
            (proj / "outputs").mkdir()
            (proj / "outputs" / "results.csv").write_text("a\n", encoding="utf-8")
            (proj / "repair_logs").mkdir()
            (proj / "repair_logs" / "attempt_01_run.json").write_text("{}", encoding="utf-8")

            _clear_project_code_files(proj)

            self.assertFalse((proj / "src").exists())
            self.assertFalse((proj / "run_experiment.py").exists())
            self.assertTrue((proj / "outputs" / "results.csv").exists())
            self.assertTrue((proj / "repair_logs" / "attempt_01_run.json").exists())

    def test_project_file_rejects_prose_in_python_file(self) -> None:
        # The exact failure that sank arXiv 2603.29359: the model returned a prose review
        # instead of code for src/simulation.py. It passed per-file structural checks, then
        # failed the project-level compile check -> whole project discarded for a template.
        issues = _validate_project_file(
            {
                "path": "src/simulation.py",
                "content_lines": [
                    "Review of reproducibility risks for the provided paper.",
                    "Key risks include: 1. Mathematical model implementation.",
                ],
            },
            "src/simulation.py",
        )
        self.assertTrue(any("not valid Python" in issue.message for issue in issues))

    def test_project_file_accepts_valid_python(self) -> None:
        issues = _validate_project_file(
            {
                "path": "src/simulation.py",
                "content_lines": ["import numpy as np", "", "def run():", "    return np.zeros(3)"],
            },
            "src/simulation.py",
        )
        self.assertFalse(any("not valid Python" in issue.message for issue in issues))

    def test_project_file_rejects_malformed_json(self) -> None:
        issues = _validate_project_file(
            {"path": "config.json", "content_lines": ["this is not json at all"]},
            "config.json",
        )
        self.assertTrue(any("not valid JSON" in issue.message for issue in issues))

    def test_project_file_accepts_valid_json(self) -> None:
        issues = _validate_project_file(
            {"path": "config.json", "content_lines": ["{", '  "bits": 1000', "}"]},
            "config.json",
        )
        self.assertFalse(any("not valid JSON" in issue.message for issue in issues))

    def test_generated_files_context_keeps_full_content(self) -> None:
        import json as _json

        from geng_agent.pipeline import _generated_files_context

        long_lines = ["filler_%03d = %d  # %s" % (i, i, "a" * 40) for i in range(40)] + ["UNIQUE_TAIL_MARKER = 1"]
        ctx = _json.loads(_generated_files_context([{"path": "src/channel.py", "content_lines": long_lines}]))
        self.assertEqual(ctx[0]["path"], "src/channel.py")
        self.assertIn("content", ctx[0])
        self.assertNotIn("content_preview", ctx[0])
        # Full content, no 1200-char truncation: a marker well past char 1200 survives.
        self.assertGreater(len(ctx[0]["content"]), 1500)
        self.assertIn("UNIQUE_TAIL_MARKER", ctx[0]["content"])

    def test_per_file_review_grounding_keeps_only_target_file_findings(self) -> None:
        from geng_agent.code_review import _ground_findings_against

        facts = {"engineering_facts": [{"value": "ber equals q function of sqrt two ebn0"}]}
        tasks = {"repro_tasks": []}
        code = "def ber(x):\n    return special_q_function(x)\n"
        grounded = {"spec_ref": "ber", "evidence_spec": "ber equals q function", "evidence_code": "special_q_function", "severity": "blocking"}
        ungrounded = {"spec_ref": "ber", "evidence_spec": "ber equals q function", "evidence_code": "absent_symbol_xyz", "severity": "blocking"}
        kept, dropped = _ground_findings_against([grounded, ungrounded], facts, tasks, code)
        self.assertEqual(len(kept), 1)
        self.assertIs(kept[0], grounded)
        self.assertEqual(len(dropped), 1)

    def test_per_file_revise_rounds_default_is_three(self) -> None:
        from geng_agent.pipeline import PER_FILE_REVIEW_REVISE_ROUNDS

        self.assertEqual(PER_FILE_REVIEW_REVISE_ROUNDS, 3)

    def test_code_review_snapshot_restore_roundtrip(self) -> None:
        from geng_agent.code_review import _restore_project, _snapshot_project

        with TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            proj = base / "proj"
            (proj / "src").mkdir(parents=True)
            (proj / "run_experiment.py").write_text("print('best')\n", encoding="utf-8")
            (proj / "src" / "metrics.py").write_text("X = 1\n", encoding="utf-8")
            (proj / "outputs").mkdir()
            (proj / "outputs" / "junk.csv").write_text("a\n", encoding="utf-8")

            snap = _snapshot_project(proj, base / "snap")
            self.assertFalse((snap / "outputs").exists())  # outputs/ ignored in snapshot

            # simulate a regressing revise, then restore the best-reviewed snapshot
            (proj / "run_experiment.py").write_text("print('worse-regressed')\n", encoding="utf-8")
            _restore_project(snap, proj)
            self.assertEqual((proj / "run_experiment.py").read_text(encoding="utf-8"), "print('best')\n")
            self.assertEqual((proj / "src" / "metrics.py").read_text(encoding="utf-8"), "X = 1\n")

    def test_pipeline_creates_review_bundle(self) -> None:
        with TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            paper = temp / "paper.md"
            paper.write_text("Simulation Results\nAWGN channel, BER vs SNR.", encoding="utf-8")

            fake = FakeLLM()
            result = ReviewPipeline(client=fake).run(paper, temp / "case", run_repro=False)

            self.assertTrue(result.review_path.exists())
            self.assertIsNotNone(result.review_docx_path)
            self.assertTrue((result.output_dir / "review.docx").exists())
            self.assertFalse((result.output_dir / "result_review.docx").exists())
            self.assertTrue((result.repro_project_dir / "run_experiment.py").exists())
            self.assertTrue((result.output_dir / "engineering_facts.json").exists())
            self.assertTrue((result.output_dir / "experiment_index.json").exists())
            self.assertIsNotNone(result.experiment_index_path)
            self.assertTrue((result.output_dir / "audit" / "01_extract_engineering_facts.md").exists())
            generated = json.loads((result.output_dir / "generated_files.json").read_text(encoding="utf-8"))
            manifest = json.loads((result.output_dir / "repro_project_manifest.json").read_text(encoding="utf-8"))
            risk_report = json.loads((result.output_dir / "risk_report.json").read_text(encoding="utf-8"))
            self.assertTrue(generated["docx_generation"]["review_docx"]["passed"])
            self.assertIn("experiment_index", generated)
            self.assertEqual(risk_report["reproducibility_verdict"]["verdict"], "inconclusive")
            self.assertEqual(result.reproducibility_verdict["verdict"], "inconclusive")
            self.assertTrue(manifest["_meta"]["chunked_generation_used"])
            self.assertTrue((result.output_dir / "audit" / "03a_generate_repro_project_plan.json").exists())
            self.assertTrue(any((result.output_dir / "audit").glob("partial_03b_generate_repro_project_file_*.json")))
            self.assertEqual(len(fake.calls), 12)

    def test_pipeline_run_stage_regenerates_reports_from_cached_artifacts(self) -> None:
        with TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            paper = temp / "paper.md"
            paper.write_text("Simulation Results\nAWGN channel, BER vs SNR.", encoding="utf-8")

            fake = FakeLLM()
            pipeline = ReviewPipeline(client=fake)
            result = pipeline.run(paper, temp / "case", run_repro=False)
            call_count_after_first_run = len(fake.calls)
            result.review_path.unlink()

            rerun = pipeline.run_stage("reports", paper, temp / "case", run_repro=False)

            self.assertTrue(rerun.review_path.exists())
            self.assertEqual(len(fake.calls), call_count_after_first_run)

    def test_pipeline_retries_invalid_json_shape(self) -> None:
        class RetryLLM(FakeLLM):
            def complete(self, prompt: str, *, system: str | None = None, response_format: dict | None = None) -> str:
                self.calls.append(prompt)
                if len(self.calls) == 1:
                    return json.dumps({"engineering_facts": "not-a-list", "missing_information": []})
                if len(self.calls) == 2:
                    return json.dumps(
                        {
                            "paper_domain": "communication",
                            "paper_repro_type": "signal_chain",
                            "engineering_facts": [
                                {
                                    "type": "channel_model",
                                    "name": "AWGN",
                                    "value": {},
                                    "source": {"chunk_id": "text_c1", "page": None, "section": "", "quote": "AWGN"},
                                    "confidence": "high",
                                    "used_for_reproduction": True,
                                }
                            ],
                            "missing_information": [],
                        }
                    )
                if len(self.calls) == 3:
                    return json.dumps(
                        {
                            "repro_tasks": [
                                {
                                    "task_id": "reproduce_fig_1",
                                    "target": "BER vs SNR",
                                    "metric": "bit_error_rate",
                                    "metric_formula": "bit_error_rate = bit_errors / total_bits",
                                    "figure_or_claim": "Fig. 1",
                                    "expected_artifacts": ["outputs/results.csv", "outputs/plot.png", "outputs/summary.json"],
                                    "output_columns": ["snr_db", "bit_error_rate"],
                                    "expected_trend": {
                                        "x_axis": "snr_db",
                                        "y_axis": "bit_error_rate",
                                        "direction": "decreasing",
                                        "reason": "Higher SNR should reduce BER in AWGN.",
                                    },
                                    "comparison": {
                                        "baselines": ["AWGN reference"],
                                        "curve_groups": ["simulated"],
                                        "tolerance": "qualitative trend",
                                    },
                                    "required_facts": [{"type": "channel_model", "name": "AWGN"}],
                                    "assumptions": [],
                                    "risk_if_unreproducible": "Core curve cannot be checked.",
                                }
                            ]
                        }
                    )
                return json.dumps(
                    {
                        "files": [
                            {"path": "README.md", "content": "Run `python run_experiment.py`.\n"},
                            {"path": "requirements.txt", "content": "\n"},
                            {"path": "config.json", "content": "{}\n"},
                            {"path": "config_smoke.json", "content": "{}\n"},
                            {"path": "run_experiment.py", "content": "print('ok')\n"},
                            {"path": "src/channel.py", "content": "\n"},
                            {"path": "src/modulation.py", "content": "\n"},
                            {"path": "src/metrics.py", "content": "\n"},
                            {"path": "src/simulation.py", "content": "\n"},
                        ]
                    }
                )

        with TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            paper = temp / "paper.md"
            paper.write_text("Simulation Results\nAWGN channel.", encoding="utf-8")

            fake = RetryLLM()
            result = ReviewPipeline(client=fake).run(paper, temp / "case", run_repro=False)

            self.assertTrue(result.review_path.exists())
            self.assertIsNotNone(result.review_docx_path)
            self.assertGreaterEqual(len(fake.calls), 4)
            self.assertTrue((result.output_dir / "audit" / "validation_01_extract_engineering_facts_attempt_1.json").exists())

    def test_pipeline_normalizes_near_miss_facts_without_falling_back(self) -> None:
        class NearMissFactsLLM(FakeLLM):
            def complete(self, prompt: str, *, system: str | None = None, response_format: dict | None = None) -> str:
                if schema_name(response_format) == "engineering_facts":
                    self.calls.append(prompt)
                    return json.dumps(
                        {
                            "paper_domain": "communication",
                            "paper_repro_type": "phy_simulation",
                            "engineering_facts": [
                                {
                                    "type": "parameter",
                                    "name": "AWGN",
                                    "value": {"snr_db": [0, 2, 4]},
                                    "source": {"chunk_id": "text_c1", "page": None, "section": None, "quote": "AWGN"},
                                    "confidence": "high",
                                    "used_for_reproduction": True,
                                    "explanation": "extra key the model added",
                                }
                            ],
                            "missing_information": [],
                        }
                    )
                return super().complete(prompt, system=system, response_format=response_format)

        with TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            paper = temp / "paper.md"
            paper.write_text("Simulation Results\nAWGN channel, BER vs SNR.", encoding="utf-8")

            fake = NearMissFactsLLM()
            result = ReviewPipeline(client=fake).run(paper, temp / "case", run_repro=False)

            facts = json.loads((result.output_dir / "engineering_facts.json").read_text(encoding="utf-8"))
            self.assertEqual(facts["engineering_facts"][0]["type"], "simulation_parameter")
            self.assertEqual(facts["paper_repro_type"], "signal_chain")
            self.assertEqual(facts["engineering_facts"][0]["source"]["section"], "")
            self.assertNotIn("explanation", facts["engineering_facts"][0])
            self.assertTrue(facts["_meta"]["normalization_used"])
            self.assertFalse((result.output_dir / "audit" / "local_fallback_01_extract_engineering_facts.json").exists())
            risk_report = json.loads((result.output_dir / "risk_report.json").read_text(encoding="utf-8"))
            self.assertTrue(any(item["type"] == "facts_normalized" for item in risk_report["findings"]))

    def test_pipeline_salvages_truncated_facts_without_falling_back(self) -> None:
        class TruncatedFactsLLM(FakeLLM):
            def complete(self, prompt: str, *, system: str | None = None, response_format: dict | None = None) -> str:
                if schema_name(response_format) == "engineering_facts":
                    self.calls.append(prompt)
                    first = json.dumps(
                        {
                            "type": "channel_model",
                            "name": "AWGN",
                            "value": {},
                            "source": {"chunk_id": "text_c1", "page": None, "section": "", "quote": "AWGN channel"},
                            "confidence": "high",
                            "used_for_reproduction": True,
                        }
                    )
                    return (
                        '{"paper_domain":"communication","paper_repro_type":"signal_chain",'
                        '"engineering_facts":[' + first + ',{"type":"metric","name":"BER","va'
                    )
                return super().complete(prompt, system=system, response_format=response_format)

        with TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            paper = temp / "paper.md"
            paper.write_text("Simulation Results\nAWGN channel, BER vs SNR.", encoding="utf-8")

            fake = TruncatedFactsLLM()
            result = ReviewPipeline(client=fake).run(paper, temp / "case", run_repro=False)

            facts = json.loads((result.output_dir / "engineering_facts.json").read_text(encoding="utf-8"))
            self.assertEqual(len(facts["engineering_facts"]), 1)
            self.assertEqual(facts["engineering_facts"][0]["name"], "AWGN")
            self.assertTrue(facts["_meta"]["truncation_recovered"])
            self.assertFalse((result.output_dir / "audit" / "local_fallback_01_extract_engineering_facts.json").exists())
            risk_report = json.loads((result.output_dir / "risk_report.json").read_text(encoding="utf-8"))
            self.assertTrue(any(item["type"] == "facts_truncation_recovered" for item in risk_report["findings"]))

    def test_pipeline_retries_transient_llm_request_error(self) -> None:
        class TransientErrorLLM(FakeLLM):
            def __init__(self) -> None:
                super().__init__()
                self.failed_once = False

            def complete(self, prompt: str, *, system: str | None = None, response_format: dict | None = None) -> str:
                if not self.failed_once:
                    self.failed_once = True
                    raise RuntimeError("LLM request failed: HTTP 500: temporary upstream error")
                return super().complete(prompt, system=system, response_format=response_format)

        with TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            paper = temp / "paper.md"
            paper.write_text("Simulation Results\nAWGN channel, BER vs SNR.", encoding="utf-8")

            fake = TransientErrorLLM()
            result = ReviewPipeline(client=fake).run(paper, temp / "case", run_repro=False)

            self.assertTrue(result.review_path.exists())
            self.assertTrue((result.output_dir / "audit" / "llm_error_01_extract_engineering_facts_attempt_1.json").exists())
            self.assertTrue((result.output_dir / "engineering_facts.json").exists())

    def test_pipeline_uses_template_fallback_when_generate_project_times_out(self) -> None:
        class GenerateProjectTimeoutLLM(FakeLLM):
            def complete(self, prompt: str, *, system: str | None = None, response_format: dict | None = None) -> str:
                if len(self.calls) < 2:
                    return super().complete(prompt, system=system, response_format=response_format)
                self.calls.append(prompt)
                raise RuntimeError("LLM request failed: TimeoutError: The read operation timed out")

        with TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            paper = temp / "paper.md"
            paper.write_text("Simulation Results\nAWGN channel, BER vs SNR.", encoding="utf-8")
            output_dir = temp / "case"

            first = GenerateProjectTimeoutLLM()
            result = ReviewPipeline(client=first).run(paper, output_dir, run_repro=False)

            self.assertTrue(result.review_path.exists())
            self.assertTrue((output_dir / "engineering_facts.json").exists())
            self.assertTrue((output_dir / "repro_tasks.json").exists())
            self.assertTrue((output_dir / "repro_project" / "run_experiment.py").exists())
            self.assertTrue((output_dir / "audit" / "template_fallback_03_generate_repro_project.json").exists())

            manifest = json.loads((output_dir / "repro_project_manifest.json").read_text(encoding="utf-8"))
            self.assertTrue(manifest["_meta"]["template_fallback_used"])

    def test_code_review_is_skipped_on_template_fallback(self) -> None:
        # Bug C: a template fallback must not be reviewed/revised by the whole-project code
        # review (doing so regenerates code over the template and desyncs disk/manifest/flag).
        class GenerateProjectTimeoutLLM(FakeLLM):
            def complete(self, prompt: str, *, system: str | None = None, response_format: dict | None = None) -> str:
                if len(self.calls) < 2:
                    return super().complete(prompt, system=system, response_format=response_format)
                self.calls.append(prompt)
                raise RuntimeError("LLM request failed: TimeoutError: The read operation timed out")

        with TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            paper = temp / "paper.md"
            paper.write_text("Simulation Results\nAWGN channel, BER vs SNR.", encoding="utf-8")
            output_dir = temp / "case"

            ReviewPipeline(client=GenerateProjectTimeoutLLM()).run(
                paper, output_dir, run_repro=False, code_review=True
            )

            manifest = json.loads((output_dir / "repro_project_manifest.json").read_text(encoding="utf-8"))
            self.assertTrue(manifest["_meta"]["template_fallback_used"])
            code_review = json.loads((output_dir / "code_review.json").read_text(encoding="utf-8"))
            self.assertFalse(code_review["enabled"])
            self.assertIn("template fallback", code_review["reason"])

    def test_pipeline_uses_template_fallback_when_runtime_repairs_fail(self) -> None:
        class BrokenProjectLLM(FakeLLM):
            def complete(self, prompt: str, *, system: str | None = None, response_format: dict | None = None) -> str:
                if len(self.calls) < 2:
                    return super().complete(prompt, system=system, response_format=response_format)
                self.calls.append(prompt)
                overrides = {"run_experiment.py": "raise RuntimeError('still broken')\n"}
                if schema_name(response_format) == "repro_project_plan":
                    return project_plan_response()
                if schema_name(response_format) == "repro_project_file":
                    return project_file_response(prompt, overrides=overrides)
                return project_manifest_response(overrides=overrides)

        with TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            paper = temp / "paper.md"
            paper.write_text("Simulation Results\nAWGN channel, BER vs SNR.", encoding="utf-8")

            fake = BrokenProjectLLM()
            result = ReviewPipeline(client=fake).run(
                paper,
                temp / "case",
                run_repro=True,
                run_timeout=10,
                repair_attempts=0,
                result_review=False,
            )

            self.assertTrue(result.runtime_passed)
            manifest = json.loads((result.output_dir / "repro_project_manifest.json").read_text(encoding="utf-8"))
            self.assertTrue(manifest["_meta"]["template_fallback_used"])
            runtime = json.loads((result.output_dir / "runtime_result.json").read_text(encoding="utf-8"))
            self.assertTrue(runtime["passed"])
            self.assertTrue(runtime["template_fallback_used"])
            self.assertTrue((result.repro_project_dir / "outputs" / "results.csv").exists())
            # The failed generated-project run is preserved before the template overwrote it.
            self.assertTrue((result.output_dir / "runtime_result_pre_fallback.json").exists())
            pre = json.loads((result.output_dir / "runtime_result_pre_fallback.json").read_text(encoding="utf-8"))
            self.assertIsNot(pre.get("passed"), True)

    def test_pipeline_can_finish_with_local_fallbacks_when_llm_is_unavailable(self) -> None:
        class UnavailableLLM:
            def __init__(self) -> None:
                self.calls = 0

            def complete(self, prompt: str, *, system: str | None = None, response_format: dict | None = None) -> str:
                self.calls += 1
                raise RuntimeError("LLM request failed: TimeoutError: The read operation timed out")

        with TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            paper = temp / "paper.md"
            paper.write_text(
                "Simulation Results\nBER vs SNR over AWGN and Rayleigh channels with QPSK and 16-QAM from 0 dB to 20 dB.",
                encoding="utf-8",
            )

            fake = UnavailableLLM()
            result = ReviewPipeline(client=fake).run(
                paper,
                temp / "case",
                run_repro=True,
                run_timeout=10,
                repair_attempts=0,
                json_repair_attempts=1,
                result_review=False,
            )

            self.assertTrue(result.runtime_passed)
            facts = json.loads((result.output_dir / "engineering_facts.json").read_text(encoding="utf-8"))
            tasks = json.loads((result.output_dir / "repro_tasks.json").read_text(encoding="utf-8"))
            manifest = json.loads((result.output_dir / "repro_project_manifest.json").read_text(encoding="utf-8"))
            risk_report = json.loads((result.output_dir / "risk_report.json").read_text(encoding="utf-8"))

            self.assertTrue(facts["_meta"]["local_fallback_used"])
            self.assertTrue(tasks["_meta"]["local_fallback_used"])
            self.assertTrue(manifest["_meta"]["template_fallback_used"])
            self.assertTrue((result.output_dir / "audit" / "local_fallback_01_extract_engineering_facts.json").exists())
            self.assertTrue((result.output_dir / "audit" / "local_fallback_02_build_repro_tasks.json").exists())
            self.assertTrue((result.output_dir / "audit" / "local_02b_build_experiment_index.json").exists())
            self.assertTrue((result.output_dir / "audit" / "template_fallback_03_generate_repro_project.json").exists())
            self.assertTrue((result.repro_project_dir / "outputs" / "summary.json").exists())
            self.assertTrue(any(item["type"] == "local_stage_fallback_used" for item in risk_report["findings"]))

    def test_pipeline_runs_result_review_after_successful_repro(self) -> None:
        class ResultReviewLLM(FakeLLM):
            def __init__(self) -> None:
                super().__init__()
                self.multimodal_calls: list[dict] = []

            def complete(self, prompt: str, *, system: str | None = None, response_format: dict | None = None) -> str:
                if len(self.calls) < 2:
                    return super().complete(prompt, system=system, response_format=response_format)
                self.calls.append(prompt)
                overrides = {
                    # No third-party deps so the guarded runner executes in any interpreter.
                    "requirements.txt": "\n",
                    "run_experiment.py": (
                        "import base64\n"
                        "from pathlib import Path\n"
                        f"PNG_B64 = '{PNG_B64}'\n"
                        "Path('outputs').mkdir(exist_ok=True)\n"
                        "Path('outputs/results.csv').write_text('snr_db,bit_error_rate\\n0,0.1\\n2,0.05\\n', encoding='utf-8')\n"
                        "Path('outputs/ber_vs_snr.png').write_bytes(base64.b64decode(PNG_B64))\n"
                        "Path('outputs/summary.json').write_text('{\"task_id\":\"reproduce_fig_1\",\"metrics\":{\"ber0\":0.1},\"assumptions\":[]}', encoding='utf-8')\n"
                    ),
                }
                if schema_name(response_format) == "repro_project_plan":
                    return project_plan_response()
                if schema_name(response_format) == "repro_project_file":
                    return project_file_response(prompt, overrides=overrides)
                return project_manifest_response(overrides=overrides)

            def complete_multimodal(self, prompt: str, *, images: list, system: str | None = None, response_format: dict | None = None) -> str:
                self.multimodal_calls.append({"prompt": prompt, "images": images, "response_format": response_format})
                return json.dumps(
                    {
                        "overall_result_credibility": "medium",
                        "overall_alignment": "partial_match",
                        "experiment_reviews": [
                            {
                                "task_id": "reproduce_fig_1",
                                "local_result_credibility": "medium",
                                "paper_alignment": "partial_match",
                                "paper_result_summary": "Paper reports BER decreasing with SNR.",
                                "local_result_summary": "Local CSV shows BER decreasing from 0.1 to 0.05.",
                                "differences": ["Only smoke-scale points were generated."],
                                "possible_causes": ["Smoke config uses fewer samples than the paper."],
                                "evidence": ["outputs/results.csv", "local_output:ber_vs_snr.png"],
                                "limitations": ["No precise figure digitization was performed."],
                                "confidence": "medium",
                            }
                        ],
                        "cross_experiment_findings": ["Smoke result has the expected qualitative trend."],
                        "recommended_human_checks": ["Run full config.json before final judgement."],
                        "note": "This is a reproducibility-risk review, not a fraud judgement.",
                    }
                )

        with TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            paper = temp / "paper.md"
            paper.write_text("Simulation Results\nAWGN channel, BER vs SNR.", encoding="utf-8")

            fake = ResultReviewLLM()
            result = ReviewPipeline(client=fake).run(paper, temp / "case", run_repro=True, run_timeout=10)

            self.assertTrue(result.runtime_passed)
            self.assertTrue(result.result_review_passed)
            self.assertEqual(len(fake.multimodal_calls), 1)
            self.assertTrue((result.output_dir / "result_review.json").exists())
            self.assertTrue((result.output_dir / "result_review.md").exists())
            self.assertTrue((result.output_dir / "review.docx").exists())
            self.assertTrue((result.output_dir / "result_review.docx").exists())
            risk_report = json.loads((result.output_dir / "risk_report.json").read_text(encoding="utf-8"))
            # Genuine (non-template) generation + partial_match review -> mostly_reproduced.
            # (Previously this fake fell back to a template, which capped the verdict to
            # partially_reproduced; P0-1 now reviews only real generated projects.)
            self.assertEqual(risk_report["reproducibility_verdict"]["verdict"], "mostly_reproduced")
            self.assertIsNotNone(result.review_docx_path)
            self.assertIsNotNone(result.result_review_docx_path)
            self.assertTrue((result.output_dir / "audit" / "04_review_reproduction_results.md").exists())
            run_cost = json.loads((result.output_dir / "run_cost.json").read_text(encoding="utf-8"))
            self.assertIn("wall_clock_s", run_cost)
            self.assertIn("by_stage", run_cost)
            self.assertTrue(any(stage["stage"] == "result_review" for stage in run_cost["by_stage"]))

    def test_pipeline_writes_result_review_error_when_multimodal_fails(self) -> None:
        class FailingVisionLLM(FakeLLM):
            def complete(self, prompt: str, *, system: str | None = None, response_format: dict | None = None) -> str:
                if len(self.calls) < 2:
                    return super().complete(prompt, system=system, response_format=response_format)
                self.calls.append(prompt)
                overrides = {
                    "requirements.txt": "\n",
                    "run_experiment.py": (
                        "import base64\n"
                        "from pathlib import Path\n"
                        f"PNG_B64 = '{PNG_B64}'\n"
                        "Path('outputs').mkdir(exist_ok=True)\n"
                        "Path('outputs/results.csv').write_text('snr_db,bit_error_rate\\n0,0.1\\n', encoding='utf-8')\n"
                        "Path('outputs/ber_vs_snr.png').write_bytes(base64.b64decode(PNG_B64))\n"
                        "Path('outputs/summary.json').write_text('{\"task_id\":\"reproduce_fig_1\",\"metrics\":{\"ber0\":0.1},\"assumptions\":[]}', encoding='utf-8')\n"
                    ),
                }
                if schema_name(response_format) == "repro_project_plan":
                    return project_plan_response()
                if schema_name(response_format) == "repro_project_file":
                    return project_file_response(prompt, overrides=overrides)
                return project_manifest_response(overrides=overrides)

            def complete_multimodal(self, prompt: str, *, images: list, system: str | None = None, response_format: dict | None = None) -> str:
                raise RuntimeError("LLM request failed: HTTP 400: image input unsupported")

        with TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            paper = temp / "paper.md"
            paper.write_text("Simulation Results\nAWGN channel, BER vs SNR.", encoding="utf-8")

            fake = FailingVisionLLM()
            result = ReviewPipeline(client=fake).run(paper, temp / "case", run_repro=True, run_timeout=10)

            self.assertTrue(result.runtime_passed)
            self.assertFalse(result.result_review_passed)
            self.assertTrue((result.output_dir / "result_review_error.json").exists())
            self.assertFalse((result.output_dir / "result_review.json").exists())
            self.assertTrue((result.output_dir / "review.docx").exists())
            self.assertFalse((result.output_dir / "result_review.docx").exists())

    def test_docx_generation_failure_does_not_abort_pipeline(self) -> None:
        with TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            paper = temp / "paper.md"
            paper.write_text("Simulation Results\nAWGN channel, BER vs SNR.", encoding="utf-8")

            fake = FakeLLM()
            with patch("geng_agent.docx_writer.write_review_docx", side_effect=RuntimeError("docx write failed")):
                result = ReviewPipeline(client=fake).run(paper, temp / "case", run_repro=False)

            self.assertTrue(result.review_path.exists())
            self.assertIsNone(result.review_docx_path)
            self.assertTrue((result.output_dir / "docx_generation_error.json").exists())
            risk_report = json.loads((result.output_dir / "risk_report.json").read_text(encoding="utf-8"))
            self.assertFalse(risk_report["docx_generation"]["review_docx"]["passed"])

    def test_pipeline_keeps_partial_output_instead_of_template_fallback(self) -> None:
        class PartialOutputLLM(FakeLLM):
            def complete(self, prompt: str, *, system: str | None = None, response_format: dict | None = None) -> str:
                if len(self.calls) < 2:
                    return super().complete(prompt, system=system, response_format=response_format)
                self.calls.append(prompt)
                overrides = {
                    # Declare no third-party deps so the guarded runner executes the
                    # project in any interpreter (the test only needs stdlib pathlib).
                    "requirements.txt": "\n",
                    "run_experiment.py": (
                        "from pathlib import Path\n"
                        "Path('outputs').mkdir(exist_ok=True)\n"
                        "Path('outputs/results.csv').write_text('snr_db,bit_error_rate\\n0,0.1\\n2,0.05\\n', encoding='utf-8')\n"
                        "raise RuntimeError('crashed after writing partial output')\n"
                    )
                }
                if schema_name(response_format) == "repro_project_plan":
                    return project_plan_response()
                if schema_name(response_format) == "repro_project_file":
                    return project_file_response(prompt, overrides=overrides)
                return project_manifest_response(overrides=overrides)

        with TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            paper = temp / "paper.md"
            paper.write_text("Simulation Results\nAWGN channel, BER vs SNR.", encoding="utf-8")

            fake = PartialOutputLLM()
            result = ReviewPipeline(client=fake).run(
                paper,
                temp / "case",
                run_repro=True,
                run_timeout=10,
                repair_attempts=0,
                result_review=False,
            )

            # A single failed experiment must NOT trigger a wholesale template fallback
            # when the generated project produced usable partial output.
            self.assertFalse(result.runtime_passed)
            manifest = json.loads((result.output_dir / "repro_project_manifest.json").read_text(encoding="utf-8"))
            self.assertIsNot(manifest.get("_meta", {}).get("template_fallback_used"), True)
            self.assertFalse((result.output_dir / "audit" / "template_fallback_03_generate_repro_project.json").exists())

            runtime = json.loads((result.output_dir / "runtime_result.json").read_text(encoding="utf-8"))
            self.assertIsNot(runtime.get("passed"), True)
            self.assertTrue(runtime["template_fallback_skipped"])
            self.assertTrue(runtime["partial_success"]["has_partial_output"])
            self.assertIn("results.csv", runtime["partial_success"]["valid_csv_files"])

            # The generated (non-template) project is preserved on disk.
            self.assertIn("crashed after writing partial output", (result.repro_project_dir / "run_experiment.py").read_text(encoding="utf-8"))
            self.assertTrue((result.repro_project_dir / "outputs" / "results.csv").exists())

            risk_report = json.loads((result.output_dir / "risk_report.json").read_text(encoding="utf-8"))
            self.assertTrue(any(item["type"] == "generated_project_partial_success" for item in risk_report["findings"]))


class AssessPartialSuccessTests(unittest.TestCase):
    def test_partial_when_valid_csv_present(self) -> None:
        out = _assess_partial_success({"artifacts": {"csv_files": ["results.csv"], "png_files": []}})
        self.assertTrue(out["has_partial_output"])
        self.assertEqual(out["valid_csv_files"], ["results.csv"])

    def test_no_partial_without_csv(self) -> None:
        self.assertFalse(_assess_partial_success({"artifacts": {"csv_files": []}})["has_partial_output"])
        self.assertFalse(_assess_partial_success({})["has_partial_output"])
        self.assertFalse(_assess_partial_success({"artifacts": None})["has_partial_output"])


if __name__ == "__main__":
    unittest.main()
