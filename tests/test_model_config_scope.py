from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from geng_agent.model_config import (
    get_current_model_config,
    load_model_config,
    model_config_scope,
)
from geng_agent.outputs import write_json
from geng_agent.pipeline import ReviewPipeline
from geng_agent.task_writer_dispatch import (
    _dispatch_task_writers,
    _refresh_cached_task_reporters,
)


def _config_file(path: Path, model: str) -> Path:
    write_json(path, {
        "schema_version": 1,
        "default": "primary",
        "profiles": {"primary": {"provider": "openai", "model": model}},
    })
    return path


def _current_model() -> str:
    configs = get_current_model_config()
    assert configs is not None
    return configs.model


def _mock_cost(monkeypatch) -> None:
    monkeypatch.setattr(
        "geng_agent.pipeline_context.PipelineRunContext.persist_cost_snapshot",
        lambda context: None,
    )


@pytest.mark.parametrize("source", ["file", "environment"])
def test_pipeline_freezes_config_for_all_flows_and_refreshes_on_resume(
    monkeypatch, tmp_path: Path, source: str,
) -> None:
    _mock_cost(monkeypatch)
    path = _config_file(tmp_path / "models.json", "model-before")
    environment = {"GENG_CODEX_MODEL": "model-before"}
    monkeypatch.setattr(
        "geng_agent.pipeline.load_model_config",
        lambda selected: load_model_config(selected, getter=environment.get),
    )
    observed = []

    def analysis(pipeline, context, **kwargs):
        observed.append(("analysis", _current_model()))
        # A user may change defaults while an existing run is still working.
        environment["GENG_CODEX_MODEL"] = "model-after"
        _config_file(path, "model-after")
        return object()

    def execution(context, result):
        observed.append(("execution", _current_model()))
        return object()

    def report(pipeline, context, analysis, execution, **kwargs):
        observed.append(("report", _current_model()))
        return context.run_id

    monkeypatch.setattr("geng_agent.pipeline.run_analysis_flow", analysis)
    monkeypatch.setattr("geng_agent.pipeline.run_execution_flow", execution)
    monkeypatch.setattr("geng_agent.pipeline.run_report_flow", report)
    kwargs = {"model_config_path": path} if source == "file" else {}
    case = tmp_path / "case"
    pipeline = ReviewPipeline()
    first_id = pipeline.run(tmp_path / "paper.pdf", case, **kwargs)
    assert get_current_model_config() is None
    second_id = pipeline.run(tmp_path / "paper.pdf", case, resume=True, **kwargs)
    assert get_current_model_config() is None
    assert observed == [
        (phase, model)
        for model in ("model-before", "model-after")
        for phase in ("analysis", "execution", "report")
    ]
    assert first_id != second_id
    for run_id, model in ((first_id, "model-before"), (second_id, "model-after")):
        snapshot = json.loads(
            (case / "audit" / "model_configs" / f"{run_id}.json").read_text(encoding="utf-8")
        )
        assert snapshot["schema_version"] == 2
        assert snapshot["run_id"] == run_id
        assert snapshot["config"]["model"] == model
    latest = json.loads((case / "audit" / "model_config.json").read_text(encoding="utf-8"))
    assert latest["run_id"] == second_id


def test_pipeline_inherits_callers_scope_and_restores_it_after_failed_override(
    monkeypatch, tmp_path: Path,
) -> None:
    _mock_cost(monkeypatch)
    outer = load_model_config(_config_file(tmp_path / "outer.json", "outer-model"))
    override_path = _config_file(tmp_path / "override.json", "override-model")
    observed = []

    def analysis(pipeline, context, **kwargs):
        observed.append(_current_model())
        if _current_model() == "override-model":
            raise RuntimeError("simulated analysis interruption")
        return object()

    monkeypatch.setattr("geng_agent.pipeline.run_analysis_flow", analysis)
    monkeypatch.setattr("geng_agent.pipeline.finish_analysis_only", lambda context, result: "done")
    with model_config_scope(outer):
        # Inheriting an explicit caller scope must not reread process defaults.
        def unexpected_load(path):
            raise AssertionError("inherited scope should already be frozen")

        with monkeypatch.context() as nested:
            nested.setattr("geng_agent.pipeline.load_model_config", unexpected_load)
            result = ReviewPipeline().run(
                tmp_path / "paper.pdf", tmp_path / "inherited", analysis_only=True,
            )
        assert result == "done"
        with pytest.raises(RuntimeError, match="simulated analysis interruption"):
            ReviewPipeline().run(
                tmp_path / "paper.pdf", tmp_path / "overridden",
                model_config_path=override_path,
            )
        assert _current_model() == "outer-model"
    assert observed == ["outer-model", "override-model"]
    assert get_current_model_config() is None


