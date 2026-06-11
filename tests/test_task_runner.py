import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest

from geng_agent.io_runtime import inject_io_runtime
from geng_agent.outputs import _valid_csv, _valid_png, _valid_summary_json
from geng_agent.prompts import PromptBook
from geng_agent.runner import run_repro_with_repair, run_tasks_once, run_tasks_with_repair, task_timeout_seconds
from geng_agent.task_scripts import build_tasks_manifest, write_task_scaffolding


MINIMAL_PNG_BYTES = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + (b"\x00" * 32)


def _good_task(task_id: str) -> str:
    return (
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
        f"    _io.begin({task_id!r}, cfg)\n"
        f"    _io.write_table({task_id!r}, ['x', 'y'], [{{'x': 1, 'y': 2.0}}, {{'x': 2, 'y': 3.5}}])\n"
        "    fig, ax = plt.subplots()\n"
        "    ax.plot([0, 1, 2], [1, 2, 3])\n"
        f"    _io.write_figure({task_id!r}, 'curve', fig)\n"
        f"    return _io.finish({task_id!r}, metrics={{'rows': 2}}, assumptions=[])\n"
        "if __name__ == '__main__':\n"
        "    raise SystemExit(main())\n"
    )


def _minimal_sleep_task(task_id: str, seconds: float) -> str:
    return (
        "import json\n"
        "import time\n"
        "from pathlib import Path\n"
        f"PNG_BYTES = {MINIMAL_PNG_BYTES!r}\n"
        "def main(config_path=None):\n"
        f"    time.sleep({seconds!r})\n"
        f"    out = Path('outputs') / {task_id!r}\n"
        "    out.mkdir(parents=True, exist_ok=True)\n"
        "    (out / 'results.csv').write_text('x\\n1\\n', encoding='utf-8')\n"
        "    (out / 'curve.png').write_bytes(PNG_BYTES)\n"
        f"    summary = {{'task_id': {task_id!r}, 'metrics': {{'rows': 1}}, 'assumptions': []}}\n"
        "    (out / 'summary.json').write_text(json.dumps(summary), encoding='utf-8')\n"
        "    return 0\n"
        "if __name__ == '__main__':\n"
        "    raise SystemExit(main())\n"
    )


CRASH_TASK = (
    "def main(config_path=None):\n"
    "    raise RuntimeError('boom: this task crashes hard')\n"
    "if __name__ == '__main__':\n"
    "    raise SystemExit(main())\n"
)

HANG_TASK = (
    "import time\n"
    "def main(config_path=None):\n"
    "    time.sleep(45)\n"  # far longer than its per-task timeout
    "if __name__ == '__main__':\n"
    "    raise SystemExit(main())\n"
)


def _build_project(root: Path, specs: list[tuple[str, str]]) -> dict:
    """specs = [(task_id, source), ...]. Returns the in-memory manifest."""
    inject_io_runtime(root)
    manifest = build_tasks_manifest({"repro_tasks": [{"task_id": tid} for tid, _ in specs]})
    write_task_scaffolding(root, manifest)
    module_by_id = {t["task_id"]: t["module"] for t in manifest["tasks"]}
    for task_id, source in specs:
        (root / "tasks" / f"{module_by_id[task_id]}.py").write_text(source, encoding="utf-8")
    for name in ("config.json", "config_smoke.json"):
        (root / name).write_text(json.dumps({"seed": 1}), encoding="utf-8")
    return manifest


BROKEN_TASK = (
    "from src import _io\n"
    "def main(config_path=None):\n"
    "    _io.begin('demo_task', {})\n"
    "    return this_symbol_is_undefined()\n"  # NameError at runtime, valid syntax
    "if __name__ == '__main__':\n"
    "    raise SystemExit(main())\n"
)


class RepairFakeLLM:
    """Returns a repair_manifest that replaces the failing task script with a working one."""

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, prompt, *, system=None, response_format=None) -> str:
        self.calls += 1
        return json.dumps(
            {
                "reason": "fix the NameError and write artifacts via _io",
                "touched_files": ["tasks/demo_task.py"],
                "scientific_changes": [],
                "files": [{"path": "tasks/demo_task.py", "content_lines": _good_task("demo_task").splitlines()}],
            }
        )


