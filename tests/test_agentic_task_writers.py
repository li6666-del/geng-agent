import base64
import json
import os
import subprocess
import sys
import textwrap
import time
import unittest
from unittest.mock import MagicMock, patch
from pathlib import Path
from tempfile import TemporaryDirectory

from geng_agent.agentic_task_writers import (
    _apply_deterministic_task_writer_repairs,
    _dispatch_task_writers,
    _is_trusted_guard_record,
    _normalize_project_text_bom,
    _prepare_task_writer_python_guard,
    _run_one_task_writer,
    _task_local_image_paths,
    _task_paper_image_paths,
    _task_writer_concurrency,
    _task_writer_runtime_result,
    _validate_paper_locator_doc,
    run_codex_task_writer_workflow,
)
from geng_agent.task_writer_support import _load_cached_task_writer_workflow
from geng_agent.task_contract import build_task_contract_draft, contract_hash
from geng_agent.resource_runtime import ResourceBroker
from geng_agent.resource_scheduler import build_resource_plan
from geng_agent.stage_cleanup import _clear_stage_outputs


PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="


def _start_test_resource_broker(root: Path, *, task_id: str) -> tuple[ResourceBroker, dict[str, str]]:
    plan = build_resource_plan(task_count=1)
    plan["execution"].update(
        {
            "cpu_cores_budget": max(4, int(plan["execution"]["cpu_cores_budget"])),
            "ram_budget_gb": max(4.0, float(plan["execution"]["ram_budget_gb"])),
            "cpu_full_max": 1,
            "resource_poll_seconds": 0.05,
            "resource_wait_timeout_seconds": 5.0,
            "enforcement_poll_seconds": 0.05,
        }
    )
    broker = ResourceBroker(
        plan=plan,
        events_path=root / "host_resource_events.jsonl",
        state_path=root / "host_resource_state.json",
    )
    broker.start()
    channel = broker.register_channel(task_id=task_id, channel_dir=root / "project" / ".geng_resource_broker")
    return broker, channel


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

            manifest = json.loads((proj / "tasks_manifest.json").read_text(encoding="utf-8"))
            task = manifest["tasks"][0]
            task_id = task["task_id"]
            module = task["module"]
            output_subdir = task["output_subdir"]
            Path(__file__).with_name(f"task_writer_prompt_{task_id}.txt").write_text(
                "---PROMPT---\n" + prompt + "\n",
                encoding="utf-8",
            )
            result_location = "__RESULT_LOCATION__"

            contract = json.loads((proj / "task_contract.json").read_text(encoding="utf-8"))
            contract["backend"] = {"requested": "cpu", "allow_cpu_fallback": True}
            contract["resources"] = {
                "execution_class": "cpu_light",
                "cpu_cores": 2,
                "ram_gb": 1.0,
                "gpu_count": 0,
                "vram_gb": 0.0,
                "confidence": "high",
            }
            (proj / "task_contract.json").write_text(json.dumps(contract), encoding="utf-8")

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


