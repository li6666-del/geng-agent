from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from .analysis_diagnostics import write_analysis_warnings
from .execution_plan import ExecutionPlanError, compile_execution_plan
from .facts_coverage import compute_fact_coverage, compute_task_coverage
from .facts_normalize import (
    engineering_facts_floor_issues,
    finalize_engineering_facts,
    recover_truncated_engineering_facts,
)
from .heuristic_fallbacks import (
    build_fallback_engineering_facts,
    build_fallback_repro_tasks,
)
from .json_utils import pretty_json
from .mineru_adapter import figure_index_prompt_summary
from .outputs import write_json
from .pipeline_context import PipelineRunContext
from .pipeline_helpers import _paper_context_for_prompt, wrap_untrusted
from .pipeline_models import AnalysisFlowResult, PipelineResult
from .risk_report import _build_run_cost
from .schemas import (
    format_issues,
    validate_fact_sources,
    validate_stage,
    validate_task_fact_refs,
)
from .scientific_materiality import SCIENTIFIC_POLICY_ID
from .semantic_merge import semantic_conflicts, semantic_merge_repro_tasks
from .task_evidence_backfill import (
    backfill_normalization_issues,
    finalize_targeted_backfill,
    validate_targeted_backfill,
    validate_terminal_gap_assumptions,
)
from .tasks_normalize import finalize_repro_tasks, recover_truncated_repro_tasks
from .workflow_policy import _execution_plan_requires_shared_science


TARGETED_BACKFILL_MAX_ROUNDS = 3


