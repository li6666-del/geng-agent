from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from .analysis_diagnostics import write_analysis_warnings
from .architecture_protocol import architecture_runtime_view
from .analysis_prompt_context import (
    analysis_paper_context, ledger_for_prompt, resolution_for_prompt,
    scientific_prompt_value, tasks_for_backfill,
)
from .execution_plan import compile_execution_plan
from .json_utils import pretty_json
from .mineru_adapter import figure_index_prompt_summary
from .outputs import write_json
from .pipeline_context import PipelineRunContext
from .pipeline_helpers import wrap_untrusted
from .pipeline_models import AnalysisFlowResult, PipelineResult
from .risk_report import _build_run_cost
from .scientific_materiality import SCIENTIFIC_POLICY_ID
from .progress import PipelineCancelled
from .consolidated_analysis import load_paper_understanding, load_experiment_plan, supervised_analysis_stage


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
    from .supervisor import REPLAY_REQUIRED, current_supervisor, supervised_call
    parse_resume = options.resume

    def repair_parse(_decision: dict[str, Any]) -> None:
        nonlocal parse_resume
        parse_resume = False

    def parse_paper() -> tuple[dict, list, dict]:
        supervisor = current_supervisor()
        if supervisor is not None and supervisor.current_instruction("paper_parse"):
            repair_parse(supervisor.current_instruction("paper_parse"))
        parsed = pipeline._load_or_create_paper(
            paper_path=paper_path, output_dir=output_dir,
            max_pages=options.max_pages, resume=parse_resume,
        )
        images = pipeline._render_paper_images(paper_path=paper_path, paper=parsed)
        layout = mineru_stage(
            paper_path=paper_path, output_dir=output_dir, audit_dir=audit_dir,
            resume=parse_resume, timeout=options.mineru_timeout, max_pages=options.max_pages,
        )
        return parsed, images, layout

    paper, paper_images, mineru_result = supervised_call(
        "paper_parse", parse_paper,
        inputs={"paper_path": str(paper_path), "max_pages": options.max_pages},
        evidence_roots={"paper_source": paper_path, "paper_chunks": output_dir / "paper_chunks.json",
                        "figure_index": output_dir / "paper_figure_index.json"},
        summarize=lambda result: {"paper": result[0], "image_count": len(result[1]), "layout": result[2]},
        repair=repair_parse,
        reconcile=lambda _state: REPLAY_REQUIRED, passthrough=(PipelineCancelled,),
    )
    valid_chunk_ids = {
        str(chunk.get("chunk_id"))
        for chunk in paper.get("chunks", [])
        if isinstance(chunk, dict) and chunk.get("chunk_id")
    }
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
    initial_facts = deepcopy(understanding["facts"])
    paper_thesis = understanding.get("paper_thesis")
    write_json(output_dir / "paper_thesis.json", paper_thesis)
    write_json(output_dir / "engineering_facts_initial.json", initial_facts)
    context.mark("facts_initial")
    context.mark("thesis")

    context.begin("tasks_preliminary")
    from .preflight import architecture_capability_inventory
    host_capabilities = architecture_capability_inventory()
    current_plan = load_experiment_plan(pipeline, context, facts=initial_facts, paper_thesis=paper_thesis,
        paper=paper, paper_context=paper_context, paper_images=paper_images, figure_index=figure_index,
        host_capabilities=host_capabilities)
    analysis_stage_invocations += int(not current_plan.get("_meta", {}).get("cache_reused"))
    # The coupled planner publishes a complete task/architecture snapshot. Tasks
    # sharing a figure can cover different regimes or claims; keep their IDs and
    # relationship references intact rather than deduplicating by figure text.
    preliminary_tasks = deepcopy(current_plan["tasks"])
    write_json(output_dir / "repro_tasks_preliminary.json", preliminary_tasks)
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

        backfill = supervised_analysis_stage(pipeline, context,
            node_id=f"targeted_backfill_{round_index:02d}",
            recovery_guidance=(current_supervisor().current_instruction("tool:backfill:search")
                               if current_supervisor() is not None else None),
            output_path=(
                audit_dir / f"02b_backfill_round_{round_index:02d}_result.json"
            ),
            output_dir=output_dir,
            audit_dir=audit_dir,
            prompt=prompt,
            stage_label=label,
            cleanup_stage="facts_backfill",
            schema_stage="targeted_fact_backfill",
            max_attempts=1,
            resume=options.resume,
            images=paper_images,
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
        analysis_stage_invocations += int(not backfill.get("_meta", {}).get("cache_reused"))
        return backfill

    def _refresh_tasks_after_round(
        round_index: int, current_tasks: dict[str, Any], current_facts: dict[str, Any],
        cumulative_resolution: dict[str, Any], search_ledger: dict[str, Any],
    ) -> dict[str, Any]:
        nonlocal current_plan, analysis_stage_invocations
        write_json(audit_dir / f"02b_round_{round_index:02d}_facts_before_planner.json", current_facts)
        write_json(output_dir / "engineering_facts.json", current_facts)
        candidate = load_experiment_plan(pipeline, context, facts=current_facts, paper_thesis=paper_thesis,
            paper=paper, paper_context=paper_context, paper_images=paper_images, figure_index=figure_index,
            host_capabilities=host_capabilities, previous_plan={**current_plan, "tasks": current_tasks},
            resolution=cumulative_resolution, ledger=search_ledger, round_index=round_index,
            recovery_guidance=(current_supervisor().current_instruction("tool:backfill:replan")
                               if current_supervisor() is not None else None))
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
        max_rounds=TARGETED_BACKFILL_MAX_ROUNDS,
        on_round=_write_round_audit,
        audit_dir=audit_dir,
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
    context.mark("facts")
    analysis_warnings = write_analysis_warnings(output_dir=output_dir, audit_dir=audit_dir,
        stage="02c_final_repro_tasks", groups={})
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
            backfill_loop.get("final_handoff", {}).get("ready_for_writer")
        ),
    }
    tasks_meta["fact_gap_handoff"] = {
        "stop_reason": backfill_loop["stop_reason"],
        "round_count": backfill_round_count,
        "task_expert_handoff": backfill_loop.get("final_handoff", {}),
        "terminal_unresolved": resolution.get("terminal_unresolved", []),
        "open": resolution.get("open", []),
        "analysis_warning_count": int(
            analysis_warnings.get("warning_count") or 0
        ),
        "analysis_warnings_artifact": "analysis_warnings.json",
    }
    tasks["_meta"] = tasks_meta
    write_json(output_dir / "repro_tasks.json", tasks)
    context.mark("tasks")

    # Claims were supplied before planning. Re-publish canonical artifacts after
    # downstream invalidation, without another thesis or acceptance model call.
    write_json(output_dir / "paper_thesis.json", paper_thesis)
    tasks.setdefault("_meta", {})["scientific_acceptance_finalization"] = {
        "paper_thesis_used": bool(paper_thesis), "policy_id": SCIENTIFIC_POLICY_ID,
        "decision_owner": "experiment_planner", "structure_is_advisory": True}
    write_json(output_dir / "repro_tasks.json", tasks)

    handoff_resume = options.resume
    handoff_repair_instruction: dict[str, Any] | None = None
    handoff_applied_instruction: dict[str, Any] | None = None
    handoff_reconcile_cache = False

    def publish_analysis_handoff() -> tuple[dict, dict, dict | None]:
        nonlocal tasks, current_plan, analysis_stage_invocations, handoff_repair_instruction, handoff_applied_instruction, handoff_reconcile_cache
        supervisor = current_supervisor()
        instruction = supervisor.current_instruction("analysis_handoff") if supervisor is not None else None
        if (instruction and instruction.get("action") == "retry" and instruction != handoff_applied_instruction
                and handoff_repair_instruction is None):
            repair_analysis_handoff(instruction)
            handoff_reconcile_cache = True
        if handoff_repair_instruction is not None:
            revised = load_experiment_plan(
                pipeline, context, facts=facts, paper_thesis=paper_thesis,
                paper=paper, paper_context=paper_context, paper_images=paper_images,
                figure_index=figure_index, host_capabilities=host_capabilities,
                previous_plan={**current_plan, "tasks": tasks}, resolution=resolution,
                ledger=backfill_loop["ledger"], round_index=backfill_round_count + 1,
                supervise=False, recovery_guidance=handoff_repair_instruction,
                recovery_resume=handoff_reconcile_cache,
            )
            analysis_stage_invocations += int(not revised.get("_meta", {}).get("cache_reused"))
            current_plan = revised
            tasks = revised["tasks"]
            write_json(output_dir / "repro_tasks.json", tasks)
            handoff_applied_instruction = handoff_repair_instruction
            handoff_repair_instruction = None
            handoff_reconcile_cache = False
        execution_plan = compile_execution_plan(tasks)
        write_json(output_dir / "execution_plan.json", execution_plan)
        write_json(
            audit_dir / "02e_execution_plan.json",
            {
                "ok": True,
                "logical_task_count": execution_plan["logical_task_count"],
                "execution_unit_count": execution_plan["execution_unit_count"],
                "task_policy": execution_plan["task_policy"],
            },
        )

        experiment_index = pipeline._load_or_create_experiment_index(
            output_dir=output_dir,
            audit_dir=audit_dir,
            facts=facts,
            tasks=tasks,
            paper=paper,
            figure_index=figure_index,
            resume=handoff_resume,
        )
        context.mark("experiment_index")
        source_architecture = current_plan.get("scientific_architecture")
        scientific_architecture = architecture_runtime_view(source_architecture)
        write_json(audit_dir / "02f_architecture_address_view.json", {
            "policy": "explicit-address-aliases-v1",
            "source": "scientific_architecture in the preserved Planner output",
            "source_sha256": hashlib.sha256(json.dumps(source_architecture, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
            "runtime_view_sha256": hashlib.sha256(json.dumps(scientific_architecture, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
            "aliases_added": source_architecture != scientific_architecture,
            "scientific_content_rewritten": False,
        })
        from .preflight import architecture_execution_capability_gaps
        if scientific_architecture is None:
            (output_dir / "scientific_architecture.json").unlink(missing_ok=True)
        else:
            write_json(output_dir / "scientific_architecture.json", scientific_architecture)
            gaps = architecture_execution_capability_gaps(scientific_architecture, host_capabilities)
            write_json(audit_dir / "02f_architecture_execution_capability_gaps.json",
                {"advisory": True, "policy": "supervisor_assesses_host_gap", "gap_count": len(gaps), "gaps": gaps})
        write_json(audit_dir / "02f_architecture_host_capabilities_current.json", host_capabilities)
        write_json(audit_dir / "02f_architecture_host_capabilities.json", host_capabilities)
        write_json(output_dir / "experiment_plan.json", {"tasks": tasks, "scientific_architecture": scientific_architecture,
            "_meta": {"planner_cache": current_plan.get("_meta", {}).get("cache"),
                      "planning_complete": bool(backfill_loop.get("final_handoff", {}).get("ready_for_writer")),
                      "backfill_stop_reason": backfill_loop["stop_reason"]}})
        context.mark("scientific_architecture")
        return execution_plan, experiment_index, scientific_architecture

    def repair_analysis_handoff(decision: dict[str, Any]) -> None:
        nonlocal handoff_resume, handoff_repair_instruction
        handoff_resume = False
        handoff_repair_instruction = decision

    def reconcile_analysis_handoff(_state: dict) -> Any:
        nonlocal handoff_reconcile_cache
        handoff_reconcile_cache = True
        return REPLAY_REQUIRED

    execution_plan, experiment_index, scientific_architecture = supervised_call(
        "analysis_handoff", publish_analysis_handoff,
        inputs={"owner": "experiment_planner", "tasks": tasks,
                "fact_artifact": str(output_dir / "engineering_facts.json"),
                "resolution": resolution},
        evidence_roots={name.replace(".", "_"): output_dir / name for name in
                        ("paper_chunks.json", "engineering_facts.json", "repro_tasks.json",
                         "execution_plan.json", "experiment_index.json", "scientific_architecture.json")},
        summarize=lambda result: {"tasks": tasks, "execution_plan": result[0], "experiment_index": result[1],
                                   "scientific_architecture": result[2],
                                   "backfill_handoff": backfill_loop.get("final_handoff"),
                                   "backfill_stop_reason": backfill_loop["stop_reason"]},
        repair=repair_analysis_handoff,
        reconcile=reconcile_analysis_handoff, passthrough=(PipelineCancelled,),
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
            "analysis_agent_count": 2,
            "analysis_stage_invocations": analysis.analysis_stage_invocations,
            "analysis_warning_count": int(
                analysis.analysis_warnings.get("warning_count") or 0
            ),
            "json_format_repair_limit": int(options.json_repair_attempts),
            "facts_stop_rule": "supervisor_selected_search_with_budget",
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
