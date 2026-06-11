import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from geng_agent.pipeline import (
    ReviewPipeline,
    _available_src_symbols,
    _per_task_file_override,
    _per_task_src_override,
)


def _schema_name(response_format) -> str | None:
    if not isinstance(response_format, dict):
        return None
    schema = response_format.get("json_schema")
    return schema.get("name") if isinstance(schema, dict) else None


def _target_path(prompt: str) -> str:
    lines = prompt.splitlines()
    for index, line in enumerate(lines[:-1]):
        if line.strip() == "目标文件：":
            return lines[index + 1].strip()
    raise AssertionError("target path not found in prompt")


TASK_DRIVER = (
    "import json\n"
    "import sys\n"
    "from pathlib import Path\n"
    "from src import _io\n"
    "import matplotlib\n"
    "matplotlib.use('Agg')\n"
    "import matplotlib.pyplot as plt\n"
    "def main(config_path=None):\n"
    "    path = config_path or (sys.argv[1] if len(sys.argv) > 1 else 'config_smoke.json')\n"
    "    cfg = json.loads(Path(path).read_text(encoding='utf-8'))\n"
    "    _io.begin('reproduce_fig_1', cfg)\n"
    "    _io.write_table('reproduce_fig_1', ['snr_db', 'bit_error_rate'],"
    " [{'snr_db': 0, 'bit_error_rate': 0.1}, {'snr_db': 5, 'bit_error_rate': 0.01}])\n"
    "    fig, ax = plt.subplots()\n"
    "    ax.semilogy([0, 5], [0.1, 0.01])\n"
    "    _io.write_figure('reproduce_fig_1', 'ber_vs_snr', fig)\n"
    "    return _io.finish('reproduce_fig_1', metrics={'rows': 2}, assumptions=[])\n"
    "if __name__ == '__main__':\n"
    "    raise SystemExit(main())\n"
)

PER_TASK_FILES = {
    "README.md": "Run `python run_experiment.py config_smoke.json`.\n",
    "requirements.txt": "numpy\nmatplotlib\n",
    "config.json": '{"seed": 1}\n',
    "config_smoke.json": '{"seed": 1}\n',
    "src/channel.py": "def noop():\n    return None\n",
    "src/modulation.py": "def noop():\n    return None\n",
    "src/metrics.py": "def noop():\n    return None\n",
    "src/simulation.py": "def noop():\n    return None\n",
    "tasks/reproduce_fig_1.py": TASK_DRIVER,
}


PAPER_THESIS = {
    "central_claim": "在密集场景下 STAB 的和速率高于 ZF。",
    "proposed_method": "STAB",
    "mechanism": "空时维度去相关用户，使等效信道更良态，压过预对数损失。",
    "comparisons": [
        {
            "claim_id": "stab_beats_zf",
            "methods_best_to_worst": ["STAB", "ZF"],
            "expected_ordering": "密集区 STAB > ZF",
            "metric": "sum rate",
            "regime": "用户密集",
            "figure_ref": "Fig.1",
            "mechanism_note": "空时去相关带来条件数优势",
        }
    ],
    "headline_shape": "和速率随功率上升，STAB 在最上方。",
    "caveats": ["稀疏用户时优势消失。"],
}


class PerTaskFakeLLM:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def complete(self, prompt: str, *, system=None, response_format=None) -> str:
        # The optional science-loop thesis call is dispatched by schema name and intentionally
        # does NOT consume a positional slot, so facts (call 1) / tasks (call 2) stay aligned
        # whether or not --science-loop is on.
        if _schema_name(response_format) == "paper_thesis":
            return json.dumps(PAPER_THESIS)
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
                            "value": {"snr_db": [0, 5]},
                            "source": {"chunk_id": "text_c1", "page": None, "section": "Sim", "quote": "AWGN"},
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
                            "expected_artifacts": ["outputs/reproduce_fig_1/results.csv"],
                            "output_columns": ["snr_db", "bit_error_rate"],
                            "expected_trend": {"x_axis": "snr_db", "y_axis": "bit_error_rate", "direction": "decreasing", "reason": "higher SNR lowers BER"},
                            "comparison": {"baselines": ["AWGN reference"], "curve_groups": ["sim"], "tolerance": "trend"},
                            "required_facts": [{"type": "channel_model", "name": "AWGN"}],
                            "assumptions": [],
                            "risk_if_unreproducible": "core curve unchecked",
                        }
                    ]
                }
            )
        if _schema_name(response_format) == "repro_project_plan":
            return json.dumps(
                {
                    "implementation_strategy": "Per-task AWGN BER smoke project.",
                    "assumptions": ["Smoke-scale synthetic data."],
                    "files": [{"path": p, "purpose": f"Generate {p}.", "key_interfaces": []} for p in PER_TASK_FILES],
                }
            )
        if _schema_name(response_format) == "repro_project_file":
            target = _target_path(prompt)
            return json.dumps({"path": target, "content_lines": PER_TASK_FILES[target].splitlines()})
        return json.dumps({"files": [{"path": p, "content": c} for p, c in PER_TASK_FILES.items()]})


