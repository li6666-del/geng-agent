from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from geng_agent.io_runtime import inject_io_runtime
from geng_agent.task_scripts import build_tasks_manifest, write_task_scaffolding


def test_manifest_carries_execution_unit_membership_and_phase() -> None:
    tasks = {
        "repro_tasks": [
            {"task_id": "train_shared_model"},
            {"task_id": "evaluate_shared_checkpoint"},
            {"task_id": "independent_bound"},
        ]
    }
    plan = {
        "schema_version": "1.0",
        "task_to_execution_unit": {
            "train_shared_model": "unit_train_eval",
            "evaluate_shared_checkpoint": "unit_train_eval",
            "independent_bound": "unit_independent",
        },
        "execution_units": [
            {
                "unit_id": "unit_train_eval",
                "mode": "compound",
                "task_ids": ["train_shared_model", "evaluate_shared_checkpoint"],
                "dependencies": [
                    {
                        "producer_task_id": "train_shared_model",
                        "consumer_task_ids": ["evaluate_shared_checkpoint"],
                        "artifact_ids": ["trained_checkpoint"],
                    }
                ],
                "artifact_ids": ["trained_checkpoint"],
            },
            {
                "unit_id": "unit_independent",
                "mode": "independent",
                "task_ids": ["independent_bound"],
                "dependencies": [],
                "artifact_ids": [],
            },
        ],
    }

    manifest = build_tasks_manifest(tasks, execution_plan=plan)

    by_id = {item["task_id"]: item for item in manifest["tasks"]}
    assert by_id["train_shared_model"]["execution_unit_id"] == "unit_train_eval"
    assert by_id["train_shared_model"]["execution_phase"] == 1
    assert by_id["evaluate_shared_checkpoint"]["execution_phase"] == 2
    assert len(manifest["execution_units"]) == 2


def test_dispatcher_selects_each_tasks_own_config() -> None:
    with TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        inject_io_runtime(root)
        manifest = build_tasks_manifest(
            {"repro_tasks": [{"task_id": "task_a"}, {"task_id": "task_b"}]}
        )
        for entry in manifest["tasks"]:
            entry["config_full"] = f"configs/{entry['module']}_config.json"
            entry["config_smoke"] = f"configs/{entry['module']}_config_smoke.json"
        write_task_scaffolding(root, manifest)
        configs = root / "configs"
        configs.mkdir()
        for entry in manifest["tasks"]:
            module = entry["module"]
            (configs / f"{module}_config_smoke.json").write_text(
                json.dumps({"owner": entry["task_id"]}), encoding="utf-8"
            )
            (root / "tasks" / f"{module}.py").write_text(
                "from __future__ import annotations\n"
                "import json\n"
                "from pathlib import Path\n"
                f"TASK_ID = {entry['task_id']!r}\n"
                "def main(config_path=None):\n"
                "    cfg = json.loads(Path(config_path).read_text(encoding='utf-8'))\n"
                "    if cfg.get('owner') != TASK_ID:\n"
                "        raise RuntimeError('wrong task config')\n"
                "    return 0\n",
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

        assert completed.returncode == 0, completed.stderr
        summary = json.loads((root / "outputs" / "summary.json").read_text(encoding="utf-8"))
        assert summary["all_passed"] is True
        assert summary["tasks"]["task_a"]["config"] == "configs/task_a_config_smoke.json"
        assert summary["tasks"]["task_b"]["config"] == "configs/task_b_config_smoke.json"


def test_failed_producer_skips_dependent_consumer_but_runs_independent_task() -> None:
    with TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        inject_io_runtime(root)
        tasks = {
            "repro_tasks": [
                {"task_id": "train_model"},
                {"task_id": "evaluate_checkpoint"},
                {"task_id": "independent_bound"},
            ]
        }
        plan = {
            "schema_version": "1.0",
            "task_to_execution_unit": {
                "train_model": "unit_train_eval",
                "evaluate_checkpoint": "unit_train_eval",
                "independent_bound": "unit_independent",
            },
            "execution_units": [
                {
                    "unit_id": "unit_train_eval",
                    "mode": "compound",
                    "task_ids": ["train_model", "evaluate_checkpoint"],
                    # Use the compiler's canonical singular dependency shape.
                    "dependencies": [
                        {
                            "producer_task_id": "train_model",
                            "consumer_task_id": "evaluate_checkpoint",
                            "artifact_id": "trained_checkpoint",
                        }
                    ],
                },
                {
                    "unit_id": "unit_independent",
                    "mode": "singleton",
                    "task_ids": ["independent_bound"],
                    "dependencies": [],
                },
            ],
        }
        manifest = build_tasks_manifest(tasks, execution_plan=plan)
        write_task_scaffolding(root, manifest)
        scripts = {entry["task_id"]: root / entry["script"] for entry in manifest["tasks"]}
        scripts["train_model"].write_text(
            "def main(config_path=None):\n"
            "    return 9\n",
            encoding="utf-8",
        )
        scripts["evaluate_checkpoint"].write_text(
            "from pathlib import Path\n"
            "def main(config_path=None):\n"
            "    stale = Path('trained_checkpoint.bin').read_text(encoding='utf-8')\n"
            "    Path('consumer_ran.txt').write_text(stale, encoding='utf-8')\n"
            "    return 0\n",
            encoding="utf-8",
        )
        scripts["independent_bound"].write_text(
            "from pathlib import Path\n"
            "def main(config_path=None):\n"
            "    Path('independent_ran.txt').write_text('ok', encoding='utf-8')\n"
            "    return 0\n",
            encoding="utf-8",
        )
        (root / "trained_checkpoint.bin").write_text("stale", encoding="utf-8")
        (root / "config_smoke.json").write_text("{}", encoding="utf-8")

        completed = subprocess.run(
            [sys.executable, "run_experiment.py", "config_smoke.json"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=30,
        )

        assert completed.returncode == 1, completed.stderr
        summary = json.loads((root / "outputs" / "summary.json").read_text(encoding="utf-8"))
        assert summary["tasks"]["train_model"]["status"] == "error"
        consumer = summary["tasks"]["evaluate_checkpoint"]
        assert consumer["status"] == "skipped"
        assert consumer["reason"] == "dependency_failed"
        assert consumer["blocked_by"] == [
            {
                "producer_task_id": "train_model",
                "producer_status": "error",
                "artifact_ids": ["trained_checkpoint"],
            }
        ]
        assert not (root / "consumer_ran.txt").exists()
        assert summary["tasks"]["independent_bound"]["status"] == "ok"
        assert (root / "independent_ran.txt").read_text(encoding="utf-8") == "ok"
