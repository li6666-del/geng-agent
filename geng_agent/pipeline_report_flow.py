from __future__ import annotations

from typing import Any, Callable

from .outputs import write_json
from .pipeline_context import PipelineRunContext
from .pipeline_models import AnalysisFlowResult, ExecutionFlowResult, PipelineResult
from .pipeline_report_delivery import run_supervised_report_editor, ReportOperationError
from .pipeline_verification import build_terminal_review_summary
from .progress import PipelineCancelled
from .risk_report import _build_run_cost, build_risk_report


def run_report_flow(
    pipeline: Any,
    context: PipelineRunContext,
    analysis: AnalysisFlowResult,
    execution: ExecutionFlowResult,
    *,
    provenance_builder: Callable[..., dict[str, Any]],
) -> PipelineResult:
    from .agentic_report_editor import run_codex_report_editor_workflow
    from .agentic_task_reporters import task_verifications_document
    from .agentic_task_writers import apply_verified_result
    from .supervisor import REPLAY_REQUIRED, current_supervisor, supervised_call

    output_dir = context.output_dir
    audit_dir = context.audit_dir
    options = context.options
    paper = analysis.paper
    facts = analysis.facts
    tasks = analysis.tasks
    experiment_index = analysis.experiment_index
    paper_thesis = analysis.paper_thesis
    runtime_result = execution.runtime_result
    task_records = execution.task_records
    agentic_result = execution.agentic_result
    validation = execution.validation
    scientific_check = execution.scientific_check
    manifest = execution.manifest
    written_files = execution.written_files
    repro_project_dir = analysis.repro_project_dir

    task_reporter_results: list[dict[str, Any]] = []
    for record in task_records:
        task_id = str(record.get("task_id") or "")
        existing = record.get("task_reporter") if isinstance(record.get("task_reporter"), dict) else {}
        raw = existing.get("task_verification")
        accepted = bool(isinstance(raw, dict) and existing.get("ok") is True and not existing.get("supervisor_blocked"))
        note = dict(raw) if isinstance(raw, dict) else {
            "task_id": task_id, "outcome": None, "host_action": None,
            "decision_authority": "no_reporter_decision", "engineering_status": "review_unavailable",
        }
        # This is the host's dispatch identity, not a correction of the note.
        note["assigned_task_id"] = task_id
        note["handoff_accepted"] = accepted
        note["coordination_status"] = record.get("coordination_status") or (
            "completed" if accepted and note.get("host_action") == "complete" else "stopped")
        if note["coordination_status"] == "stopped":
            note["coordination_reason"] = record.get("coordination_reason") or existing.get("error") or (
                "Execution coordination ended; an uncompleted Reporter request is retained without changing its conclusion.")
        if raw is not None and not accepted:
            record["unaccepted_task_reporter"] = existing
        record["task_verification"] = note
        task_reporter_results.append({**existing, "ok": accepted, "task_id": task_id, "task_verification": note})

    verification_result = task_verifications_document(task_reporter_results)
    write_json(output_dir / "verification_result.json", verification_result)
    # Scientific notes are metadata beside the original execution inputs. No
    # repeated schema gate, config mutation or portable-project refreeze.
    write_json(output_dir / "runtime_result.json", runtime_result)
    runtime_result = apply_verified_result(
        task_records=task_records, verification_result=verification_result,
        output_dir=output_dir, audit_dir=audit_dir, repro_project_dir=repro_project_dir,
    )
    runtime_result.update({
        "scientific_all_terminal": verification_result.get("all_terminal"),
        "scientific_all_successful": verification_result.get("all_successful"),
        "scientific_outcome_counts": verification_result.get("outcome_counts", {}),
    })
    write_json(output_dir / "runtime_result.json", runtime_result)
    terminal_review = build_terminal_review_summary(verification_result)
    all_successful = bool(terminal_review["all_successful"])
    outcome_counts = terminal_review["outcome_counts"]
    if isinstance(agentic_result.get("status"), dict):
        agentic_result["status"].update(
            {
                "stop_class": (
                    "delivery_incomplete" if runtime_result.get("delivery_status") in ("partial", "blocked")
                    else "reporter_handoff_collected"
                ),
                "stopped_reason": "Reporter decisions and coordination stops are preserved for report editing",
                "runtime": {
                    "passed": runtime_result.get("passed"),
                    "coverage": runtime_result.get("coverage"),
                },
            }
        )
    agentic_result["runtime_result"] = runtime_result
    agentic_result["task_records"] = task_records
    writer_review_document = terminal_review["writer_review_document"]
    writer_summary_result = terminal_review["writer_summary_result"]

    # This deliberately preserves the existing second risk-report build. A
    # separate behavior change can later decide how preliminary enrichments merge.
    risk_report = build_risk_report(
        facts,
        tasks,
        validation,
        runtime_result=runtime_result,
        scientific_check=scientific_check,
        result_review_result=writer_summary_result,
        paper_format=paper.get("format") if isinstance(paper, dict) else None,
    )
    risk_report["experiment_index"] = experiment_index
    risk_report["verification_result"] = verification_result
    risk_report["mineru_layout"] = {
        "ok": analysis.mineru_result.get("ok"),
        "fallback_used": analysis.mineru_result.get("fallback_used"),
        "error_kind": analysis.mineru_result.get("error_kind"),
        "figure_count": analysis.mineru_result.get("figure_count", 0),
    }
    verification_round = max(
        [int(record.get("writer_session_count") or 1) for record in task_records]
        or [1]
    )

    context.mark("task_reporters")
    context.begin("report_editor")
    report_mode = "model"
    report_runner = run_codex_report_editor_workflow
    report_editor_result, report_editor_invocations = run_supervised_report_editor(
        report_runner,
        arguments={
            "paper": paper, "facts": facts, "tasks": tasks, "paper_thesis": paper_thesis,
            "runtime_result": runtime_result, "risk_report": risk_report,
            "task_records": task_records,
            "task_verifications": [item["task_verification"] for item in task_reporter_results
                                   if isinstance(item.get("task_verification"), dict)],
            "output_dir": output_dir, "audit_dir": audit_dir, "resume": options.resume,
        },
    )
    result_review_result = report_editor_result.get("result_review_result")
    if not isinstance(result_review_result, dict):
        result_review_result = {
            "enabled": True,
            "passed": False,
            "reason": "report packaging did not return a result-review status",
        }
    if not report_editor_result.get("ok"):
        risk_report.setdefault("findings", []).append(
            {
                "type": "report_editor_failed",
                "message": (
                    "Human-facing report packaging degraded; scientific task "
                    "results were preserved."
                ),
                "error": result_review_result.get("reason"),
            }
        )
        write_json(output_dir / "risk_report.json", risk_report)

    # The Editor explains the accepted Reporter results. Python does not assign
    # a new aggregate scientific label or subjective confidence.
    reproducibility_verdict = None
    risk_report["reproducibility_verdict"] = reproducibility_verdict
    risk_report["task_reporters"] = {
        "ok": all(bool(item.get("ok")) for item in task_reporter_results),
        "mode": "isolated_task_reporters",
        "task_count": len(task_reporter_results),
        "verification_rounds": verification_round,
        "all_terminal": bool(verification_result.get("all_terminal")),
        "all_successful": bool(verification_result.get("all_successful")),
        "outcome_counts": verification_result.get("outcome_counts", {}),
    }
    risk_report["report_editor"] = {
        "ok": report_editor_result.get("ok"),
        "mode": report_editor_result.get("mode"),
        "cached": report_editor_result.get("cached"),
        "completion_mode": report_editor_result.get("completion_mode"),
        "degraded_report_generation": report_editor_result.get(
            "degraded_report_generation", False
        ),
        "invocations": report_editor_invocations,
    }
    context.mark("report_editor")
    context.begin("reports")
    docx_generation: dict[str, Any] = {}

    def deliver_word_reports() -> dict[str, Any]:
        nonlocal docx_generation
        docx_generation = pipeline._generate_docx_reports(
            output_dir=output_dir, result_review_result=result_review_result,
        )
        if result_review_result.get("passed") and any(
            isinstance(item, dict) and item.get("passed") is False
            for key, item in docx_generation.items() if key != "review_docx"
        ):
            raise ReportOperationError({"error": "Word conversion failed; original Markdown reports are preserved",
                                        "docx_generation": docx_generation})
        return docx_generation

    if result_review_result.get("passed"):
        try:
            docx_generation = supervised_call(
                "report_delivery", deliver_word_reports,
                inputs={"owner": "host_delivery", "reports": ["review.md", "result_review.md", "reproduction_report.md"]},
                evidence_roots={name.replace(".", "_"): output_dir / name for name in
                                ("review.md", "result_review.md", "reproduction_report.md",
                                 "review.docx", "result_review.docx", "reproduction_report.docx", "report_assets")},
                summarize=lambda result: result,
                reconcile=lambda _state: REPLAY_REQUIRED, passthrough=(PipelineCancelled,),
            )
        except PipelineCancelled:
            raise
        except Exception as exc:
            docx_generation["delivery_error"] = {"passed": False, "error": f"{type(exc).__name__}: {exc}",
                                                  "markdown_preserved": True}
    else:
        docx_generation = pipeline._generate_docx_reports(
            output_dir=output_dir, result_review_result=result_review_result,
        )
    risk_report["docx_generation"] = docx_generation

    review_path = output_dir / "review.md"
    risk_report_path = output_dir / "risk_report.json"
    write_json(risk_report_path, risk_report)
    write_json(
        output_dir / "generated_files.json",
        {
            "files": [
                path.relative_to(repro_project_dir).as_posix()
                for path in written_files
            ],
            "validation": validation,
            "runtime_result": runtime_result,
            "scientific_check": scientific_check,
            "paper_thesis": paper_thesis,
            "experiment_index": experiment_index,
            "manifest_meta": manifest.get("_meta", {}),
            "result_review": result_review_result,
            "task_reporters": task_reporter_results,
            "report_editor": report_editor_result,
            "verification_result": verification_result,
            "reproducibility_verdict": reproducibility_verdict,
            "docx_generation": docx_generation,
        },
    )
    context.mark("reports")

    run_cost = _build_run_cost(
        context.cost_marks,
        total_wall_s=context.elapsed_s(),
        by_model=context.usage_by_model(),
        audit_dir=context.audit_dir,
        codex_since=context.wall_start,
    )
    run_cost["analysis_backend"] = options.analysis_backend
    run_cost["project_backend"] = "codex"
    run_cost["codex_agent_mode"] = "task-writers"
    run_cost["report_backend"] = "codex_task_reporters_plus_" + report_mode
    run_cost["report_mode"] = report_mode
    run_cost["task_reporter_count"] = len(task_records)
    run_cost["task_reporter_verification_rounds"] = verification_round
    run_cost["report_editor_invocations"] = report_editor_invocations
    run_cost["analysis_warning_count"] = int(
        analysis.analysis_warnings.get("warning_count") or 0
    )
    run_cost["json_format_repair_limit"] = int(options.json_repair_attempts)
    run_cost["facts_stop_rule"] = "supervisor_selected_search_with_budget"
    run_cost["tasks_stop_rule"] = "supervisor_accepts_planner_handoff"
    run_cost["mineru_layout"] = {
        "ok": analysis.mineru_result.get("ok"),
        "cached": analysis.mineru_result.get("cached"),
        "fallback_used": analysis.mineru_result.get("fallback_used"),
        "duration_s": analysis.mineru_result.get("duration_s"),
        "figure_count": analysis.mineru_result.get("figure_count", 0),
    }
    if options.analysis_backend == "codex":
        run_cost["codex_session_policy"] = "unbounded_until_exit_or_user_stop"
        run_cost["analysis_agent_count"] = 2
        run_cost["analysis_stage_invocations"] = (
            analysis.analysis_stage_invocations
        )
    from .codex_cost import persist_pipeline_cost
    persist_pipeline_cost(output_dir, run_cost, run_id=context.run_id, started_at=context.wall_start)
    write_json(
        output_dir / "automation_provenance.json",
        provenance_builder(
            output_dir=output_dir,
            paper_path=analysis.paper_path,
            facts=facts,
            tasks=tasks,
            experiment_index=experiment_index,
            runtime_result=runtime_result,
            agentic_status=agentic_result.get("status", {}),
            settings={
                "analysis_backend": options.analysis_backend,
                "analysis_agent_count": 2,
                "facts_stop_rule": (
                    "supervisor_selected_search_with_budget"
                ),
                "tasks_stop_rule": "thesis_informed_core_conclusion_contract",
                "task_writer_stop_rule": (
                    "supervisor_coordinates_reporter_actions_or_stop"
                ),
                "verification_stop_rule": (
                    "reporter_decisions_preserved_with_coordination_status"
                ),
                "report_backend": (
                    "parallel_task_reporters_plus_final_editor"
                ),
            },
        ),
    )

    result_review_markdown_path = output_dir / "result_review.md"
    reproduction_report_path = output_dir / "reproduction_report.md"
    review_docx_path = output_dir / "review.docx"
    reproduction_report_docx_path = output_dir / "reproduction_report.docx"
    result_review_docx_path = output_dir / "result_review.docx"
    reports_accepted = bool(report_editor_result.get("ok") and result_review_result.get("passed"))

    def accepted_docx(name: str, path: Any) -> Any:
        status = docx_generation.get(name)
        return path if (reports_accepted and isinstance(status, dict) and status.get("passed") is True
                        and path.is_file()) else None

    delivery_status = runtime_result.get("delivery_status", "complete")
    if not report_editor_result.get("ok") or any(
        isinstance(item, dict) and item.get("passed") is False for key, item in docx_generation.items() if key != "review_docx"
    ):
        delivery_status = "partial"
    context.finish()
    return PipelineResult(
        output_dir=output_dir,
        review_path=review_path,
        repro_project_dir=repro_project_dir,
        risk_report_path=risk_report_path,
        runtime_passed=runtime_result.get("passed"),
        delivery_status=delivery_status,
        supervision_path=(audit_dir / "supervisor" / "goal.json") if current_supervisor() is not None else None,
        experiment_index_path=(
            output_dir / "experiment_index.json"
            if (output_dir / "experiment_index.json").exists()
            else None
        ),
        scientific_architecture_path=(
            output_dir / "scientific_architecture.json"
            if (output_dir / "scientific_architecture.json").exists()
            else None
        ),
        result_review_path=(
            result_review_markdown_path
            if reports_accepted and result_review_markdown_path.exists()
            else None
        ),
        result_review_passed=result_review_result.get("passed"),
        reproducibility_verdict=reproducibility_verdict,
        review_docx_path=accepted_docx("review_docx", review_docx_path),
        result_review_docx_path=(
            accepted_docx("result_review_docx", result_review_docx_path)
        ),
        reproduction_report_path=(
            reproduction_report_path if reports_accepted and reproduction_report_path.exists() else None
        ),
        reproduction_report_docx_path=(
            accepted_docx("reproduction_report_docx", reproduction_report_docx_path)
        ),
    )