def run_analysis_flow(
    pipeline: Any,
    context: PipelineRunContext,
    *,
    mineru_stage: Callable[..., dict[str, Any]],
    backfill_loop_runner: Callable[..., dict[str, Any]],
) -> AnalysisFlowResult:
    output_dir = context.output_dir
    audit_dir = context.audit_dir
    options = context.options
    paper_path = context.paper_path.expanduser().resolve()

    context.mark("start")
    paper = pipeline._load_or_create_paper(
        paper_path=paper_path,
        output_dir=output_dir,
        max_pages=options.max_pages,
        resume=options.resume,
    )
    valid_chunk_ids = {
        str(chunk.get("chunk_id"))
        for chunk in paper.get("chunks", [])
        if isinstance(chunk, dict) and chunk.get("chunk_id")
    }
    paper_images = pipeline._render_paper_images(
        paper_path=paper_path,
        paper=paper,
    )
    mineru_result = mineru_stage(
        paper_path=paper_path,
        output_dir=output_dir,
        audit_dir=audit_dir,
        resume=options.resume,
        timeout=options.mineru_timeout,
        max_pages=options.max_pages,
    )
    figure_index = (
        mineru_result.get("figure_index")
        if isinstance(mineru_result.get("figure_index"), dict)
        else {"figures": [], "unmatched_visuals": []}
    )
    context.mark("mineru_layout")
    paper_context_raw = pretty_json(
        {
            "paper_source_sha256": paper.get("source_sha256"),
            "paper_chunks": json.loads(_paper_context_for_prompt(paper["chunks"])),
            "paper_figure_index": figure_index_prompt_summary(figure_index),
        }
    )
    paper_context = wrap_untrusted("paper_chunks_json", paper_context_raw)
    valid_pages: set[int] = set()
    for image in paper_images:
        label = getattr(image, "label", "") or ""
        if label.startswith("paper_page:") and label.split(":", 1)[1].isdigit():
            valid_pages.add(int(label.split(":", 1)[1]))

    prompt_1 = pipeline.prompt_book.render(
        "extract_engineering_facts.md",
        paper_chunks_json=paper_context,
    )
    initial_facts = pipeline._load_or_create_analysis_stage_json(
        output_path=output_dir / "engineering_facts_initial.json",
        output_dir=output_dir,
        audit_dir=audit_dir,
        prompt=prompt_1,
        stage_label="01_extract_engineering_facts",
        cleanup_stage="facts",
        schema_stage="engineering_facts",
        max_attempts=options.json_repair_attempts + 1,
        resume=options.resume,
        images=paper_images,
        candidate_normalizer=lambda parsed: finalize_engineering_facts(
            parsed, valid_chunk_ids, valid_pages
        ),
        truncation_recovery=recover_truncated_engineering_facts,
        backend=options.analysis_backend,
        cache_inputs={
            "paper_source_sha256": paper.get("source_sha256"),
            "figure_index": figure_index_prompt_summary(figure_index),
            "visible_figure_pages": sorted(valid_pages),
        },
        fallback_factory=(
            (
                lambda exc: build_fallback_engineering_facts(
                    paper=paper,
                    reason=(
                        f"{options.analysis_backend} engineering fact extraction "
                        f"failed after format repair: {exc}"
                    ),
                )
            )
            if options.analysis_fallback
            else None
        ),
    )
    initial_facts = finalize_engineering_facts(
        initial_facts, valid_chunk_ids, valid_pages
    )
    initial_structure_issues = validate_stage("engineering_facts", initial_facts)
    if initial_structure_issues:
        raise RuntimeError(
            "Internal initial fact normalization produced an unreadable document: "
            + format_issues(initial_structure_issues)
        )
    write_analysis_warnings(
        output_dir=output_dir,
        audit_dir=audit_dir,
        stage="01_extract_engineering_facts",
        groups={
            "fact_source": validate_fact_sources(
                initial_facts, valid_chunk_ids, valid_pages
            ),
            "fact_coverage_floor": engineering_facts_floor_issues(initial_facts),
        },
    )
    write_json(output_dir / "engineering_facts_initial.json", initial_facts)
    context.mark("facts_initial")

    fact_coverage = compute_fact_coverage(
        paper.get("chunks", []) if isinstance(paper, dict) else [],
        initial_facts.get("engineering_facts", []),
    )
    declared_missing_count = len(initial_facts.get("missing_information", []))
    fact_coverage["declared_missing_count"] = declared_missing_count
    fact_coverage["declared_complete_conflicts_with_coverage"] = (
        declared_missing_count == 0
        and (
            not fact_coverage.get("fully_covered")
            or not fact_coverage.get("fully_detailed")
        )
    )
    facts_initial_meta = (
        dict(initial_facts.get("_meta", {}))
        if isinstance(initial_facts.get("_meta"), dict)
        else {}
    )
    facts_initial_meta["deterministic_coverage"] = {
        "fully_covered": bool(fact_coverage.get("fully_covered")),
        "fully_detailed": bool(fact_coverage.get("fully_detailed")),
        "declared_complete_conflicts_with_coverage": bool(
            fact_coverage["declared_complete_conflicts_with_coverage"]
        ),
    }
    initial_facts["_meta"] = facts_initial_meta
    write_json(output_dir / "engineering_facts_initial.json", initial_facts)
    write_json(
        audit_dir / "01_fact_coverage_after_global_extraction.json",
        fact_coverage,
    )

    context.begin("tasks_preliminary")
    prompt_2 = pipeline.prompt_book.render(
        "build_repro_tasks.md",
        engineering_facts_json=wrap_untrusted(
            "engineering_facts_json", pretty_json(initial_facts)
        ),
        fact_coverage_json=wrap_untrusted(
            "fact_coverage_json", pretty_json(fact_coverage)
        ),
        paper_context_json=paper_context,
    )
    preliminary_tasks = pipeline._load_or_create_analysis_stage_json(
        output_path=output_dir / "repro_tasks_preliminary.json",
        output_dir=output_dir,
        audit_dir=audit_dir,
        prompt=prompt_2,
        stage_label="02a_build_preliminary_repro_tasks",
        cleanup_stage="tasks",
        schema_stage="repro_tasks",
        max_attempts=options.json_repair_attempts + 1,
        resume=options.resume,
        candidate_normalizer=lambda parsed: finalize_repro_tasks(
            parsed, initial_facts
        ),
        truncation_recovery=recover_truncated_repro_tasks,
        request_timeout=options.tasks_timeout,
        backend=options.analysis_backend,
        cache_inputs={
            "paper_source_sha256": paper.get("source_sha256"),
            "facts": initial_facts,
            "fact_coverage": fact_coverage,
        },
        fallback_factory=(
            (
                lambda exc: build_fallback_repro_tasks(
                    facts=initial_facts,
                    paper=paper,
                    reason=(
                        f"{options.analysis_backend} reproduction task generation "
                        f"failed after format repair: {exc}"
                    ),
                )
            )
            if options.analysis_fallback
            else None
        ),
    )
    preliminary_meta = (
        preliminary_tasks.get("_meta", {})
        if isinstance(preliminary_tasks.get("_meta"), dict)
        else {}
    )
    preliminary_cache = preliminary_meta.get("cache")
    preliminary_merge_base: dict[str, Any] = {"repro_tasks": []}
    if isinstance(preliminary_cache, dict):
        preliminary_merge_base["_meta"] = {"cache": dict(preliminary_cache)}
    preliminary_tasks, _ = semantic_merge_repro_tasks(
        preliminary_merge_base, preliminary_tasks
    )
    preliminary_tasks = finalize_repro_tasks(preliminary_tasks, initial_facts)
    preliminary_structure_issues = validate_stage("repro_tasks", preliminary_tasks)
    if preliminary_structure_issues:
        preliminary_tasks = finalize_repro_tasks(
            preliminary_tasks, initial_facts
        )
        remaining_preliminary_issues = validate_stage(
            "repro_tasks", preliminary_tasks
        )
        write_json(
            audit_dir / "02a_preliminary_task_structure_warning.json",
            {
                "advisory": True,
                "recovered_with_minimum_handoff": not remaining_preliminary_issues,
                "warnings": [
                    {"path": issue.path, "message": issue.message}
                    for issue in preliminary_structure_issues
                ],
                "remaining_warnings": [
                    {"path": issue.path, "message": issue.message}
                    for issue in remaining_preliminary_issues
                ],
            },
        )
    write_analysis_warnings(
        output_dir=output_dir,
        audit_dir=audit_dir,
        stage="02a_build_preliminary_repro_tasks",
        groups={
            "task_fact_reference": validate_task_fact_refs(
                preliminary_tasks, initial_facts
            )
        },
    )
    write_json(output_dir / "repro_tasks_preliminary.json", preliminary_tasks)
    preliminary_runtime_meta = (
        dict(preliminary_tasks.get("_meta", {}))
        if isinstance(preliminary_tasks.get("_meta"), dict)
        else {}
    )
    preliminary_runtime_meta.pop("cache", None)
    if preliminary_runtime_meta:
        preliminary_tasks["_meta"] = preliminary_runtime_meta
    else:
        preliminary_tasks.pop("_meta", None)
    context.mark("tasks_preliminary")

    def _run_backfill_round(
        round_index: int,
        requests: list[dict[str, Any]],
        current_facts: dict[str, Any],
        current_tasks: dict[str, Any],
        search_ledger: dict[str, Any],
    ) -> dict[str, Any]:
        label = f"02b_round_{round_index:02d}_targeted_fact_backfill"
        prompt = pipeline.prompt_book.render(
            "targeted_fact_backfill.md",
            round_index=str(round_index),
            targeted_requests_json=wrap_untrusted(
                "targeted_requests_json", pretty_json(requests)
            ),
            existing_facts_json=wrap_untrusted(
                "existing_facts_json", pretty_json(current_facts)
            ),
            current_tasks_json=wrap_untrusted(
                "current_tasks_json", pretty_json(current_tasks)
            ),
            search_ledger_json=wrap_untrusted(
                "search_ledger_json", pretty_json(search_ledger)
            ),
            paper_context_json=paper_context,
        )

        def _normalize_backfill(parsed: dict[str, Any]) -> dict[str, Any]:
            return finalize_targeted_backfill(
                parsed,
                requests,
                current_facts,
                valid_chunk_ids,
                valid_pages,
            )

        backfill = pipeline._load_or_create_analysis_stage_json(
            output_path=(
                audit_dir / f"02b_backfill_round_{round_index:02d}_result.json"
            ),
            output_dir=output_dir,
            audit_dir=audit_dir,
            prompt=prompt,
            stage_label=label,
            cleanup_stage="facts_backfill",
            schema_stage="targeted_fact_backfill",
            max_attempts=options.json_repair_attempts + 1,
            resume=options.resume,
            images=paper_images,
            candidate_normalizer=_normalize_backfill,
            backend=options.analysis_backend,
            cache_inputs={
                "paper_source_sha256": paper.get("source_sha256"),
                "round_index": round_index,
                "requests": requests,
                "facts": current_facts,
                "tasks": current_tasks,
                "search_ledger": search_ledger,
            },
        )
        write_analysis_warnings(
            output_dir=output_dir,
            audit_dir=audit_dir,
            stage=label,
            groups={
                "normalization": backfill_normalization_issues(backfill),
                "fact_source": validate_fact_sources(
                    backfill, valid_chunk_ids, valid_pages
                ),
                "evidence_contract": validate_targeted_backfill(
                    backfill, requests, current_facts
                ),
            },
        )
        return backfill

    def _refresh_tasks_after_round(
        round_index: int,
        current_tasks: dict[str, Any],
        current_facts: dict[str, Any],
        cumulative_resolution: dict[str, Any],
        search_ledger: dict[str, Any],
    ) -> dict[str, Any]:
        label = f"02c_round_{round_index:02d}_refresh_repro_tasks"
        prompt = pipeline.prompt_book.render(
            "finalize_repro_tasks.md",
            round_index=str(round_index),
            current_tasks_json=wrap_untrusted(
                "current_tasks_json", pretty_json(current_tasks)
            ),
            final_engineering_facts_json=wrap_untrusted(
                "final_engineering_facts_json", pretty_json(current_facts)
            ),
            backfill_resolution_json=wrap_untrusted(
                "backfill_resolution_json", pretty_json(cumulative_resolution)
            ),
            search_ledger_json=wrap_untrusted(
                "search_ledger_json", pretty_json(search_ledger)
            ),
            paper_thesis_json=wrap_untrusted("paper_thesis_json", "{}"),
            paper_context_json=paper_context,
        )
        refreshed = pipeline._load_or_create_analysis_stage_json(
            output_path=(
                audit_dir
                / f"02b_backfill_round_{round_index:02d}_task_refresh.json"
            ),
            output_dir=output_dir,
            audit_dir=audit_dir,
            prompt=prompt,
            stage_label=label,
            cleanup_stage="tasks_finalize",
            schema_stage="repro_tasks",
            max_attempts=options.json_repair_attempts + 1,
            resume=options.resume,
            images=paper_images,
            candidate_normalizer=lambda parsed: finalize_repro_tasks(
                parsed, current_facts
            ),
            truncation_recovery=recover_truncated_repro_tasks,
            request_timeout=options.tasks_timeout,
            backend=options.analysis_backend,
            cache_inputs={
                "paper_source_sha256": paper.get("source_sha256"),
                "round_index": round_index,
                "tasks": current_tasks,
                "facts": current_facts,
                "backfill_resolution": cumulative_resolution,
                "search_ledger": search_ledger,
            },
        )
        write_analysis_warnings(
            output_dir=output_dir,
            audit_dir=audit_dir,
            stage=label,
            groups={
                "task_fact_reference": validate_task_fact_refs(
                    refreshed, current_facts
                )
            },
        )
        return refreshed

    def _write_round_audit(round_index: int, summary: dict[str, Any]) -> None:
        write_json(
            audit_dir / f"02b_backfill_round_{round_index:02d}_delta.json",
            summary,
        )

    backfill_loop = backfill_loop_runner(
        initial_facts=initial_facts,
        preliminary_tasks=preliminary_tasks,
        run_backfill=_run_backfill_round,
        refresh_tasks=_refresh_tasks_after_round,
        normalize_tasks=finalize_repro_tasks,
        max_rounds=TARGETED_BACKFILL_MAX_ROUNDS,
        on_round=_write_round_audit,
    )
    facts = backfill_loop["facts"]
    tasks = backfill_loop["tasks"]
    resolution = backfill_loop["resolution"]
    resolution["round_count"] = backfill_loop["round_count"]
    resolution["max_rounds"] = backfill_loop["max_rounds"]
    resolution["stop_reason"] = backfill_loop["stop_reason"]
    backfill_round_count = int(backfill_loop["round_count"])

    facts_meta = (
        dict(facts.get("_meta", {}))
        if isinstance(facts.get("_meta"), dict)
        else {}
    )
    facts_meta["task_driven_backfill"] = {
        "request_count": resolution["request_count"],
        "resolved_count": resolution["resolved_count"],
        "terminal_unresolved_count": resolution["terminal_unresolved_count"],
        "open_count": resolution["open_count"],
        "round_count": backfill_round_count,
        "max_rounds": TARGETED_BACKFILL_MAX_ROUNDS,
        "stop_reason": backfill_loop["stop_reason"],
    }
    facts["_meta"] = facts_meta
    write_json(
        output_dir / "engineering_facts_backfill.json",
        backfill_loop["cumulative_backfill"],
    )
    write_json(output_dir / "engineering_facts.json", facts)
    write_json(
        audit_dir / "02b_backfill_search_ledger.json", backfill_loop["ledger"]
    )
    write_json(
        audit_dir / "02b_targeted_fact_backfill_summary.json", resolution
    )
    write_json(
        audit_dir / "02b_targeted_fact_requests.json",
        {
            "request_count": len(backfill_loop["known_requests"]),
            "requests": backfill_loop["known_requests"],
            "round_count": backfill_round_count,
        },
    )
    final_fact_coverage = compute_fact_coverage(
        paper.get("chunks", []) if isinstance(paper, dict) else [],
        facts.get("engineering_facts", []),
    )
    write_json(audit_dir / "02b_final_fact_coverage.json", final_fact_coverage)
    write_json(
        output_dir / "fact_conflicts.json",
        {"conflicts": semantic_conflicts(facts, "fact")},
    )
    context.mark("facts")

    task_structure_issues = validate_stage("repro_tasks", tasks)
    if task_structure_issues:
        tasks = finalize_repro_tasks(tasks, facts)
        remaining_task_issues = validate_stage("repro_tasks", tasks)
        write_json(
            audit_dir / "02c_final_task_structure_warning.json",
            {
                "advisory": True,
                "recovered_with_minimum_handoff": not remaining_task_issues,
                "warnings": [
                    {"path": issue.path, "message": issue.message}
                    for issue in task_structure_issues
                ],
                "remaining_warnings": [
                    {"path": issue.path, "message": issue.message}
                    for issue in remaining_task_issues
                ],
            },
        )
    terminal_gap_issues = validate_terminal_gap_assumptions(tasks, resolution)
    write_json(
        audit_dir / "02c_terminal_gap_diagnostics.json",
        {
            "advisory": True,
            "passed": not terminal_gap_issues,
            "issue_count": len(terminal_gap_issues),
            "issues": [issue.as_dict() for issue in terminal_gap_issues],
        },
    )
    analysis_warnings = write_analysis_warnings(
        output_dir=output_dir,
        audit_dir=audit_dir,
        stage="02c_final_repro_tasks",
        groups={
            "task_fact_reference": validate_task_fact_refs(tasks, facts),
            "terminal_gap": terminal_gap_issues,
        },
    )
    final_task_coverage = compute_task_coverage(facts, tasks)
    final_task_coverage["stop_reason"] = backfill_loop["stop_reason"]
    tasks_meta = (
        dict(tasks.get("_meta", {}))
        if isinstance(tasks.get("_meta"), dict)
        else {}
    )
    tasks_meta["task_driven_finalization"] = {
        "used_targeted_backfill": bool(backfill_round_count),
        "targeted_request_count": resolution["request_count"],
        "unresolved_request_count": resolution["unresolved_count"],
        "round_count": backfill_round_count,
        "stop_reason": backfill_loop["stop_reason"],
        "handoff_ready": bool(
            backfill_loop.get("final_handoff", {}).get("ready_for_writer", True)
        ),
    }
    tasks_meta["fact_gap_handoff"] = {
        "stop_reason": backfill_loop["stop_reason"],
        "round_count": backfill_round_count,
        "task_expert_handoff": backfill_loop.get("final_handoff", {}),
        "terminal_unresolved": resolution.get("terminal_unresolved", []),
        "open": resolution.get("open", []),
        "assumption_diagnostics": [
            issue.as_dict() for issue in terminal_gap_issues
        ],
        "analysis_warning_count": int(
            analysis_warnings.get("warning_count") or 0
        ),
        "analysis_warnings_artifact": "analysis_warnings.json",
    }
    tasks["_meta"] = tasks_meta
    write_json(output_dir / "repro_tasks.json", tasks)
    write_json(audit_dir / "02c_final_task_coverage.json", final_task_coverage)
    write_json(
        output_dir / "task_conflicts.json",
        {"conflicts": semantic_conflicts(tasks, "task")},
    )
    context.mark("tasks")

    paper_thesis = pipeline._load_or_create_paper_thesis(
        output_dir=output_dir,
        audit_dir=audit_dir,
        facts=facts,
        paper_context=paper_context,
        paper_images=paper_images,
        resume=options.resume,
        paper_source_sha256=paper.get("source_sha256"),
        max_attempts=options.json_repair_attempts + 1,
        analysis_backend=options.analysis_backend,
    )
    context.mark("thesis")

    final_task_prompt = pipeline.prompt_book.render(
        "finalize_repro_tasks.md",
        round_index="final",
        current_tasks_json=wrap_untrusted(
            "current_tasks_json", pretty_json(tasks)
        ),
        final_engineering_facts_json=wrap_untrusted(
            "final_engineering_facts_json", pretty_json(facts)
        ),
        backfill_resolution_json=wrap_untrusted(
            "backfill_resolution_json", pretty_json(resolution)
        ),
        search_ledger_json=wrap_untrusted(
            "search_ledger_json", pretty_json(backfill_loop["ledger"])
        ),
        paper_thesis_json=wrap_untrusted(
            "paper_thesis_json", pretty_json(paper_thesis or {})
        ),
        paper_context_json=paper_context,
    )
    previous_tasks = tasks
    write_json(audit_dir / "02d_tasks_before_final_snapshot.json", previous_tasks)
    try:
        final_task_candidate = pipeline._load_or_create_analysis_stage_json(
            output_path=audit_dir / "02d_final_scientific_acceptance.json",
            output_dir=output_dir,
            audit_dir=audit_dir,
            prompt=final_task_prompt,
            stage_label="02d_finalize_scientific_acceptance",
            cleanup_stage="tasks_finalize",
            schema_stage="repro_tasks",
            max_attempts=options.json_repair_attempts + 1,
            resume=options.resume,
            candidate_normalizer=lambda parsed: finalize_repro_tasks(
                parsed, facts
            ),
            truncation_recovery=recover_truncated_repro_tasks,
            request_timeout=options.tasks_timeout,
            images=paper_images,
            backend=options.analysis_backend,
            cache_inputs={
                "paper_source_sha256": paper.get("source_sha256"),
                "tasks": previous_tasks,
                "facts": facts,
                "paper_thesis": paper_thesis or {},
                "backfill_resolution": resolution,
                "search_ledger": backfill_loop["ledger"],
            },
        )
        tasks, _ = semantic_merge_repro_tasks(
            previous_tasks,
            final_task_candidate,
            merge_mode="snapshot",
        )
        write_json(
            audit_dir / "02d_final_task_snapshot_changes.json",
            tasks.get("_meta", {}).get("semantic_merge", {}),
        )
        tasks = finalize_repro_tasks(tasks, facts)
    except Exception as exc:
        tasks = finalize_repro_tasks(previous_tasks, facts)
        write_json(
            audit_dir / "02d_final_scientific_acceptance_warning.json",
            {
                "advisory": True,
                "fallback": "normalized_pre_thesis_tasks",
                "error": f"{type(exc).__name__}: {exc}",
            },
        )
    tasks_meta = (
        dict(tasks.get("_meta", {}))
        if isinstance(tasks.get("_meta"), dict)
        else {}
    )
    tasks_meta["scientific_acceptance_finalization"] = {
        "paper_thesis_used": bool(paper_thesis),
        "policy_id": SCIENTIFIC_POLICY_ID,
        "structure_is_advisory": True,
    }
    tasks["_meta"] = tasks_meta
    write_json(output_dir / "repro_tasks.json", tasks)
    write_json(
        output_dir / "task_conflicts.json",
        {"conflicts": semantic_conflicts(tasks, "task")},
    )
    write_json(
        audit_dir / "02d_final_task_coverage.json",
        compute_task_coverage(facts, tasks),
    )

    try:
        execution_plan = compile_execution_plan(tasks)
    except ExecutionPlanError as exc:
        write_json(
            audit_dir / "02e_execution_plan_error.json",
            {
                "decision": "stop",
                "pipeline_can_continue": False,
                "error_code": exc.code,
                "path": exc.path,
                "error": str(exc),
            },
        )
        raise RuntimeError(
            f"material task execution relationship is not executable: {exc}"
        ) from exc
    write_json(output_dir / "execution_plan.json", execution_plan)
    write_json(
        audit_dir / "02e_execution_plan.json",
        {
            "ok": True,
            "logical_task_count": execution_plan["logical_task_count"],
            "execution_unit_count": execution_plan["execution_unit_count"],
            "compound_unit_count": sum(
                1
                for unit in execution_plan["execution_units"]
                if unit.get("mode") == "compound"
            ),
            "weak_consistency_group_count": len(
                execution_plan["weak_consistency_groups"]
            ),
        },
    )

    experiment_index = pipeline._load_or_create_experiment_index(
        output_dir=output_dir,
        audit_dir=audit_dir,
        facts=facts,
        tasks=tasks,
        paper=paper,
        figure_index=figure_index,
        resume=options.resume,
    )
    context.mark("experiment_index")
    scientific_architecture: dict[str, Any] | None = None
    try:
        scientific_architecture = pipeline._load_or_create_scientific_architecture(
            output_dir=output_dir,
            audit_dir=audit_dir,
            facts=facts,
            tasks=tasks,
            experiment_index=experiment_index,
            execution_plan=execution_plan,
            paper_thesis=paper_thesis,
            paper_context=paper_context,
            paper_images=paper_images,
            resume=options.resume,
            paper_source_sha256=paper.get("source_sha256"),
            max_attempts=options.json_repair_attempts + 1,
            analysis_backend=options.analysis_backend,
        )
    except Exception as exc:
        if _execution_plan_requires_shared_science(execution_plan):
            write_json(
                audit_dir / "02f_scientific_architecture_fallback.json",
                {
                    "policy": "preserve_material_task_relationships",
                    "decision": "stop",
                    "pipeline_can_continue": False,
                    "fallback": None,
                    "warning": f"{type(exc).__name__}: {exc}",
                },
            )
            raise RuntimeError(
                "scientific architecture failed for tasks that require shared "
                "execution or a frozen cross-task definition"
            ) from exc
        scientific_architecture = None
        write_json(
            audit_dir / "02f_scientific_architecture_fallback.json",
            {
                "policy": "reproduction_first",
                "decision": "fallback",
                "pipeline_can_continue": True,
                "fallback": "task_local_writers_without_foundation",
                "warning": f"{type(exc).__name__}: {exc}",
            },
        )
    else:
        from .scientific_architecture_normalize import (
            scientific_architecture_normalization_warnings,
        )

        analysis_warnings = write_analysis_warnings(
            output_dir=output_dir,
            audit_dir=audit_dir,
            stage="02f_design_scientific_architecture",
            groups={
                "structural_normalization": (
                    scientific_architecture_normalization_warnings(
                        scientific_architecture
                    )
                )
            },
        )
    context.mark("scientific_architecture")
    analysis_stage_invocations = (
        4
        + (2 * backfill_round_count)
        + (1 if scientific_architecture is not None else 0)
    )
    return AnalysisFlowResult(
        paper_path=paper_path,
        paper=paper,
        paper_images=paper_images,
        mineru_result=mineru_result,
        figure_index=figure_index,
        paper_context=paper_context,
        facts=facts,
        tasks=tasks,
        paper_thesis=paper_thesis,
        execution_plan=execution_plan,
        experiment_index=experiment_index,
        scientific_architecture=scientific_architecture,
        analysis_warnings=analysis_warnings,
        analysis_stage_invocations=analysis_stage_invocations,
        repro_project_dir=output_dir / "repro_project",
    )


