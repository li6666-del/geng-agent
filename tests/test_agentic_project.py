import base64
import json
import os
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from geng_agent.agentic_project import (
    _augment_review_display_images,
    _feedback_from_results,
    _is_better_score,
    _is_success,
    _load_cached_agentic_project,
    _prepare_writer_selftest_shim,
    _render_task_markdown_section,
    _review_image_kind,
    _score_candidate,
    build_writer_brief,
    run_codex_project_workflow,
    strip_review_control_footer,
    summarize_markdown_review,
)
from geng_agent.agentic_task_writers import (
    _prepare_task_writer_python_guard,
    _task_paper_image_paths,
    _task_writer_runtime_result,
    _task_writer_concurrency,
    _validate_paper_locator_doc,
    run_codex_task_writer_workflow,
)
from geng_agent.pipeline import ReviewPipeline
from geng_agent.prompts import PromptBook


PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="


class DummyLLM:
    def complete(self, *args, **kwargs):
        raise AssertionError("LLM repair/codegen must not be used by the Codex project backend")


class MinimalPipelineLLM:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, prompt: str, *, system=None, response_format=None) -> str:
        self.calls += 1
        if self.calls == 1:
            return json.dumps(
                {
                    "paper_domain": "communication",
                    "paper_repro_type": "signal_chain",
                    "engineering_facts": [
                        {
                            "type": "channel_model",
                            "name": "AWGN",
                            "value": {"snr_db": [0, 5]},
                            "source": {
                                "source_kind": "text",
                                "chunk_id": "text_c1",
                                "page": None,
                                "section": "Simulation",
                                "quote": "AWGN",
                                "figure_ref": "",
                            },
                            "confidence": "high",
                            "used_for_reproduction": True,
                        }
                    ],
                    "missing_information": [],
                }
            )
        if self.calls == 2:
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
                            "expected_trend": {
                                "x_axis": "snr_db",
                                "y_axis": "bit_error_rate",
                                "direction": "decreasing",
                                "reason": "higher SNR lowers BER",
                            },
                            "comparison": {"baselines": ["AWGN reference"], "curve_groups": ["sim"], "tolerance": "trend"},
                            "required_facts": [{"type": "channel_model", "name": "AWGN"}],
                            "assumptions": [],
                            "risk_if_unreproducible": "core curve unchecked",
                        }
                    ]
                }
            )
        raise AssertionError("Codex backend should not ask the LLM to write project code")


def _review(verdict: str, alignment: str, differences=None, causes=None) -> dict:
    dims = [
        "artifact_coverage",
        "reproduction_logic",
        "trend_shape",
        "metric_axis_scale",
        "baseline_comparison",
        "statistical_reliability",
        "conclusion_support",
    ]
    return {
        "task_id": "reproduce_fig_1",
        "local_result_credibility": "high",
        "paper_alignment": alignment,
        "scientific_verdict": verdict,
        "dimension_reviews": [
            {"dimension": dim, "rating": "strong", "finding": f"{dim} ok", "evidence": ["mock evidence"]}
            for dim in dims
        ],
        "paper_result_summary": "paper says curve should increase",
        "local_result_summary": "local curve increases",
        "differences": differences or [],
        "possible_causes": causes or [],
        "evidence": ["mock evidence"],
        "limitations": ["mock reviewer"],
        "confidence": "high",
    }


def _write_mock_writer(temp: Path) -> str:
    script = temp / "mock_writer.py"
    script.write_text(
        textwrap.dedent(
            r'''
            import json
            import os
            import sys
            import shutil
            from pathlib import Path

            args = sys.argv[1:]
            proj = Path(args[args.index("--cd") + 1])
            prompt = sys.stdin.read() if args and args[-1] == "-" else args[-1]
            log = Path(__file__).with_name("writer_prompts.txt")
            with log.open("a", encoding="utf-8") as handle:
                handle.write("---PROMPT---\n" + prompt + "\n")
            Path(__file__).with_name("writer_env.txt").write_text(
                json.dumps({
                    "mode": os.environ.get("GENG_WRITER_SELFTEST_MODE", ""),
                    "geng_python": os.environ.get("GENG_PYTHON", ""),
                    "python": os.environ.get("PYTHON", ""),
                    "path0": (os.environ.get("PATH") or os.environ.get("Path") or "").split(os.pathsep)[0],
                }),
                encoding="utf-8",
            )

            (proj / "src").mkdir(exist_ok=True)
            (proj / "tasks").mkdir(exist_ok=True)
            (proj / "README.md").write_text("mock codex project\n", encoding="utf-8")
            (proj / "requirements.txt").write_text("numpy\nmatplotlib\n", encoding="utf-8")
            (proj / "config.json").write_text('{"seed": 1}\n', encoding="utf-8")
            (proj / "config_smoke.json").write_text('{"seed": 1}\n', encoding="utf-8")
            (proj / "src" / "channel.py").write_text("def gain():\n    return 1.0\n", encoding="utf-8")
            (proj / "src" / "modulation.py").write_text("def method_name():\n    return 'mock'\n", encoding="utf-8")
            (proj / "src" / "metrics.py").write_text("def score(x):\n    return float(x)\n", encoding="utf-8")
            (proj / "src" / "simulation.py").write_text(
                "def run_curve():\n    return [(0, 0.1), (1, 0.3), (2, 0.5)]\n",
                encoding="utf-8",
            )
            (proj / "tasks" / "reproduce_fig_1.py").write_text(
                "\n".join([
                    "from __future__ import annotations",
                    "import json",
                    "import matplotlib.pyplot as plt",
                    "from src import _io",
                    "from src.simulation import run_curve",
                    "",
                    "def main(config_path=None) -> int:",
                    "    cfg_path = config_path or 'config_smoke.json'",
                    "    with open(cfg_path, 'r', encoding='utf-8') as handle:",
                    "        cfg = json.load(handle)",
                    "    task_id = 'reproduce_fig_1'",
                    "    _io.begin(task_id, cfg)",
                    "    rows = run_curve()",
                    "    _io.write_table(task_id, ['snr', 'rate'], rows)",
                    "    fig, ax = plt.subplots()",
                    "    ax.plot([x for x, _ in rows], [y for _, y in rows])",
                    "    ax.set_xlabel('snr')",
                    "    ax.set_ylabel('rate')",
                    "    _io.write_figure(task_id, 'fig1', fig)",
                    "    return _io.finish(task_id, metrics={'backend': 'cpu', 'points': len(rows)}, assumptions=['mock'])",
                    "",
                    "if __name__ == '__main__':",
                    "    raise SystemExit(main())",
                    "",
                ]),
                encoding="utf-8",
            )
            (proj / "tasks" / "reproduce_fig_2.py").write_text(
                "\n".join([
                    "from __future__ import annotations",
                    "import json",
                    "import matplotlib.pyplot as plt",
                    "from src import _io",
                    "from src.simulation import run_curve",
                    "",
                    "def main(config_path=None) -> int:",
                    "    cfg_path = config_path or 'config_smoke.json'",
                    "    with open(cfg_path, 'r', encoding='utf-8') as handle:",
                    "        cfg = json.load(handle)",
                    "    task_id = 'reproduce_fig_2'",
                    "    _io.begin(task_id, cfg)",
                    "    rows = run_curve()",
                    "    _io.write_table(task_id, ['snr', 'rate'], rows)",
                    "    fig, ax = plt.subplots()",
                    "    ax.plot([x for x, _ in rows], [y for _, y in rows])",
                    "    ax.set_xlabel('snr')",
                    "    ax.set_ylabel('rate')",
                    "    _io.write_figure(task_id, 'fig2', fig)",
                    "    return _io.finish(task_id, metrics={'backend': 'cpu', 'points': len(rows)}, assumptions=['mock'])",
                    "",
                    "if __name__ == '__main__':",
                    "    raise SystemExit(main())",
                    "",
                ]),
                encoding="utf-8",
            )
            (proj / "src" / "_io.py").write_text("SABOTAGED\n", encoding="utf-8")
            (proj / "run_experiment.py").write_text("SABOTAGED\n", encoding="utf-8")
            (proj / "unexpected.py").write_text("print('bad')\n", encoding="utf-8")
            evidence = proj / "paper_evidence"
            if evidence.exists():
                shutil.rmtree(evidence)
            evidence.write_text("writer tampered evidence\n", encoding="utf-8")
            print("writer done")
            if Path(__file__).with_name("writer_exit_1.flag").exists():
                raise SystemExit(1)
            '''
        ),
        encoding="utf-8",
    )
    return f'"{sys.executable}" "{script}"'