def test_parallel_pipelines_propagate_isolated_configs_to_all_dispatch_paths(
    monkeypatch, tmp_path: Path,
) -> None:
    _mock_cost(monkeypatch)
    configs = {
        name: _config_file(tmp_path / f"{name}.json", name)
        for name in ("model-alpha", "model-beta")
    }
    analysis_barrier = Barrier(2)
    writer_barrier = Barrier(4)
    reporter_barrier = Barrier(6)
    pairs = [({"task_id": task_id}, {"task_id": task_id}) for task_id in ("a", "b", "c")]
    plan = {"execution_units": [{"unit_id": "shared", "task_ids": ["a", "b"]}]}

    def analysis(pipeline, context, **kwargs):
        analysis_barrier.wait(timeout=10)
        assert _current_model() == context.output_dir.name
        return object()

    def writer_records(members):
        writer_barrier.wait(timeout=10)
        return [
            {"index": index, "task_id": task["task_id"], "writer_completed": True,
             "observed_model": _current_model()}
            for index, task, entry in members
        ]

    def single_writer(**kwargs):
        return writer_records([(kwargs["index"], kwargs["task"], kwargs["manifest_entry"])])[0]

    def compound_writer(**kwargs):
        return writer_records(kwargs["unit"]["members"])

    def reporter(index, task, record, round_no):
        reporter_barrier.wait(timeout=10)
        return {"ok": True, "task_verification": {}, "observed_model": _current_model()}

    def execution(context, analysis):
        records, _ = _dispatch_task_writers(
            task_pairs=pairs, facts={}, experiment_index={}, paper={},
            paper_path=context.paper_path, paper_context_json="", paper_images=[],
            paper_thesis=None, analysis_snapshot_hash="test", analysis_artifacts={},
            task_root=context.output_dir / "sandboxes", audit_dir=context.audit_dir,
            run_repro=False, execution_plan=plan,
        )
        refreshed, _, _, _ = _refresh_cached_task_reporters(
            task_pairs=pairs, cached_records=records, experiment_index={},
            task_review_callback=reporter,
        )
        return refreshed

    def report(pipeline, context, analysis, execution, **kwargs):
        assert _current_model() == context.output_dir.name
        return execution

    monkeypatch.setattr("geng_agent.pipeline.run_analysis_flow", analysis)
    monkeypatch.setattr("geng_agent.pipeline.run_execution_flow", execution)
    monkeypatch.setattr("geng_agent.pipeline.run_report_flow", report)
    monkeypatch.setattr("geng_agent.task_writer_dispatch._run_one_task_writer", single_writer)
    monkeypatch.setattr("geng_agent.task_writer_dispatch._run_one_execution_unit_writer", compound_writer)

    def run_one(name: str):
        result = ReviewPipeline().run(
            tmp_path / "paper.pdf", tmp_path / name, model_config_path=configs[name],
        )
        assert get_current_model_config() is None
        return result

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = {name: executor.submit(run_one, name) for name in configs}
        results = {name: future.result(timeout=30) for name, future in futures.items()}
    for name, records in results.items():
        assert [record["task_id"] for record in records] == ["a", "b", "c"]
        assert {record["observed_model"] for record in records} == {name}
        assert {record["task_reporter"]["observed_model"] for record in records} == {name}
    assert get_current_model_config() is None


def test_run_stage_freezes_explicit_model_config_before_cleanup(monkeypatch, tmp_path: Path) -> None:
    _mock_cost(monkeypatch)
    selected = _config_file(tmp_path / "models.json", "before-cleanup")

    def cleanup(*args):
        assert _current_model() == "before-cleanup"
        _config_file(selected, "after-cleanup")

    def analysis(pipeline, context, **kwargs):
        assert context.options.resume is True
        return _current_model()

    monkeypatch.setattr("geng_agent.pipeline._clear_stage_outputs", cleanup)
    monkeypatch.setattr("geng_agent.pipeline.run_analysis_flow", analysis)
    monkeypatch.setattr("geng_agent.pipeline.finish_analysis_only", lambda context, result: result)
    assert ReviewPipeline().run_stage(
        "reports", tmp_path / "paper.pdf", tmp_path / "case", model_config_path=selected,
        analysis_only=True,
    ) == "before-cleanup"
    assert get_current_model_config() is None


def test_invalid_stage_model_config_preserves_existing_outputs(monkeypatch, tmp_path: Path) -> None:
    selected = tmp_path / "invalid-models.json"
    selected.write_text("{invalid-json", encoding="utf-8")
    case = tmp_path / "case"
    case.mkdir()
    sentinel = case / "result_review.md"
    original = b"Existing reviewed report must remain untouched."
    sentinel.write_bytes(original)
    cleanup_calls = []

    def cleanup(*args):
        cleanup_calls.append(args)
        sentinel.unlink()

    monkeypatch.setattr("geng_agent.pipeline._clear_stage_outputs", cleanup)
    with pytest.raises(ValueError, match="模型配置无效"):
        ReviewPipeline().run_stage(
            "reports", tmp_path / "paper.pdf", case, model_config_path=selected,
        )
    assert cleanup_calls == []
    assert sentinel.read_bytes() == original
    assert get_current_model_config() is None


@pytest.mark.parametrize("single_stage", [False, True])
def test_unified_model_rejects_separate_legacy_analysis_before_any_mutation(
    monkeypatch, tmp_path: Path, single_stage: bool,
) -> None:
    selected = _config_file(tmp_path / "models.json", "one-project-model")
    case = tmp_path / "case"
    case.mkdir()
    sentinel = case / "result_review.md"
    sentinel.write_text("Keep previous report.", encoding="utf-8")

    def unexpected_cleanup(*args):
        raise AssertionError("Model conflict must be checked before cleanup")

    monkeypatch.setattr("geng_agent.pipeline._clear_stage_outputs", unexpected_cleanup)
    pipeline = ReviewPipeline(client=object())
    kwargs = dict(paper_path=tmp_path / "paper.pdf", output_dir=case,
                  model_config_path=selected, analysis_backend="llm")
    with pytest.raises(ValueError, match="统一模型配置不能"):
        if single_stage:
            pipeline.run_stage("reports", **kwargs)
        else:
            pipeline.run(**kwargs)
    assert sentinel.read_text(encoding="utf-8") == "Keep previous report."
    assert list(case.iterdir()) == [sentinel]
    assert get_current_model_config() is None