class PerTaskLayoutPipelineTests(unittest.TestCase):
    def test_generates_and_runs_a_per_task_project_end_to_end(self) -> None:
        with TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            paper = temp / "paper.md"
            paper.write_text("Simulation Results\nAWGN channel, BER vs SNR.", encoding="utf-8")

            fake = PerTaskFakeLLM()
            result = ReviewPipeline(client=fake).run(
                paper,
                temp / "case",
                run_repro=True,
                run_timeout=60,
                repair_attempts=0,
                result_review=False,
                facts_gap_rounds=0,
                tasks_gap_rounds=0,
                per_task_layout=True,
            )

            project = result.repro_project_dir
            # The model generated the task driver; the harness injected the rest.
            self.assertTrue((project / "tasks" / "reproduce_fig_1.py").exists())
            self.assertTrue((project / "tasks" / "__init__.py").exists())
            self.assertTrue((project / "tasks_manifest.json").exists())
            self.assertTrue((project / "src" / "_io.py").exists())
            dispatcher = (project / "run_experiment.py").read_text(encoding="utf-8")
            self.assertIn("from tasks import reproduce_fig_1", dispatcher)  # static import, not LLM-written
            driver = (project / "tasks" / "reproduce_fig_1.py").read_text(encoding="utf-8")
            self.assertIn("_io.finish", driver)

            # The task-aware runner ran it as a subprocess and it passed.
            self.assertTrue(result.runtime_passed)
            runtime = json.loads((result.output_dir / "runtime_result.json").read_text(encoding="utf-8"))
            self.assertTrue(runtime.get("per_task_orchestration"))
            self.assertEqual(runtime.get("coverage"), "1/1")
            from geng_agent.outputs import _valid_csv

            self.assertTrue(_valid_csv(project / "outputs" / "reproduce_fig_1" / "results.csv"))

            manifest = json.loads((result.output_dir / "repro_project_manifest.json").read_text(encoding="utf-8"))
            self.assertIn("tasks_manifest", manifest["_meta"])
            self.assertFalse(manifest["_meta"].get("template_fallback_used"))
            # science-loop is OFF by default -> no thesis stage runs, path unchanged.
            self.assertFalse((result.output_dir / "paper_thesis.json").exists())

    def test_science_loop_distills_paper_thesis_anchor(self) -> None:
        with TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            paper = temp / "paper.md"
            paper.write_text("Simulation Results\nAWGN channel, BER vs SNR.", encoding="utf-8")

            fake = PerTaskFakeLLM()
            result = ReviewPipeline(client=fake).run(
                paper,
                temp / "case",
                run_repro=True,
                run_timeout=60,
                repair_attempts=0,
                result_review=False,
                facts_gap_rounds=0,
                tasks_gap_rounds=0,
                per_task_layout=True,
                science_loop=True,
            )

            # --science-loop emits the thesis anchor and threads it into generated_files.json.
            thesis = json.loads((result.output_dir / "paper_thesis.json").read_text(encoding="utf-8"))
            self.assertEqual(thesis["proposed_method"], "STAB")
            self.assertEqual(thesis["comparisons"][0]["methods_best_to_worst"], ["STAB", "ZF"])
            generated = json.loads((result.output_dir / "generated_files.json").read_text(encoding="utf-8"))
            self.assertEqual(generated["paper_thesis"]["central_claim"], thesis["central_claim"])
            # the thesis anchor must actually reach codegen: the plan prompt and the science-file
            # prompts carry the "复现靶子" block; non-.py files (README) do not.
            audit = result.output_dir / "audit"
            plan_prompt = (audit / "03a_generate_repro_project_plan.md").read_text(encoding="utf-8")
            self.assertIn("论文思路·复现靶子", plan_prompt)
            self.assertIn("密集区 STAB > ZF", plan_prompt)
            task_prompts = list(audit.glob("03b_generate_repro_project_file_*reproduce_fig_1*.md"))
            self.assertTrue(task_prompts)
            self.assertIn("论文思路·复现靶子", task_prompts[0].read_text(encoding="utf-8"))
            # the thesis stage must not disturb the per-task flow: still a real run, not a fallback.
            self.assertTrue(result.runtime_passed)
            self.assertFalse(json.loads(
                (result.output_dir / "repro_project_manifest.json").read_text(encoding="utf-8")
            )["_meta"].get("template_fallback_used"))


