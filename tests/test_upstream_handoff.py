"""Small local fixtures for cross-task delivery; no production cases or models."""
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import sys
from unittest.mock import Mock

import pytest

from geng_agent.artifact_paths import file_sha256
from geng_agent.outputs import write_json
from geng_agent.task_inputs import (UpstreamHandoffError, copy_upstream_inputs,
    handoff_path, require_verified_handoff, upstream_input_identity)
from geng_agent.execution_receipts import ExecutionBroker, _producer_chain_issues


def producer(tmp_path):
    root = tmp_path / "producer"
    dataset = root / "execution_units/train/artifacts/dataset"
    dataset.mkdir(parents=True)
    for name in ("metadata.json", "rays_1.npz", "cir_1.npz", "split.json"):
        (dataset / name).write_text(name, encoding="utf-8")
    record = {"task_id": "train", "sandbox": str(root), "execution_unit_id": "train", "writer_completed": True,
              "result_json": {"artifact_exports": [
                  {"artifact_id": "artifacts/dataset/rays", "files": [{"source": "execution_units/train/artifacts/dataset/rays_1.npz", "target": "artifacts/dataset/rays_1.npz"}]},
                  {"artifact_id": "artifacts/dataset/cir", "files": [
                      {"source": "execution_units/train/artifacts/dataset/cir_1.npz", "target": "artifacts/dataset/cir_1.npz"},
                      {"source": "execution_units/train/artifacts/dataset/split.json", "target": "artifacts/dataset/split.json"}]}]}}
    task = {"depends_on": [{"task_id": "train", "artifacts": ["artifacts/dataset/metadata.json", "artifacts/dataset/rays", "artifacts/dataset/cir"]}]}
    audit = tmp_path / "audit"
    receipt = {"observer": "orchestration_host", "run_id": "first", "workspace": str(root.resolve()),
               "task_id": "train", "mode": "full", "returncode": 0, "inputs_stable": True,
               "dependency_issues": [], "source_hashes": {}, "input_hashes": {}, "finished_at": 1,
               "observed_distributions": {},
               "produced_artifacts": {p.relative_to(root).as_posix(): file_sha256(p) for p in dataset.iterdir()}}
    write_json(audit / "execution_runs/first/execution_receipt.json", receipt)
    return root, record, task, audit


def test_execution_unit_and_explicit_flat_file_aliases(tmp_path):
    source, record, task, audit = producer(tmp_path)
    dest = tmp_path / "consumer"
    result = copy_upstream_inputs(dest, task, [record], audit_dir=audit)
    assert result["state"] == "verified"
    files = result["inputs"][0]["files"]
    assert len(files) == 4
    for f in files:
        assert (dest / f["target"]).read_bytes() == (source / f["source"]).read_bytes()
        assert f["producer_run_id"] == "first"
    assert require_verified_handoff(dest, audit)["state"] == "verified"


def test_missing_file_keeps_previous_delivery_and_results(tmp_path):
    _source, record, task, audit = producer(tmp_path)
    dest = tmp_path / "consumer"
    copy_upstream_inputs(dest, task, [record], audit_dir=audit)
    old = {str(p): p.read_bytes() for p in (dest / "upstream_tasks").rglob("*") if p.is_file()}
    result = dest / "outputs/result.csv"
    result.parent.mkdir(); result.write_text("previous science")
    task["depends_on"][0]["artifacts"].append("absent")
    with pytest.raises(UpstreamHandoffError):
        copy_upstream_inputs(dest, task, [record], audit_dir=audit)
    assert all(Path(p).read_bytes() == b for p, b in old.items())
    assert result.read_text() == "previous science"
    with pytest.raises(UpstreamHandoffError, match="not verified"):
        require_verified_handoff(dest, audit)


@pytest.mark.parametrize("failure", ["interrupt", "corrupt", "source_changed"])
def test_incomplete_transfer_never_publishes_active_tree(tmp_path, monkeypatch, failure):
    source, record, task, audit = producer(tmp_path)
    dest = tmp_path / "consumer"
    import geng_agent.task_inputs as transfer
    original = transfer.shutil.copy2
    def broken(src, dst):
        original(src, dst)
        if failure == "interrupt":
            raise OSError("simulated copy interruption")
        if failure == "corrupt":
            Path(dst).write_text("bad copy")
        else:
            Path(src).write_text("changed while copying")
    monkeypatch.setattr(transfer.shutil, "copy2", broken)
    with pytest.raises((UpstreamHandoffError, OSError)):
        copy_upstream_inputs(dest, task, [record], audit_dir=audit)
    assert not (dest / "upstream_tasks").exists()
    assert json.loads(handoff_path(dest, audit).read_text())["state"] == "failed"
    assert list((dest / ".geng_handoff").rglob("*.json"))