class TaskRepairTests(unittest.TestCase):
    def test_failing_task_is_repaired_on_its_own_script(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest = _build_project(root, [("demo_task", BROKEN_TASK)])
            client = RepairFakeLLM()

            result = run_tasks_with_repair(
                repro_project_dir=root,
                manifest=manifest,
                client=client,
                prompt_book=PromptBook(),
                system_message="system",
                max_repair_attempts=1,
            )

            self.assertTrue(result["passed"])
            self.assertEqual(result["coverage"], "1/1")
            self.assertGreaterEqual(client.calls, 1)  # repair was actually invoked
            # The fix landed on the real project's task script, and it now produces artifacts.
            self.assertIn("_io.finish", (root / "tasks" / "demo_task.py").read_text(encoding="utf-8"))
            self.assertTrue(_valid_csv(root / "outputs" / "demo_task" / "results.csv"))


class TaskRunnerIsolationTests(unittest.TestCase):
    def test_tasks_run_in_parallel_and_keep_manifest_order(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            specs = [(f"slow_task_{index}", _minimal_sleep_task(f"slow_task_{index}", 1.0)) for index in range(4)]
            manifest = _build_project(root, specs)
            for task in manifest["tasks"]:
                task["timeout_smoke_s"] = 10

            started = time.perf_counter()
            result = run_tasks_once(root, manifest, phase="smoke", logs_dir=None)
            elapsed = time.perf_counter() - started

            self.assertEqual(result["coverage"], "4/4")
            self.assertEqual([item["task_id"] for item in result["per_task"]], [task_id for task_id, _ in specs])
            self.assertLess(elapsed, 3.0)

    def test_task_timeout_is_capped_at_2000_seconds(self) -> None:
        self.assertEqual(task_timeout_seconds({"timeout_full_s": 9999}, "full"), 2000.0)
        self.assertEqual(task_timeout_seconds({}, "full"), 2000.0)
        self.assertEqual(task_timeout_seconds({"timeout_smoke_s": "not-a-number"}, "smoke"), 60.0)

    def test_a_crash_and_a_hang_do_not_sink_the_passing_task(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest = _build_project(
                root,
                [("good_task", _good_task("good_task")), ("bad_task", CRASH_TASK), ("hang_task", HANG_TASK)],
            )
            # Give the hang task a tiny per-task budget so timeout isolation is proven fast.
            for task in manifest["tasks"]:
                if task["task_id"] == "hang_task":
                    task["timeout_smoke_s"] = 2

            result = run_tasks_once(root, manifest, phase="smoke", logs_dir=None)
            by_id = {item["task_id"]: item for item in result["per_task"]}

            # The crash (Python exception) and the hang (timeout) each fail only themselves.
            self.assertTrue(by_id["good_task"]["passed"])
            self.assertFalse(by_id["bad_task"]["passed"])
            self.assertNotEqual(by_id["bad_task"]["returncode"], 0)
            self.assertFalse(by_id["hang_task"]["passed"])
            self.assertTrue(by_id["hang_task"]["timed_out"])

            # Partial success is first-class, not zeroed out.
            self.assertEqual(result["coverage"], "1/3")
            self.assertFalse(result["all_passed"])
            self.assertEqual(result["tasks_passed"], 1)

            # The passing task's artifacts survived BOTH the crash and the hang (separate procs).
            base = root / "outputs" / "good_task"
            self.assertTrue(_valid_csv(base / "results.csv"))
            self.assertTrue(_valid_png(base / "curve.png"))
            self.assertTrue(_valid_summary_json(base / "summary.json"))

    def test_run_repro_with_repair_dispatches_to_task_path_when_manifest_present(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _build_project(root, [("solo_task", _good_task("solo_task"))])

            # Manifest present -> the task-aware path is taken; client args are unused there.
            result = run_repro_with_repair(
                root, None, None, "system", max_repair_attempts=0
            )
            self.assertTrue(result.get("per_task_orchestration"))
            self.assertTrue(result["passed"])
            self.assertEqual(result["coverage"], "1/1")


if __name__ == "__main__":
    unittest.main()