def _write_mock_task_writer(temp: Path, *, result_location: str = "root") -> str:
    script = temp / "mock_task_writer.py"
    script_source = r'''
            import base64
            import json
            import os
            import subprocess
            import sys
            from pathlib import Path

            PNG_BYTES = base64.b64decode("__PNG_B64__")
            PAPER_BYTES = PNG_BYTES + b"writer-paper-target"

            args = sys.argv[1:]
            proj = Path(args[args.index("--cd") + 1])
            last = Path(args[args.index("--output-last-message") + 1])
            prompt = sys.stdin.read() if args and args[-1] == "-" else ""
            with Path(__file__).with_name("task_writer_prompts.txt").open("a", encoding="utf-8") as handle:
                handle.write("---PROMPT---\n" + prompt + "\n")

            manifest = json.loads((proj / "tasks_manifest.json").read_text(encoding="utf-8"))
            task = manifest["tasks"][0]
            task_id = task["task_id"]
            module = task["module"]
            output_subdir = task["output_subdir"]
            result_location = "__RESULT_LOCATION__"

            task_source = f"""
from __future__ import annotations
import json
import matplotlib.pyplot as plt
from pathlib import Path
from src import _io

def main(config_path=None) -> int:
    cfg_path = config_path or 'config_smoke.json'
    cfg = json.loads(Path(cfg_path).read_text(encoding='utf-8'))
    task_id = {task_id!r}
    _io.begin(task_id, cfg)
    rows = [{{'x': 0, 'y': 0.1}}, {{'x': 1, 'y': 0.2}}, {{'x': 2, 'y': 0.3}}]
    _io.write_table(task_id, ['x', 'y'], rows)
    fig, ax = plt.subplots()
    ax.plot([row['x'] for row in rows], [row['y'] for row in rows])
    ax.set_xlabel('x')
    ax.set_ylabel('y')
    _io.write_figure(task_id, 'curve', fig)
    return _io.finish(task_id, metrics={{'points': len(rows)}}, assumptions=['mock'])

if __name__ == '__main__':
    raise SystemExit(main())
"""
            (proj / "tasks" / f"{module}.py").write_text(task_source, encoding="utf-8")
            (proj / "tasks" / f"{module}_lib.py").write_text("HELPER = True\n", encoding="utf-8")
            for context in (proj / "paper_evidence").rglob("context.md"):
                (context.parent / "paper_page_1.png").write_bytes(PNG_BYTES)

            py = os.environ["PYTHON"]
            completed = subprocess.run([py, "-m", f"tasks.{module}", "config.json"], cwd=proj, check=False)
            if completed.returncode != 0:
                raise SystemExit(completed.returncode)

            paper_image_rel = f"outputs/{output_subdir}/paper_target_locator.png"
            paper_image_path = proj / paper_image_rel
            paper_image_path.parent.mkdir(parents=True, exist_ok=True)
            paper_image_path.write_bytes(PAPER_BYTES)
            paper_locator = {
                "target_figure": task.get("figure_or_claim", task_id),
                "source_page": 1,
                "bbox_norm": [0.1, 0.1, 0.9, 0.9],
                "confidence": "low",
                "contains_only_target": False,
                "fallback_used": True,
                "reason": "mock writer locator",
                "paper_image_paths": [paper_image_rel],
            }

            status = "explained_gap" if "gap" in task_id else "matched"
            result = {
                "task_id": task_id,
                "status": status,
                "summary": "mock writer completed the assigned full run",
                "differences": ["scale differs"] if status == "explained_gap" else [],
                "possible_causes": ["missing paper parameter"] if status == "explained_gap" else [],
                "remaining_uncertainties": ["exact seed"] if status == "explained_gap" else [],
                "evidence_files": [f"outputs/{output_subdir}/results.csv", f"outputs/{output_subdir}/curve.png", paper_image_rel],
                "local_image_paths": [f"outputs/{output_subdir}/curve.png"],
                "paper_image_paths": [paper_image_rel],
            }
            result_dir = (proj / "outputs" / output_subdir) if result_location == "output" else proj
            result_dir.mkdir(parents=True, exist_ok=True)
            (result_dir / "paper_target_figure.json").write_text(
                json.dumps(paper_locator, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            (result_dir / "task_agent_result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            (result_dir / "task_agent_result.md").write_text(
                f"# {task_id}\n\nWriter conclusion: {status}\n\nEvidence: outputs/{output_subdir}/curve.png\n",
                encoding="utf-8",
            )
            last.write_text("task writer finished", encoding="utf-8")
            print("task writer finished")
            '''
    script_text = "\n".join(
        line[12:] if line.startswith("            ") else line
        for line in script_source.splitlines()
    ).lstrip()
    script.write_text(
        script_text.replace("__PNG_B64__", PNG_B64).replace("__RESULT_LOCATION__", result_location),
        encoding="utf-8",
    )
    return f'"{sys.executable}" "{script}"'


def _write_usage_limited_task_writer(temp: Path) -> str:
    script = temp / "usage_limited_task_writer.py"
    script.write_text(
        textwrap.dedent(
            r'''
            import sys
            from pathlib import Path

            args = sys.argv[1:]
            if "--output-last-message" in args:
                last = Path(args[args.index("--output-last-message") + 1])
                last.write_text("Codex usage limit reached", encoding="utf-8")
            print(
                "ERROR: You've hit your usage limit. Visit https://chatgpt.com/codex/settings/usage "
                "to purchase more credits or try again at 9:13 PM.",
                file=sys.stderr,
            )
            raise SystemExit(1)
            '''
        ),
        encoding="utf-8",
    )
    return f'"{sys.executable}" "{script}"'


def _write_spoofing_task_writer(temp: Path) -> str:
    script = temp / "spoofing_task_writer.py"
    script.write_text(
        textwrap.dedent(
            f'''
            import base64
            import json
            import sys
            from pathlib import Path

            PNG_BYTES = base64.b64decode({PNG_B64!r})
            PAPER_BYTES = PNG_BYTES + b"writer-paper-target"

            args = sys.argv[1:]
            proj = Path(args[args.index("--cd") + 1])
            last = Path(args[args.index("--output-last-message") + 1])
            manifest = json.loads((proj / "tasks_manifest.json").read_text(encoding="utf-8"))
            task = manifest["tasks"][0]
            task_id = task["task_id"]
            output_subdir = task["output_subdir"]

            for context in (proj / "paper_evidence").rglob("context.md"):
                (context.parent / "paper_page_1.png").write_bytes(PNG_BYTES)

            out = proj / "outputs" / output_subdir
            out.mkdir(parents=True, exist_ok=True)
            (out / "results.csv").write_text("x,y\\n0,0.1\\n", encoding="utf-8")
            (out / "curve.png").write_bytes(PNG_BYTES)
            (out / "paper_target_locator.png").write_bytes(PAPER_BYTES)
            (out / "summary.json").write_text(
                json.dumps({{"task_id": task_id, "metrics": {{"points": 1}}, "assumptions": []}}),
                encoding="utf-8",
            )
            (proj / "task_agent_runs.jsonl").write_text(
                json.dumps({{"profile": "full", "returncode": 0, "guard_token": "fake"}}) + "\\n",
                encoding="utf-8",
            )
            result = {{
                "task_id": task_id,
                "status": "matched",
                "summary": "spoofed result without running guard",
                "differences": [],
                "possible_causes": [],
                "remaining_uncertainties": [],
                "evidence_files": [f"outputs/{{output_subdir}}/results.csv", f"outputs/{{output_subdir}}/paper_target_locator.png"],
                "local_image_paths": [f"outputs/{{output_subdir}}/curve.png"],
                "paper_image_paths": [f"outputs/{{output_subdir}}/paper_target_locator.png"],
            }}
            locator = {{
                "target_figure": task.get("figure_or_claim", task_id),
                "source_page": 1,
                "bbox_norm": [0.1, 0.1, 0.9, 0.9],
                "confidence": "low",
                "contains_only_target": False,
                "fallback_used": True,
                "reason": "mock locator",
                "paper_image_paths": [f"outputs/{{output_subdir}}/paper_target_locator.png"],
            }}
            (proj / "paper_target_figure.json").write_text(json.dumps(locator, ensure_ascii=False), encoding="utf-8")
            (proj / "task_agent_result.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
            (proj / "task_agent_result.md").write_text("# spoof\\n\\nThis writer delivered artifacts and self-review without a trusted run log.\\n", encoding="utf-8")
            last.write_text("spoofed", encoding="utf-8")
            '''
        ),
        encoding="utf-8",
    )
    return f'"{sys.executable}" "{script}"'


def _write_failed_delivery_task_writer(temp: Path) -> str:
    script = temp / "failed_delivery_task_writer.py"
    script.write_text(
        textwrap.dedent(
            f'''
            import base64
            import json
            import sys
            from pathlib import Path

            PNG_BYTES = base64.b64decode({PNG_B64!r})
            PAPER_BYTES = PNG_BYTES + b"writer-paper-target"

            args = sys.argv[1:]
            proj = Path(args[args.index("--cd") + 1])
            last = Path(args[args.index("--output-last-message") + 1])
            manifest = json.loads((proj / "tasks_manifest.json").read_text(encoding="utf-8"))
            task = manifest["tasks"][0]
            task_id = task["task_id"]
            output_subdir = task["output_subdir"]

            for context in (proj / "paper_evidence").rglob("context.md"):
                (context.parent / "paper_page_1.png").write_bytes(PNG_BYTES)

            out = proj / "outputs" / output_subdir
            out.mkdir(parents=True, exist_ok=True)
            (out / "curve.png").write_bytes(PNG_BYTES)
            (out / "paper_target_locator.png").write_bytes(PAPER_BYTES)
            result = {{
                "task_id": task_id,
                "status": "failed",
                "summary": "writer could not complete the assigned scientific reproduction",
                "differences": [],
                "possible_causes": [],
                "remaining_uncertainties": [],
                "evidence_files": [],
                "local_image_paths": [f"outputs/{{output_subdir}}/curve.png"],
                "paper_image_paths": [f"outputs/{{output_subdir}}/paper_target_locator.png"],
            }}
            locator = {{
                "target_figure": task.get("figure_or_claim", task_id),
                "source_page": 1,
                "bbox_norm": [0.1, 0.1, 0.9, 0.9],
                "confidence": "low",
                "contains_only_target": False,
                "fallback_used": True,
                "reason": "mock failed locator",
                "paper_image_paths": [f"outputs/{{output_subdir}}/paper_target_locator.png"],
            }}
            (proj / "paper_target_figure.json").write_text(json.dumps(locator, ensure_ascii=False), encoding="utf-8")
            (proj / "task_agent_result.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
            (proj / "task_agent_result.md").write_text(
                "# failed task\\n\\nWriter reports this task as failed despite leaving diagnostic artifacts.\\n",
                encoding="utf-8",
            )
            last.write_text("failed delivery", encoding="utf-8")
            '''
        ),
        encoding="utf-8",
    )
    return f'"{sys.executable}" "{script}"'


def _write_failing_after_first_reviewer(temp: Path) -> str:
    script = temp / "mock_reviewer_fail_after_first.py"
    script.write_text(
        textwrap.dedent(
            r'''
            import json
            import sys
            from pathlib import Path

            args = sys.argv[1:]
            out = Path(args[args.index("--output-last-message") + 1])
            counter = Path(__file__).with_name("review_count_fail_after_first.txt")
            n = int(counter.read_text(encoding="utf-8")) if counter.exists() else 0
            counter.write_text(str(n + 1), encoding="utf-8")
            if n > 0:
                print("reviewer failed on the second task")
                raise SystemExit(1)
            markdown = "\n".join([
                "# Mock review",
                "",
                "The first task has enough detail to be accepted as a Markdown review.",
                "",
                "<!-- geng-agent-review-summary",
                "task_id: reproduce_fig_1",
                "scientific_verdict: supports_paper_claim",
                "paper_alignment: match",
                "confidence: high",
                "-->",
            ])
            out.write_text(markdown, encoding="utf-8")
            print(markdown)
            '''
        ),
        encoding="utf-8",
    )
    return f'"{sys.executable}" "{script}"'