def finish_analysis_only(
    context: PipelineRunContext,
    analysis: AnalysisFlowResult,
) -> PipelineResult:
    output_dir = context.output_dir
    options = context.options
    run_cost = _build_run_cost(
        context.cost_marks,
        total_wall_s=context.elapsed_s(),
        by_model=context.usage_by_model(),
        audit_dir=context.audit_dir,
        codex_since=context.wall_start,
    )
    run_cost.update(
        {
            "analysis_backend": options.analysis_backend,
            "analysis_only": True,
            "analysis_agent_count": 1,
            "analysis_stage_invocations": analysis.analysis_stage_invocations,
            "analysis_warning_count": int(
                analysis.analysis_warnings.get("warning_count") or 0
            ),
            "json_format_repair_limit": int(options.json_repair_attempts),
            "facts_stop_rule": "single_global_then_selected_blockers_max_3",
            "tasks_stop_rule": "thesis_informed_core_conclusion_contract",
            "mineru_layout": {
                "ok": analysis.mineru_result.get("ok"),
                "cached": analysis.mineru_result.get("cached"),
                "fallback_used": analysis.mineru_result.get("fallback_used"),
                "duration_s": analysis.mineru_result.get("duration_s"),
                "figure_count": analysis.mineru_result.get("figure_count", 0),
            },
        }
    )
    from .codex_cost import persist_pipeline_cost
    persist_pipeline_cost(output_dir, run_cost, run_id=context.run_id, started_at=context.wall_start)
    write_json(
        output_dir / "analysis_result.json",
        {
            "completed": True,
            "analysis_only": True,
            "facts_count": len(analysis.facts.get("engineering_facts", [])),
            "tasks_count": len(analysis.tasks.get("repro_tasks", [])),
            "experiments_count": len(
                analysis.experiment_index.get("experiments", [])
            ),
            "execution_units_count": analysis.execution_plan.get(
                "execution_unit_count", 0
            ),
            "architecture_components_count": len(
                (analysis.scientific_architecture or {}).get("components", [])
            ),
            "architecture_bindings_count": len(
                (analysis.scientific_architecture or {}).get("bindings", [])
            ),
            "analysis_stage_invocations": analysis.analysis_stage_invocations,
            "task_driven_backfill": analysis.facts.get("_meta", {}).get(
                "task_driven_backfill", {}
            ),
            "task_finalization": analysis.tasks.get("_meta", {}).get(
                "task_driven_finalization", {}
            ),
            "mineru_layout": {
                "ok": analysis.mineru_result.get("ok"),
                "fallback_used": analysis.mineru_result.get("fallback_used"),
                "figure_count": analysis.mineru_result.get("figure_count", 0),
            },
        },
    )
    context.finish()
    return PipelineResult(
        output_dir=output_dir,
        review_path=output_dir / "review.md",
        repro_project_dir=analysis.repro_project_dir,
        risk_report_path=output_dir / "risk_report.json",
        runtime_passed=None,
        experiment_index_path=output_dir / "experiment_index.json",
        scientific_architecture_path=(
            output_dir / "scientific_architecture.json"
            if analysis.scientific_architecture is not None
            else None
        ),
    )
