import ast
import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from geng_agent.io_runtime import inject_io_runtime, io_slug
from geng_agent.manifest_utils import expected_generated_paths
from geng_agent.outputs import _valid_csv, _valid_png, _valid_summary_json
from geng_agent.security import static_scan_repro_project
from geng_agent.task_scripts import (
    build_tasks_manifest,
    load_tasks_manifest,
    render_run_experiment_dispatcher,
    task_module_name,
    write_task_scaffolding,
)


# A realistic thin task script: science elided, all artifact IO delegated to src/_io.
TASK_PY = (
    "import json\n"
    "import sys\n"
    "from pathlib import Path\n"
    "from src import _io\n"
    "import matplotlib\n"
    "matplotlib.use('Agg')\n"
    "import matplotlib.pyplot as plt\n"
    "\n"
    "def main(config_path=None):\n"
    "    path = config_path or (sys.argv[1] if len(sys.argv) > 1 else 'config_smoke.json')\n"
    "    cfg = json.loads(Path(path).read_text(encoding='utf-8'))\n"
    "    _io.begin('demo_task', cfg)\n"
    "    _io.write_table('demo_task', ['x', 'y'], [{'x': 1, 'y': 2.0}, {'x': 2, 'y': 3.5}])\n"
    "    fig, ax = plt.subplots()\n"
    "    ax.plot([0, 1, 2], [1, 2, 3])\n"
    "    _io.write_figure('demo_task', 'curve', fig)\n"
    "    return _io.finish('demo_task', metrics={'rows': 2}, assumptions=[])\n"
    "\n"
    "if __name__ == '__main__':\n"
    "    raise SystemExit(main())\n"
)


def _tasks_doc(*task_ids):
    return {"repro_tasks": [{"task_id": tid} for tid in task_ids]}


class TaskModuleNameTests(unittest.TestCase):
    def test_slugs_to_valid_identifier(self) -> None:
        self.assertEqual(task_module_name("reproduce_fig_7"), "reproduce_fig_7")
        self.assertEqual(task_module_name("Reproduce Fig.6-heatmap"), "reproduce_fig_6_heatmap")
        self.assertTrue(task_module_name("reproduce fig 6").isidentifier())

    def test_digit_start_is_prefixed(self) -> None:
        name = task_module_name("4_cdf")
        self.assertTrue(name.isidentifier())
        self.assertTrue(name.startswith("t_"))

    def test_empty_defaults_and_dedupes(self) -> None:
        used: set[str] = set()
        self.assertEqual(task_module_name("", used), "task")
        # Two ids that collapse to the same slug get distinct module names.
        a = task_module_name("Fig 4", used)
        b = task_module_name("fig-4", used)
        self.assertNotEqual(a, b)


class ManifestTests(unittest.TestCase):
    def test_manifest_entries(self) -> None:
        manifest = build_tasks_manifest(_tasks_doc("reproduce_fig_7", "reproduce_fig_6_heatmap"))
        self.assertEqual(manifest["version"], 1)
        self.assertEqual([t["task_id"] for t in manifest["tasks"]], ["reproduce_fig_7", "reproduce_fig_6_heatmap"])
        first = manifest["tasks"][0]
        self.assertEqual(first["module"], "reproduce_fig_7")
        self.assertEqual(first["script"], "tasks/reproduce_fig_7.py")
        self.assertEqual(first["output_subdir"], io_slug("reproduce_fig_7"))
        self.assertIn("timeout_smoke_s", first)
        self.assertEqual(first["timeout_full_s"], 2000)

    def test_duplicate_task_ids_get_unique_modules(self) -> None:
        manifest = build_tasks_manifest({"repro_tasks": [{"task_id": "Fig 4"}, {"task_id": "fig_4"}]})
        modules = [t["module"] for t in manifest["tasks"]]
        self.assertEqual(len(set(modules)), 2)

    def test_output_subdirs_are_stably_unique_across_slug_and_case_collisions(self) -> None:
        task_ids = ["Fig 4", "Fig_4", "fig_4", "independent"]
        first = build_tasks_manifest(_tasks_doc(*task_ids))
        reordered = build_tasks_manifest(_tasks_doc(*reversed(task_ids)))

        first_by_id = {entry["task_id"]: entry["output_subdir"] for entry in first["tasks"]}
        reordered_by_id = {
            entry["task_id"]: entry["output_subdir"] for entry in reordered["tasks"]
        }
        output_keys = [entry["output_subdir"].casefold() for entry in first["tasks"]]
        self.assertEqual(len(output_keys), len(set(output_keys)))
        self.assertEqual(first_by_id, reordered_by_id)
        self.assertEqual(first_by_id["independent"], io_slug("independent"))
        self.assertNotEqual(first_by_id["Fig 4"], io_slug("Fig 4"))

    def test_handles_missing_and_malformed(self) -> None:
        manifest = build_tasks_manifest({"repro_tasks": [{}, "junk", {"task_id": "ok"}]})
        ids = [t["task_id"] for t in manifest["tasks"]]
        self.assertIn("ok", ids)
        self.assertTrue(all(t["module"] for t in manifest["tasks"]))


