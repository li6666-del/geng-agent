from __future__ import annotations


from pathlib import Path
from typing import Any

from .documents import load_paper
from .experiment_index import build_local_experiment_index
from .outputs import write_json
from .pipeline_helpers import _read_json_file
from .runtime_status import (
    _load_valid_stage_cache,
    _paper_cache_matches,
    _sha256_file,
    build_stage_cache_metadata,
)
from .scientific_materiality import SCIENTIFIC_POLICY_ID
from .stage_cleanup import _clear_stage_outputs


def load_or_create_paper(
    *,
    paper_path: Path,
    output_dir: Path,
    max_pages: int | None,
    resume: bool,
) -> dict[str, Any]:
    cache_path = output_dir / "paper_chunks.json"
    if resume and cache_path.exists():
        cached = _read_json_file(cache_path)
        if _paper_cache_matches(cached, paper_path):
            return cached
    paper = load_paper(paper_path, max_pages=max_pages)
    paper["source_sha256"] = _sha256_file(paper_path)
    write_json(cache_path, paper)
    _clear_stage_outputs(
        output_dir,
        "paper",
        preserve_audit=True,
        preserve_paths={"paper_chunks.json"},
    )
    return paper


def load_or_create_experiment_index(
    *,
    output_dir: Path,
    audit_dir: Path,
    facts: dict[str, Any],
    tasks: dict[str, Any],
    paper: dict[str, Any],
    figure_index: dict[str, Any] | None = None,
    resume: bool,
) -> dict[str, Any]:
    output_path = output_dir / "experiment_index.json"
    stage_label = "02e_build_experiment_index"
    cache_metadata = build_stage_cache_metadata(
        stage_label=stage_label,
        schema_stage="experiment_index",
        prompt="local deterministic experiment index",
        policy_version=SCIENTIFIC_POLICY_ID,
        inputs={
            "facts": facts,
            "tasks": tasks,
            "paper_source_sha256": paper.get("source_sha256"),
            "figure_index": figure_index or {},
        },
    )
    if resume and output_path.exists():
        cached = _load_valid_stage_cache(
            path=output_path,
            audit_dir=audit_dir,
            stage_label=stage_label,
            schema_stage="experiment_index",
            expected_cache_metadata=cache_metadata,
        )
        if cached is not None:
            return cached

    experiment_index = build_local_experiment_index(
        facts,
        tasks,
        paper,
        figure_index,
    )
    meta = (
        dict(experiment_index.get("_meta", {}))
        if isinstance(experiment_index.get("_meta"), dict)
        else {}
    )
    meta["cache"] = cache_metadata
    experiment_index["_meta"] = meta
    write_json(output_path, experiment_index)
    _clear_stage_outputs(
        output_dir,
        "experiment_index",
        preserve_audit=True,
        preserve_paths={"experiment_index.json"},
    )
    write_json(
        audit_dir / "local_02e_build_experiment_index.json",
        {
            "ok": True,
            "experiment_count": len(experiment_index.get("experiments", [])),
            "meta": experiment_index.get("_meta", {}),
        },
    )
    return experiment_index


def render_paper_images(
    pipeline: Any,
    *,
    paper_path: Path,
    paper: dict[str, Any],
) -> list[Any]:
    if paper.get("format") != "pdf":
        return []
    if pipeline.client is not None and not hasattr(
        pipeline.client, "complete_multimodal"
    ):
        return []
    try:
        from .paper_evidence import render_pdf_pages_for_llm

        return render_pdf_pages_for_llm(
            paper_path,
            pages=None,
            max_pages=None,
        )
    except Exception:
        return []
