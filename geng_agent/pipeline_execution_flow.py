from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .outputs import write_json
from .pipeline_context import PipelineRunContext
from .pipeline_models import AnalysisFlowResult, ExecutionFlowResult
from .risk_report import (
    build_risk_report,
    build_scientific_check,
    detect_nondeterminism_findings,
)
from .workflow_policy import _shared_foundation_is_material


def run_execution_flow(
    context: PipelineRunContext,
    analysis: AnalysisFlowResult,
) -> ExecutionFlowResult:
    from .agentic_task_reporters import run_codex_task_reporter_workflow
    from .agentic_task_writers import run_codex_task_writer_workflow
    from .case_environment import EnvironmentPolicyError, RequirementRequest
    from .foundation_revision import FoundationRevisionRequired
    from .case_runtime import (
        EnvironmentRequestRequired,
        EnvironmentResolutionError,
        ensure_case_runtime,
    )

    output_dir = context.output_dir
    audit_dir = context.audit_dir
    options = context.options
    paper = analysis.paper
    paper_path = analysis.paper_path
    facts = analysis.facts
    tasks = analysis.tasks
    experiment_index = analysis.experiment_index
    paper_thesis = analysis.paper_thesis
    paper_images = analysis.paper_images
    figure_index = analysis.figure_index
    scientific_architecture = analysis.scientific_architecture
    execution_plan = analysis.execution_plan
    repro_project_dir = analysis.repro_project_dir

    def _stop_for_environment(exc: BaseException, *, source: str) -> None:
        category = str(
            getattr(exc, "category", "environment_resolution_failed")
        )
        report = getattr(exc, "report", None)
        write_json(
            audit_dir / "03a_environment_blocked.json",
            {
                "decision": "stop",
                "stop_class": "blocked_environment",
                "pipeline_can_continue": False,
                "source": source,
                "category": category,
                "error": f"{type(exc).__name__}: {exc}",
                "report": report if isinstance(report, dict) else None,
            },
        )
        raise EnvironmentResolutionError(
            category,
            f"case environment blocked at {source}: {exc}",
            report=report if isinstance(report, dict) else None,
        ) from exc

    context.begin("environment")
    try:
        case_runtime = ensure_case_runtime(
            output_dir=output_dir,
            audit_dir=audit_dir,
            scientific_architecture=scientific_architecture,
            resume=options.resume,
        )
    except (EnvironmentResolutionError, EnvironmentPolicyError, OSError) as exc:
        _stop_for_environment(exc, source="initial_resolution")
    context.mark("environment")

    environment_requests: list[RequirementRequest] = []
    environment_states: set[tuple[str, tuple[str, ...]]] = set()

    def _extend_case_runtime(pending: EnvironmentRequestRequired) -> None:
        nonlocal case_runtime
        environment_requests.extend(pending.requests)
        requested = tuple(
            sorted(request.requirement for request in environment_requests)
        )
        state = (case_runtime.environment_hash, requested)
        if state in environment_states:
            _stop_for_environment(
                EnvironmentResolutionError(
                    "resolution_stalled",
                    "the same dependency request recurred without environment progress",
                ),
                source=pending.source,
            )
        environment_states.add(state)
        previous_hash = case_runtime.environment_hash
        try:
            newer = ensure_case_runtime(
                output_dir=output_dir,
                audit_dir=audit_dir,
                scientific_architecture=scientific_architecture,
                extra_requirements=environment_requests,
                resume=True,
            )
        except (EnvironmentResolutionError, EnvironmentPolicyError, OSError) as exc:
            _stop_for_environment(exc, source=pending.source)
        if newer.environment_hash == previous_hash:
            _stop_for_environment(
                EnvironmentResolutionError(
                    "resolution_stalled",
                    "dependency request did not change the verified case environment",
                ),
                source=pending.source,
            )
        case_runtime = newer
        write_json(
            audit_dir / "03a_environment_extensions.json",
            {
                "extension_count": len(environment_states),
                "latest_source": pending.source,
                "requirements": requested,
                "environment_lock_hash": case_runtime.environment_hash,
            },
        )

    validation = {
        "required_files_present": True,
        "missing_files": [],
        "python_compiles": True,
        "compile_errors": [],
        "host_validation_skipped": True,
    }
    scientific_check = build_scientific_check(tasks)
    generation_marked = False

    def _review_one_task(
        task_index: int,
        assigned_task: dict[str, Any],
        task_record: dict[str, Any],
        writer_round: int,
    ) -> dict[str, Any]:
        result = run_codex_task_reporter_workflow(
            index=task_index,
            task=assigned_task,
            task_record=task_record,
            paper=paper,
            paper_path=paper_path,
            facts=facts,
            experiment_index=experiment_index,
            paper_thesis=paper_thesis,
            paper_images=paper_images,
            figure_index=figure_index,
            output_dir=output_dir,
            audit_dir=audit_dir,
            resume=options.resume,
            round_no=writer_round,
        )
        reporter_owned_retry = (
            not result.get("ok")
            and isinstance(result.get("codex_status"), dict)
            and result["codex_status"].get("ok")
        )
        if reporter_owned_retry:
            result = run_codex_task_reporter_workflow(
                index=task_index,
                task=assigned_task,
                task_record=task_record,
                paper=paper,
                paper_path=paper_path,
                facts=facts,
                experiment_index=experiment_index,
                paper_thesis=paper_thesis,
                paper_images=paper_images,
                figure_index=figure_index,
                output_dir=output_dir,
                audit_dir=audit_dir,
                resume=False,
                round_no=writer_round * 100 + 1,
                include_all_paper_pages=True,
            )
        return result

    foundation: dict[str, Any] | None = None
    foundation_resume = options.resume
    writer_resume = options.resume
    foundation_progress_marked = False
    generation_started = False
    pending_revision: dict[str, Any] | None = None
    revision_states: set[tuple[str, str]] = set()
    forced_task_ids: set[str] = set()
    declined_revision_ids: set[str] = set()
    if options.resume:
        for failure_path in (audit_dir / "03b_foundation_revision_failures").glob("*.json"):
            try:
                failure = json.loads(failure_path.read_text(encoding="utf-8"))
                if failure.get("decision") == "retain_previous_version_and_report_unresolved_science":
                    declined_revision_ids.add(str(failure["request"]["request_id"]))
            except (OSError, ValueError, KeyError, TypeError):
                continue

    def _decline_scientific_revision(exc: BaseException) -> bool:
        nonlocal pending_revision, forced_task_ids
        if pending_revision is None:
            return False
        request_id = str(pending_revision.get("request_id") or "")
        declined_revision_ids.add(request_id)
        write_json(audit_dir / "03b_foundation_revision_failures" / f"{request_id}.json",
            {"request": pending_revision, "error": f"{type(exc).__name__}: {exc}",
             "decision": "retain_previous_version_and_report_unresolved_science"})
        pending_revision = None
        forced_task_ids = set()
        return True

    def _record_optional_foundation_fallback(exc: BaseException) -> None:
        report = getattr(exc, "report", None)
        write_json(
            audit_dir / "03b_foundation_fallback.json",
            {
                "policy": "reproduction_first",
                "decision": "fallback",
                "stage_usable": False,
                "pipeline_can_continue": True,
                "fallback": "isolated_task_writers_without_shared_foundation",
                "category": str(
                    getattr(exc, "category", "foundation_failed")
                ),
                "warning": f"{type(exc).__name__}: {exc}",
                "report": report if isinstance(report, dict) else None,
            },
        )

    while True:
        if scientific_architecture is not None:
            from .agentic_foundation import run_codex_foundation_writer_workflow

            if not foundation_progress_marked:
                context.begin("foundation")
            try:
                foundation = run_codex_foundation_writer_workflow(
                    facts=facts,
                    tasks=tasks,
                    experiment_index=experiment_index,
                    scientific_architecture=scientific_architecture,
                    paper=paper,
                    paper_path=paper_path,
                    paper_images=paper_images,
                    paper_thesis=paper_thesis,
                    output_dir=output_dir,
                    audit_dir=audit_dir,
                    resume=foundation_resume,
                    case_runtime=case_runtime,
                    execution_plan=execution_plan,
                    **({"revision_request": pending_revision,
                        "revision_evidence_root": Path(pending_revision["evidence_root"]),
                        "previous_foundation": foundation} if pending_revision else {}),
                )
                if pending_revision:
                    request_file = Path(pending_revision["evidence_root"]) / "foundation_revision_request.json"
                    request_file.unlink(missing_ok=True)
                    write_json(audit_dir / "03b_foundation_revision_applied.json", pending_revision)
                    pending_revision = None
            except EnvironmentRequestRequired as pending:
                prior_request_count = len(environment_requests)
                try:
                    _extend_case_runtime(pending)
                except (EnvironmentResolutionError, EnvironmentPolicyError, OSError) as exc:
                    if _decline_scientific_revision(exc):
                        # Failed repair-only dependencies must not contaminate
                        # a later, unrelated Writer environment request.
                        del environment_requests[prior_request_count:]
                        blocked_path = audit_dir / "03a_environment_blocked.json"
                        blocked = json.loads(blocked_path.read_text(encoding="utf-8"))
                        blocked.update(scope="foundation_revision", pipeline_can_continue=True,
                                       decision="retain_existing_environment_and_report_unresolved_science")
                        write_json(blocked_path, blocked)
                        continue
                    raise
                foundation_resume = True
                writer_resume = True
                continue
            except (EnvironmentResolutionError, EnvironmentPolicyError) as exc:
                if _decline_scientific_revision(exc):
                    continue
                if _shared_foundation_is_material(
                    execution_plan,
                    scientific_architecture,
                ):
                    _stop_for_environment(exc, source="foundation_writer")
                foundation = None
                _record_optional_foundation_fallback(exc)
            except Exception as exc:
                if _decline_scientific_revision(exc):
                    continue
                if _shared_foundation_is_material(
                    execution_plan,
                    scientific_architecture,
                ):
                    write_json(
                        audit_dir / "03b_foundation_fallback.json",
                        {
                            "policy": "preserve_material_shared_science",
                            "decision": "stop",
                            "stage_usable": False,
                            "pipeline_can_continue": False,
                            "fallback": None,
                            "warning": f"{type(exc).__name__}: {exc}",
                        },
                    )
                    raise RuntimeError(
                        "Foundation failed for task relationships that require one "
                        "shared scientific implementation"
                    ) from exc
                foundation = None
                _record_optional_foundation_fallback(exc)
        if not foundation_progress_marked:
            context.mark("foundation")
            foundation_progress_marked = True
        if not generation_started:
            context.begin("generation")
            generation_started = True
        try:
            agentic_result = run_codex_task_writer_workflow(
                facts=facts,
                tasks=tasks,
                execution_plan=execution_plan,
                experiment_index=experiment_index,
                paper=paper,
                paper_path=paper_path,
                paper_context_json=analysis.paper_context,
                paper_images=paper_images,
                paper_thesis=paper_thesis,
                output_dir=output_dir,
                audit_dir=audit_dir,
                repro_project_dir=repro_project_dir,
                run_repro=options.run_repro,
                run_timeout=options.run_timeout,
                resume=writer_resume,
                task_review_callback=_review_one_task,
                foundation=foundation,
                case_runtime=case_runtime,
                force_task_ids=forced_task_ids,
                declined_foundation_revision_ids=declined_revision_ids,
            )
        except EnvironmentRequestRequired as pending:
            _extend_case_runtime(pending)
            foundation_resume = True
            writer_resume = True
            continue
        except FoundationRevisionRequired as pending:
            if foundation is None:
                raise RuntimeError("a shared-science revision requires an existing Foundation") from pending
            state = (str(foundation.get("manifest", {}).get("snapshot_hash")), str(pending.request.get("request_id")))
            if state in revision_states:
                pending_revision = pending.request
                _decline_scientific_revision(RuntimeError("same scientific revision repeated without progress"))
                continue
            revision_states.add(state)
            pending_revision = pending.request
            forced_task_ids = set(pending.request.get("affected_task_ids") or [])
            foundation_resume = True
            writer_resume = True
            continue
        except (EnvironmentResolutionError, EnvironmentPolicyError) as exc:
            _stop_for_environment(exc, source="task_writers")
        break

    manifest = agentic_result["manifest"]
    written_files = [
        Path(path) for path in agentic_result.get("written_files", [])
    ]
    runtime_result = agentic_result["runtime_result"]
    task_records = (
        agentic_result.get("task_records")
        if isinstance(agentic_result.get("task_records"), list)
        else []
    )
    writer_review_document = (
        agentic_result.get("writer_review_doc")
        if isinstance(agentic_result.get("writer_review_doc"), dict)
        else {}
    )
    writer_summary_result = {
        "enabled": True,
        "passed": False,
        "mode": "task_writer_task_reporter_loops",
        "overall_alignment": "candidate",
        "overall_result_credibility": "low",
    }
    if not generation_marked:
        context.mark("generation")
        context.mark("runtime")

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
    for nd_finding in detect_nondeterminism_findings(repro_project_dir):
        risk_report.setdefault("findings", []).append(nd_finding)
    if not runtime_result.get("passed"):
        risk_report.setdefault("findings", []).append(
            {
                "type": "runtime_not_fully_valid",
                "severity": "warning",
                "message": (
                    "One or more tasks did not finish a valid full; the failure "
                    "remains reportable."
                ),
            }
        )
    return ExecutionFlowResult(
        validation=validation,
        scientific_check=scientific_check,
        agentic_result=agentic_result,
        manifest=manifest,
        written_files=written_files,
        runtime_result=runtime_result,
        task_records=task_records,
        writer_review_document=writer_review_document,
        writer_summary_result=writer_summary_result,
        risk_report=risk_report,
    )