class DispatcherTests(unittest.TestCase):
    def test_dispatcher_compiles_with_static_imports(self) -> None:
        manifest = build_tasks_manifest(_tasks_doc("reproduce_fig_7", "reproduce_fig_4"))
        source = render_run_experiment_dispatcher(manifest)
        ast.parse(source)  # must be valid Python
        self.assertIn("from tasks import reproduce_fig_7 as _task_reproduce_fig_7", source)
        self.assertIn("from tasks import reproduce_fig_4 as _task_reproduce_fig_4", source)
        self.assertIn("try:", source)
        self.assertIn("except Exception", source)
        # no dynamic import / process spawning in generated code (only static imports)
        self.assertNotIn("importlib", source)
        self.assertNotIn("import subprocess", source)

    def test_empty_manifest_still_compiles(self) -> None:
        ast.parse(render_run_experiment_dispatcher({"tasks": []}))

    def test_dispatcher_treats_false_and_nonzero_integer_returns_as_failures(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            inject_io_runtime(root)
            manifest = build_tasks_manifest(
                _tasks_doc("returns_false", "returns_nonzero", "returns_true")
            )
            write_task_scaffolding(root, manifest)
            returns = {
                "returns_false": "False",
                "returns_nonzero": "7",
                "returns_true": "True",
            }
            for entry in manifest["tasks"]:
                (root / entry["script"]).write_text(
                    "def main(config_path=None):\n"
                    f"    return {returns[entry['task_id']]}\n",
                    encoding="utf-8",
                )
            (root / "config_smoke.json").write_text("{}", encoding="utf-8")

            completed = subprocess.run(
                [sys.executable, "run_experiment.py", "config_smoke.json"],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=30,
            )

            self.assertEqual(completed.returncode, 1, msg=completed.stderr)
            summary = json.loads(
                (root / "outputs" / "summary.json").read_text(encoding="utf-8")
            )
            self.assertEqual(summary["tasks"]["returns_false"]["status"], "error")
            self.assertIn("False", summary["tasks"]["returns_false"]["error"])
            self.assertEqual(summary["tasks"]["returns_nonzero"]["status"], "error")
            self.assertIn("7", summary["tasks"]["returns_nonzero"]["error"])
            self.assertEqual(summary["tasks"]["returns_true"]["status"], "ok")

    def test_scaffolding_is_scan_clean(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            inject_io_runtime(root)
            manifest = build_tasks_manifest(_tasks_doc("demo_task"))
            write_task_scaffolding(root, manifest)
            (root / "tasks" / "demo_task.py").write_text(TASK_PY, encoding="utf-8")
            self.assertEqual(static_scan_repro_project(root), [])


class ScaffoldingIoTests(unittest.TestCase):
    def test_write_and_load_roundtrip(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest = build_tasks_manifest(_tasks_doc("demo_task"))
            write_task_scaffolding(root, manifest)
            self.assertTrue((root / "tasks" / "__init__.py").exists())
            self.assertTrue((root / "run_experiment.py").exists())
            loaded = load_tasks_manifest(root)
            self.assertEqual(loaded["tasks"][0]["task_id"], "demo_task")

    def test_load_absent_returns_none(self) -> None:
        with TemporaryDirectory() as temp_dir:
            self.assertIsNone(load_tasks_manifest(Path(temp_dir)))


class EndToEndDispatcherTests(unittest.TestCase):
    def test_dispatcher_runs_task_and_writes_per_task_artifacts(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            inject_io_runtime(root)
            manifest = build_tasks_manifest(_tasks_doc("demo_task"))
            write_task_scaffolding(root, manifest)
            (root / "tasks" / "demo_task.py").write_text(TASK_PY, encoding="utf-8")
            (root / "config_smoke.json").write_text(json.dumps({"seed": 1}), encoding="utf-8")

            completed = subprocess.run(
                [sys.executable, "run_experiment.py", "config_smoke.json"],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=90,
            )
            self.assertEqual(completed.returncode, 0, msg=completed.stderr[-2000:])

            base = root / "outputs" / "demo_task"
            self.assertTrue(_valid_csv(base / "results.csv"))
            self.assertTrue(_valid_png(base / "curve.png"))
            self.assertTrue(_valid_summary_json(base / "summary.json"))
            # the dispatcher's aggregate summary records the per-task pass
            aggregate = json.loads((root / "outputs" / "summary.json").read_text(encoding="utf-8"))
            self.assertTrue(aggregate["all_passed"])
            self.assertEqual(aggregate["tasks"]["demo_task"]["status"], "ok")

    def test_writer_can_run_full_config_directly(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            inject_io_runtime(root)
            manifest = build_tasks_manifest(_tasks_doc("demo_task"))
            write_task_scaffolding(root, manifest)
            (root / "tasks" / "demo_task.py").write_text(TASK_PY, encoding="utf-8")
            (root / "config.json").write_text(json.dumps({"seed": 2}), encoding="utf-8")

            completed = subprocess.run(
                [sys.executable, "-m", "tasks.demo_task", "config.json"],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=90,
            )

            self.assertEqual(completed.returncode, 0, msg=completed.stderr[-2000:])


class GeneratedPathContractTests(unittest.TestCase):
    def test_expected_paths_are_task_owned_scripts_only(self) -> None:
        scripts = [t["script"] for t in build_tasks_manifest(_tasks_doc("reproduce_fig_7", "reproduce_fig_4"))["tasks"]]
        expected = expected_generated_paths(scripts)
        self.assertEqual(
            expected,
            {"tasks/reproduce_fig_7.py", "tasks/reproduce_fig_4.py"},
        )
        # shared scientific modules belong to the architecture/Foundation stage
        self.assertNotIn("src/simulation.py", expected)
        # harness-injected files are NOT in the model-generated set
        self.assertNotIn("run_experiment.py", expected)
        self.assertNotIn("src/_io.py", expected)

if __name__ == "__main__":
    unittest.main()
