from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from geng_agent.agentic_task_writers import (
    _build_artifact_lineage,
    _collect_task_writer_delivery,
    _dispatch_task_writers,
    _execution_unit_sandbox,
    _load_task_writer_resume_records,
    _merge_task_writer_deliveries,
    _portable_environment_lock,
    _run_one_execution_unit_writer,
    _task_manifest_with_configs,
    _streaming_file_sha256,
)
from geng_agent.execution_plan import compile_execution_plan
from geng_agent.manifest_utils import expected_generated_paths
from geng_agent.project_portability import build_source_inventory
from geng_agent.task_scripts import build_tasks_manifest
from geng_agent.task_writer_support import _manifest_disk_paths, _manifest_from_project


def _task(task_id: str) -> dict:
    return {"task_id": task_id}


def _record(index: int, task: dict, entry: dict, sandbox: Path, unit_id: str) -> dict:
    return {
        "index": index,
        "task_id": task["task_id"],
        "module": entry["module"],
        "output_subdir": entry["output_subdir"],
        "sandbox": str(sandbox),
        "execution_unit_id": unit_id,
        "writer_completed": True,
        "task_writer_status": "ready_for_review",
        "result_json": {"status": "ready_for_review"},
        "artifacts": {"has_artifacts": True},
    }


def _five_task_document() -> dict:
    return {
        "repro_tasks": [
            _task("train"),
            _task("evaluate"),
            _task("curve_a"),
            _task("curve_b"),
            _task("analytic_bound"),
        ],
        "execution_relationships": [
            {
                "relationship_id": "checkpoint_flow",
                "kind": "checkpoint_flow",
                "strength": "strong",
                "task_ids": ["train", "evaluate"],
                "producer_task_id": "train",
                "consumer_task_ids": ["evaluate"],
                "artifact_ids": ["trained_checkpoint"],
            },
            {
                "relationship_id": "same_random_realization",
                "kind": "shared_random_realization",
                "strength": "strong",
                "task_ids": ["curve_a", "curve_b"],
                "producer_task_id": None,
                "consumer_task_ids": [],
                "artifact_ids": ["random_state"],
            },
        ],
    }


def test_dispatches_one_writer_per_execution_unit(monkeypatch, tmp_path: Path) -> None:
    tasks = _five_task_document()
    plan = compile_execution_plan(tasks)
    manifest = _task_manifest_with_configs(
        build_tasks_manifest(tasks, execution_plan=plan)
    )
    pairs = list(zip(tasks["repro_tasks"], manifest["tasks"]))
    compound_calls: list[str] = []
    singleton_calls: list[str] = []

    def fake_compound(**kwargs):
        unit = kwargs["unit"]
        compound_calls.append(unit["unit_id"])
        sandbox = tmp_path / unit["unit_id"]
        return [
            _record(index, task, entry, sandbox, unit["unit_id"])
            for index, task, entry in unit["members"]
        ]

    def fake_single(**kwargs):
        singleton_calls.append(kwargs["task"]["task_id"])
        return _record(
            kwargs["index"],
            kwargs["task"],
            kwargs["manifest_entry"],
            tmp_path / kwargs["task"]["task_id"],
            "singleton",
        )

    monkeypatch.setattr(
        "geng_agent.task_writer_dispatch._run_one_execution_unit_writer",
        fake_compound,
    )
    monkeypatch.setattr(
        "geng_agent.task_writer_dispatch._run_one_task_writer",
        fake_single,
    )

    records, audit = _dispatch_task_writers(
        task_pairs=pairs,
        facts={},
        experiment_index={},
        paper={},
        paper_path=tmp_path / "paper.pdf",
        paper_context_json="",
        paper_images=[],
        paper_thesis=None,
        analysis_snapshot_hash="a" * 64,
        analysis_artifacts={},
        task_root=tmp_path / "sandboxes",
        audit_dir=tmp_path / "audit",
        run_repro=False,
        execution_plan=plan,
    )

    assert len(records) == 5
    assert len(compound_calls) == 2
    assert singleton_calls == ["analytic_bound"]
    assert audit["logical_task_count"] == 5
    assert audit["execution_unit_count"] == 3


