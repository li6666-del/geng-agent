from __future__ import annotations

from typing import Any, Callable

from .outputs import write_json
from .pipeline_context import PipelineRunContext
from .pipeline_models import AnalysisFlowResult, ExecutionFlowResult, PipelineResult
from .pipeline_report_delivery import report_editor_exception_result
from .pipeline_verification import build_terminal_review_summary
from .risk_report import _build_run_cost, build_risk_report
from .schemas import validate_stage


def run_report_flow(
    pipeline: Any,
    context: PipelineRunContext,
    analysis: AnalysisFlowResult,
    execution: ExecutionFlowResult,
    *,
    derive_verdict: Callable[..., dict[str, Any]],
    provenance_builder: Callable[..., dict[str, Any]],
) -> PipelineResult:
    from .agentic_report_editor import run_codex_report_editor_workflow
    from .agentic_task_reporters import task_verifications_document
    from .agentic_task_writers import apply_verified_result
    from .verification_result import (
        normalize_task_verification,
        verification_result_issues,
    )

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

    task_by_id = {
        str(item.get("task_id") or ""): item
        for item in tasks.get("repro_tasks", [])
        if isinstance(item, dict) and str(item.get("task_id") or "")
    }
    task_reporter_results: list[dict[str, Any]] = []
    for record in task_records:
        task_id = str(record.get("task_id") or "")
        existing = (
            record.get("task_reporter")
            if isinstance(record.get("task_reporter"), dict)
            else {}
        )
        verification = (
            existing.get("task_verification")
            if isinstance(existing.get("task_verification"), dict)
            else None
        )
        if verification:
            task_reporter_results.append(existing)
            continue
        execution_summary = (
            record.get("execution_summary")
            if isinstance(record.get("execution_summary"), dict)
            else {}
        )
        try:
            full_run_count = int(execution_summary.get("full_run_count") or 0)
        except (TypeError, ValueError):
            full_run_count = 0
        run_valid_hint = (
            full_run_count >= 1
            and execution_summary.get("last_returncode") == 0
        )
        verification = normalize_task_verification(
            {},
            task_id,
            task=task_by_id.get(task_id),
            run_valid_hint=run_valid_hint,
        )
        verification.setdefault("remaining_uncertainties", []).append(
            "The isolated Reporter did not produce a usable note; the host "
            "retained a conservative terminal outcome."
        )
        synthetic = {
            "ok": True,
            "synthetic_terminal": True,
            "task_id": task_id,
            "task_verification": verification,
            "scientific_terminal": True,
            "scientific_successful": verification.get("outcome")
            in {"reproduced", "reproduced_with_assumptions"},
            "validation_warnings": [
                str(existing.get("error") or "reporter unavailable")
            ],
        }
        record["task_reporter"] = synthetic
        record["task_verification"] = verification
        task_reporter_results.append(synthetic)

    verification_result = task_verifications_document(task_reporter_results)
    if not verification_result.get("all_terminal"):
        for result in task_reporter_results:
            verification = (
                result.get("task_verification")
                if isinstance(result, dict)
                else None
            )
            if (
                not isinstance(verification, dict)
                or verification.get("host_action") != "rerun_writer"
            ):
                continue
            verification["host_action"] = "complete"
            verification["rerun_reason"] = "none"
            verification["outcome"] = (
                "execution_failed"
                if verification.get("run_valid") is False
                else "not_reproduced"
            )
            verification.setdefault("remaining_uncertainties", []).append(
                "A requested causal rerun could not be completed; recorded as "
                "a terminal outcome."
            )
        verification_result = task_verifications_document(task_reporter_results)

    schema_issues = validate_stage("verification_result", verification_result)
    verification_warnings = [
        f"{issue.path}: {issue.message}" for issue in schema_issues
    ] + verification_result_issues(
        verification_result,
        [str(record.get("task_id") or "") for record in task_records],
    )
    write_json(
        audit_dir / "04_task_verification_warnings.json",
        {
            "advisory": True,
            "warning_count": len(verification_warnings),
            "warnings": verification_warnings,
        },
    )
    write_json(output_dir / "verification_result.json", verification_result)
    runtime_result = apply_verified_result(
        task_records=task_records,
        verification_result=verification_result,
        output_dir=output_dir,
        audit_dir=audit_dir,
        repro_project_dir=repro_project_dir,
    )
    terminal_review = build_terminal_review_summary(verification_result)
    all_successful = bool(terminal_review["all_successful"])
    outcome_counts = terminal_review["outcome_counts"]
    if isinstance(agentic_result.get("status"), dict):
        agentic_result["status"].update(
            {
                "stop_class": (
                    "verified_matched" if all_successful else "verified_terminal"
                ),
                "stopped_reason": (
                    "all tasks reproduced their core conclusions"
                    if all_successful
                    else "all tasks reached reportable scientific terminal outcomes"
                ),
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
    try:
        report_editor_result = run_codex_report_editor_workflow(
            paper=paper,
            facts=facts,
            tasks=tasks,
            paper_thesis=paper_thesis,
            runtime_result=runtime_result,
            risk_report=risk_report,
            task_records=task_records,
            task_verifications=[
                item.get("task_verification")
                for item in task_reporter_results
                if isinstance(item, dict)
                and isinstance(item.get("task_verification"), dict)
            ],
            output_dir=output_dir,
            audit_dir=audit_dir,
            resume=options.resume,
        )
    except Exception as exc:
        report_editor_result = report_editor_exception_result(exc)
    first_editor_status = report_editor_result.get("codex_status")
    report_editor_invocations = int(
        not report_editor_result.get("cached")
        and isinstance(first_editor_status, dict)
        and first_editor_status.get("role") == "report_editor"
    )
    if not report_editor_result.get("ok") and report_editor_result.get(
        "retryable"
    ):
        try:
            report_editor_result = run_codex_report_editor_workflow(
                paper=paper,
                facts=facts,
                tasks=tasks,
                paper_thesis=paper_thesis,
                runtime_result=runtime_result,
                risk_report=risk_report,
                task_records=task_records,
                task_verifications=[
                    item.get("task_verification")
                    for item in task_reporter_results
                    if isinstance(item, dict)
                    and isinstance(item.get("task_verification"), dict)
                ],
                output_dir=output_dir,
                audit_dir=audit_dir,
                resume=False,
                attempt_no=2,
                repair_context=report_editor_result,
                allow_fallback=True,
            )
        except Exception as exc:
            report_editor_result = report_editor_exception_result(exc)
        second_editor_status = report_editor_result.get("codex_status")
        report_editor_invocations += int(
            isinstance(second_editor_status, dict)
            and second_editor_status.get("role") == "report_editor"
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

    reproducibility_verdict = derive_verdict(
        risk_report=risk_report,
        runtime_result=runtime_result,
        result_review=writer_review_document,
    )
    verdict_issues = validate_stage(
        "reproducibility_verdict", reproducibility_verdict
    )
    if verdict_issues:
        write_json(
            audit_dir / "04b_reproducibility_verdict_fallback.json",
            {
                "advisory": True,
                "errors": [issue.as_dict() for issue in verdict_issues],
                "candidate": reproducibility_verdict,
            },
        )
        reproducibility_verdict = {
            "verdict": "inconclusive",
            "confidence": "low",
            "reasons": [
                "internal verdict formatting failed; task-level scientific "
                "evidence remains available"
            ],
            "recommended_action": (
                "Use task verification and runtime artifacts as the authority; "
                "regenerate only the summary verdict."
            ),
        }
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
    docx_generation = pipeline._generate_docx_reports(
        output_dir=output_dir,
        result_review_result=result_review_result,
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
    run_cost["report_backend"] = "codex_task_reporters_plus_editor"
    run_cost["task_reporter_count"] = len(task_records)
    run_cost["task_reporter_verification_rounds"] = verification_round
    run_cost["report_editor_invocations"] = report_editor_invocations
    run_cost["analysis_warning_count"] = int(
        analysis.analysis_warnings.get("warning_count") or 0
    )
    run_cost["json_format_repair_limit"] = int(options.json_repair_attempts)
    run_cost["facts_stop_rule"] = "single_global_then_selected_blockers_max_3"
    run_cost["tasks_stop_rule"] = "preliminary_or_refreshed_handoff_ready"
    run_cost["mineru_layout"] = {
        "ok": analysis.mineru_result.get("ok"),
        "cached": analysis.mineru_result.get("cached"),
        "fallback_used": analysis.mineru_result.get("fallback_used"),
        "duration_s": analysis.mineru_result.get("duration_s"),
        "figure_count": analysis.mineru_result.get("figure_count", 0),
    }
    if options.analysis_backend == "codex":
        run_cost["codex_session_policy"] = "unbounded_until_exit_or_user_stop"
        run_cost["analysis_agent_count"] = 1
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
                "analysis_agent_count": 1,
                "facts_stop_rule": (
                    "single_global_then_selected_blockers_max_3"
                ),
                "tasks_stop_rule": "thesis_informed_core_conclusion_contract",
                "task_writer_stop_rule": (
                    "host_terminal_scientific_outcome_or_external_blocker"
                ),
                "verification_stop_rule": (
                    "all_tasks_reach_reportable_core_conclusion_outcomes"
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
    context.finish()
    return PipelineResult(
        output_dir=output_dir,
        review_path=review_path,
        repro_project_dir=repro_project_dir,
        risk_report_path=risk_report_path,
        runtime_passed=runtime_result.get("passed"),
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
            if result_review_markdown_path.exists()
            else None
        ),
        result_review_passed=result_review_result.get("passed"),
        reproducibility_verdict=reproducibility_verdict,
        review_docx_path=review_docx_path if review_docx_path.exists() else None,
        result_review_docx_path=(
            result_review_docx_path if result_review_docx_path.exists() else None
        ),
        reproduction_report_path=(
            reproduction_report_path if reproduction_report_path.exists() else None
        ),
        reproduction_report_docx_path=(
            reproduction_report_docx_path
            if reproduction_report_docx_path.exists()
            else None
        ),
    )