class TaskWriterGuardTests(unittest.TestCase):
    def test_task_writer_concurrency_is_independent_from_full_slots(self) -> None:
        old_cpu = os.environ.get("GENG_TASK_WRITER_CPU_FULL_SLOTS")
        old_gpu = os.environ.get("GENG_TASK_WRITER_GPU_FULL_SLOTS")
        try:
            os.environ["GENG_TASK_WRITER_CPU_FULL_SLOTS"] = "1"
            os.environ["GENG_TASK_WRITER_GPU_FULL_SLOTS"] = "1"
            fake_plan = {"writer": {"initial_concurrency": 4}}
            with patch("geng_agent.agentic_task_writers.build_resource_plan", return_value=fake_plan):
                self.assertEqual(_task_writer_concurrency(4, 4, run_repro=True), 4)
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
            contract_path = project / "task_contract.json"
            contract_path.write_text(
                json.dumps(build_task_contract_draft({"task_id": "demo_task", "target": "demo"}, memory_snapshot_hash="test")),
                encoding="utf-8-sig",
            )
            broker, channel = _start_test_resource_broker(temp, task_id="demo_task")
            shim = _prepare_task_writer_python_guard(
                audit_dir=audit,
                label="task_01",
                task_id="demo_task",
                module="demo_task",
                output_subdir="demo_task",
                run_log=audit / "trusted_task_agent_runs.jsonl",
                allow_full=True,
                run_timeout=30,
                contract_path=contract_path,
                memory_snapshot_hash="test",
                resource_channel_dir=Path(channel["channel_dir"]),
                resource_channel_token=channel["token"],
                resource_wait_timeout=5.0,
                timeout_state_path=project / ".timeout_state.json",
            )
            env = dict(os.environ)
            env.update(shim["env"])
            self.assertTrue(Path(env["GENG_TASK_WRITER_BROKER_CHANNEL"]).is_relative_to(project))
            self.assertNotIn("GENG_TASK_WRITER_RESOURCE_PLAN", env)
            self.assertNotIn("GENG_TASK_WRITER_LOCK_DIR", env)
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
            self.assertEqual(
                records[-1]["contract_hash"],
                contract_hash(json.loads(contract_path.read_text(encoding="utf-8-sig"))),
            )
            self.assertFalse((project / "task_agent_runs.jsonl").exists())

            dispatcher = subprocess.run(
                [env["GENG_PYTHON"], "run_experiment.py", "config.json"],
                cwd=project,
                capture_output=True,
                text=True,
                timeout=30,
                env=env,
            )
            broker.stop()
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
            contract_path = project / "task_contract.json"
            contract = build_task_contract_draft({"task_id": "slow_task", "target": "slow"}, memory_snapshot_hash="test")
            contract["backend"] = {"requested": "cpu", "allow_cpu_fallback": True}
            contract["resources"] = {
                "execution_class": "cpu_light",
                "cpu_cores": 1,
                "ram_gb": 0.5,
                "gpu_count": 0,
                "vram_gb": 0.0,
                "confidence": "high",
            }
            contract_path.write_text(json.dumps(contract), encoding="utf-8")
            broker, channel = _start_test_resource_broker(temp, task_id="slow_task")
            shim = _prepare_task_writer_python_guard(
                audit_dir=audit,
                label="slow_task",
                task_id="slow_task",
                module="slow_task",
                output_subdir="slow_task",
                run_log=audit / "slow_runs.jsonl",
                allow_full=True,
                run_timeout=0.2,
                contract_path=contract_path,
                memory_snapshot_hash="test",
                resource_channel_dir=Path(channel["channel_dir"]),
                resource_channel_token=channel["token"],
                resource_wait_timeout=5.0,
                timeout_state_path=project / ".timeout_state.json",
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

            broker.stop()
            self.assertEqual(completed.returncode, 124)
            records = [json.loads(line) for line in (audit / "slow_runs.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertTrue(records[-1]["timed_out"])
            self.assertEqual(records[-1]["returncode"], 124)


class TaskWriterWorkflowTests(unittest.TestCase):
    def test_project_merge_normalizes_utf8_bom_before_scans(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "tasks").mkdir()
            script = root / "tasks" / "demo.py"
            requirements = root / "requirements.txt"
            script.write_text("print('ok')\n", encoding="utf-8-sig")
            requirements.write_bytes(b"numpy\n\xef\xbb\xbfmatplotlib\n")

            normalized = _normalize_project_text_bom(root)

            self.assertEqual(set(normalized), {"requirements.txt", "tasks/demo.py"})
            self.assertFalse(script.read_bytes().startswith(b"\xef\xbb\xbf"))
            self.assertEqual(requirements.read_text(encoding="utf-8"), "numpy\nmatplotlib\n")

    def test_host_deterministic_repairs_normalize_bom_confidence_and_paths(self) -> None:
        with TemporaryDirectory() as temp_dir:
            sandbox = Path(temp_dir)
            output_dir = sandbox / "outputs" / "demo_task"
            output_dir.mkdir(parents=True)
            for name in ("results.csv", "curve.png", "paper_crop.png"):
                (output_dir / name).write_bytes(b"content")

            result = {
                "task_id": "demo_task",
                "status": "matched",
                "evidence_files": [str((output_dir / "results.csv").resolve())],
                "local_image_paths": [str((output_dir / "curve.png").resolve())],
                "paper_image_paths": [str((output_dir / "paper_crop.png").resolve())],
            }
            locator = {
                "target_figure": "Fig. 1",
                "reason": "target",
                "confidence": "0.86",
                "source_page": 3.0,
                "fallback_used": False,
                "contains_only_target": True,
                "paper_image_paths": [str((output_dir / "paper_crop.png").resolve())],
            }
            (output_dir / "task_agent_result.json").write_bytes(
                b"\xef\xbb\xbf" + json.dumps(result).encode("utf-8")
            )
            (output_dir / "task_agent_result.md").write_bytes(b"\xef\xbb\xbf# report\n")
            (output_dir / "paper_target_figure.json").write_bytes(
                b"\xef\xbb\xbf" + json.dumps(locator).encode("utf-8")
            )

            actions = _apply_deterministic_task_writer_repairs(
                sandbox=sandbox,
                output_subdir="demo_task",
            )

            normalized_result = json.loads(
                (output_dir / "task_agent_result.json").read_text(encoding="utf-8")
            )
            normalized_locator = json.loads(
                (output_dir / "paper_target_figure.json").read_text(encoding="utf-8")
            )
            self.assertEqual(normalized_result["evidence_files"], ["outputs/demo_task/results.csv"])
            self.assertEqual(normalized_result["local_image_paths"], ["outputs/demo_task/curve.png"])
            self.assertEqual(normalized_locator["paper_image_paths"], ["outputs/demo_task/paper_crop.png"])
            self.assertEqual(normalized_locator["confidence"], "high")
            self.assertEqual(normalized_locator["confidence_score"], 0.86)
            self.assertEqual(normalized_locator["source_page"], 3)
            self.assertFalse((output_dir / "task_agent_result.md").read_bytes().startswith(b"\xef\xbb\xbf"))
            self.assertTrue(any(item["kind"] == "json_metadata_normalized" for item in actions))

    def test_resume_dispatch_never_reruns_a_passed_task(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            passed_sandbox = root / "sandboxes" / "01_task_1"
            failed_sandbox = root / "sandboxes" / "02_task_2"
            passed_sandbox.mkdir(parents=True)
            failed_sandbox.mkdir(parents=True)
            passed = {
                "index": 1,
                "task_id": "task_1",
                "module": "task_1",
                "sandbox": str(passed_sandbox),
                "structural_ok": True,
                "task_writer_status": "matched",
                "writer_status": {"ok": True},
            }
            failed = {
                "index": 2,
                "task_id": "task_2",
                "module": "task_2",
                "sandbox": str(failed_sandbox),
                "structural_ok": False,
                "task_writer_status": "failed",
                "writer_status": {"ok": True},
                "errors": ["missing valid local CSV artifact"],
            }
            calls: list[dict] = []

            def fake_writer(**kwargs):
                calls.append(kwargs)
                return {
                    "index": 2,
                    "task_id": "task_2",
                    "module": "task_2",
                    "sandbox": str(failed_sandbox),
                    "structural_ok": True,
                    "task_writer_status": "matched",
                    "writer_status": {"ok": True},
                }

            pairs = [
                ({"task_id": f"task_{index}"}, {"task_id": f"task_{index}", "module": f"task_{index}"})
                for index in (1, 2)
            ]
            plan = {
                "writer": {
                    "minimum_concurrency": 1,
                    "initial_concurrency": 2,
                    "max_concurrency": 2,
                    "successes_before_increase": 3,
                    "capacity_retries": 0,
                    "retry_base_seconds": 0.0,
                }
            }
            with patch("geng_agent.agentic_task_writers._run_one_task_writer", side_effect=fake_writer):
                records, audit = _dispatch_task_writers(
                    task_pairs=pairs,
                    facts={},
                    experiment_index={},
                    paper={},
                    paper_path=root / "paper.pdf",
                    paper_context_json="{}",
                    paper_thesis=None,
                    paper_memory=None,
                    memory_snapshot_hash="memory",
                    task_root=root / "sandboxes",
                    audit_dir=root / "audit",
                    rounds=5,
                    timeout=30,
                    run_timeout=30,
                    run_repro=True,
                    shared_failure_memory_path=root / "failure_memory.jsonl",
                    resource_plan=plan,
                    resource_plan_path=root / "resource_plan.json",
                    resource_broker=None,
                    initial_records_by_index={1: passed, 2: failed},
                )

            self.assertEqual([item["task_id"] for item in records], ["task_1", "task_2"])
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0]["index"], 2)
            self.assertTrue(calls[0]["reuse_existing"])
            self.assertIs(calls[0]["resume_record"], failed)
            self.assertEqual(audit["reused_task_ids"], ["task_1"])
            self.assertEqual(audit["repair_task_ids"], ["task_2"])

    def test_resume_cleanup_preserves_task_sandboxes_but_removes_final_project(self) -> None:
        with TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            sandbox_marker = output_dir / "audit" / "03c_task_writer_sandboxes" / "01_demo" / "marker.txt"
            sandbox_marker.parent.mkdir(parents=True)
            sandbox_marker.write_text("keep", encoding="utf-8")
            final_marker = output_dir / "repro_project" / "marker.txt"
            final_marker.parent.mkdir(parents=True)
            final_marker.write_text("remove", encoding="utf-8")

            _clear_stage_outputs(output_dir, "manifest", preserve_audit=True)

            self.assertTrue(sandbox_marker.exists())
            self.assertFalse((output_dir / "repro_project").exists())

    def test_resume_metadata_repair_reuses_sandbox_and_disables_full(self) -> None:
        record = self._run_mocked_resume_repair(
            first_errors=["missing or too-short task_agent_result.md"],
            expected_full_allowed=False,
        )
        self.assertTrue(record["structural_ok"])

    def test_resume_execution_repair_reuses_sandbox_and_allows_task_full(self) -> None:
        record = self._run_mocked_resume_repair(
            first_errors=["missing valid local CSV artifact"],
            expected_full_allowed=True,
        )
        self.assertTrue(record["structural_ok"])

    def test_resume_deterministic_success_does_not_wake_writer(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            task_root = root / "sandboxes"
            sandbox = task_root / "01_demo_task"
            sandbox.mkdir(parents=True)
            audit_dir = root / "audit"
            audit_dir.mkdir()
            broker = MagicMock()
            broker.register_channel.return_value = {}
            passed = {
                "index": 1,
                "task_id": "demo_task",
                "module": "demo_task",
                "structural_ok": True,
                "task_writer_status": "matched",
                "errors": [],
                "warnings": [],
            }

            with (
                patch("geng_agent.agentic_task_writers._prepare_task_writer_sandbox"),
                patch("geng_agent.agentic_task_writers._restore_trusted_files"),
                patch(
                    "geng_agent.agentic_task_writers._host_repair_and_validate_task_writer",
                    return_value=(passed, [{"kind": "utf8_bom_removed"}]),
                ),
                patch("geng_agent.agentic_task_writers._run_task_writer_codex_session") as run_session,
            ):
                result = _run_one_task_writer(
                    index=1,
                    attempt=2,
                    reuse_existing=True,
                    resume_record={"writer_status": {"ok": True}},
                    guard_token="token",
                    task={"task_id": "demo_task"},
                    manifest_entry={"task_id": "demo_task", "module": "demo_task", "output_subdir": "demo_task"},
                    facts={},
                    experiment_index={},
                    paper={},
                    paper_path=root / "paper.pdf",
                    paper_context_json="{}",
                    paper_thesis=None,
                    paper_memory=None,
                    memory_snapshot_hash="memory",
                    task_root=task_root,
                    audit_dir=audit_dir,
                    rounds=5,
                    timeout=30,
                    run_timeout=30,
                    run_repro=True,
                    shared_failure_memory_path=root / "failure_memory.jsonl",
                    resource_broker=broker,
                )

            run_session.assert_not_called()
            self.assertTrue(result["structural_ok"])
            self.assertEqual(result["host_repair_history"][0]["repair_kind"], "deterministic")

    def _run_mocked_resume_repair(self, *, first_errors: list[str], expected_full_allowed: bool) -> dict:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            task_root = root / "sandboxes"
            sandbox = task_root / "01_demo_task"
            (sandbox / "tasks").mkdir(parents=True)
            task_script = sandbox / "tasks" / "demo_task.py"
            task_script.write_text("ORIGINAL = True\n", encoding="utf-8")
            audit_dir = root / "audit"
            audit_dir.mkdir()
            broker = MagicMock()
            broker.register_channel.return_value = {}
            invalid = {
                "index": 1,
                "task_id": "demo_task",
                "module": "demo_task",
                "structural_ok": False,
                "task_writer_status": "failed",
                "result_json": {},
                "errors": first_errors,
                "warnings": [],
            }
            passed = {
                "index": 1,
                "task_id": "demo_task",
                "module": "demo_task",
                "structural_ok": True,
                "task_writer_status": "matched",
                "errors": [],
                "warnings": [],
            }

            def fake_repair_session(**kwargs):
                task_script.write_text("CHANGED = True\n", encoding="utf-8")
                return {"ok": True}

            with (
                patch("geng_agent.agentic_task_writers._prepare_task_writer_sandbox") as prepare_sandbox,
                patch("geng_agent.agentic_task_writers._restore_trusted_files"),
                patch(
                    "geng_agent.agentic_task_writers._host_repair_and_validate_task_writer",
                    side_effect=[(invalid, []), (passed, [])],
                ),
                patch(
                    "geng_agent.agentic_task_writers._run_task_writer_codex_session",
                    side_effect=fake_repair_session,
                ) as run_session,
            ):
                result = _run_one_task_writer(
                    index=1,
                    attempt=2,
                    reuse_existing=True,
                    resume_record={"writer_status": {"ok": True}},
                    guard_token="token",
                    task={"task_id": "demo_task"},
                    manifest_entry={"task_id": "demo_task", "module": "demo_task", "output_subdir": "demo_task"},
                    facts={},
                    experiment_index={},
                    paper={},
                    paper_path=root / "paper.pdf",
                    paper_context_json="{}",
                    paper_thesis=None,
                    paper_memory=None,
                    memory_snapshot_hash="memory",
                    task_root=task_root,
                    audit_dir=audit_dir,
                    rounds=5,
                    timeout=30,
                    run_timeout=30,
                    run_repro=True,
                    shared_failure_memory_path=root / "failure_memory.jsonl",
                    resource_broker=broker,
                )

            prepare_sandbox.assert_called_once()
            self.assertTrue(prepare_sandbox.call_args.kwargs["reuse_existing"])
            run_session.assert_called_once()
            self.assertEqual(run_session.call_args.kwargs["sandbox"], sandbox)
            self.assertEqual(run_session.call_args.kwargs["allow_full"], expected_full_allowed)
            expected_script = "CHANGED = True\n" if expected_full_allowed else "ORIGINAL = True\n"
            self.assertEqual(task_script.read_text(encoding="utf-8"), expected_script)
            return result

    def test_dispatch_retries_capacity_error_and_preserves_sandbox(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            calls = []

            def fake_writer(**kwargs):
                calls.append(kwargs)
                index = kwargs["index"]
                attempt = kwargs["attempt"]
                if index == 1 and attempt == 1:
                    return {
                        "index": index,
                        "task_id": "task_1",
                        "module": "task_1",
                        "structural_ok": False,
                        "task_writer_status": "failed",
                        "writer_error_kind": "codex_rate_limit",
                        "writer_status": {"ok": False, "error_kind": "codex_rate_limit"},
                    }
                return {
                    "index": index,
                    "task_id": f"task_{index}",
                    "module": f"task_{index}",
                    "structural_ok": True,
                    "task_writer_status": "matched",
                    "writer_error_kind": None,
                    "writer_status": {"ok": True},
                }

            pairs = [
                ({"task_id": f"task_{index}"}, {"task_id": f"task_{index}", "module": f"task_{index}"})
                for index in (1, 2)
            ]
            plan = {
                "writer": {
                    "minimum_concurrency": 1,
                    "initial_concurrency": 2,
                    "max_concurrency": 2,
                    "successes_before_increase": 3,
                    "capacity_retries": 1,
                    "retry_base_seconds": 0.0,
                }
            }
            with patch("geng_agent.agentic_task_writers._run_one_task_writer", side_effect=fake_writer):
                records, audit = _dispatch_task_writers(
                    task_pairs=pairs,
                    facts={},
                    experiment_index={},
                    paper={},
                    paper_path=root / "paper.pdf",
                    paper_context_json="{}",
                    paper_thesis=None,
                    paper_memory=None,
                    memory_snapshot_hash="memory",
                    task_root=root / "sandboxes",
                    audit_dir=root / "audit",
                    rounds=5,
                    timeout=30,
                    run_timeout=30,
                    run_repro=True,
                    shared_failure_memory_path=root / "failure_memory.jsonl",
                    resource_plan=plan,
                    resource_plan_path=root / "resource_plan.json",
                    resource_broker=None,
                )

            self.assertEqual([record["task_writer_status"] for record in records], ["matched", "matched"])
            first_task_calls = [item for item in calls if item["index"] == 1]
            self.assertEqual(len(first_task_calls), 2)
            self.assertFalse(first_task_calls[0]["reuse_existing"])
            self.assertTrue(first_task_calls[1]["reuse_existing"])
            self.assertIsNone(first_task_calls[1]["resume_record"])
            self.assertEqual(first_task_calls[0]["guard_token"], first_task_calls[1]["guard_token"])
            self.assertEqual(audit["concurrency_events"][0]["event"], "capacity_backoff")

    def test_dispatch_capacity_error_applies_global_cooldown(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            starts: dict[tuple[int, int], float] = {}
            capacity_time: list[float] = []

            def fake_writer(**kwargs):
                index = kwargs["index"]
                attempt = kwargs["attempt"]
                starts[(index, attempt)] = time.monotonic()
                if index == 1 and attempt == 1:
                    capacity_time.append(time.monotonic())
                    return {
                        "index": index,
                        "task_id": "task_1",
                        "module": "task_1",
                        "structural_ok": False,
                        "task_writer_status": "failed",
                        "writer_error_kind": "codex_rate_limit",
                        "writer_status": {"ok": False, "error_kind": "codex_rate_limit"},
                    }
                if index == 2:
                    time.sleep(0.05)
                return {
                    "index": index,
                    "task_id": f"task_{index}",
                    "module": f"task_{index}",
                    "structural_ok": True,
                    "task_writer_status": "matched",
                    "writer_error_kind": None,
                    "writer_status": {"ok": True},
                }

            pairs = [
                ({"task_id": f"task_{index}"}, {"task_id": f"task_{index}", "module": f"task_{index}"})
                for index in (1, 2, 3)
            ]
            plan = {
                "writer": {
                    "minimum_concurrency": 1,
                    "initial_concurrency": 2,
                    "max_concurrency": 2,
                    "successes_before_increase": 99,
                    "capacity_retries": 1,
                    "retry_base_seconds": 0.3,
                }
            }
            with patch("geng_agent.agentic_task_writers._run_one_task_writer", side_effect=fake_writer):
                records, audit = _dispatch_task_writers(
                    task_pairs=pairs,
                    facts={},
                    experiment_index={},
                    paper={},
                    paper_path=root / "paper.pdf",
                    paper_context_json="{}",
                    paper_thesis=None,
                    paper_memory=None,
                    memory_snapshot_hash="memory",
                    task_root=root / "sandboxes",
                    audit_dir=root / "audit",
                    rounds=5,
                    timeout=30,
                    run_timeout=30,
                    run_repro=True,
                    shared_failure_memory_path=root / "failure_memory.jsonl",
                    resource_plan=plan,
                    resource_plan_path=root / "resource_plan.json",
                    resource_broker=None,
                )

            self.assertEqual(len(records), 3)
            self.assertGreaterEqual(starts[(3, 1)] - capacity_time[0], 0.25)
            self.assertEqual(audit["concurrency_events"][0]["scope"], "global")

    def test_dispatch_does_not_scale_up_for_structurally_invalid_delivery(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            record = {
                "index": 1,
                "task_id": "task_1",
                "module": "task_1",
                "structural_ok": False,
                "task_writer_status": "explained_gap",
                "writer_error_kind": None,
                "writer_status": {"ok": True},
            }
            plan = {
                "writer": {
                    "minimum_concurrency": 1,
                    "initial_concurrency": 1,
                    "max_concurrency": 2,
                    "successes_before_increase": 1,
                    "capacity_retries": 0,
                    "retry_base_seconds": 0.0,
                }
            }
            with patch("geng_agent.agentic_task_writers._run_one_task_writer", return_value=record):
                _records, audit = _dispatch_task_writers(
                    task_pairs=[({"task_id": "task_1"}, {"task_id": "task_1", "module": "task_1"})],
                    facts={},
                    experiment_index={},
                    paper={},
                    paper_path=root / "paper.pdf",
                    paper_context_json="{}",
                    paper_thesis=None,
                    paper_memory=None,
                    memory_snapshot_hash="memory",
                    task_root=root / "sandboxes",
                    audit_dir=root / "audit",
                    rounds=5,
                    timeout=30,
                    run_timeout=30,
                    run_repro=True,
                    shared_failure_memory_path=root / "failure_memory.jsonl",
                    resource_plan=plan,
                    resource_plan_path=root / "resource_plan.json",
                    resource_broker=None,
                )

            self.assertEqual(audit["final_concurrency"], 1)
            self.assertFalse(any(item["event"] == "stable_success_increase" for item in audit["concurrency_events"]))

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

            prompts = "\n".join(
                path.read_text(encoding="utf-8")
                for path in sorted(temp.glob("task_writer_prompt_*.txt"))
            )
            self.assertEqual(prompts.count("---PROMPT---"), 2)
            self.assertIn("Assigned task_id: `match_task`", prompts)
            self.assertIn("Do not run `python run_experiment.py config.json`", prompts)
            self.assertIn("Mandatory self-iteration protocol", prompts)
            self.assertIn("Do not stop after the first imperfect output", prompts)
            self.assertIn("continue to the next repair/rerun cycle until cycle 3", prompts)

            cached = _load_cached_task_writer_workflow(
                output_dir=out,
                repro_project_dir=out / "repro_project",
                run_repro=True,
                result_review=True,
            )
            self.assertIsNotNone(cached)
            self.assertTrue(cached["status"]["cached"])

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

    def test_paper_locator_doc_accepts_multi_page_claim_evidence(self) -> None:
        errors = _validate_paper_locator_doc(
            {
                "target_figure": "Theorem and formula evidence across pages",
                "source_page": [6, 7],
                "bbox_norm": {
                    "page_6": [0.1, 0.2, 0.8, 0.9],
                    "page_7": [0.2, 0.1, 0.7, 0.4],
                },
                "confidence": "high",
                "contains_only_target": True,
                "fallback_used": False,
                "reason": "The evidence spans a theorem statement and its following formula.",
                "paper_image_paths": ["outputs/claim_task/paper_target_crop.png"],
            }
        )

        self.assertEqual(errors, [])

    def test_paper_locator_doc_accepts_numeric_confidence_score(self) -> None:
        errors = _validate_paper_locator_doc(
            {
                "target_figure": "Fig. 7",
                "source_page": 9,
                "bbox_norm": [0.1, 0.2, 0.8, 0.9],
                "confidence": 0.72,
                "contains_only_target": False,
                "fallback_used": True,
                "reason": "Red-box locator used for a multi-figure page.",
                "paper_image_paths": ["outputs/reproduce_fig_7/paper_target_locator.png"],
            }
        )

        self.assertEqual(errors, [])

    def test_trusted_guard_accepts_current_v2_records(self) -> None:
        base = {
            "guard_token": "token",
            "task_module": "demo",
            "output_subdir": "demo",
        }
        self.assertTrue(_is_trusted_guard_record({**base, "guard": "geng_task_writer_python_guard_v1"}, "token", "demo", "demo"))
        self.assertTrue(_is_trusted_guard_record({**base, "guard": "geng_task_writer_python_guard_v2"}, "token", "demo", "demo"))

    def test_task_writer_local_images_exclude_paper_target_outputs(self) -> None:
        with TemporaryDirectory() as temp_dir:
            sandbox = Path(temp_dir)
            output_dir = sandbox / "outputs" / "crop_task"
            output_dir.mkdir(parents=True)
            local = output_dir / "curve.png"
            paper = output_dir / "paper_target_crop.png"
            local.write_bytes(base64.b64decode(PNG_B64))
            paper.write_bytes(base64.b64decode(PNG_B64) + b"paper")

            images = _task_local_image_paths(
                sandbox,
                "crop_task",
                result_doc={
                    "local_image_paths": [
                        "outputs/crop_task/curve.png",
                        "outputs/crop_task/paper_target_crop.png",
                    ]
                },
                paper_images=[str(paper.resolve())],
            )

            self.assertEqual(images, [str(local.resolve())])

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


if __name__ == "__main__":
    unittest.main()
