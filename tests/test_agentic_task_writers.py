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
    _dispatch_task_writers,
    _prepare_task_writer_python_guard,
    _run_one_task_writer,
    _task_writer_concurrency,
    _task_writer_runtime_result,
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

            status = "explained_gap" if "gap" in task_id else "matched"
            result = {
                "task_id": task_id,
                "status": status,
                "summary": "mock writer completed the assigned full run",
                "differences": ["scale differs"] if status == "explained_gap" else [],
                "possible_causes": ["missing paper parameter"] if status == "explained_gap" else [],
                "remaining_uncertainties": ["exact seed"] if status == "explained_gap" else [],
                "evidence_files": [f"outputs/{output_subdir}/results.csv", f"outputs/{output_subdir}/curve.png"],
                "local_image_paths": [f"outputs/{output_subdir}/curve.png"],
            }
            result_dir = (proj / "outputs" / output_subdir) if result_location == "output" else proj
            result_dir.mkdir(parents=True, exist_ok=True)
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
                "evidence_files": [f"outputs/{{output_subdir}}/results.csv", f"outputs/{{output_subdir}}/curve.png"],
                "local_image_paths": [f"outputs/{{output_subdir}}/curve.png"],
            }}
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
            result = {{
                "task_id": task_id,
                "status": "failed",
                "summary": "writer could not complete the assigned scientific reproduction",
                "differences": [],
                "possible_causes": [],
                "remaining_uncertainties": [],
                "evidence_files": [],
                "local_image_paths": [f"outputs/{{output_subdir}}/curve.png"],
            }}
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
    def test_dispatch_launches_every_task_before_first_wait(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pairs = [
                ({"task_id": f"task_{index}"}, {"task_id": f"task_{index}", "module": f"task_{index}"})
                for index in range(1, 5)
            ]

            def fake_writer(**kwargs):
                time.sleep(0.05)
                index = int(kwargs["index"])
                return {
                    "index": index,
                    "task_id": f"task_{index}",
                    "module": f"task_{index}",
                    "writer_completed": True,
                    "task_writer_status": "matched",
                    "writer_status": {"ok": True},
                }

            plan = build_resource_plan(task_count=len(pairs))
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
                    timeout=30,
                    run_timeout=30,
                    run_repro=True,
                    resource_plan=plan,
                    resource_plan_path=root / "resource_plan.json",
                    resource_broker=None,
                )

            self.assertEqual(len(records), 4)
            self.assertEqual(
                audit["dispatch_batches"][0]["task_ids"],
                ["task_1", "task_2", "task_3", "task_4"],
            )
            self.assertTrue(audit["dispatch_batches"][0]["launched_before_wait"])

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
                "writer_completed": True,
                "task_writer_status": "matched",
                "writer_status": {"ok": True},
            }
            failed = {
                "index": 2,
                "task_id": "task_2",
                "module": "task_2",
                "sandbox": str(failed_sandbox),
                "writer_completed": False,
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
                    "writer_completed": True,
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
                    timeout=30,
                    run_timeout=30,
                    run_repro=True,
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
                        "writer_completed": False,
                        "task_writer_status": "failed",
                        "writer_error_kind": "codex_rate_limit",
                        "writer_status": {"ok": False, "error_kind": "codex_rate_limit"},
                    }
                return {
                    "index": index,
                    "task_id": f"task_{index}",
                    "module": f"task_{index}",
                    "writer_completed": True,
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
                    timeout=30,
                    run_timeout=30,
                    run_repro=True,
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
                        "writer_completed": False,
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
                    "writer_completed": True,
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
                    timeout=30,
                    run_timeout=30,
                    run_repro=True,
                    resource_plan=plan,
                    resource_plan_path=root / "resource_plan.json",
                    resource_broker=None,
                )

            self.assertEqual(len(records), 3)
            self.assertGreaterEqual(starts[(3, 1)] - capacity_time[0], 0.25)
            self.assertEqual(audit["concurrency_events"][0]["scope"], "global")

    def test_dispatch_does_not_treat_incomplete_writer_as_success(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            record = {
                "index": 1,
                "task_id": "task_1",
                "module": "task_1",
                "writer_completed": False,
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
                    timeout=30,
                    run_timeout=30,
                    run_repro=True,
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
                    "writer_completed": True,
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

    def test_task_writer_workflow_delivers_scientific_results_without_final_reports(self) -> None:
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
                    timeout=30,
                    run_timeout=30,
                    resume=False,
                )
            finally:
                if old_task_writer is None:
                    os.environ.pop("GENG_CODEX_TASK_WRITER_CMD", None)
                else:
                    os.environ["GENG_CODEX_TASK_WRITER_CMD"] = old_task_writer

            self.assertTrue(result["runtime_result"]["passed"], msg=json.dumps(result["runtime_result"], ensure_ascii=False))
            self.assertFalse(result["runtime_result"]["host_repeated_full"])
            self.assertEqual(result["writer_review_doc"]["_meta"]["mode"], "task_writer_scientific_results")
            self.assertFalse((out / "result_review.json").exists())
            self.assertFalse((out / "result_review.md").exists())
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

            prompts = "\n".join(
                path.read_text(encoding="utf-8")
                for path in sorted(temp.glob("task_writer_prompt_*.txt"))
            )
            self.assertEqual(prompts.count("---PROMPT---"), 2)
            self.assertIn("Assigned task_id: `match_task`", prompts)
            self.assertIn("Do not run `python run_experiment.py config.json`", prompts)
            self.assertIn("Mandatory self-iteration protocol", prompts)
            self.assertIn("Do not stop after the first imperfect output", prompts)
            self.assertIn("There is no cycle limit", prompts)
            self.assertIn("Never rerun an unchanged full", prompts)
            self.assertIn("dedicated report agent", prompts)
            self.assertNotIn("paper_target_figure.json", prompts)
            self.assertNotIn("paper_image_paths", prompts)

            cached = _load_cached_task_writer_workflow(
                output_dir=out,
                repro_project_dir=out / "repro_project",
                run_repro=True,
            )
            self.assertIsNotNone(cached)
            self.assertTrue(cached["status"]["cached"])

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
                    timeout=30,
                    run_timeout=30,
                    resume=False,
                )
            finally:
                if old_task_writer is None:
                    os.environ.pop("GENG_CODEX_TASK_WRITER_CMD", None)
                else:
                    os.environ["GENG_CODEX_TASK_WRITER_CMD"] = old_task_writer

            self.assertTrue(result["runtime_result"]["passed"], msg=json.dumps(result["runtime_result"], ensure_ascii=False))
            task_result = result["runtime_result"]["per_task"][0]
            self.assertTrue(task_result["writer_completed"])
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
                    timeout=30,
                    run_timeout=30,
                    resume=False,
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
            self.assertIn("额度", result["writer_review_doc"]["overall_summary"])

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
                    timeout=30,
                    run_timeout=30,
                    resume=False,
                )
            finally:
                if old_task_writer is None:
                    os.environ.pop("GENG_CODEX_TASK_WRITER_CMD", None)
                else:
                    os.environ["GENG_CODEX_TASK_WRITER_CMD"] = old_task_writer

            self.assertTrue(result["runtime_result"]["passed"], msg=json.dumps(result["runtime_result"], ensure_ascii=False))
            task_result = result["runtime_result"]["per_task"][0]
            self.assertTrue(task_result["writer_completed"])
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
                    timeout=30,
                    run_timeout=30,
                    resume=False,
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
            self.assertTrue(task_result["writer_completed"])
            self.assertFalse(task_result["passed"])
            self.assertEqual(task_result["task_writer_status"], "failed")
            self.assertEqual(result["status"]["stop_class"], "task_failures_reported")
            self.assertEqual(result["writer_review_doc"]["overall_alignment"], "inconclusive")


if __name__ == "__main__":
    unittest.main()