def _write_mock_reviewer(temp: Path) -> str:
    script = temp / "mock_reviewer.py"
    script.write_text(
        textwrap.dedent(
            r'''
            import sys
            from pathlib import Path

            args = sys.argv[1:]
            out = Path(args[args.index("--output-last-message") + 1])
            counter = Path(__file__).with_name("review_count.txt")
            n = int(counter.read_text(encoding="utf-8")) if counter.exists() else 0
            counter.write_text(str(n + 1), encoding="utf-8")
            markdown = "\n".join([
                "# reproduce_fig_1",
                "",
                "## 结论",
                "本地曲线与论文趋势一致，建议人工复核数值量级。",
                "",
                "## 原论文结果摘要",
                "paper says curve should increase",
                "",
                "## 本地复现结果摘要",
                "local curve increases",
                "",
                "## 七维度审查",
                "- artifact_coverage: mock evidence",
                "- reproduction_logic: mock evidence",
                "- trend_shape: mock evidence",
                "- metric_axis_scale: mock evidence",
                "- baseline_comparison: mock evidence",
                "- statistical_reliability: mock evidence",
                "- conclusion_support: mock evidence",
                "",
                "## 主要差异",
                "无明显差异。",
                "",
                "## 可能原因",
                "mock modeling issue 已被修正。",
                "",
                "## 人工复核建议",
                "请核对 CSV 和 PNG。",
                "",
            ])
            markdown += "\n<!-- geng-agent-review-summary\n"
            markdown += "task_id: reproduce_fig_1\n"
            markdown += "scientific_verdict: supports_paper_claim\n"
            markdown += "paper_alignment: match\n"
            markdown += "confidence: high\n"
            markdown += "-->\n"
            out.write_text(markdown, encoding="utf-8")
            print(markdown)
            '''
        ),
        encoding="utf-8",
    )
    return f'"{sys.executable}" "{script}"'


def _write_partial_reviewer(temp: Path) -> str:
    script = temp / "mock_partial_reviewer.py"
    script.write_text(
        textwrap.dedent(
            r'''
            import sys
            from pathlib import Path

            args = sys.argv[1:]
            out = Path(args[args.index("--output-last-message") + 1])
            counter = Path(__file__).with_name("partial_review_count.txt")
            n = int(counter.read_text(encoding="utf-8")) if counter.exists() else 0
            counter.write_text(str(n + 1), encoding="utf-8")
            markdown = "\n".join([
                "# reproduce_fig_1",
                "",
                "## 结论",
                "本地曲线只部分支持论文主张，仍需继续调整模型。",
                "",
                "## 原论文结果摘要",
                "paper says curve should increase",
                "",
                "## 本地复现结果摘要",
                "local curve increases but scale remains weak",
                "",
                "## 七维度审查",
                "- artifact_coverage: mock evidence",
                "- reproduction_logic: surrogate model remains incomplete",
                "- trend_shape: qualitative trend only",
                "- metric_axis_scale: numeric scale differs",
                "- baseline_comparison: baseline not fully matched",
                "- statistical_reliability: few samples",
                "- conclusion_support: partial only",
                "",
                "## 主要差异",
                "- numeric scale still differs from the paper",
                "",
                "## 可能原因",
                "- missing paper parameters",
                "",
            ])
            markdown += "\n<!-- geng-agent-review-summary\n"
            markdown += "task_id: reproduce_fig_1\n"
            markdown += "scientific_verdict: partially_supports_paper_claim\n"
            markdown += "paper_alignment: partial_match\n"
            markdown += "confidence: high\n"
            markdown += "-->\n"
            out.write_text(markdown, encoding="utf-8")
            print(markdown)
            '''
        ),
        encoding="utf-8",
    )
    return f'"{sys.executable}" "{script}"'


def _write_mock_analysis(temp: Path) -> str:
    script = temp / "mock_analysis.py"
    script.write_text(
        textwrap.dedent(
            r'''
            import json
            import sys
            from pathlib import Path

            args = sys.argv[1:]
            out = Path(args[args.index("--output-last-message") + 1])
            prompt = sys.stdin.read() if args and args[-1] == "-" else args[-1]
            log = Path(__file__).with_name("analysis_prompts.txt")
            with log.open("a", encoding="utf-8") as handle:
                handle.write("---PROMPT---\n" + prompt + "\n")
            if "Schema: engineering_facts" in prompt:
                doc = {
                    "paper_domain": "communication",
                    "paper_repro_type": "signal_chain",
                    "engineering_facts": [
                        {
                            "type": "channel_model",
                            "name": "AWGN",
                            "value": {"snr_db": [0, 5]},
                            "source": {
                                "source_kind": "text",
                                "chunk_id": "text_c1",
                                "page": None,
                                "section": "Simulation",
                                "quote": "AWGN",
                                "figure_ref": "",
                            },
                            "confidence": "high",
                            "used_for_reproduction": True,
                        }
                    ],
                    "missing_information": [],
                }
            elif "Schema: repro_tasks" in prompt:
                doc = {
                    "repro_tasks": [
                        {
                            "task_id": "reproduce_fig_1",
                            "target": "BER vs SNR",
                            "metric": "bit_error_rate",
                            "metric_formula": "bit_error_rate = bit_errors / total_bits",
                            "figure_or_claim": "Fig. 1",
                            "expected_artifacts": ["outputs/reproduce_fig_1/results.csv"],
                            "output_columns": ["snr_db", "bit_error_rate"],
                            "expected_trend": {
                                "x_axis": "snr_db",
                                "y_axis": "bit_error_rate",
                                "direction": "decreasing",
                                "reason": "higher SNR lowers BER",
                            },
                            "comparison": {"baselines": ["AWGN reference"], "curve_groups": ["sim"], "tolerance": "trend"},
                            "required_facts": [{"type": "channel_model", "name": "AWGN"}],
                            "assumptions": [],
                            "risk_if_unreproducible": "core curve unchecked",
                        }
                    ]
                }
            else:
                raise SystemExit("unexpected analysis prompt")
            out.write_text(json.dumps(doc), encoding="utf-8")
            print(json.dumps(doc))
            '''
        ),
        encoding="utf-8",
    )
    return f'"{sys.executable}" "{script}"'


class WriterSelftestShimTests(unittest.TestCase):
    def test_python_shim_allows_only_smoke_commands(self) -> None:
        with TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            project = temp / "project"
            audit = temp / "audit"
            project.mkdir()
            audit.mkdir()
            (project / "run_experiment.py").write_text("print('smoke ok')\n", encoding="utf-8")
            shim = _prepare_writer_selftest_shim(audit, "round_01")
            env = dict(os.environ)
            env.update(shim["env"])
            env["PATH"] = str(shim["bin_dir"]) + os.pathsep + env.get("PATH", "")
            if os.name == "nt":
                env["Path"] = env["PATH"]

            allowed = subprocess.run(
                [env["GENG_PYTHON"], "run_experiment.py", "config_smoke.json"],
                cwd=project,
                capture_output=True,
                text=True,
                timeout=30,
                env=env,
            )
            self.assertEqual(allowed.returncode, 0, msg=allowed.stderr)
            self.assertIn("smoke ok", allowed.stdout)

            full = subprocess.run(
                [env["GENG_PYTHON"], "run_experiment.py", "config.json"],
                cwd=project,
                capture_output=True,
                text=True,
                timeout=30,
                env=env,
            )
            self.assertEqual(full.returncode, 97)
            self.assertIn("only run smoke self-tests", full.stderr)

            arbitrary = subprocess.run(
                [env["GENG_PYTHON"], "-c", "print('heavy')"],
                cwd=project,
                capture_output=True,
                text=True,
                timeout=30,
                env=env,
            )
            self.assertEqual(arbitrary.returncode, 97)
            self.assertIn("only run smoke self-tests", arbitrary.stderr)


