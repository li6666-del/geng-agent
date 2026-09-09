from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .analysis_diagnostics import write_analysis_warnings
from .analysis_prompt_context import (
    analysis_paper_context, ledger_for_prompt, resolution_for_prompt,
    scientific_prompt_value, tasks_for_backfill,
)
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
from .pipeline_helpers import wrap_untrusted
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
from .consolidated_analysis import load_paper_understanding, load_experiment_plan


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
    def _evidence_context(*, images: list[Any], requests: list[dict[str, Any]] | None = None) -> str:
        return analysis_paper_context(
            paper=paper, figure_summary=figure_index_prompt_summary(figure_index),
            paper_path=paper_path, chunks_path=output_dir / "paper_chunks.json",
            images=images, backend=options.analysis_backend, requests=requests,
        )

    paper_context = _evidence_context(images=paper_images)
    valid_pages: set[int] = set()
    for image in paper_images:
        label = getattr(image, "label", "") or ""
        if label.startswith("paper_page:") and label.split(":", 1)[1].isdigit():
            valid_pages.add(int(label.split(":", 1)[1]))

    understanding = load_paper_understanding(pipeline, context, paper=paper, paper_context=paper_context,
        paper_images=paper_images, valid_chunk_ids=valid_chunk_ids, valid_pages=valid_pages)
    analysis_stage_invocations = int(not understanding.get("_meta", {}).get("cache_reused"))
    initial_facts = understanding["facts"]
    paper_thesis = understanding.get("paper_thesis")
    write_json(output_dir / "paper_thesis.json", paper_thesis)
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
    context.mark("thesis")

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
    facts_initial_meta["paper_understanding_limitations"] = understanding.get("limitations", [])
    initial_facts["_meta"] = facts_initial_meta
    write_json(output_dir / "engineering_facts_initial.json", initial_facts)
    write_json(
        audit_dir / "01_fact_coverage_after_global_extraction.json",
        fact_coverage,
    )

    context.begin("tasks_preliminary")
    from .preflight import architecture_capability_inventory
    host_capabilities = architecture_capability_inventory()
    current_plan = load_experiment_plan(pipeline, context, facts=initial_facts, paper_thesis=paper_thesis,
        paper=paper, paper_context=paper_context, paper_images=paper_images, figure_index=figure_index,
        host_capabilities=host_capabilities)
    analysis_stage_invocations += int(not current_plan.get("_meta", {}).get("cache_reused"))
    preliminary_tasks = current_plan["tasks"]
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
        nonlocal analysis_stage_invocations
        label = f"02b_round_{round_index:02d}_targeted_fact_backfill"
        prompt = pipeline.prompt_book.render(
            "targeted_fact_backfill.md",
            round_index=str(round_index),
            targeted_requests_json=wrap_untrusted(
                "targeted_requests_json", pretty_json(requests)
            ),
            existing_facts_json=wrap_untrusted(
                "existing_facts_json", pretty_json(scientific_prompt_value(current_facts))
            ),
            current_tasks_json=wrap_untrusted(
                "current_tasks_json", pretty_json(tasks_for_backfill(current_tasks, requests))
            ),
            search_ledger_json=wrap_untrusted(
                "search_ledger_json", pretty_json(ledger_for_prompt(search_ledger))
            ),
            paper_context_json=_evidence_context(images=paper_images, requests=requests),
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
        analysis_stage_invocations += int(not backfill.get("_meta", {}).get("cache_reused"))
        return backfill

    def _refresh_tasks_after_round(
        round_index: int, current_tasks: dict[str, Any], current_facts: dict[str, Any],
        cumulative_resolution: dict[str, Any], search_ledger: dict[str, Any],
    ) -> dict[str, Any]:
        nonlocal current_plan, analysis_stage_invocations
        candidate = load_experiment_plan(pipeline, context, facts=current_facts, paper_thesis=paper_thesis,
            paper=paper, paper_context=paper_context, paper_images=paper_images, figure_index=figure_index,
            host_capabilities=host_capabilities, previous_plan={**current_plan, "tasks": current_tasks},
            resolution=cumulative_resolution, ledger=search_ledger, round_index=round_index)
        analysis_stage_invocations += int(not candidate.get("_meta", {}).get("cache_reused"))
        current_plan = candidate
        return candidate["tasks"]

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

    # Claims were supplied before planning. Re-publish canonical artifacts after
    # downstream invalidation, without another thesis or acceptance model call.
    write_json(output_dir / "paper_thesis.json", paper_thesis)
    tasks.setdefault("_meta", {})["scientific_acceptance_finalization"] = {
        "paper_thesis_used": bool(paper_thesis), "policy_id": SCIENTIFIC_POLICY_ID,
        "decision_owner": "experiment_planner", "structure_is_advisory": True}
    write_json(output_dir / "repro_tasks.json", tasks)

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
    scientific_architecture = current_plan.get("scientific_architecture")
    from .preflight import architecture_execution_capability_gaps
    from .scientific_architecture import partition_scientific_architecture_issues
    if scientific_architecture is None:
        if _execution_plan_requires_shared_science(execution_plan):
            raise RuntimeError("Experiment plan lacks the shared scientific architecture required by its task dependencies")
        (output_dir / "scientific_architecture.json").unlink(missing_ok=True)
    else:
        blockers, warnings = partition_scientific_architecture_issues(scientific_architecture,
            facts=facts, tasks=tasks, experiment_index=experiment_index, execution_plan=execution_plan)
        if blockers and _execution_plan_requires_shared_science(execution_plan):
            raise RuntimeError("Tasks and architecture are not a coherent executable plan: " + format_issues(blockers))
        write_json(output_dir / "scientific_architecture.json", scientific_architecture)
        write_analysis_warnings(output_dir=output_dir, audit_dir=audit_dir,
            stage="02a_plan_experiments", groups={"architecture": [*warnings, *blockers]})
        gaps = architecture_execution_capability_gaps(scientific_architecture, host_capabilities)
        write_json(audit_dir / "02f_architecture_execution_capability_gaps.json",
            {"ok": not gaps, "policy": "preserve_architecture_and_report_host_gap", "gap_count": len(gaps), "gaps": gaps})
    write_json(audit_dir / "02f_architecture_host_capabilities_current.json", host_capabilities)
    write_json(audit_dir / "02f_architecture_host_capabilities.json", host_capabilities)
    write_json(output_dir / "experiment_plan.json", {"tasks": tasks, "scientific_architecture": scientific_architecture,
        "_meta": {"planner_cache": current_plan.get("_meta", {}).get("cache"),
                  "planning_complete": bool(backfill_loop.get("final_handoff", {}).get("ready_for_writer", True)),
                  "backfill_stop_reason": backfill_loop["stop_reason"]}})
    context.mark("scientific_architecture")
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
            "analysis_agent_count": 2,
            "analysis_stage_invocations": analysis.analysis_stage_invocations,
            "analysis_warning_count": int(
                analysis.analysis_warnings.get("warning_count") or 0
            ),
            "json_format_repair_limit": int(options.json_repair_attempts),
            "facts_stop_rule": "single_global_then_selected_blockers_max_3",
            "tasks_stop_rule": "joint_experiment_plan_with_targeted_evidence",
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
