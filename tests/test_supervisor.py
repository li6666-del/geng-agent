from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from geng_agent.supervisor import (
    SuperviseOptions,
    build_supervisor_prompt,
    collect_case_evidence,
    heuristic_supervisor_decision,
    run_supervised_review,
)


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
                            "expected_artifacts": ["outputs/results.csv", "outputs/plot.png", "outputs/summary.json"],
                            "output_columns": ["snr_db", "bit_error_rate"],
                            "expected_trend": {
                                "x_axis": "snr_db",
                                "y_axis": "bit_error_rate",
                                "direction": "decreasing",
                                "reason": "Higher SNR should reduce BER.",
                            },
                            "comparison": {"baselines": ["AWGN"], "curve_groups": ["simulated"], "tolerance": "qualitative"},
                            "required_facts": [{"type": "channel_model", "name": "AWGN"}],
                            "assumptions": [],
                            "risk_if_unreproducible": "Core result cannot be checked.",
                        }
                    ]
                }
            )
        return json.dumps(
            {
                "files": [
                    {"path": "README.md", "content": "Run it.\n"},
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


class SupervisorTests(unittest.TestCase):
    def test_supervise_finishes_normal_review_and_writes_reflections(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paper = root / "paper.md"
            paper.write_text("Simulation Results\nAWGN channel, BER vs SNR.", encoding="utf-8")

            result = run_supervised_review(
                client=FakeLLM(),
                paper_path=paper,
                output_dir=root / "case",
                options=SuperviseOptions(run_repro=False, use_llm_reflection=False),
            )

            self.assertTrue(result.completed)
            self.assertTrue((root / "case" / "review.md").exists())
            self.assertTrue((root / "case" / "reflections" / "step_001.json").exists())
            final = json.loads((root / "case" / "reflections" / "final_reflection.json").read_text(encoding="utf-8"))
            self.assertTrue(final["completed"])
            self.assertEqual(final["final_decision"]["action"], "stop")

    def test_supervise_records_local_fallbacks_when_llm_is_unavailable(self) -> None:
        class UnavailableLLM:
            def complete(self, prompt: str, *, system: str | None = None, response_format: dict | None = None) -> str:
                raise RuntimeError("LLM request failed: TimeoutError")

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paper = root / "paper.md"
            paper.write_text("BER vs SNR over AWGN and Rayleigh channels with QPSK from 0 dB to 10 dB.", encoding="utf-8")

            result = run_supervised_review(
                client=UnavailableLLM(),
                paper_path=paper,
                output_dir=root / "case",
                options=SuperviseOptions(run_repro=True, result_review=False, json_repair_attempts=1, use_llm_reflection=False),
            )

            self.assertTrue(result.completed)
            self.assertTrue((root / "case" / "engineering_facts.json").exists())
            self.assertTrue((root / "case" / "repro_project" / "outputs" / "summary.json").exists())
            final = json.loads((root / "case" / "reflections" / "final_reflection.json").read_text(encoding="utf-8"))
            self.assertTrue(final["case_memory"]["effective_actions"]["local_stage_fallback"])
            self.assertTrue(final["case_memory"]["effective_actions"]["template_fallback"])

    def test_heuristic_asks_human_on_security_block(self) -> None:
        evidence = {
            "runtime_result": {
                "enabled": True,
                "passed": False,
                "blocked_by_security": True,
                "security_issues": [{"file": "run_experiment.py", "message": "env access"}],
            },
            "paths": {"runtime_result.json": "case/runtime_result.json"},
        }

        decision = heuristic_supervisor_decision(
            status={"next_stage": "runtime", "stages": []},
            evidence=evidence,
            stage_retries={},
            options=SuperviseOptions(run_repro=True),
        )

        self.assertEqual(decision["action"], "ask_human")
        self.assertEqual(decision["risk_level"], "high")

    def test_heuristic_uses_fallback_after_repeated_stage_failure(self) -> None:
        decision = heuristic_supervisor_decision(
            status={"next_stage": "repro_project_manifest", "stages": []},
            evidence={"paths": {}},
            stage_retries={"repro_project_manifest": 2},
            options=SuperviseOptions(max_stage_retries=2, template_fallback=True),
        )

        self.assertEqual(decision["action"], "use_fallback")

    def test_collect_case_evidence_reads_runtime_and_repair_logs(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            case = root / "case"
            logs = case / "repro_project" / "repair_logs"
            logs.mkdir(parents=True)
            (case / "runtime_result.json").write_text('{"enabled": true, "passed": false}\n', encoding="utf-8")
            (logs / "attempt_01_run.json").write_text('{"passed": false, "stderr": "boom"}\n', encoding="utf-8")

            evidence = collect_case_evidence(case, {"latest_audit": []})

            self.assertFalse(evidence["runtime_result"]["passed"])
            self.assertEqual(len(evidence["repair_logs"]), 1)

    def test_supervisor_prompt_includes_repair_backend_limits(self) -> None:
        prompt = build_supervisor_prompt(
            status={"next_stage": "runtime"},
            evidence={"runtime_result": {"enabled": True, "passed": False}},
            stage_retries={},
            options=SuperviseOptions(run_repro=True, repair_backend="openhands", openhands_timeout=123, openhands_max_iterations=4),
        )

        self.assertIn('"repair_backend": "openhands"', prompt)
        self.assertIn('"openhands_timeout": 123', prompt)
        self.assertIn('"openhands_max_iterations": 4', prompt)

    def test_normalize_unwraps_and_strips_supervisor_decision(self) -> None:
        from geng_agent.supervisor import _normalize_supervisor_decision

        wrapped = {
            "supervisor_decision": {
                "action": "stop", "target_stage": "complete", "reason": "done",
                "evidence_paths": [], "risk_level": "low", "confidence": "high",
                "extra_key": "drop me",
            }
        }
        out = _normalize_supervisor_decision(wrapped)
        self.assertEqual(out["action"], "stop")
        self.assertNotIn("extra_key", out)

    def test_llm_decision_accepts_wrapped_object(self) -> None:
        from geng_agent.supervisor import _llm_or_heuristic_decision

        class WrappingLLM:
            def complete(self, prompt: str, *, system: str | None = None, response_format: dict | None = None) -> str:
                return json.dumps(
                    {
                        "supervisor_decision": {
                            "action": "retry_stage", "target_stage": "engineering_facts",
                            "reason": "regenerate facts", "evidence_paths": [],
                            "risk_level": "medium", "confidence": "medium",
                            "prompt_adjustment": "focus on channel coding", "human_question": None,
                            "note": "stray field",
                        }
                    }
                )

        with TemporaryDirectory() as temp_dir:
            out_dir = Path(temp_dir) / "case"
            (out_dir / "reflections").mkdir(parents=True)
            decision, source = _llm_or_heuristic_decision(
                client=WrappingLLM(),
                status={"next_stage": "engineering_facts", "stages": []},
                evidence={},
                output_dir=out_dir,
                stage_retries={},
                options=SuperviseOptions(),
            )

            self.assertEqual(source, "llm")
            self.assertEqual(decision["action"], "retry_stage")
            self.assertEqual(decision.get("prompt_adjustment"), "focus on channel coding")
            self.assertFalse((out_dir / "reflections" / "supervisor_decision_error.json").exists())


if __name__ == "__main__":
    unittest.main()