class TaskWriterGuardTests(unittest.TestCase):
    def test_task_writer_concurrency_caps_repro_runs_by_full_slots(self) -> None:
        old_cpu = os.environ.get("GENG_TASK_WRITER_CPU_FULL_SLOTS")
        old_gpu = os.environ.get("GENG_TASK_WRITER_GPU_FULL_SLOTS")
        try:
            os.environ["GENG_TASK_WRITER_CPU_FULL_SLOTS"] = "1"
            os.environ["GENG_TASK_WRITER_GPU_FULL_SLOTS"] = "1"
            self.assertEqual(_task_writer_concurrency(4, None, run_repro=True), 1)
            self.assertEqual(_task_writer_concurrency(4, 4, run_repro=True), 1)
            self.assertEqual(_task_writer_concurrency(4, 4, run_repro=False), 4)
        finally:
            if old_cpu is None:
                os.environ.pop("GENG_TASK_WRITER_CPU_FULL_SLOTS", None)
            else:
                os.environ["GENG_TASK_WRITER_CPU_FULL_SLOTS"] = old_cpu
            if old_gpu is None:
                os.environ.pop("GENG_TASK_WRITER_GPU_FULL_SLOTS", None)
            else:
                os.environ["GENG_TASK_WRITER_GPU_FULL_SLOTS"] = old_gpu

    def test_task_writer_guard_allows_assigned_full_and_rejects_dispatcher_full(self) -> None:
        with TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            project = temp / "project"
            audit = temp / "audit"
            project.mkdir()
            audit.mkdir()
            (project / "tasks").mkdir()
            (project / "outputs" / "demo_task").mkdir(parents=True)
            (project / "run_experiment.py").write_text("print('dispatcher')\n", encoding="utf-8")
            (project / "tasks" / "__init__.py").write_text("", encoding="utf-8")
            (project / "tasks" / "demo_task.py").write_text(
                "from pathlib import Path\n"
                "def main():\n"
                "    out=Path('outputs/demo_task'); out.mkdir(parents=True, exist_ok=True)\n"
                "    (out/'results.csv').write_text('x\\n1\\n', encoding='utf-8')\n"
                f"    (out/'plot.png').write_bytes(__import__('base64').b64decode({PNG_B64!r}))\n"
                "    (out/'summary.json').write_text('{\"task_id\":\"demo_task\",\"metrics\":{\"x\":1},\"assumptions\":[]}', encoding='utf-8')\n"
                "    return 0\n"
                "if __name__ == '__main__': raise SystemExit(main())\n",
                encoding="utf-8",
            )
            shim = _prepare_task_writer_python_guard(
                audit_dir=audit,
                label="task_01",
                module="demo_task",
                output_subdir="demo_task",
                run_log=audit / "trusted_task_agent_runs.jsonl",
                lock_dir=audit / "locks",
                allow_full=True,
                run_timeout=30,
            )
            env = dict(os.environ)
            env.update(shim["env"])
            env["PATH"] = str(shim["bin_dir"]) + os.pathsep + env.get("PATH", "")
            if os.name == "nt":
                env["Path"] = env["PATH"]

            full = subprocess.run(
                [env["GENG_PYTHON"], "-m", "tasks.demo_task", "config.json"],
                cwd=project,
                capture_output=True,
                text=True,
                timeout=30,
                env=env,
            )
            self.assertEqual(full.returncode, 0, msg=full.stderr)
            records = [
                json.loads(line)
                for line in (audit / "trusted_task_agent_runs.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(records[-1]["profile"], "full")
            self.assertEqual(records[-1]["returncode"], 0)
            self.assertEqual(records[-1]["guard_token"], shim["guard_token"])
            self.assertFalse((project / "task_agent_runs.jsonl").exists())

            dispatcher = subprocess.run(
                [env["GENG_PYTHON"], "run_experiment.py", "config.json"],
                cwd=project,
                capture_output=True,
                text=True,
                timeout=30,
                env=env,
            )
            self.assertEqual(dispatcher.returncode, 97)
            self.assertIn("only the assigned task module", dispatcher.stderr)

    def test_task_writer_guard_records_timeout_for_assigned_full(self) -> None:
        with TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            project = temp / "project"
            audit = temp / "audit"
            project.mkdir()
            audit.mkdir()
            (project / "tasks").mkdir()
            (project / "tasks" / "__init__.py").write_text("", encoding="utf-8")
            (project / "tasks" / "slow_task.py").write_text(
                "import time\n"
                "def main(config_path=None):\n"
                "    time.sleep(5)\n"
                "    return 0\n"
                "if __name__ == '__main__': raise SystemExit(main())\n",
                encoding="utf-8",
            )
            shim = _prepare_task_writer_python_guard(
                audit_dir=audit,
                label="slow_task",
                module="slow_task",
                output_subdir="slow_task",
                run_log=audit / "slow_runs.jsonl",
                lock_dir=audit / "locks",
                allow_full=True,
                run_timeout=0.2,
            )
            env = dict(os.environ)
            env.update(shim["env"])
            env["PATH"] = str(shim["bin_dir"]) + os.pathsep + env.get("PATH", "")
            if os.name == "nt":
                env["Path"] = env["PATH"]

            completed = subprocess.run(
                [env["GENG_PYTHON"], "-m", "tasks.slow_task", "config.json"],
                cwd=project,
                capture_output=True,
                text=True,
                timeout=30,
                env=env,
            )

            self.assertEqual(completed.returncode, 124)
            records = [json.loads(line) for line in (audit / "slow_runs.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertTrue(records[-1]["timed_out"])
            self.assertEqual(records[-1]["returncode"], 124)


class TaskWriterWorkflowTests(unittest.TestCase):
    def test_task_writer_runtime_treats_requirement_warnings_as_nonblocking(self) -> None:
        runtime = _task_writer_runtime_result(
            task_records=[
                {
                    "task_id": "demo_task",
                    "module": "demo_task",
                    "structural_ok": True,
                    "task_writer_status": "matched",
                    "artifacts": {
                        "csv_files": ["results.csv"],
                        "png_files": ["curve.png"],
                        "summary_json_files": ["summary.json"],
                    },
                    "output_subdir": "demo_task",
                    "errors": [],
                    "warnings": [],
                }
            ],
            validation={"required_files_present": True, "python_compiles": True},
            manifest_issues=[],
            requirement_issues=[],
            requirement_warnings=[
                {
                    "file": "tasks/demo_task.py",
                    "line": "1",
                    "message": "third-party import is not declared in requirements.txt: scipy.linalg (expected package scipy)",
                    "severity": "warning",
                }
            ],
            security_issues=[],
        )

        self.assertTrue(runtime["passed"], msg=json.dumps(runtime, ensure_ascii=False))
        self.assertEqual(runtime["requirements_issues"], [])
        self.assertEqual(len(runtime["requirements_warnings"]), 1)

    def test_task_writer_workflow_merges_parallel_self_reviewed_tasks_without_reviewer_or_final_full(self) -> None:
        with TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            paper = temp / "paper.md"
            paper.write_text("Figure 1 and Figure 2 show increasing mock curves.", encoding="utf-8")
            out = temp / "case"
            audit = out / "audit"

            old_task_writer = os.environ.get("GENG_CODEX_TASK_WRITER_CMD")
            os.environ["GENG_CODEX_TASK_WRITER_CMD"] = _write_mock_task_writer(temp)
            try:
                result = run_codex_task_writer_workflow(
                    facts={"engineering_facts": []},
                    tasks={
                        "repro_tasks": [
                            {"task_id": "match_task", "figure_or_claim": "Fig. 1", "expected_artifacts": ["curve.png"]},
                            {"task_id": "gap_task", "figure_or_claim": "Fig. 2", "expected_artifacts": ["curve.png"]},
                        ]
                    },
                    experiment_index={"experiments": []},
                    paper={"format": "markdown", "chunks": []},
                    paper_path=paper,
                    paper_context_json="Figure 1 and Figure 2 show increasing mock curves.",
                    paper_thesis={"central_claim": "mock curves increase", "mechanism": "mock", "comparisons": []},
                    output_dir=out,
                    audit_dir=audit,
                    repro_project_dir=out / "repro_project",
                    client=DummyLLM(),
                    prompt_book=PromptBook(),
                    system_message="system",
                    run_repro=True,
                    result_review=True,
                    rounds=3,
                    timeout=30,
                    run_timeout=30,
                    resume=False,
                    agent_concurrency=2,
                )
            finally:
                if old_task_writer is None:
                    os.environ.pop("GENG_CODEX_TASK_WRITER_CMD", None)
                else:
                    os.environ["GENG_CODEX_TASK_WRITER_CMD"] = old_task_writer

            self.assertTrue(result["runtime_result"]["passed"], msg=json.dumps(result["runtime_result"], ensure_ascii=False))
            self.assertFalse(result["runtime_result"]["host_repeated_full"])
            self.assertEqual(result["result_review_result"]["mode"], "codex_task_writer_self_review")
            self.assertFalse((out / "result_review.json").exists())
            self.assertFalse(list(audit.glob("*reviewer*")))

            statuses = {
                item["task_id"]: item["task_writer_status"]
                for item in result["runtime_result"]["per_task"]
            }
            self.assertEqual(statuses["match_task"], "matched")
            self.assertEqual(statuses["gap_task"], "explained_gap")

            manifest = json.loads((out / "repro_project" / "tasks_manifest.json").read_text(encoding="utf-8"))
            manifest_by_task = {item["task_id"]: item for item in manifest["tasks"]}
            self.assertEqual(manifest_by_task["match_task"]["config_full"], "configs/match_task_config.json")
            self.assertEqual(manifest_by_task["gap_task"]["config_smoke"], "configs/gap_task_config_smoke.json")

            for task_id in ("match_task", "gap_task"):
                task_dir = out / "repro_project" / "outputs" / task_id
                self.assertTrue((task_dir / "results.csv").exists())
                self.assertTrue((task_dir / "curve.png").exists())
                self.assertTrue((task_dir / "summary.json").exists())
                self.assertTrue((task_dir / "task_agent_result.json").exists())
                self.assertFalse((task_dir / "task_agent_runs.jsonl").exists())

            review_md = (out / "result_review.md").read_text(encoding="utf-8")
            self.assertTrue(review_md.startswith("## 1. match_task"))
            self.assertIn("**Writer 结论：** `matched`", review_md)
            self.assertIn("**Writer 结论：** `explained_gap`", review_md)
            self.assertIn("| 本地复现图 | 论文原图 |", review_md)
            self.assertIn("### 简短审查结论", review_md)
            self.assertIn("## 附录：Writer 自审原文", review_md)
            self.assertIn("### A1. match_task", review_md)
            self.assertNotIn("### Writer 自审正文", review_md)

            prompts = (temp / "task_writer_prompts.txt").read_text(encoding="utf-8")
            self.assertEqual(prompts.count("---PROMPT---"), 2)
            self.assertIn("Assigned task_id: `match_task`", prompts)
            self.assertIn("Do not run `python run_experiment.py config.json`", prompts)
            self.assertIn("Mandatory self-iteration protocol", prompts)
            self.assertIn("Do not stop after the first imperfect output", prompts)
            self.assertIn("continue to the next repair/rerun cycle until cycle 3", prompts)

    def test_task_writer_paper_images_use_writer_declared_target_image(self) -> None:
        with TemporaryDirectory() as temp_dir:
            sandbox = Path(temp_dir)
            output_dir = sandbox / "outputs" / "crop_task"
            output_dir.mkdir(parents=True)
            target = output_dir / "paper_target_crop.png"
            target.write_bytes(base64.b64decode(PNG_B64))

            images, warnings, errors = _task_paper_image_paths(
                sandbox=sandbox,
                output_subdir="crop_task",
                result_doc={"paper_image_paths": ["outputs/crop_task/paper_target_crop.png"]},
                locator_doc={},
            )

            self.assertEqual(errors, [])
            self.assertEqual(warnings, [])
            self.assertEqual(images, [str(target.resolve())])

    def test_task_writer_paper_images_reject_raw_rendered_page(self) -> None:
        with TemporaryDirectory() as temp_dir:
            sandbox = Path(temp_dir)
            evidence_dir = sandbox / "paper_evidence" / "01_crop_task"
            evidence_dir.mkdir(parents=True)
            raw_page = evidence_dir / "paper_page_1.png"
            raw_page.write_bytes(base64.b64decode(PNG_B64))

            images, warnings, errors = _task_paper_image_paths(
                sandbox=sandbox,
                output_subdir="crop_task",
                result_doc={"paper_image_paths": ["paper_evidence/01_crop_task/paper_page_1.png"]},
                locator_doc={},
            )

            self.assertEqual(images, [])
            self.assertIn("writer declared paper_image_paths but none were usable", warnings)
            self.assertTrue(any("not raw page" in error for error in errors))

    def test_task_writer_paper_images_reject_renamed_raw_rendered_page(self) -> None:
        with TemporaryDirectory() as temp_dir:
            sandbox = Path(temp_dir)
            evidence_dir = sandbox / "paper_evidence" / "01_crop_task"
            evidence_dir.mkdir(parents=True)
            raw_bytes = base64.b64decode(PNG_B64)
            (evidence_dir / "paper_page_1.png").write_bytes(raw_bytes)
            output_dir = sandbox / "outputs" / "crop_task"
            output_dir.mkdir(parents=True)
            renamed = output_dir / "paper_target_crop.png"
            renamed.write_bytes(raw_bytes)

            images, warnings, errors = _task_paper_image_paths(
                sandbox=sandbox,
                output_subdir="crop_task",
                result_doc={"paper_image_paths": ["outputs/crop_task/paper_target_crop.png"]},
                locator_doc={},
            )

            self.assertEqual(images, [])
            self.assertIn("writer declared paper_image_paths but none were usable", warnings)
            self.assertTrue(any("unmodified rendered paper page" in error for error in errors))

    def test_task_writer_paper_image_root_path_is_copied_into_task_output(self) -> None:
        with TemporaryDirectory() as temp_dir:
            sandbox = Path(temp_dir)
            source = sandbox / "paper_target_locator.png"
            source.write_bytes(base64.b64decode(PNG_B64) + b"target")

            images, warnings, errors = _task_paper_image_paths(
                sandbox=sandbox,
                output_subdir="crop_task",
                result_doc={"paper_image_paths": ["paper_target_locator.png"]},
                locator_doc={},
            )

            expected = sandbox / "outputs" / "crop_task" / "paper_target_locator.png"
            self.assertEqual(errors, [])
            self.assertTrue(any("copied into outputs/crop_task" in warning for warning in warnings))
            self.assertEqual(images, [str(expected.resolve())])
            self.assertTrue(expected.exists())

    def test_task_writer_paper_image_path_does_not_basename_fallback_nested_paths(self) -> None:
        with TemporaryDirectory() as temp_dir:
            sandbox = Path(temp_dir)
            output_dir = sandbox / "outputs" / "crop_task"
            output_dir.mkdir(parents=True)
            (output_dir / "paper_target_crop.png").write_bytes(base64.b64decode(PNG_B64) + b"target")

            images, _warnings, errors = _task_paper_image_paths(
                sandbox=sandbox,
                output_subdir="crop_task",
                result_doc={"paper_image_paths": ["nested/paper_target_crop.png"]},
                locator_doc={},
            )

            self.assertEqual(images, [])
            self.assertTrue(any("does not exist" in error for error in errors))

    def test_paper_locator_doc_requires_minimum_fields(self) -> None:
        errors = _validate_paper_locator_doc({})

        self.assertTrue(any("target_figure" in error for error in errors))
        self.assertTrue(any("source_page" in error for error in errors))
        self.assertTrue(any("fallback_used" in error for error in errors))

    def test_task_writer_workflow_accepts_result_files_in_output_subdir(self) -> None:
        with TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            paper = temp / "paper.md"
            paper.write_text("Figure 1 shows a mock curve.", encoding="utf-8")
            out = temp / "case"
            audit = out / "audit"

            old_task_writer = os.environ.get("GENG_CODEX_TASK_WRITER_CMD")
            os.environ["GENG_CODEX_TASK_WRITER_CMD"] = _write_mock_task_writer(temp, result_location="output")
            try:
                result = run_codex_task_writer_workflow(
                    facts={"engineering_facts": []},
                    tasks={"repro_tasks": [{"task_id": "output_result_task", "figure_or_claim": "Fig. 1"}]},
                    experiment_index={"experiments": []},
                    paper={"format": "markdown", "chunks": []},
                    paper_path=paper,
                    paper_context_json="Figure 1 shows a mock curve.",
                    paper_thesis=None,
                    output_dir=out,
                    audit_dir=audit,
                    repro_project_dir=out / "repro_project",
                    client=DummyLLM(),
                    prompt_book=PromptBook(),
                    system_message="system",
                    run_repro=True,
                    result_review=True,
                    rounds=1,
                    timeout=30,
                    run_timeout=30,
                    resume=False,
                    agent_concurrency=1,
                )
            finally:
                if old_task_writer is None:
                    os.environ.pop("GENG_CODEX_TASK_WRITER_CMD", None)
                else:
                    os.environ["GENG_CODEX_TASK_WRITER_CMD"] = old_task_writer

            self.assertTrue(result["runtime_result"]["passed"], msg=json.dumps(result["runtime_result"], ensure_ascii=False))
            task_result = result["runtime_result"]["per_task"][0]
            self.assertEqual(task_result["errors"], [])
            self.assertIn("accepted as fallback", " ".join(task_result["warnings"]))
            self.assertTrue((out / "repro_project" / "outputs" / "output_result_task" / "task_agent_result.json").exists())

    def test_task_writer_workflow_marks_codex_usage_limit_as_blocked(self) -> None:
        with TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            paper = temp / "paper.md"
            paper.write_text("Figure 1 shows a mock curve.", encoding="utf-8")
            out = temp / "case"
            audit = out / "audit"

            old_task_writer = os.environ.get("GENG_CODEX_TASK_WRITER_CMD")
            os.environ["GENG_CODEX_TASK_WRITER_CMD"] = _write_usage_limited_task_writer(temp)
            try:
                result = run_codex_task_writer_workflow(
                    facts={"engineering_facts": []},
                    tasks={"repro_tasks": [{"task_id": "limited_task", "figure_or_claim": "Fig. 1"}]},
                    experiment_index={"experiments": []},
                    paper={"format": "markdown", "chunks": []},
                    paper_path=paper,
                    paper_context_json="Figure 1 shows a mock curve.",
                    paper_thesis=None,
                    output_dir=out,
                    audit_dir=audit,
                    repro_project_dir=out / "repro_project",
                    client=DummyLLM(),
                    prompt_book=PromptBook(),
                    system_message="system",
                    run_repro=True,
                    result_review=True,
                    rounds=1,
                    timeout=30,
                    run_timeout=30,
                    resume=False,
                    agent_concurrency=1,
                )
            finally:
                if old_task_writer is None:
                    os.environ.pop("GENG_CODEX_TASK_WRITER_CMD", None)
                else:
                    os.environ["GENG_CODEX_TASK_WRITER_CMD"] = old_task_writer

            runtime = result["runtime_result"]
            self.assertFalse(runtime["passed"])
            task_result = runtime["per_task"][0]
            self.assertEqual(task_result["writer_error_kind"], "codex_usage_limit")
            self.assertEqual(task_result["blocked_reason"], "Codex CLI usage limit exhausted")
            self.assertEqual(result["status"]["stop_class"], "blocked_by_codex")
            self.assertIn("额度", result["result_review_result"]["overall_summary"])

    def test_task_writer_workflow_accepts_delivery_without_trusted_full_log(self) -> None:
        with TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            paper = temp / "paper.md"
            paper.write_text("Figure 1 shows a mock curve.", encoding="utf-8")
            out = temp / "case"
            audit = out / "audit"

            old_task_writer = os.environ.get("GENG_CODEX_TASK_WRITER_CMD")
            os.environ["GENG_CODEX_TASK_WRITER_CMD"] = _write_spoofing_task_writer(temp)
            try:
                result = run_codex_task_writer_workflow(
                    facts={"engineering_facts": []},
                    tasks={"repro_tasks": [{"task_id": "spoof_task", "figure_or_claim": "Fig. 1"}]},
                    experiment_index={"experiments": []},
                    paper={"format": "markdown", "chunks": []},
                    paper_path=paper,
                    paper_context_json="Figure 1 shows a mock curve.",
                    paper_thesis=None,
                    output_dir=out,
                    audit_dir=audit,
                    repro_project_dir=out / "repro_project",
                    client=DummyLLM(),
                    prompt_book=PromptBook(),
                    system_message="system",
                    run_repro=True,
                    result_review=True,
                    rounds=1,
                    timeout=30,
                    run_timeout=30,
                    resume=False,
                    agent_concurrency=1,
                )
            finally:
                if old_task_writer is None:
                    os.environ.pop("GENG_CODEX_TASK_WRITER_CMD", None)
                else:
                    os.environ["GENG_CODEX_TASK_WRITER_CMD"] = old_task_writer

            self.assertTrue(result["runtime_result"]["passed"], msg=json.dumps(result["runtime_result"], ensure_ascii=False))
            task_result = result["runtime_result"]["per_task"][0]
            self.assertEqual(task_result["errors"], [])
            self.assertIn("task_agent_runs.jsonl contains no trusted guard records", " ".join(task_result["warnings"]))
            self.assertFalse((out / "repro_project" / "outputs" / "spoof_task" / "task_agent_runs.jsonl").exists())

    def test_task_writer_workflow_failed_status_does_not_pass_runtime(self) -> None:
        with TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            paper = temp / "paper.md"
            paper.write_text("Figure 1 shows a mock curve.", encoding="utf-8")
            out = temp / "case"
            audit = out / "audit"

            old_task_writer = os.environ.get("GENG_CODEX_TASK_WRITER_CMD")
            os.environ["GENG_CODEX_TASK_WRITER_CMD"] = _write_failed_delivery_task_writer(temp)
            try:
                result = run_codex_task_writer_workflow(
                    facts={"engineering_facts": []},
                    tasks={"repro_tasks": [{"task_id": "failed_task", "figure_or_claim": "Fig. 1"}]},
                    experiment_index={"experiments": []},
                    paper={"format": "markdown", "chunks": []},
                    paper_path=paper,
                    paper_context_json="Figure 1 shows a mock curve.",
                    paper_thesis=None,
                    output_dir=out,
                    audit_dir=audit,
                    repro_project_dir=out / "repro_project",
                    client=DummyLLM(),
                    prompt_book=PromptBook(),
                    system_message="system",
                    run_repro=True,
                    result_review=True,
                    rounds=1,
                    timeout=30,
                    run_timeout=30,
                    resume=False,
                    agent_concurrency=1,
                )
            finally:
                if old_task_writer is None:
                    os.environ.pop("GENG_CODEX_TASK_WRITER_CMD", None)
                else:
                    os.environ["GENG_CODEX_TASK_WRITER_CMD"] = old_task_writer

            runtime = result["runtime_result"]
            self.assertFalse(runtime["passed"], msg=json.dumps(runtime, ensure_ascii=False))
            self.assertEqual(runtime["coverage"], "0/1")
            self.assertEqual(runtime["delivery_coverage"], "1/1")
            task_result = runtime["per_task"][0]
            self.assertTrue(task_result["delivery_ok"])
            self.assertFalse(task_result["passed"])
            self.assertEqual(task_result["task_writer_status"], "failed")
            self.assertEqual(result["status"]["stop_class"], "task_failures_reported")
            self.assertEqual(result["result_review_result"]["overall_alignment"], "inconclusive")


class AgenticProjectWorkflowTests(unittest.TestCase):
    def test_markdown_review_partial_summary_blocks_success_and_feeds_back(self) -> None:
        markdown = "\n".join(
            [
                "## 结论",
                "本地复现部分支持 Fig. 7 的核心结论。",
                "整体科学结论更适合写成 partially_supports_paper_claim，不是强复现。",
                "",
                "## 主要差异",
                "1. ZF 排名差异明显。",
                "",
                "## 可能原因",
                "- baseline 用户选择策略不同。",
            ]
        )
        review = summarize_markdown_review(task_id="reproduce_fig_7", markdown=markdown)
        review_doc = {"_meta": {"markdown_review": True}, "experiment_reviews": [review]}

        self.assertEqual(review["scientific_verdict"], "partially_supports_paper_claim")
        self.assertEqual(review["paper_alignment"], "partial_match")
        self.assertFalse(_is_success({"passed": True, "coverage": "1/1"}, review_doc))

        score = _score_candidate(
            {"passed": True, "coverage": "1/1"},
            review_doc,
            {"enabled": True, "passed": True},
            {"ok": True},
            {"required_files_present": True, "python_compiles": True},
            [],
        )
        self.assertEqual(score["partial_count"], 1)
        self.assertEqual(score["scientific_gap_count"], 1)

        feedback = _feedback_from_results(
            {"passed": True, "coverage": "1/1"},
            review_doc,
            {"enabled": True, "passed": True},
        )
        self.assertEqual(feedback[0]["task_id"], "reproduce_fig_7")
        self.assertIn("ZF 排名差异明显", feedback[0]["differences"][0])
        self.assertIn("baseline 用户选择策略不同", feedback[0]["possible_causes"][0])

    def test_markdown_review_control_footer_overrides_ambiguous_text_and_is_stripped(self) -> None:
        markdown = "\n".join(
            [
                "## Conclusion",
                "The result is obviously inconsistent in one baseline, but the core claim is partially supported.",
                "",
                "<!-- geng-agent-review-summary",
                "task_id: reproduce_fig_7",
                "scientific_verdict: partially_supports_paper_claim",
                "paper_alignment: partial_match",
                "confidence: medium",
                "-->",
            ]
        )
        review = summarize_markdown_review(task_id="reproduce_fig_7", markdown=markdown)

        self.assertEqual(review["scientific_verdict"], "partially_supports_paper_claim")
        self.assertEqual(review["paper_alignment"], "partial_match")
        self.assertEqual(review["confidence"], "medium")
        stripped = strip_review_control_footer(markdown)
        self.assertNotIn("geng-agent-review-summary", stripped)
        self.assertIn("obviously inconsistent", stripped)

    def test_empty_markdown_review_doc_is_not_success(self) -> None:
        review_doc = {"_meta": {"markdown_review": True}, "experiment_reviews": []}
        self.assertFalse(_is_success({"passed": True, "coverage": "1/1"}, review_doc))

    def test_partial_review_feedback_is_actionable_for_next_writer_round(self) -> None:
        review_doc = {
            "experiment_reviews": [
                _review(
                    "partially_supports_paper_claim",
                    "partial_match",
                    differences=["Fig. 5 should use a log y-axis"],
                    causes=["plotting uses a linear axis"],
                )
            ]
        }
        feedback = _feedback_from_results(
            {"passed": True, "coverage": "1/1"},
            review_doc,
            {"enabled": True, "passed": True},
        )
        self.assertEqual(len(feedback), 1)
        self.assertEqual(feedback[0]["type"], "paper_alignment_gap")
        self.assertEqual(feedback[0]["scientific_verdict"], "partially_supports_paper_claim")
        self.assertIn("Fig. 5 should use a log y-axis", feedback[0]["differences"])

        brief = build_writer_brief(
            facts={"engineering_facts": []},
            tasks={"repro_tasks": [{"task_id": "reproduce_fig_1"}]},
            experiment_index={"experiments": []},
            paper_context_json="paper context",
            paper_thesis=None,
            task_manifest={
                "version": 1,
                "tasks": [{"task_id": "reproduce_fig_1", "module": "reproduce_fig_1", "script": "tasks/reproduce_fig_1.py"}],
            },
            expected_paths={"README.md", "tasks/reproduce_fig_1.py"},
            feedback=feedback,
            round_no=2,
            max_rounds=3,
        )
        self.assertIn("Mandatory moderator feedback", brief)
        self.assertIn("supports_paper_claim", brief)
        self.assertIn("Fig. 5 should use a log y-axis", brief)
        self.assertNotIn("No reviewer/runtime feedback yet.", brief)

    def test_partial_reviews_count_as_scientific_gap_for_best_round(self) -> None:
        partial_doc = {
            "experiment_reviews": [
                _review("partially_supports_paper_claim", "partial_match", differences=["still partial"])
            ]
        }
        support_doc = {"experiment_reviews": [_review("supports_paper_claim", "match")]}

        common_runtime = {"passed": True, "coverage": "1/1"}
        common_status = {"enabled": True, "passed": True}
        common_writer = {"ok": True}
        common_validation = {"required_files_present": True, "python_compiles": True}

        partial_score = _score_candidate(
            common_runtime, partial_doc, common_status, common_writer, common_validation, []
        )
        support_score = _score_candidate(
            common_runtime, support_doc, common_status, common_writer, common_validation, []
        )

        self.assertEqual(partial_score["partial_count"], 1)
        self.assertEqual(partial_score["scientific_gap_count"], 1)
        self.assertEqual(support_score["support_count"], 1)
        self.assertEqual(support_score["scientific_gap_count"], 0)
        self.assertTrue(_is_better_score(support_score, partial_score))

    def test_best_round_score_keeps_runtime_coverage_fallbacks(self) -> None:
        review_doc = {"experiment_reviews": [_review("supports_paper_claim", "match")]}
        status = {"enabled": True, "passed": True}
        writer = {"ok": True}
        validation = {"required_files_present": True, "python_compiles": True}

        nested_score = _score_candidate(
            {"passed": True, "full": {"coverage": "4/4"}},
            review_doc,
            status,
            writer,
            validation,
            [],
        )
        passed_only_score = _score_candidate(
            {"passed": True},
            review_doc,
            status,
            writer,
            validation,
            [],
        )

        self.assertEqual(nested_score["coverage_passed"], 4)
        self.assertEqual(nested_score["coverage_total"], 4)
        self.assertEqual(passed_only_score["coverage_passed"], 1)
        self.assertEqual(passed_only_score["coverage_total"], 1)

    def test_codex_loop_stops_after_configured_stall_rounds(self) -> None:
        with TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            paper = temp / "paper.md"
            paper.write_text("Figure 1 shows rate increasing with SNR.", encoding="utf-8")
            out = temp / "case"
            audit = out / "audit"

            old_writer = os.environ.get("GENG_CODEX_WRITER_CMD")
            old_reviewer = os.environ.get("GENG_CODEX_REVIEWER_CMD")
            os.environ["GENG_CODEX_WRITER_CMD"] = _write_mock_writer(temp)
            os.environ["GENG_CODEX_REVIEWER_CMD"] = _write_partial_reviewer(temp)
            try:
                result = run_codex_project_workflow(
                    facts={"engineering_facts": []},
                    tasks={"repro_tasks": [{"task_id": "reproduce_fig_1", "expected_artifacts": ["fig1.png"]}]},
                    experiment_index={"experiments": []},
                    paper={"format": "markdown", "chunks": []},
                    paper_path=paper,
                    paper_context_json="Figure 1 shows rate increasing with SNR.",
                    paper_thesis=None,
                    output_dir=out,
                    audit_dir=audit,
                    repro_project_dir=out / "repro_project",
                    client=DummyLLM(),
                    prompt_book=PromptBook(),
                    system_message="system",
                    run_repro=True,
                    result_review=True,
                    rounds=5,
                    stall_rounds=2,
                    timeout=30,
                    run_timeout=30,
                    resume=False,
                )
            finally:
                if old_writer is None:
                    os.environ.pop("GENG_CODEX_WRITER_CMD", None)
                else:
                    os.environ["GENG_CODEX_WRITER_CMD"] = old_writer
                if old_reviewer is None:
                    os.environ.pop("GENG_CODEX_REVIEWER_CMD", None)
                else:
                    os.environ["GENG_CODEX_REVIEWER_CMD"] = old_reviewer

            status = result["status"]
            self.assertEqual(status["rounds_requested"], 5)
            self.assertEqual(status["stall_rounds_requested"], 2)
            self.assertEqual(status["rounds_run"], 3)
            self.assertEqual(status["best_round"], 1)
            self.assertEqual(status["stop_class"], "plateau")
            self.assertIn("best score did not improve", status["stopped_reason"])
            self.assertEqual([item["improved_best"] for item in status["rounds"]], [True, False, False])
            self.assertEqual([item["stall_count"] for item in status["rounds"]], [0, 1, 2])
            prompts = (temp / "writer_prompts.txt").read_text(encoding="utf-8")
            self.assertEqual(prompts.count("---PROMPT---"), 3)
            self.assertIn("Writer self-tests are smoke-only", prompts)
            self.assertIn("Forbidden for writer self-checks", prompts)
            writer_env = json.loads((temp / "writer_env.txt").read_text(encoding="utf-8"))
            self.assertEqual(writer_env["mode"], "smoke_only")
            self.assertIn("writer_python_shim", writer_env["geng_python"])
            self.assertEqual(writer_env["geng_python"], writer_env["python"])
            self.assertIn("writer_python_shim", writer_env["path0"])

    def test_codex_writer_reviewer_loop_restores_trusted_files_and_feeds_back_review(self) -> None:
        with TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            paper = temp / "paper.md"
            paper.write_text("Figure 1 shows rate increasing with SNR.", encoding="utf-8")
            out = temp / "case"
            audit = out / "audit"
            writer_cmd = _write_mock_writer(temp)
            reviewer_cmd = _write_mock_reviewer(temp)

            import os

            old_writer = os.environ.get("GENG_CODEX_WRITER_CMD")
            old_reviewer = os.environ.get("GENG_CODEX_REVIEWER_CMD")
            os.environ["GENG_CODEX_WRITER_CMD"] = writer_cmd
            os.environ["GENG_CODEX_REVIEWER_CMD"] = reviewer_cmd
            try:
                result = run_codex_project_workflow(
                    facts={"engineering_facts": []},
                    tasks={
                        "repro_tasks": [
                            {"task_id": "reproduce_fig_1", "expected_artifacts": ["fig1.png"]},
                            {"task_id": "reproduce_fig_2", "expected_artifacts": ["fig2.png"]},
                        ]
                    },
                    experiment_index={"experiments": []},
                    paper={"format": "markdown", "chunks": []},
                    paper_path=paper,
                    paper_context_json="Figure 1 shows rate increasing with SNR.",
                    paper_thesis={"central_claim": "rate increases", "mechanism": "mock", "comparisons": []},
                    output_dir=out,
                    audit_dir=audit,
                    repro_project_dir=out / "repro_project",
                    client=DummyLLM(),
                    prompt_book=PromptBook(),
                    system_message="system",
                    run_repro=True,
                    result_review=True,
                    rounds=2,
                    timeout=30,
                    run_timeout=30,
                    resume=False,
                )
            finally:
                if old_writer is None:
                    os.environ.pop("GENG_CODEX_WRITER_CMD", None)
                else:
                    os.environ["GENG_CODEX_WRITER_CMD"] = old_writer
                if old_reviewer is None:
                    os.environ.pop("GENG_CODEX_REVIEWER_CMD", None)
                else:
                    os.environ["GENG_CODEX_REVIEWER_CMD"] = old_reviewer

            self.assertTrue(result["runtime_result"]["passed"])
            self.assertTrue(result["result_review_result"]["passed"])
            self.assertEqual(result["status"]["best_round"], 1)
            self.assertIn("def begin", (out / "repro_project" / "src" / "_io.py").read_text(encoding="utf-8"))
            self.assertIn("Auto-generated run-all dispatcher", (out / "repro_project" / "run_experiment.py").read_text(encoding="utf-8"))
            self.assertFalse((out / "repro_project" / "unexpected.py").exists())
            evidence_index_path = out / "repro_project" / "paper_evidence" / "index.json"
            self.assertTrue(evidence_index_path.exists())
            self.assertTrue(evidence_index_path.parent.is_dir())
            evidence_index = json.loads(evidence_index_path.read_text(encoding="utf-8"))
            self.assertEqual(evidence_index["kind"], "task_scoped_paper_evidence")
            self.assertTrue(evidence_index["paper_source"]["copied"])
            task_evidence_rel = evidence_index["tasks"][0]["task_evidence_json"]
            self.assertTrue((out / "repro_project" / task_evidence_rel).exists())
            self.assertFalse((out / "result_review.json").exists())
            self.assertTrue((out / "result_review.md").exists())
            round1_brief = (audit / "03c_agentic_project_round_01_writer_brief.md").read_text(encoding="utf-8")
            self.assertIn("Task-level paper evidence bundle", round1_brief)
            self.assertIn(task_evidence_rel, round1_brief)
            self.assertIn("Writer self-tests are smoke-only", round1_brief)
            self.assertIn("python run_experiment.py config.json", round1_brief)
            writer_env = json.loads((temp / "writer_env.txt").read_text(encoding="utf-8"))
            self.assertEqual(writer_env["mode"], "smoke_only")
            self.assertIn("writer_python_shim", writer_env["geng_python"])
            self.assertEqual(writer_env["geng_python"], writer_env["python"])
            self.assertIn("writer_python_shim", writer_env["path0"])
            result_review_md = (out / "result_review.md").read_text(encoding="utf-8")
            self.assertTrue(result_review_md.startswith("## 1. reproduce_fig_1"))
            self.assertNotIn("输入证据摘要", result_review_md)
            self.assertNotIn("审查任务状态", result_review_md)
            self.assertIn("![本地复现图:", result_review_md)
            self.assertIn("### 审查正文", result_review_md)
            self.assertIn("本地曲线与论文趋势一致", result_review_md)

    def test_failed_reviewer_round_cannot_win_best_round_or_leave_stale_error(self) -> None:
        with TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            paper = temp / "paper.md"
            paper.write_text("Figure 1 shows rate increasing with SNR.", encoding="utf-8")
            out = temp / "case"
            audit = out / "audit"
            writer_cmd = _write_mock_writer(temp)
            reviewer_cmd = _write_failing_after_first_reviewer(temp)

            import os

            old_writer = os.environ.get("GENG_CODEX_WRITER_CMD")
            old_reviewer = os.environ.get("GENG_CODEX_REVIEWER_CMD")
            os.environ["GENG_CODEX_WRITER_CMD"] = writer_cmd
            os.environ["GENG_CODEX_REVIEWER_CMD"] = reviewer_cmd
            try:
                result = run_codex_project_workflow(
                    facts={"engineering_facts": []},
                    tasks={
                        "repro_tasks": [
                            {"task_id": "reproduce_fig_1", "expected_artifacts": ["fig1.png"]},
                            {"task_id": "reproduce_fig_2", "expected_artifacts": ["fig2.png"]},
                        ]
                    },
                    experiment_index={"experiments": []},
                    paper={"format": "markdown", "chunks": []},
                    paper_path=paper,
                    paper_context_json="Figure 1 shows rate increasing with SNR.",
                    paper_thesis={"central_claim": "rate increases", "mechanism": "mock", "comparisons": []},
                    output_dir=out,
                    audit_dir=audit,
                    repro_project_dir=out / "repro_project",
                    client=DummyLLM(),
                    prompt_book=PromptBook(),
                    system_message="system",
                    run_repro=True,
                    result_review=True,
                    rounds=2,
                    timeout=30,
                    run_timeout=30,
                    resume=False,
                )
            finally:
                if old_writer is None:
                    os.environ.pop("GENG_CODEX_WRITER_CMD", None)
                else:
                    os.environ["GENG_CODEX_WRITER_CMD"] = old_writer
                if old_reviewer is None:
                    os.environ.pop("GENG_CODEX_REVIEWER_CMD", None)
                else:
                    os.environ["GENG_CODEX_REVIEWER_CMD"] = old_reviewer

            self.assertEqual(result["status"]["best_round"], 1)
            self.assertTrue(result["result_review_result"]["passed"])
            self.assertEqual(result["result_review_result"]["partial_failures"], 1)
            round1_score = result["status"]["rounds"][0]["score"]
            self.assertEqual(round1_score["cannot_assess_count"], 1)
            self.assertGreater(round1_score["scientific_gap_count"], 0)
            self.assertNotEqual(
                result["status"].get("stopped_reason"),
                "runtime passed and every reviewed task supports the paper claim",
            )
            self.assertTrue((out / "result_review.md").exists())
            self.assertFalse((out / "result_review_error.json").exists())
            review_md = (out / "result_review.md").read_text(encoding="utf-8")
            self.assertIn("Mock review", review_md)
            self.assertIn("Reviewer failed", review_md)

    def test_task_markdown_section_does_not_embed_raw_paper_page_as_final_original(self) -> None:
        section = _render_task_markdown_section(
            index=1,
            task_id="reproduce_fig_1",
            image_entries=[
                {
                    "label": "local_output:reproduce_fig_1/fig1.png",
                    "kind": "local_output",
                    "mime_type": "image/png",
                    "path": "C:/tmp/local_fig1.png",
                },
                {
                    "label": "paper_page:3",
                    "kind": "paper_page",
                    "mime_type": "image/png",
                    "path": "C:/tmp/paper_page_3.png",
                },
            ],
            body_markdown="Reviewer body.",
        )

        self.assertTrue(section.startswith("## 1. reproduce_fig_1"))
        self.assertIn("![本地复现图: reproduce_fig_1/fig1.png](C:/tmp/local_fig1.png)", section)
        self.assertNotIn("![论文原图页: p3](C:/tmp/paper_page_3.png)", section)
        self.assertIn("未记录。", section)
        self.assertIn("### 审查正文", section)

    def test_task_markdown_section_prefers_writer_paper_target_when_available(self) -> None:
        section = _render_task_markdown_section(
            index=1,
            task_id="reproduce_fig_2",
            image_entries=[
                {
                    "label": "paper_page:3",
                    "kind": "paper_page",
                    "mime_type": "image/png",
                    "path": "C:/tmp/paper_page_3.png",
                },
                {
                    "label": "paper_target:Fig. 2",
                    "kind": "paper_target",
                    "mime_type": "image/png",
                    "path": "C:/tmp/paper_target_crop.png",
                },
            ],
            body_markdown="Reviewer body.",
        )

        self.assertIn("![论文目标图: Fig. 2](C:/tmp/paper_target_crop.png)", section)
        self.assertNotIn("![论文原图页: p3](C:/tmp/paper_page_3.png)", section)

    def test_review_image_kind_classifies_writer_paper_images(self) -> None:
        self.assertEqual(_review_image_kind("paper_target:Fig. 2"), "paper_target")
        self.assertEqual(_review_image_kind("paper_locator:Fig. 2"), "paper_locator")

    def test_augment_review_display_images_no_longer_auto_crops_paper_pages(self) -> None:
        entries = _augment_review_display_images(
            task={"task_id": "reproduce_fig_9a", "figure_or_claim": "Fig. 9(a) BER curve"},
            image_entries=[
                {
                    "label": "paper_page:1",
                    "kind": "paper_page",
                    "mime_type": "image/png",
                    "path": "C:/tmp/paper_page_1.png",
                }
            ],
        )

        self.assertEqual(entries[0]["kind"], "paper_page")
        self.assertFalse(any(entry.get("kind") != "paper_page" for entry in entries))

    def test_cached_markdown_review_ignored_when_newer_error_exists(self) -> None:
        with TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            out = temp / "case"
            audit = out / "audit"
            paper = temp / "paper.md"
            paper.write_text("Figure 1 shows rate increasing with SNR.", encoding="utf-8")

            old_writer = os.environ.get("GENG_CODEX_WRITER_CMD")
            old_reviewer = os.environ.get("GENG_CODEX_REVIEWER_CMD")
            os.environ["GENG_CODEX_WRITER_CMD"] = _write_mock_writer(temp)
            os.environ["GENG_CODEX_REVIEWER_CMD"] = _write_mock_reviewer(temp)
            try:
                run_codex_project_workflow(
                    facts={"engineering_facts": []},
                    tasks={"repro_tasks": [{"task_id": "reproduce_fig_1", "expected_artifacts": ["fig1.png"]}]},
                    experiment_index={"experiments": []},
                    paper={"format": "markdown", "chunks": []},
                    paper_path=paper,
                    paper_context_json="Figure 1 shows rate increasing with SNR.",
                    paper_thesis=None,
                    output_dir=out,
                    audit_dir=audit,
                    repro_project_dir=out / "repro_project",
                    client=DummyLLM(),
                    prompt_book=PromptBook(),
                    system_message="system",
                    run_repro=True,
                    result_review=True,
                    rounds=1,
                    timeout=30,
                    run_timeout=30,
                    resume=False,
                )
            finally:
                if old_writer is None:
                    os.environ.pop("GENG_CODEX_WRITER_CMD", None)
                else:
                    os.environ["GENG_CODEX_WRITER_CMD"] = old_writer
                if old_reviewer is None:
                    os.environ.pop("GENG_CODEX_REVIEWER_CMD", None)
                else:
                    os.environ["GENG_CODEX_REVIEWER_CMD"] = old_reviewer

            review_md = out / "result_review.md"
            self.assertTrue(review_md.exists())
            error_path = out / "result_review_error.json"
            error_path.write_text(json.dumps({"enabled": True, "passed": False, "error": "newer failure"}), encoding="utf-8")
            os.utime(review_md, (1, 1))
            os.utime(error_path, (2, 2))

            cached = _load_cached_agentic_project(
                output_dir=out,
                repro_project_dir=out / "repro_project",
                run_repro=True,
                result_review=True,
            )
            self.assertIsNone(cached)

    def test_writer_brief_includes_dependency_snapshot_and_backend_runtime_api(self) -> None:
        with TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            paper = temp / "paper.md"
            paper.write_text("Figure 1 shows rate increasing with SNR.", encoding="utf-8")
            out = temp / "case"
            audit = out / "audit"
            writer_cmd = _write_mock_writer(temp)
            reviewer_cmd = _write_mock_reviewer(temp)

            import os

            old_writer = os.environ.get("GENG_CODEX_WRITER_CMD")
            old_reviewer = os.environ.get("GENG_CODEX_REVIEWER_CMD")
            os.environ["GENG_CODEX_WRITER_CMD"] = writer_cmd
            os.environ["GENG_CODEX_REVIEWER_CMD"] = reviewer_cmd
            try:
                run_codex_project_workflow(
                    facts={"engineering_facts": []},
                    tasks={"repro_tasks": [{"task_id": "reproduce_fig_1", "expected_artifacts": ["fig1.png"]}]},
                    experiment_index={"experiments": []},
                    paper={"format": "markdown", "chunks": []},
                    paper_path=paper,
                    paper_context_json="Figure 1 shows rate increasing with SNR.",
                    paper_thesis=None,
                    output_dir=out,
                    audit_dir=audit,
                    repro_project_dir=out / "repro_project",
                    client=DummyLLM(),
                    prompt_book=PromptBook(),
                    system_message="system",
                    run_repro=True,
                    result_review=True,
                    rounds=1,
                    timeout=30,
                    run_timeout=30,
                    resume=False,
                )
            finally:
                if old_writer is None:
                    os.environ.pop("GENG_CODEX_WRITER_CMD", None)
                else:
                    os.environ["GENG_CODEX_WRITER_CMD"] = old_writer
                if old_reviewer is None:
                    os.environ.pop("GENG_CODEX_REVIEWER_CMD", None)
                else:
                    os.environ["GENG_CODEX_REVIEWER_CMD"] = old_reviewer

            brief = (audit / "03c_agentic_project_round_01_writer_brief.md").read_text(encoding="utf-8")
            self.assertIn("Dependency policy snapshot", brief)
            self.assertIn("torch", brief)
            self.assertIn("src/_backend.py", brief)
            self.assertIn("_backend.select_backend", brief)

    def test_writer_brief_truncates_large_paper_context(self) -> None:
        brief = build_writer_brief(
            facts={"engineering_facts": []},
            tasks={"repro_tasks": [{"task_id": "t1"}]},
            experiment_index={"experiments": []},
            paper_context_json="x" * 60_000,
            paper_thesis=None,
            task_manifest={"version": 1, "tasks": [{"task_id": "t1", "module": "t1", "script": "tasks/t1.py"}]},
            expected_paths={"README.md", "tasks/t1.py"},
            feedback=[],
            round_no=1,
            max_rounds=3,
        )

        self.assertIn("global_paper_context_json truncated by geng-agent", brief)
        self.assertLess(len(brief), 60_000)

    def test_full_pipeline_codex_backend_generates_final_reports_with_fake_agents(self) -> None:
        with TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            paper = temp / "paper.md"
            paper.write_text("Simulation Results\nAWGN channel, BER vs SNR. Fig. 1 decreases.", encoding="utf-8")
            task_writer_cmd = _write_mock_task_writer(temp)

            import os

            old_task_writer = os.environ.get("GENG_CODEX_TASK_WRITER_CMD")
            os.environ["GENG_CODEX_TASK_WRITER_CMD"] = task_writer_cmd
            try:
                result = ReviewPipeline(client=MinimalPipelineLLM()).run(
                    paper,
                    temp / "case",
                    run_repro=True,
                    run_timeout=30,
                    repair_attempts=0,
                    result_review=True,
                    resume=False,
                    facts_gap_rounds=0,
                    tasks_gap_rounds=0,
                    analysis_backend="llm",
                    project_backend="codex",
                    codex_agent_rounds=2,
                    codex_agent_timeout=30,
                )
            finally:
                if old_task_writer is None:
                    os.environ.pop("GENG_CODEX_TASK_WRITER_CMD", None)
                else:
                    os.environ["GENG_CODEX_TASK_WRITER_CMD"] = old_task_writer

            self.assertTrue(result.runtime_passed)
            self.assertTrue(result.result_review_passed)
            self.assertIsNotNone(result.result_review_path)
            self.assertIsNotNone(result.result_review_docx_path)
            generated = json.loads((result.output_dir / "generated_files.json").read_text(encoding="utf-8"))
            self.assertTrue(generated["result_review"]["passed"])
            self.assertEqual(generated["result_review"]["overall_alignment"], "match")
            self.assertEqual(generated["result_review"]["overall_result_credibility"], "medium")
            self.assertEqual(generated["docx_generation"]["result_review_docx"]["passed"], True)
            self.assertNotEqual(result.reproducibility_verdict["verdict"], "inconclusive")
            run_cost = json.loads((result.output_dir / "run_cost.json").read_text(encoding="utf-8"))
            self.assertEqual(run_cost["codex_agent_mode"], "task-writers")

    def test_full_pipeline_codex_analysis_default_needs_no_llm_client(self) -> None:
        with TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            paper = temp / "paper.md"
            paper.write_text("Simulation Results\nAWGN channel, BER vs SNR. Fig. 1 decreases.", encoding="utf-8")
            analysis_cmd = _write_mock_analysis(temp)
            task_writer_cmd = _write_mock_task_writer(temp)

            old_analysis = os.environ.get("GENG_CODEX_ANALYSIS_CMD")
            old_task_writer = os.environ.get("GENG_CODEX_TASK_WRITER_CMD")
            os.environ["GENG_CODEX_ANALYSIS_CMD"] = analysis_cmd
            os.environ["GENG_CODEX_TASK_WRITER_CMD"] = task_writer_cmd
            try:
                result = ReviewPipeline().run(
                    paper,
                    temp / "case",
                    run_repro=True,
                    run_timeout=30,
                    repair_attempts=0,
                    result_review=True,
                    resume=False,
                    facts_gap_rounds=0,
                    tasks_gap_rounds=0,
                    project_backend="codex",
                    codex_agent_rounds=1,
                    codex_agent_timeout=30,
                    codex_analysis_timeout=30,
                )
            finally:
                if old_analysis is None:
                    os.environ.pop("GENG_CODEX_ANALYSIS_CMD", None)
                else:
                    os.environ["GENG_CODEX_ANALYSIS_CMD"] = old_analysis
                if old_task_writer is None:
                    os.environ.pop("GENG_CODEX_TASK_WRITER_CMD", None)
                else:
                    os.environ["GENG_CODEX_TASK_WRITER_CMD"] = old_task_writer

            self.assertTrue(result.runtime_passed)
            self.assertTrue(result.result_review_passed)
            facts = json.loads((result.output_dir / "engineering_facts.json").read_text(encoding="utf-8"))
            tasks = json.loads((result.output_dir / "repro_tasks.json").read_text(encoding="utf-8"))
            run_cost = json.loads((result.output_dir / "run_cost.json").read_text(encoding="utf-8"))
            self.assertEqual(facts["_meta"]["analysis_backend"], "codex")
            self.assertEqual(tasks["_meta"]["analysis_backend"], "codex")
            self.assertEqual(run_cost["analysis_backend"], "codex")
            prompts = (temp / "analysis_prompts.txt").read_text(encoding="utf-8")
            self.assertEqual(prompts.count("---PROMPT---"), 2)

    def test_writer_transport_failure_after_edits_is_explicitly_annotated(self) -> None:
        with TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            (temp / "writer_exit_1.flag").write_text("fail after edits", encoding="utf-8")
            paper = temp / "paper.md"
            paper.write_text("Figure 1 shows rate increasing with SNR.", encoding="utf-8")
            out = temp / "case"
            audit = out / "audit"
            writer_cmd = _write_mock_writer(temp)
            reviewer_cmd = _write_mock_reviewer(temp)

            import os

            old_writer = os.environ.get("GENG_CODEX_WRITER_CMD")
            old_reviewer = os.environ.get("GENG_CODEX_REVIEWER_CMD")
            os.environ["GENG_CODEX_WRITER_CMD"] = writer_cmd
            os.environ["GENG_CODEX_REVIEWER_CMD"] = reviewer_cmd
            try:
                result = run_codex_project_workflow(
                    facts={"engineering_facts": []},
                    tasks={"repro_tasks": [{"task_id": "reproduce_fig_1", "expected_artifacts": ["fig1.png"]}]},
                    experiment_index={"experiments": []},
                    paper={"format": "markdown", "chunks": []},
                    paper_path=paper,
                    paper_context_json="Figure 1 shows rate increasing with SNR.",
                    paper_thesis=None,
                    output_dir=out,
                    audit_dir=audit,
                    repro_project_dir=out / "repro_project",
                    client=DummyLLM(),
                    prompt_book=PromptBook(),
                    system_message="system",
                    run_repro=True,
                    result_review=True,
                    rounds=1,
                    timeout=30,
                    run_timeout=30,
                    resume=False,
                )
            finally:
                if old_writer is None:
                    os.environ.pop("GENG_CODEX_WRITER_CMD", None)
                else:
                    os.environ["GENG_CODEX_WRITER_CMD"] = old_writer
                if old_reviewer is None:
                    os.environ.pop("GENG_CODEX_REVIEWER_CMD", None)
                else:
                    os.environ["GENG_CODEX_REVIEWER_CMD"] = old_reviewer

            writer = result["status"]["rounds"][0]["writer"]
            self.assertFalse(writer["ok"])
            self.assertTrue(writer["transport_failed_after_edits"])
            self.assertTrue((audit / "03c_agentic_project_round_01_writer_post_run.json").exists())


if __name__ == "__main__":
    unittest.main()
