from __future__ import annotations

from pathlib import Path
from typing import Any

from .documents import load_paper
from .execution_plan import compile_execution_plan
from .experiment_index import build_local_experiment_index
from .json_utils import pretty_json
from .outputs import write_json
from .pipeline_helpers import _read_json_file, wrap_untrusted
from .runtime_status import (
    _load_valid_stage_cache,
    _paper_cache_matches,
    _sha256_file,
    build_stage_cache_metadata,
)
from .schemas import ValidationIssue, format_issues, validate_stage
from .scientific_materiality import SCIENTIFIC_POLICY_ID
from .stage_cleanup import _clear_stage_outputs
from .workflow_policy import _execution_plan_requires_shared_science


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


def load_or_create_paper_thesis(
    pipeline: Any,
    *,
    output_dir: Path,
    audit_dir: Path,
    facts: dict[str, Any],
    paper_context: str,
    paper_images: list[Any],
    resume: bool,
    max_attempts: int,
    analysis_backend: str = "llm",
    paper_source_sha256: str | None = None,
) -> dict[str, Any] | None:
    """Distill the central claim without making thesis extraction fatal."""

    prompt = pipeline.prompt_book.render(
        "extract_paper_thesis.md",
        engineering_facts_json=wrap_untrusted(
            "engineering_facts_json", pretty_json(facts)
        ),
        paper_chunks_json=paper_context,
    )
    try:
        return pipeline._load_or_create_stage_json(
            output_path=output_dir / "paper_thesis.json",
            output_dir=output_dir,
            audit_dir=audit_dir,
            prompt=prompt,
            stage_label="02d_extract_paper_thesis",
            cleanup_stage="paper_thesis",
            schema_stage="paper_thesis",
            max_attempts=max_attempts,
            resume=resume,
            images=paper_images,
            backend=analysis_backend,
            cache_inputs={
                "paper_source_sha256": paper_source_sha256,
                "facts": facts,
            },
            fallback_factory=None,
        )
    except Exception as exc:
        write_json(
            audit_dir / "paper_thesis_error.json",
            {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
        )
        return None


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
    issues = validate_stage("experiment_index", experiment_index)
    if issues:
        raise RuntimeError(
            f"{stage_label} failed local validation: {format_issues(issues)}"
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


def load_or_create_scientific_architecture(
    pipeline: Any,
    *,
    output_dir: Path,
    audit_dir: Path,
    facts: dict[str, Any],
    tasks: dict[str, Any],
    experiment_index: dict[str, Any],
    paper_thesis: dict[str, Any] | None,
    paper_context: str,
    paper_images: list[Any],
    resume: bool,
    max_attempts: int,
    analysis_backend: str,
    execution_plan: dict[str, Any] | None = None,
    paper_source_sha256: str | None = None,
) -> dict[str, Any]:
    from .preflight import (
        architecture_capability_inventory,
        architecture_execution_capability_gaps,
    )
    from .scientific_architecture import partition_scientific_architecture_issues
    from .scientific_architecture_normalize import (
        finalize_scientific_architecture,
        scientific_architecture_normalization_errors,
        scientific_architecture_normalization_warnings,
        validate_scientific_architecture_repair_preservation,
    )

    execution_plan = (
        execution_plan
        if isinstance(execution_plan, dict)
        else compile_execution_plan(tasks)
    )
    shared_science_is_material = _execution_plan_requires_shared_science(
        execution_plan
    )

    def _candidate_architecture_issues(
        parsed: dict[str, Any],
    ) -> list[ValidationIssue]:
        issues = list(scientific_architecture_normalization_errors(parsed))
        execution_blockers, _advisory_warnings = (
            partition_scientific_architecture_issues(
                parsed,
                facts=facts,
                tasks=tasks,
                experiment_index=experiment_index,
                execution_plan=execution_plan,
            )
        )
        if shared_science_is_material:
            issues.extend(execution_blockers)
        return issues

    architecture_path = output_dir / "scientific_architecture.json"
    cached_architecture_bytes: bytes | None = None
    if resume and architecture_path.is_file():
        try:
            cached_architecture_bytes = architecture_path.read_bytes()
        except OSError:
            cached_architecture_bytes = None

    host_capabilities = architecture_capability_inventory()
    prompt = pipeline.prompt_book.render(
        "design_scientific_architecture.md",
        engineering_facts_json=wrap_untrusted(
            "engineering_facts_json", pretty_json(facts)
        ),
        repro_tasks_json=wrap_untrusted("repro_tasks_json", pretty_json(tasks)),
        paper_thesis_json=wrap_untrusted(
            "paper_thesis_json", pretty_json(paper_thesis or {})
        ),
        experiment_index_json=wrap_untrusted(
            "experiment_index_json", pretty_json(experiment_index)
        ),
        execution_plan_json=wrap_untrusted(
            "execution_plan_json", pretty_json(execution_plan)
        ),
        host_capabilities_json=wrap_untrusted(
            "host_capabilities_json",
            pretty_json(host_capabilities),
        ),
        paper_chunks_json=paper_context,
    )

    architecture = pipeline._load_or_create_analysis_stage_json(
        output_path=architecture_path,
        output_dir=output_dir,
        audit_dir=audit_dir,
        prompt=prompt,
        stage_label="02f_design_scientific_architecture",
        cleanup_stage="scientific_architecture",
        schema_stage="scientific_architecture",
        max_attempts=max_attempts,
        resume=resume,
        candidate_extra_validation=_candidate_architecture_issues,
        candidate_normalizer=finalize_scientific_architecture,
        repair_preservation_validator=(
            validate_scientific_architecture_repair_preservation
        ),
        salvage_failed_candidates=True,
        images=paper_images,
        backend=analysis_backend,
        cache_inputs={
            "paper_source_sha256": paper_source_sha256,
            "facts": facts,
            "tasks": tasks,
            "paper_thesis": paper_thesis or {},
            "experiment_index": experiment_index,
            "execution_plan": execution_plan,
            "host_capabilities": host_capabilities,
        },
        fallback_factory=None,
    )
    reused_cached_architecture = False
    if cached_architecture_bytes is not None and architecture_path.is_file():
        try:
            reused_cached_architecture = (
                architecture_path.read_bytes() == cached_architecture_bytes
            )
        except OSError:
            reused_cached_architecture = False

    write_json(
        audit_dir / "02f_architecture_host_capabilities_current.json",
        host_capabilities,
    )
    generation_inventory_path = (
        audit_dir / "02f_architecture_host_capabilities.json"
    )
    if not reused_cached_architecture:
        write_json(generation_inventory_path, host_capabilities)
    elif not generation_inventory_path.is_file():
        write_json(
            audit_dir
            / "02f_architecture_host_capabilities_generation_unavailable.json",
            {
                "status": "unavailable",
                "reason": (
                    "cached architecture predates generation-time capability inventory"
                ),
            },
        )
    capability_gaps = architecture_execution_capability_gaps(
        architecture,
        host_capabilities,
    )
    write_json(
        audit_dir / "02f_architecture_execution_capability_gaps.json",
        {
            "ok": not capability_gaps,
            "policy": "preserve_architecture_and_report_host_gap",
            "gap_count": len(capability_gaps),
            "gaps": capability_gaps,
        },
    )
    normalization_warnings = scientific_architecture_normalization_warnings(
        architecture
    )
    raw_execution_blockers, cross_document_warnings = (
        partition_scientific_architecture_issues(
            architecture,
            facts=facts,
            tasks=tasks,
            experiment_index=experiment_index,
            execution_plan=execution_plan,
        )
    )
    optional_execution_gaps = (
        [] if shared_science_is_material else raw_execution_blockers
    )
    final_execution_blockers = (
        raw_execution_blockers if shared_science_is_material else []
    )
    combined_warnings = normalization_warnings + cross_document_warnings
    combined_warnings.extend(optional_execution_gaps)
    write_json(
        audit_dir / "02f_scientific_architecture_normalization.json",
        {
            "ok": not final_execution_blockers,
            "policy": "reproduction_first",
            "execution_blocker_count": len(final_execution_blockers),
            "warning_count": len(combined_warnings),
            "warnings": [issue.as_dict() for issue in combined_warnings],
            "groups": {
                "execution_blockers": [
                    issue.as_dict() for issue in final_execution_blockers
                ],
                "structural_normalization": [
                    issue.as_dict() for issue in normalization_warnings
                ],
                "cross_document_diagnostics": [
                    issue.as_dict() for issue in cross_document_warnings
                ],
                "optional_execution_gaps": [
                    issue.as_dict() for issue in optional_execution_gaps
                ],
            },
        },
    )
    if optional_execution_gaps:
        raise RuntimeError(
            "optional scientific architecture has execution gaps; "
            "continue with task-local Writers without Foundation"
        )
    return architecture


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