def test_idempotent_retry_and_changed_version_archive(tmp_path):
    source, record, task, audit = producer(tmp_path)
    dest = tmp_path / "consumer"
    copy_upstream_inputs(dest, task, [record], audit_dir=audit)
    data = dest / "upstream_tasks/train/artifacts/dataset/cir_1.npz"
    original_mtime = data.stat().st_mtime_ns
    before_identity = upstream_input_identity(task, [record])
    copy_upstream_inputs(dest, task, [record], audit_dir=audit)
    assert data.stat().st_mtime_ns == original_mtime
    assert not list((dest / "writer_progress").glob("upstream_*"))
    src = source / "execution_units/train/artifacts/dataset/cir_1.npz"
    src.write_text("new measured version")
    receipt_path = audit / "execution_runs/first/execution_receipt.json"
    receipt = json.loads(receipt_path.read_text())
    receipt.update(run_id="second", finished_at=2)
    receipt["produced_artifacts"][src.relative_to(source).as_posix()] = file_sha256(src)
    write_json(audit / "execution_runs/second/execution_receipt.json", receipt)
    assert upstream_input_identity(task, [record]) != before_identity
    copy_upstream_inputs(dest, task, [record], audit_dir=audit)
    assert data.read_text() == "new measured version"
    archived = list((dest / "writer_progress").glob("upstream_*/train/artifacts/dataset/cir_1.npz"))
    assert len(archived) == 1 and archived[0].read_text() == "cir_1.npz"


@pytest.mark.parametrize("kind", ["copy", "source", "receipt", "forged_public", "unbound"])
def test_tamper_or_missing_provenance_is_not_accepted(tmp_path, kind):
    source, record, task, audit = producer(tmp_path)
    dest = tmp_path / "consumer"
    copy_upstream_inputs(dest, task, [record], audit_dir=audit)
    if kind == "copy":
        (dest / "upstream_tasks/train/artifacts/dataset/cir_1.npz").write_text("tampered")
    elif kind == "source":
        (source / "execution_units/train/artifacts/dataset/cir_1.npz").write_text("tampered")
    elif kind == "receipt":
        (audit / "execution_runs/first/execution_receipt.json").write_text("{}")
    elif kind == "forged_public":
        trusted = handoff_path(dest, audit)
        value = json.loads(trusted.read_text()); value["state"] = "failed"; write_json(trusted, value)
        write_json(dest / "upstream_task_inputs.json", {"state": "verified"})
    else:
        handoff_path(dest, audit).unlink()
    with pytest.raises(UpstreamHandoffError):
        require_verified_handoff(dest, audit)


def test_upstream_prefix_no_longer_bypasses_lineage(tmp_path):
    issues = _producer_chain_issues(tmp_path, tmp_path / "audit", {"upstream_tasks/t/model.bin": "fake"})
    assert issues and "upstream source unavailable" in issues[0]


@pytest.mark.parametrize("target", ["../../outputs/science.csv", "/outside", "C:/outside"])
def test_mapping_cannot_escape_upstream_namespace(tmp_path, target):
    _source, record, task, audit = producer(tmp_path)
    record["result_json"]["artifact_exports"][0]["files"][0]["target"] = target
    with pytest.raises(UpstreamHandoffError):
        copy_upstream_inputs(tmp_path / "consumer", task, [record], audit_dir=audit)


def test_consumers_can_copy_concurrently_without_changing_producer(tmp_path):
    source, record, task, audit = producer(tmp_path)
    original = {p: p.read_bytes() for p in source.rglob("*") if p.is_file()}
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(copy_upstream_inputs, tmp_path / n, task, [record], audit_dir=audit) for n in ("a", "b")]
        assert all(f.result()["state"] == "verified" for f in futures)
    assert all(p.read_bytes() == data for p, data in original.items())


class LocalBoundary:
    """Run only the tiny trusted fixture program, never case-provided code."""
    policy = {"provider": "local_test_fixture"}
    def allow_read(self, path):
        pass
    def run(self, *args, **kwargs):
        return subprocess.run(*args, **kwargs)
    def popen(self, *args, **kwargs):
        kwargs.pop("allow_network")
        return subprocess.Popen(*args, **kwargs)


def project(root, code, audit):
    (root / "tasks").mkdir(parents=True)
    (root / "tasks/__init__.py").write_text("")
    (root / "tasks/t.py").write_text(code, encoding="utf-8")
    write_json(root / "config.json", {})
    write_json(root / "tasks_manifest.json", {"tasks": [{"task_id": "t", "module": "t", "output_subdir": "t", "config_full": "config.json"}]})
    return ExecutionBroker(root, audit, Path(sys.executable), process_boundary=LocalBoundary())