def test_merge_packages_checkpoint_and_hash_lineage(tmp_path: Path) -> None:
    tasks = {
        "repro_tasks": [_task("train"), _task("evaluate")],
        "execution_relationships": [
            {
                "relationship_id": "checkpoint_flow",
                "kind": "checkpoint_flow",
                "strength": "strong",
                "task_ids": ["train", "evaluate"],
                "producer_task_id": "train",
                "consumer_task_ids": ["evaluate"],
                "artifact_ids": ["trained_checkpoint"],
            }
        ],
    }
    plan = compile_execution_plan(tasks)
    manifest = _task_manifest_with_configs(
        build_tasks_manifest(tasks, execution_plan=plan)
    )
    unit_id = plan["execution_units"][0]["unit_id"]
    sandbox = tmp_path / "unit"
    (sandbox / "tasks").mkdir(parents=True)
    (sandbox / "configs").mkdir()
    checkpoint = sandbox / "execution_units" / unit_id / "checkpoints" / "model.bin"
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_bytes(b"shared-checkpoint")
    records = []
    for index, (task, entry) in enumerate(zip(tasks["repro_tasks"], manifest["tasks"]), start=1):
        (sandbox / "tasks" / f"{entry['module']}.py").write_text(
            "def main(config_path=None):\n    return 0\n", encoding="utf-8"
        )
        (sandbox / "configs" / f"{entry['module']}_config.json").write_text(
            json.dumps({"task_id": task["task_id"]}), encoding="utf-8"
        )
        (sandbox / "configs" / f"{entry['module']}_config_smoke.json").write_text(
            json.dumps({"task_id": task["task_id"], "smoke": True}), encoding="utf-8"
        )
        output = sandbox / "outputs" / entry["output_subdir"]
        output.mkdir(parents=True)
        (output / "task_agent_result.json").write_text(
            json.dumps({"task_id": task["task_id"], "status": "ready_for_review"}),
            encoding="utf-8",
        )
        records.append(_record(index, task, entry, sandbox, unit_id))
    (sandbox / "requirements.txt").write_text("numpy\n", encoding="utf-8")
    (sandbox / "execution_unit_result.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "execution_unit_id": unit_id,
                "task_ids": ["train", "evaluate"],
                "artifact_lineage": [
                    {
                        "artifact_id": "trained_checkpoint",
                        "path": f"execution_units/{unit_id}/checkpoints/model.bin",
                        "producer_task_id": "train",
                        "consumer_task_ids": ["evaluate"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    project = tmp_path / "project"
    project.mkdir()
    expected = expected_generated_paths([item["script"] for item in manifest["tasks"]])

    _merge_task_writer_deliveries(
        repro_project_dir=project,
        task_manifest=manifest,
        expected_paths=expected,
        task_records=records,
        execution_plan=plan,
        require_lineage=True,
    )

    assert (
        project / "execution_units" / unit_id / "checkpoints" / "model.bin"
    ).read_bytes() == b"shared-checkpoint"
    lineage = json.loads((project / "artifact_lineage.json").read_text(encoding="utf-8"))
    assert lineage["artifacts"][0]["producer_task_id"] == "train"
    assert len(lineage["artifacts"][0]["sha256"]) == 64
    assert (project / "execution_plan.json").is_file()
    assert (project / "reproducibility_manifest.json").is_file()


def test_compound_delivery_never_uses_one_root_result_for_all_tasks(tmp_path: Path) -> None:
    sandbox = tmp_path / "unit"
    for task_id in ("task_a", "task_b"):
        output = sandbox / "outputs" / task_id
        output.mkdir(parents=True)
        (output / "task_agent_result.json").write_text(
            json.dumps(
                {
                    "task_id": task_id,
                    "status": "ready_for_review",
                    "summary": task_id,
                    "execution_summary": {"full_run_count": 1, "last_returncode": 0},
                }
            ),
            encoding="utf-8",
        )
    (sandbox / "task_agent_result.json").write_text(
        json.dumps({"task_id": "wrong_root", "status": "ready_for_review"}),
        encoding="utf-8",
    )

    records = [
        _collect_task_writer_delivery(
            index=index,
            task={"task_id": task_id},
            manifest_entry={
                "task_id": task_id,
                "module": task_id,
                "output_subdir": task_id,
            },
            sandbox=sandbox,
            writer_status={"ok": True},
            allow_root_result_fallback=False,
        )
        for index, task_id in enumerate(("task_a", "task_b"), start=1)
    ]

    assert [record["result_json"]["task_id"] for record in records] == [
        "task_a",
        "task_b",
    ]


def test_compound_checkpoint_records_resume_as_one_atomic_unit(tmp_path: Path) -> None:
    tasks = {
        "repro_tasks": [_task("train"), _task("evaluate")],
        "execution_relationships": [
            {
                "relationship_id": "checkpoint_flow",
                "kind": "checkpoint_flow",
                "strength": "strong",
                "task_ids": ["train", "evaluate"],
                "producer_task_id": "train",
                "consumer_task_ids": ["evaluate"],
                "artifact_ids": ["checkpoint"],
            }
        ],
    }
    plan = compile_execution_plan(tasks)
    manifest = _task_manifest_with_configs(
        build_tasks_manifest(tasks, execution_plan=plan)
    )
    pairs = list(zip(tasks["repro_tasks"], manifest["tasks"]))
    audit = tmp_path / "audit"
    task_root = audit / "03c_task_writer_sandboxes"
    unit_id = plan["execution_units"][0]["unit_id"]
    sandbox = _execution_unit_sandbox(task_root, unit_id)
    (sandbox / "paper_evidence").mkdir(parents=True)
    snapshot = "a" * 64
    records = []
    for index, (task, entry) in enumerate(pairs, start=1):
        record = _record(index, task, entry, sandbox, unit_id)
        record["analysis_snapshot_hash"] = snapshot
        records.append(record)
    audit.mkdir(parents=True, exist_ok=True)
    (audit / "03c_task_writers_records.json").write_text(
        json.dumps({"tasks": records}), encoding="utf-8"
    )

    resumed = _load_task_writer_resume_records(
        audit_dir=audit,
        task_pairs=pairs,
        expected_analysis_snapshot_hash=snapshot,
        execution_plan=plan,
    )

    assert set(resumed) == {1, 2}
    assert {record["sandbox"] for record in resumed.values()} == {str(sandbox)}
    assert {record["execution_unit_id"] for record in resumed.values()} == {unit_id}


def test_merge_preserves_writer_src_and_namespaces_tests_but_not_host_contracts(
    tmp_path: Path,
) -> None:
    tasks = {"repro_tasks": [_task("single")], "execution_relationships": []}
    plan = compile_execution_plan(tasks)
    manifest = _task_manifest_with_configs(
        build_tasks_manifest(tasks, execution_plan=plan)
    )
    task, entry = tasks["repro_tasks"][0], manifest["tasks"][0]
    sandbox = tmp_path / "single"
    (sandbox / "tasks").mkdir(parents=True)
    (sandbox / "src").mkdir()
    (sandbox / "tests").mkdir()
    (sandbox / "tasks" / f"{entry['module']}.py").write_text(
        "def main(config_path=None):\n    return 0\n", encoding="utf-8"
    )
    (sandbox / "src" / "model.py").write_text("VALUE = 1\n", encoding="utf-8")
    (sandbox / "tests" / "test_model.py").write_text(
        "def test_value():\n    assert True\n", encoding="utf-8"
    )
    (sandbox / "execution_plan.json").write_text(
        json.dumps({"malicious": True}), encoding="utf-8"
    )
    record = _record(1, task, entry, sandbox, plan["execution_units"][0]["unit_id"])
    project = tmp_path / "project"
    project.mkdir()

    _merge_task_writer_deliveries(
        repro_project_dir=project,
        task_manifest=manifest,
        expected_paths=expected_generated_paths([entry["script"]]),
        task_records=[record],
        execution_plan=plan,
        require_lineage=False,
    )

    assert (project / "src" / "model.py").is_file()
    unit_id = plan["execution_units"][0]["unit_id"]
    assert (
        project
        / "execution_units"
        / unit_id
        / "tests"
        / "test_model.py"
    ).is_file()
    assert json.loads((project / "execution_plan.json").read_text(encoding="utf-8")) == plan


def test_undirected_strong_artifact_requires_persisted_lineage(tmp_path: Path) -> None:
    tasks = {
        "repro_tasks": [_task("curve_a"), _task("curve_b")],
        "execution_relationships": [
            {
                "relationship_id": "same_random_state",
                "kind": "shared_random_realization",
                "strength": "strong",
                "task_ids": ["curve_a", "curve_b"],
                "producer_task_id": None,
                "consumer_task_ids": [],
                "artifact_ids": ["random_state"],
            }
        ],
    }
    plan = compile_execution_plan(tasks)
    manifest = _task_manifest_with_configs(
        build_tasks_manifest(tasks, execution_plan=plan)
    )
    sandbox = tmp_path / "unit"
    (sandbox / "tasks").mkdir(parents=True)
    records = []
    unit_id = plan["execution_units"][0]["unit_id"]
    for index, (task, entry) in enumerate(
        zip(tasks["repro_tasks"], manifest["tasks"]), start=1
    ):
        (sandbox / "tasks" / f"{entry['module']}.py").write_text(
            "def main(config_path=None):\n    return 0\n", encoding="utf-8"
        )
        records.append(_record(index, task, entry, sandbox, unit_id))
    (sandbox / "execution_unit_result.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "execution_unit_id": unit_id,
                "task_ids": ["curve_a", "curve_b"],
                "artifact_lineage": [],
            }
        ),
        encoding="utf-8",
    )
    project = tmp_path / "project"
    project.mkdir()

    try:
        _merge_task_writer_deliveries(
            repro_project_dir=project,
            task_manifest=manifest,
            expected_paths=expected_generated_paths(
                [entry["script"] for entry in manifest["tasks"]]
            ),
            task_records=records,
            execution_plan=plan,
            require_lineage=True,
        )
    except RuntimeError as exc:
        assert "random_state" in str(exc)
    else:
        raise AssertionError("missing undirected strong lineage must block packaging")


def test_large_binary_is_packaged_by_inventory_not_embedded_as_manifest_text(
    tmp_path: Path,
) -> None:
    (tmp_path / "README.md").write_text("portable\n", encoding="utf-8")
    checkpoint = tmp_path / "checkpoints" / "large.bin"
    checkpoint.parent.mkdir()
    checkpoint.write_bytes(b"x" * 2_000_001)

    manifest = _manifest_from_project(
        repro_project_dir=tmp_path,
        expected_paths={"README.md", "checkpoints/large.bin"},
        task_manifest={"version": 1, "tasks": []},
        round_no=1,
    )

    assert [item["path"] for item in manifest["files"]] == ["README.md"]
    packaged = manifest["_meta"]["packaged_only_files"]
    assert packaged[0]["path"] == "checkpoints/large.bin"
    assert packaged[0]["bytes"] == 2_000_001
    assert len(packaged[0]["sha256"]) == 64


def test_manifest_disk_paths_reports_outputs_binaries_and_inventory(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("portable\n", encoding="utf-8")
    raw_output = tmp_path / "outputs" / "raw.bin"
    raw_output.parent.mkdir()
    raw_output.write_bytes(b"\x00\x01\x02")
    (tmp_path / "source_inventory.json").write_text(
        json.dumps(build_source_inventory(tmp_path)),
        encoding="utf-8",
    )
    manifest = _manifest_from_project(
        repro_project_dir=tmp_path,
        expected_paths={"README.md"},
        task_manifest={"version": 1, "tasks": []},
        round_no=1,
    )

    disk_paths = {
        path.relative_to(tmp_path).as_posix()
        for path in _manifest_disk_paths(manifest, tmp_path)
    }

    assert disk_paths == {"README.md", "outputs/raw.bin", "source_inventory.json"}


def test_large_binary_hashing_does_not_use_path_read_bytes(
    monkeypatch,
    tmp_path: Path,
) -> None:
    checkpoint = tmp_path / "large.bin"
    checkpoint.write_bytes(b"x" * 2_000_001)

    def fail_read_bytes(_path):
        raise AssertionError("large package files must be hashed incrementally")

    monkeypatch.setattr(Path, "read_bytes", fail_read_bytes)

    assert len(_streaming_file_sha256(checkpoint)) == 64


def test_strong_lineage_cannot_alias_task_source_as_material_artifact(tmp_path: Path) -> None:
    tasks = {
        "repro_tasks": [_task("train"), _task("evaluate")],
        "execution_relationships": [
            {
                "relationship_id": "checkpoint_flow",
                "kind": "checkpoint_flow",
                "strength": "strong",
                "task_ids": ["train", "evaluate"],
                "producer_task_id": "train",
                "consumer_task_ids": ["evaluate"],
                "artifact_ids": ["trained_checkpoint"],
            }
        ],
    }
    plan = compile_execution_plan(tasks)
    unit_id = plan["execution_units"][0]["unit_id"]
    sandbox = tmp_path / "sandbox"
    project = tmp_path / "project"
    for root in (sandbox, project):
        (root / "tasks").mkdir(parents=True)
        (root / "tasks" / "train.py").write_text("VALUE = 1\n", encoding="utf-8")
    (sandbox / "execution_unit_result.json").write_text(
        json.dumps(
            {
                "artifact_lineage": [
                    {
                        "artifact_id": "trained_checkpoint",
                        "path": "tasks/train.py",
                        "producer_task_id": "train",
                        "consumer_task_ids": ["evaluate"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    try:
        _build_artifact_lineage(
            repro_project_dir=project,
            execution_plan=plan,
            task_records=[
                {
                    "task_id": "train",
                    "execution_unit_id": unit_id,
                    "sandbox": str(sandbox),
                }
            ],
            require_lineage=True,
        )
    except RuntimeError as exc:
        assert "must be persisted under" in str(exc)
    else:
        raise AssertionError("source code must not satisfy persisted checkpoint lineage")


def test_merge_removes_stale_outputs_repair_logs_and_case_variant_caches(
    tmp_path: Path,
) -> None:
    tasks = {"repro_tasks": [_task("single")], "execution_relationships": []}
    plan = compile_execution_plan(tasks)
    manifest = _task_manifest_with_configs(build_tasks_manifest(tasks, execution_plan=plan))
    task, entry = tasks["repro_tasks"][0], manifest["tasks"][0]
    sandbox = tmp_path / "sandbox"
    (sandbox / "tasks").mkdir(parents=True)
    (sandbox / "tasks" / f"{entry['module']}.py").write_text(
        "def main(config_path=None):\n    return 0\n",
        encoding="utf-8",
    )
    (sandbox / "Node_Modules").mkdir()
    (sandbox / "Node_Modules" / "host.js").write_text("stale\n", encoding="utf-8")
    project = tmp_path / "project"
    (project / "outputs" / "removed_task").mkdir(parents=True)
    (project / "outputs" / "removed_task" / "old.csv").write_text("old\n", encoding="utf-8")
    (project / "repair_logs").mkdir()
    (project / "repair_logs" / "old.log").write_text("old\n", encoding="utf-8")

    _merge_task_writer_deliveries(
        repro_project_dir=project,
        task_manifest=manifest,
        expected_paths=expected_generated_paths([entry["script"]]),
        task_records=[
            _record(1, task, entry, sandbox, plan["execution_units"][0]["unit_id"])
        ],
        execution_plan=plan,
    )

    assert not (project / "outputs" / "removed_task").exists()
    assert not (project / "repair_logs").exists()
    assert not (project / "Node_Modules").exists()
    assert (project / "outputs" / entry["output_subdir"]).is_dir()


def test_portable_environment_lock_keeps_transitive_distribution_versions() -> None:
    runtime = SimpleNamespace(
        environment_hash="environment-hash",
        lock={
            "ready": True,
            "interpreter": {"python_full_version": "3.11.9"},
            "requirements": [{"requirement": "torch>=2", "installed_version": "2.4.1"}],
            "installed_distributions": [
                {"distribution": "torch", "version": "2.4.1"},
                {"distribution": "triton", "version": "3.0.0"},
            ],
        },
    )

    lock = _portable_environment_lock(runtime)

    assert lock["installed_distributions"] == runtime.lock["installed_distributions"]


def test_no_source_change_still_runs_each_logical_reporter_before_stopping(
    monkeypatch,
    tmp_path: Path,
) -> None:
    tasks = {
        "repro_tasks": [_task("task_a"), _task("task_b")],
        "execution_relationships": [
            {
                "relationship_id": "same_run",
                "kind": "same_run_outputs",
                "strength": "strong",
                "task_ids": ["task_a", "task_b"],
                "producer_task_id": None,
                "consumer_task_ids": [],
                "artifact_ids": [],
            }
        ],
    }
    plan = compile_execution_plan(tasks)
    manifest = _task_manifest_with_configs(
        build_tasks_manifest(tasks, execution_plan=plan)
    )
    unit = {
        **plan["execution_units"][0],
        "unit_index": 1,
        "members": [
            (index, task, entry)
            for index, (task, entry) in enumerate(
                zip(tasks["repro_tasks"], manifest["tasks"]), start=1
            )
        ],
    }
    reporter_calls: list[tuple[str, int]] = []

    monkeypatch.setattr(
        "geng_agent.task_writer_runner._prepare_execution_unit_writer_sandbox",
        lambda **kwargs: Path(kwargs["sandbox"]).mkdir(parents=True, exist_ok=True),
    )
    monkeypatch.setattr(
        "geng_agent.task_writer_runner._run_task_writer_codex_session",
        lambda **kwargs: {"ok": True},
    )
    monkeypatch.setattr(
        "geng_agent.task_writer_runner._collect_task_writer_delivery",
        lambda **kwargs: {
            "index": kwargs["index"],
            "task_id": kwargs["task"]["task_id"],
            "module": kwargs["manifest_entry"]["module"],
            "output_subdir": kwargs["manifest_entry"]["output_subdir"],
            "sandbox": str(kwargs["sandbox"]),
            "writer_completed": True,
            "task_writer_status": "ready_for_review",
            "result_json": {"status": "ready_for_review"},
            "artifacts": {"has_artifacts": True},
        },
    )
    monkeypatch.setattr(
        "geng_agent.task_writer_runner._writer_source_config_fingerprint",
        lambda sandbox: "unchanged",
    )
    monkeypatch.setattr(
        "geng_agent.task_writer_runner._archive_execution_unit_delivery",
        lambda **kwargs: None,
    )

    def fake_attach(**kwargs):
        task_id = kwargs["task"]["task_id"]
        round_no = kwargs["session_round"]
        reporter_calls.append((task_id, round_no))
        if task_id == "task_a":
            feedback = {
                "task_id": task_id,
                "host_action": "rerun_writer",
                "outcome": "not_reproduced",
                "run_valid": True,
                "rerun_reason": "core_conclusion_failed",
                "remaining_uncertainties": [],
            }
            kwargs["record"]["task_verification"] = feedback
            return "writer_revision", feedback
        terminal = {
            "task_id": task_id,
            "host_action": "complete",
            "outcome": "reproduced",
            "run_valid": True,
            "rerun_reason": "none",
            "remaining_uncertainties": [],
        }
        kwargs["record"]["task_verification"] = terminal
        return "terminal", None

    monkeypatch.setattr(
        "geng_agent.task_writer_runner._attach_task_reporter_review",
        fake_attach,
    )

    records = _run_one_execution_unit_writer(
        unit=unit,
        reuse_existing=False,
        runtime_refresh_required=False,
        facts={},
        experiment_index={},
        paper={},
        paper_path=tmp_path / "paper.pdf",
        paper_context_json="",
        paper_images=[],
        paper_thesis=None,
        foundation=None,
        analysis_snapshot_hash="a" * 64,
        analysis_artifacts={},
        task_root=tmp_path / "sandboxes",
        audit_dir=tmp_path / "audit",
        run_repro=True,
        review_feedback={},
        task_review_callback=lambda *args: {},
        case_runtime=None,
    )

    assert reporter_calls == [
        ("task_a", 1),
        ("task_b", 1),
        ("task_a", 2),
        ("task_b", 2),
    ]
    by_id = {record["task_id"]: record for record in records}
    assert by_id["task_b"]["task_verification"]["outcome"] == "reproduced"


def _two_task_compound_unit() -> tuple[dict, dict, list[dict]]:
    tasks = {
        "repro_tasks": [_task("train"), _task("evaluate")],
        "execution_relationships": [
            {
                "relationship_id": "checkpoint_flow",
                "kind": "checkpoint_flow",
                "strength": "strong",
                "task_ids": ["train", "evaluate"],
                "producer_task_id": "train",
                "consumer_task_ids": ["evaluate"],
                "artifact_ids": ["trained_checkpoint"],
            }
        ],
    }
    plan = compile_execution_plan(tasks)
    manifest = _task_manifest_with_configs(
        build_tasks_manifest(tasks, execution_plan=plan)
    )
    members = [
        (index, task, entry)
        for index, (task, entry) in enumerate(
            zip(tasks["repro_tasks"], manifest["tasks"]), start=1
        )
    ]
    unit = {**plan["execution_units"][0], "unit_index": 1, "members": members}
    return tasks, unit, manifest["tasks"]


def test_resumed_compound_repeated_causal_request_runs_one_shared_continuation(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _tasks, unit, _entries = _two_task_compound_unit()
    writer_calls: list[str] = []
    reporter_calls: list[str] = []
    archive_calls: list[int] = []
    fingerprints = iter(["source-before", "source-after"])

    monkeypatch.setattr(
        "geng_agent.task_writer_runner._prepare_execution_unit_writer_sandbox",
        lambda **kwargs: Path(kwargs["sandbox"]).mkdir(parents=True, exist_ok=True),
    )
    monkeypatch.setattr(
        "geng_agent.task_writer_runner._build_execution_unit_writer_brief",
        lambda **kwargs: "base",
    )
    monkeypatch.setattr(
        "geng_agent.task_writer_runner._run_task_writer_codex_session",
        lambda **kwargs: writer_calls.append(str(kwargs["label"])) or {"ok": True},
    )
    monkeypatch.setattr(
        "geng_agent.task_writer_runner._restore_trusted_files",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "geng_agent.task_writer_runner._collect_task_writer_delivery",
        lambda **kwargs: _record(
            kwargs["index"],
            kwargs["task"],
            kwargs["manifest_entry"],
            kwargs["sandbox"],
            unit["unit_id"],
        ),
    )
    monkeypatch.setattr(
        "geng_agent.task_writer_runner._writer_source_config_fingerprint",
        lambda sandbox: next(fingerprints),
    )
    monkeypatch.setattr(
        "geng_agent.task_writer_runner._archive_execution_unit_delivery",
        lambda **kwargs: archive_calls.append(int(kwargs["round_no"])),
    )

    def fake_attach(**kwargs):
        task_id = str(kwargs["task"]["task_id"])
        reporter_calls.append(task_id)
        if task_id == "train":
            feedback = {
                "schema_version": "2.0",
                "task_id": task_id,
                "host_action": "rerun_writer",
                "outcome": "not_reproduced",
                "run_valid": True,
                "rerun_reason": "core_conclusion_failed",
                "remaining_uncertainties": [],
                "rerun_evidence": {
                    "rerun_reason": "core_conclusion_failed",
                    "contract_item_ids": ["claim_train"],
                    "paper_evidence_files": ["paper_evidence/index.json"],
                    "causal_change": "correct the shared normalization",
                    "change_targets": ["src/model.py:normalize"],
                    "predicted_effect": "restore the paper ordering",
                },
            }
            kwargs["record"]["task_verification"] = feedback
            return "writer_revision", feedback
        terminal = {
            "schema_version": "2.0",
            "task_id": task_id,
            "host_action": "complete",
            "outcome": "reproduced",
            "run_valid": True,
            "rerun_reason": "none",
            "remaining_uncertainties": [],
        }
        kwargs["record"]["task_verification"] = terminal
        return "terminal", None

    monkeypatch.setattr(
        "geng_agent.task_writer_runner._attach_task_reporter_review",
        fake_attach,
    )

    records = _run_one_execution_unit_writer(
        unit=unit,
        reuse_existing=True,
        runtime_refresh_required=False,
        facts={},
        experiment_index={},
        paper={},
        paper_path=tmp_path / "paper.pdf",
        paper_context_json="",
        paper_images=[],
        paper_thesis=None,
        foundation=None,
        analysis_snapshot_hash="a" * 64,
        analysis_artifacts={},
        task_root=tmp_path / "sandboxes",
        audit_dir=tmp_path / "audit",
        run_repro=True,
        review_feedback={},
        task_review_callback=lambda *args: {},
        case_runtime=None,
    )

    assert len(writer_calls) == 1
    assert archive_calls == [1]
    assert reporter_calls == ["train", "evaluate", "train", "evaluate"]
    by_id = {record["task_id"]: record for record in records}
    assert by_id["train"]["scientific_stop_reason"] == (
        "repeated_execution_unit_rerun_request_without_new_causal_plan"
    )
    assert by_id["train"]["task_verification"]["host_action"] == "complete"


def test_compound_runtime_refresh_marker_requires_fresh_success(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _tasks, unit, entries = _two_task_compound_unit()

    monkeypatch.setattr(
        "geng_agent.task_writer_runner._prepare_execution_unit_writer_sandbox",
        lambda **kwargs: Path(kwargs["sandbox"]).mkdir(parents=True, exist_ok=True),
    )

    def run_case(case_name: str, status: dict, *, write_delivery: bool) -> tuple[Path, list[dict]]:
        task_root = tmp_path / case_name / "sandboxes"
        sandbox = _execution_unit_sandbox(task_root, unit["unit_id"])
        sandbox.mkdir(parents=True)

        def fake_session(**kwargs):
            if write_delivery:
                for entry in entries:
                    output = Path(kwargs["sandbox"]) / "outputs" / entry["output_subdir"]
                    output.mkdir(parents=True, exist_ok=True)
                    (output / "task_agent_result.json").write_text(
                        json.dumps({"status": "ready_for_review"}),
                        encoding="utf-8",
                    )
            return dict(status)

        monkeypatch.setattr(
            "geng_agent.task_writer_runner._run_task_writer_codex_session",
            fake_session,
        )
        records = _run_one_execution_unit_writer(
            unit=unit,
            reuse_existing=True,
            runtime_refresh_required=True,
            facts={},
            experiment_index={},
            paper={},
            paper_path=tmp_path / "paper.pdf",
            paper_context_json="",
            paper_images=[],
            paper_thesis=None,
            foundation=None,
            analysis_snapshot_hash="a" * 64,
            analysis_artifacts={},
            task_root=task_root,
            audit_dir=tmp_path / case_name / "audit",
            run_repro=False,
            review_feedback={},
            task_review_callback=None,
            case_runtime=None,
        )
        return sandbox, records

    successful_sandbox, successful_records = run_case(
        "success",
        {"ok": True},
        write_delivery=True,
    )
    marker_name = ".geng_runtime_refresh_pending.json"
    assert not (successful_sandbox / marker_name).exists()
    assert all(record["runtime_refresh_completed"] is True for record in successful_records)
    assert all(record["environment_refresh_completed"] is True for record in successful_records)

    failed_sandbox, failed_records = run_case(
        "failure",
        {"ok": False, "error_kind": "codex_failed", "error": "boom"},
        write_delivery=False,
    )
    assert (failed_sandbox / marker_name).is_file()
    assert all(record["runtime_refresh_completed"] is False for record in failed_records)
    assert all(record["environment_refresh_completed"] is False for record in failed_records)

    environment_sandbox, environment_records = run_case(
        "environment",
        {
            "ok": False,
            "error_kind": "environment_request",
            "environment_requests": [],
        },
        write_delivery=False,
    )
    assert (environment_sandbox / marker_name).is_file()
    assert all(record["runtime_refresh_completed"] is False for record in environment_records)
    assert all(record["environment_refresh_completed"] is False for record in environment_records)


def test_completed_compound_refresh_is_reusable_on_next_dispatch(
    monkeypatch,
    tmp_path: Path,
) -> None:
    tasks, unit, _entries = _two_task_compound_unit()
    pairs = [(task, entry) for _index, task, entry in unit["members"]]
    sandbox = _execution_unit_sandbox(tmp_path / "sandboxes", unit["unit_id"])
    completed: dict[int, dict] = {}
    for index, task, entry in unit["members"]:
        record = _record(index, task, entry, sandbox, unit["unit_id"])
        record.update(
            {
                "runtime_refresh_required": True,
                "runtime_refresh_completed": True,
                "environment_refresh_required": True,
                "environment_refresh_completed": True,
            }
        )
        completed[index] = record

    def unexpected_launch(**kwargs):
        raise AssertionError(f"completed refresh was relaunched: {kwargs}")

    monkeypatch.setattr(
        "geng_agent.task_writer_dispatch._run_one_execution_unit_writer",
        unexpected_launch,
    )
    monkeypatch.setattr(
        "geng_agent.task_writer_dispatch._run_one_task_writer",
        unexpected_launch,
    )

    records, audit = _dispatch_task_writers(
        task_pairs=pairs,
        facts={},
        experiment_index={},
        paper={},
        paper_path=tmp_path / "paper.pdf",
        paper_context_json="",
        paper_images=[],
        paper_thesis=None,
        analysis_snapshot_hash="a" * 64,
        analysis_artifacts={},
        task_root=tmp_path / "sandboxes",
        audit_dir=tmp_path / "audit",
        run_repro=False,
        initial_records_by_index=completed,
        execution_plan={
            "execution_units": [
                {
                    key: value
                    for key, value in unit.items()
                    if key not in {"members", "unit_index"}
                }
            ]
        },
    )

    assert [record["task_id"] for record in records] == [
        task["task_id"] for task in tasks["repro_tasks"]
    ]
    assert audit["reused_execution_unit_ids"] == [unit["unit_id"]]
    assert audit["launched_execution_unit_ids"] == []


def test_compound_continuation_archives_stale_outputs_and_shared_artifacts(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _tasks, unit, entries = _two_task_compound_unit()
    task_root = tmp_path / "sandboxes"
    sandbox = _execution_unit_sandbox(task_root, unit["unit_id"])
    unit_assets = sandbox / "execution_units" / unit["unit_id"]
    unit_assets.mkdir(parents=True)
    checkpoint = unit_assets / "model.bin"
    checkpoint.write_bytes(b"stale-checkpoint")
    for entry in entries:
        output = sandbox / "outputs" / entry["output_subdir"]
        output.mkdir(parents=True)
        (output / "results.csv").write_text("x\n1\n", encoding="utf-8")
        (output / "task_agent_result.json").write_text(
            json.dumps({"status": "ready_for_review"}),
            encoding="utf-8",
        )
    (sandbox / "execution_unit_result.json").write_text(
        json.dumps(
            {
                "execution_unit_id": unit["unit_id"],
                "task_ids": ["train", "evaluate"],
                "artifact_lineage": [
                    {
                        "artifact_id": "trained_checkpoint",
                        "path": checkpoint.relative_to(sandbox).as_posix(),
                        "producer_task_id": "train",
                        "consumer_task_ids": ["evaluate"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "geng_agent.task_writer_runner._prepare_execution_unit_writer_sandbox",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        "geng_agent.task_writer_runner._run_task_writer_codex_session",
        lambda **kwargs: {"ok": False, "error_kind": "codex_failed", "error": "boom"},
    )

    records = _run_one_execution_unit_writer(
        unit=unit,
        reuse_existing=True,
        runtime_refresh_required=False,
        facts={},
        experiment_index={},
        paper={},
        paper_path=tmp_path / "paper.pdf",
        paper_context_json="",
        paper_images=[],
        paper_thesis=None,
        foundation=None,
        analysis_snapshot_hash="a" * 64,
        analysis_artifacts={},
        task_root=task_root,
        audit_dir=tmp_path / "audit",
        run_repro=False,
        review_feedback={
            "train": {
                "task_id": "train",
                "host_action": "rerun_writer",
                "outcome": "not_reproduced",
                "run_valid": True,
                "rerun_reason": "core_conclusion_failed",
                "remaining_uncertainties": [],
            }
        },
        task_review_callback=None,
        case_runtime=None,
    )

    assert all(record["writer_completed"] is False for record in records)
    assert not (sandbox / "execution_unit_result.json").exists()
    assert checkpoint.read_bytes() == b"stale-checkpoint"
    assert unit_assets.is_dir()
    for entry in entries:
        assert not (sandbox / "outputs" / entry["output_subdir"]).exists()
        archived_output = (
            sandbox
            / "writer_progress"
            / "round_001"
            / "outputs"
            / entry["output_subdir"]
        )
        assert (archived_output / "results.csv").is_file()
        assert (archived_output / "task_agent_result.json").is_file()
    archive_root = sandbox / "writer_progress" / "round_001"
    assert (archive_root / "execution_unit_result.json").is_file()
    # Kept for possible reuse; the execution broker separately rejects a
    # checkpoint without a current producer receipt. A continuation alone
    # does not prove the training recipe changed.
    assert not (archive_root / "shared_artifacts" / "execution_units").exists()