class _ExplodingLLM:
    """A client that must never be called: any call means resume failed to reuse a cache."""

    def complete(self, prompt: str, *, system=None, response_format=None) -> str:
        raise AssertionError(f"resume re-ran an LLM stage (schema={_schema_name(response_format)})")


class PerTaskResumeTests(unittest.TestCase):
    def test_resume_reuses_per_task_manifest_without_regenerating(self) -> None:
        # The cached per-task manifest has no run_experiment.py in files (harness-injected),
        # so validating it against the DEFAULT required set always failed -> the whole codegen
        # silently re-ran on same-dir resume (observed live: a resumed run re-burned the full
        # generation budget). The cache must validate against the per-task required set.
        with TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            paper = temp / "paper.md"
            paper.write_text("Simulation Results\nAWGN channel, BER vs SNR.", encoding="utf-8")
            first = ReviewPipeline(client=PerTaskFakeLLM()).run(
                paper, temp / "case", run_repro=True, run_timeout=60, repair_attempts=0,
                result_review=False, facts_gap_rounds=0, tasks_gap_rounds=0, per_task_layout=True,
            )
            self.assertTrue(first.runtime_passed)

            # Second run on the SAME output dir with resume (default): every stage must come
            # from cache — the exploding client proves codegen (and everything else) never re-ran.
            second = ReviewPipeline(client=_ExplodingLLM()).run(
                paper, temp / "case", run_repro=True, run_timeout=60, repair_attempts=0,
                result_review=False, facts_gap_rounds=0, tasks_gap_rounds=0, per_task_layout=True,
            )
            self.assertTrue(second.runtime_passed)
            audit = temp / "case" / "audit"
            self.assertTrue((audit / "resume_used_03_generate_repro_project.json").exists())
            self.assertFalse((audit / "resume_invalid_03_generate_repro_project.json").exists())


class SrcSymbolContractTests(unittest.TestCase):
    def test_extracts_real_symbols_and_excludes_io_and_dunder(self) -> None:
        files = [
            {"path": "src/modulation.py", "content_lines": ["def qpsk_mod(bits):", "    return bits", "class Demapper:", "    pass", "BITS_PER_SYM = 2", "def _helper():", "    return 1"]},
            {"path": "src/_io.py", "content_lines": ["def begin():", "    return None"]},  # trusted runtime: excluded
            {"path": "README.md", "content_lines": ["docs"]},  # non-src: excluded
        ]
        symbols = _available_src_symbols(files)
        self.assertEqual(symbols, {"src.modulation": ["qpsk_mod", "Demapper", "BITS_PER_SYM"]})  # no _helper, no _io

    def test_override_pins_imports_to_real_symbols(self) -> None:
        symbols = {"src.modulation": ["qpsk_mod"], "src.channel": ["awgn"]}
        override = _per_task_file_override("reproduce_fig_1", {"repro_tasks": [{"task_id": "reproduce_fig_1"}]}, symbols)
        self.assertIn("import 白名单", override)
        self.assertIn("src.modulation: qpsk_mod", override)
        self.assertIn("src.channel: awgn", override)
        self.assertIn("绝不要 import 不存在的名字", override)

    def test_src_override_forbids_plotting_and_guarded_imports(self) -> None:
        # v3 fell back to template because src/simulation.py imported matplotlib inside a
        # try/except -> consistency gate blocked the run. src/ must be computation-only.
        override = _per_task_src_override()
        self.assertIn("禁止 import matplotlib", override)
        self.assertIn("_io.write_figure", override)
        self.assertIn("禁止把任何 import 包进 try/except", override)


if __name__ == "__main__":
    unittest.main()