@pytest.mark.parametrize("order,expected", [("write_read", 0), ("read_write", 1), ("append_read", 1)])
def test_real_broker_distinguishes_generated_reads(tmp_path, order, expected):
    body = {
        "write_read": "p.write_text('generated'); p.read_text()",
        "read_write": "p.read_text(); p.write_text('changed')",
        "append_read": "p.open('a').write('more'); p.read_text()",
    }[order]
    root = tmp_path / "project"
    broker = project(root, "from pathlib import Path\ndef main(config):\n    p=Path('execution_units/data.txt')\n    " + body + "\n    Path('outputs/t/result.csv').write_text('1')\n", tmp_path / "audit")
    (root / "execution_units").mkdir()
    if expected:
        (root / "execution_units/data.txt").write_text("old")
    receipt = broker.execute({"task_id": "t", "mode": "full", "device": "cpu"})
    assert receipt["returncode"] == 0
    assert len(receipt["dependency_issues"]) == expected
    assert ("execution_units/data.txt" in receipt["input_hashes"]) == bool(expected)
    if not expected:
        assert "execution_units/data.txt" in receipt["generated_reads"]


def test_gate_precedes_output_archival_and_cancel_wins(tmp_path, monkeypatch):
    root = tmp_path / "project"; audit = tmp_path / "audit"
    broker = project(root, "def main(config):\n    raise AssertionError('must not run')\n", audit)
    output = root / "outputs/t/result.csv"; output.parent.mkdir(parents=True); output.write_text("keep")
    write_json(handoff_path(root, audit), {"state": "failed", "workspace": str(root.resolve())})
    select = Mock(side_effect=AssertionError("must not allocate GPU"))
    monkeypatch.setattr("geng_agent.gpu_resources.select_compute", select)
    with pytest.raises(UpstreamHandoffError):
        broker.execute({"task_id": "t"})
    broker.cancelled.set()
    from geng_agent.progress import PipelineCancelled
    with pytest.raises(PipelineCancelled):
        broker.execute({"task_id": "t"})
    assert output.read_text() == "keep"
    assert not (audit / "execution_runs").exists()
    select.assert_not_called()


def test_real_producer_to_real_consumer_then_tamper(tmp_path):
    audit = tmp_path / "audit"
    source = tmp_path / "producer"
    make = project(source, "from pathlib import Path\ndef main(config):\n    p=Path('execution_units/t/artifacts/data.txt')\n    p.parent.mkdir(parents=True,exist_ok=True)\n    p.write_text('measured data')\n    Path('outputs/t/result.csv').write_text(p.read_text())\n", audit)
    original = make.execute({"task_id": "t", "mode": "full", "device": "cpu"})
    assert original["dependency_issues"] == []
    dest = tmp_path / "consumer"
    consume = project(dest, "from pathlib import Path\ndef main(config):\n    value=Path('upstream_tasks/t/artifacts/data.txt').read_text()\n    Path('outputs/t/result.csv').write_text(value)\n", audit)
    task = {"depends_on": [{"task_id": "t", "artifacts": ["artifacts/data.txt"]}]}
    record = {"task_id": "t", "sandbox": str(source), "execution_unit_id": "t", "writer_completed": True}
    copy_upstream_inputs(dest, task, [record], audit_dir=audit)
    result = consume.execute({"task_id": "t", "mode": "full", "device": "cpu"})
    assert result["returncode"] == 0 and result["dependency_issues"] == []
    assert (dest / "outputs/t/result.csv").read_text() == "measured data"
    assert list(result["input_hashes"]) == ["upstream_tasks/t/artifacts/data.txt"]
    (dest / "upstream_tasks/t/artifacts/data.txt").write_text("tampered")
    with pytest.raises(UpstreamHandoffError):
        consume.execute({"task_id": "t", "mode": "full", "device": "cpu"})
    assert (dest / "outputs/t/result.csv").read_text() == "measured data"


def test_publish_failure_rolls_back_and_retry_succeeds(tmp_path, monkeypatch):
    _source, record, task, audit = producer(tmp_path)
    dest = tmp_path / "consumer"
    old = dest / "upstream_tasks/previous.txt"; old.parent.mkdir(parents=True); old.write_text("preserve")
    rename = Path.rename
    def fail_commit(path, target):
        if ".geng_handoff" in path.parts and path.name == "upstream_tasks":
            raise OSError("simulated publication failure")
        return rename(path, target)
    with monkeypatch.context() as patch:
        patch.setattr(Path, "rename", fail_commit)
        with pytest.raises(UpstreamHandoffError):
            copy_upstream_inputs(dest, task, [record], audit_dir=audit)
    assert old.read_text() == "preserve"
    copy_upstream_inputs(dest, task, [record], audit_dir=audit)
    archived = list((dest / "writer_progress").glob("upstream_*/previous.txt"))
    assert len(archived) == 1 and archived[0].read_text() == "preserve"
    assert require_verified_handoff(dest, audit)["state"] == "verified"


def test_cancellation_during_copy_preserves_old_inputs(tmp_path, monkeypatch):
    _source, record, task, audit = producer(tmp_path)
    dest = tmp_path / "consumer"
    old = dest / "upstream_tasks/old.txt"; old.parent.mkdir(parents=True); old.write_text("keep")
    from geng_agent.progress import PipelineCancelled
    supervisor = Mock()
    supervisor._check_cancelled.side_effect = [None, None, PipelineCancelled("user cancelled")]
    monkeypatch.setattr("geng_agent.supervisor.current_supervisor", lambda: supervisor)
    with pytest.raises(PipelineCancelled):
        copy_upstream_inputs(dest, task, [record], audit_dir=audit)
    assert old.read_text() == "keep"
    assert json.loads(handoff_path(dest, audit).read_text())["state"] == "failed"


def test_producer_sees_consumers_exact_export_requests(tmp_path):
    from geng_agent.task_writer_inputs import write_writer_input
    dependency = {"task_id": "producer", "artifacts": ["rays", "cir"]}
    write_json(tmp_path / "paper_evidence/analysis_artifacts/execution_plan.json", {
        "execution_units": [{"task_ids": ["consumer"], "depends_on": [dependency]}]})
    packet = write_writer_input(sandbox=tmp_path, members=[(1, {"task_id": "producer"}, {})],
        facts={}, experiment_index={}, bindings={}, paper={}, case_runtime=None)
    assert packet["requested_exports"] == [{"consumer_task_ids": ["consumer"], "dependency": dependency}]


def test_no_export_alias_is_guessed_from_similar_filenames(tmp_path):
    _source, record, task, audit = producer(tmp_path)
    record["result_json"] = {}
    with pytest.raises(UpstreamHandoffError, match="explicit artifact_exports"):
        copy_upstream_inputs(tmp_path / "consumer", task, [record], audit_dir=audit)


@pytest.mark.parametrize("changed", [False, True])
def test_legacy_cache_migration_does_not_repeat_unchanged_science(tmp_path, monkeypatch, changed):
    from geng_agent.task_inputs import legacy_upstream_input_identity
    from tests.test_agent_owned_flow import _dispatch_fixture
    source = tmp_path / "producer"; source.mkdir()
    data = source / "model.bin"; data.write_text("original")
    task = {"task_id": "evaluate", "depends_on": [{"task_id": "train", "artifacts": ["model.bin"]}]}
    records = {1: {"task_id": "train", "writer_completed": True, "sandbox": str(source)},
               2: {"task_id": "evaluate", "writer_completed": True}}
    records[2]["upstream_input_identity"] = legacy_upstream_input_identity(task, list(records.values()))
    if changed:
        data.write_text("new measured input")
    writer = Mock(return_value={"task_id": "evaluate", "writer_completed": True})
    monkeypatch.setattr("geng_agent.task_writer_dispatch._run_one_task_writer", writer)
    _dispatch_fixture(tmp_path, [{"task_id": "train"}, task], initial_records_by_index=records)
    assert writer.call_count == int(changed)
    if changed:
        assert writer.call_args.kwargs["runtime_refresh_required"] is True


def test_failed_refresh_transfer_does_not_archive_previous_science(tmp_path, monkeypatch):
    import geng_agent.task_writer_runner as runner
    sandbox = tmp_path / "writers/01_evaluate"
    result = sandbox / "outputs/evaluate/result.csv"
    result.parent.mkdir(parents=True); result.write_text("original result")
    archive = Mock(side_effect=AssertionError("must not archive on handoff failure"))
    monkeypatch.setattr(runner, "_archive_nonterminal_writer_delivery", archive)
    monkeypatch.setattr(runner, "_prepare_task_writer_sandbox", Mock())
    with pytest.raises(UpstreamHandoffError):
        runner._run_one_task_writer(index=1, task={"task_id": "evaluate", "depends_on": ["absent"]},
            manifest_entry={"task_id": "evaluate", "module": "evaluate"}, facts={}, experiment_index={},
            paper={}, paper_path=tmp_path / "paper.pdf", paper_context_json="{}", paper_images=[],
            paper_thesis=None, analysis_snapshot_hash="updated", analysis_artifacts={},
            task_root=tmp_path / "writers", audit_dir=tmp_path / "audit", run_repro=True,
            review_feedback=None, reuse_existing=True, runtime_refresh_required=True)
    assert result.read_text() == "original result"
    archive.assert_not_called()
