from __future__ import annotations

from contextlib import nullcontext
from dataclasses import asdict
from functools import wraps
import json
from pathlib import Path
from typing import Any, Callable

from .outputs import write_json
from .moderator import moderator_scope
from .pipeline_context import PipelineRunContext
from .pipeline_models import AnalysisFlowResult, ExecutionFlowResult
from .risk_report import build_risk_report
from .workflow_policy import _shared_foundation_is_material
from .paper_evidence import safe_label
from .progress import PipelineCancelled
from .security import redact_text
from .supervisor import StageBlocked, NodeFailure, REPLAY_REQUIRED, RunSupervisor, current_supervisor, supervised_call, supervisor_scope
from .supervisor_tools import SupervisorTool


_PENDING_ENVIRONMENT_PATH = "03a_pending_environment.json"


def _pending_environment_from_audit(audit_dir: Path):
    """Restore a host-recorded dependency request after an interrupted stage."""
    from .case_environment import RequirementRequest
    from .case_runtime import EnvironmentRequestRequired

    candidates = [audit_dir / _PENDING_ENVIRONMENT_PATH]
    candidates.extend(sorted((audit_dir / "03b_foundation_environment_resume").glob("*.json")))
    for path in candidates:
        try:
            value = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(value, dict) or value.get("state") != "awaiting_environment":
                continue
            source = str(value.get("source") or ("foundation_writer" if path.name != _PENDING_ENVIRONMENT_PATH else ""))
            raw_requests = value.get("requests")
            if not source or not isinstance(raw_requests, list):
                continue
            if not raw_requests and source != "shared_runtime_refresh":
                continue
            requests = [RequirementRequest(**item) for item in raw_requests if isinstance(item, dict)]
            if len(requests) != len(raw_requests):
                continue
            return EnvironmentRequestRequired(requests, source=source)
        except (OSError, TypeError, ValueError):
            continue
    return None


def _write_pending_environment(audit_dir: Path, pending) -> None:
    write_json(audit_dir / _PENDING_ENVIRONMENT_PATH, {
        "schema_version": 1,
        "state": "awaiting_environment",
        "source": pending.source,
        "requests": [asdict(item) for item in pending.requests],
    })


def _observe_agent_activity(
    flow: Callable[[PipelineRunContext, AnalysisFlowResult], ExecutionFlowResult],
) -> Callable[[PipelineRunContext, AnalysisFlowResult], ExecutionFlowResult]:
    @wraps(flow)
    def run(context: PipelineRunContext, analysis: AnalysisFlowResult) -> ExecutionFlowResult:
        from .agent_activity import agent_activity_scope

        recovery_scope = (nullcontext() if current_supervisor() is not None else
                          moderator_scope(context.audit_dir, context.progress_tracker.reporter))
        with agent_activity_scope(context.audit_dir, context.progress_tracker.reporter), recovery_scope:
            return flow(context, analysis)

    return run


@_observe_agent_activity
def run_execution_flow(
    context: PipelineRunContext,
    analysis: AnalysisFlowResult,
) -> ExecutionFlowResult:
    from .agentic_task_reporters import run_codex_task_reporter_workflow
    from .agentic_task_writers import run_codex_task_writer_workflow
    from .foundation_revision import FoundationRevisionRequired
    from .case_runtime import (
        EnvironmentRequestRequired,
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
    validation = {
        "required_files_present": True,
        "missing_files": [],
        "python_compiles": None,
        "compile_errors": [],
        "host_validation_skipped": True,
    }
    scientific_check = {"enabled": False, "decision_authority": "reporter_and_supervisor"}

    def _run_review_one_task(
        task_index: int,
        assigned_task: dict[str, Any],
        task_record: dict[str, Any],
        writer_round: int,
        resume_override: bool | None = None,
    ) -> dict[str, Any]:
        clarification = task_record.get("moderator_review_request")
        clarification = clarification if isinstance(clarification, dict) else None
        repair_context = None
        review_round = writer_round
        if clarification is not None:
            prior = task_record.get("task_reporter")
            repair_context = {
                **(prior if isinstance(prior, dict) else {}),
                "kind": "moderator_clarification",
                "instructions": str(clarification.get("instructions") or ""),
                "decision_id": str(clarification.get("decision_id") or ""),
            }
            # The Reporter also allocates the next available round on disk.
            # Keep clarification attempts visibly separate from Writer rounds.
            review_round = writer_round * 1000 + 200
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
            resume=False if clarification is not None else (options.resume if resume_override is None else resume_override),
            round_no=review_round,
            **({"repair_context": repair_context} if repair_context is not None else {}),
        )
        if clarification is not None:
            task_record["last_moderator_review_request"] = task_record.pop("moderator_review_request")
        return result

    def _review_one_task(task_index, assigned_task, task_record, writer_round):
        task_id = str(assigned_task.get("task_id") or task_record.get("task_id") or task_index)
        last_result = None
        resume_review = None

        def review():
            nonlocal last_result
            try:
                last_result = _run_review_one_task(task_index, assigned_task, task_record, writer_round,
                                                 resume_override=resume_review)
            except PipelineCancelled:
                raise
            except Exception as exc:
                # An unavailable review is reportable as unavailable. Do not
                # make the whole task wait for a moderator decision when the
                # report editor can preserve the absence of a scientific note.
                last_result = {"ok": False, "error_kind": "reporter_exception",
                               "error": redact_text(f"{type(exc).__name__}: {exc}")[:1000],
                               "task_verification": {}}
            return last_result

        def reconcile(_state):
            nonlocal resume_review
            resume_review = True
            return REPLAY_REQUIRED

        try:
            return supervised_call(
                f"reporter:{task_id}:round:{writer_round}", review,
                inputs={"owner": "task_reporter", "task": assigned_task,
                        "writer_round": writer_round, "writer_completed": task_record.get("writer_completed"),
                        "execution_summary": task_record.get("execution_summary"),
                        "instruction": "Judge handoff completeness; preserve the Reporter's scientific outcome, including not_reproduced."},
                evidence_roots={"writer": Path(str(task_record.get("sandbox") or audit_dir)),
                                "reporter": audit_dir / "04a_task_reporters" / f"{task_index:02d}_{safe_label(task_id)}"},
                summarize=lambda value: {key: value.get(key) for key in
                    ("ok", "workspace", "task_verification", "error", "recovery_kind")},
                reconcile=reconcile,
            )
        except StageBlocked as exc:
            # An independent review failure belongs to this task. Preserve its
            # raw result while other concurrent Writers/Reporters finish.
            return {**(last_result or {}), "ok": False, "error": str(exc),
                    "supervisor_blocked": exc.decision, "error_kind": "reporter_supervisor_blocked"}

    # Capabilities retain their concrete inputs. Offers only describe which
    # operations have the necessary objects; the supervisor selects every action.
    case_runtime = None
    foundation = None
    foundation_required = (scientific_architecture is not None
                           and _shared_foundation_is_material(execution_plan, scientific_architecture))
    foundation_ready = not foundation_required
    environment_requests = []
    # Rehydrating this host-owned request prevents a resumed flow from merely
    # re-verifying the baseline interpreter after a Foundation/Writer delivery
    # has already requested a concrete environment extension.
    pending_environment = _pending_environment_from_audit(audit_dir) if options.resume else None
    pending_revision = None
    forced_task_ids: set[str] = set()
    declined_revision_ids: set[str] = set()
    failures: dict[str, dict] = {}
    agentic_result = None
    complete = False
    attempts: dict[str, int] = {}
    environment_extension_count = 0
    supervisor = current_supervisor()
    if supervisor is None:
        supervisor = RunSupervisor(output_dir, audit_dir, {
            "paper": str(paper_path), "tasks": tasks, "run_repro": options.run_repro,
            "objective": "按既定论文目标安排复现工具，保留全部失败与已有结果。"},
            reporter=context.progress_tracker.reporter)

    def prepare_environment(decision):
        context.begin("environment")
        requests = environment_requests + (list(pending_environment.requests) if pending_environment else [])
        result = ensure_case_runtime(output_dir=output_dir, audit_dir=audit_dir,
            scientific_architecture=scientific_architecture,
            execution_task_ids=[str(task.get("task_id")) for task in tasks.get("repro_tasks", [])
                                if isinstance(task, dict) and task.get("task_id")],
            extra_requirements=requests,
            resume=options.resume or attempts.get("environment", 0) > 1)
        context.mark("environment")
        return result

    def build_foundation(decision):
        from .agentic_foundation import run_codex_foundation_writer_workflow
        context.begin("foundation")
        request = pending_revision
        if request is not None and not set(decision.get("component_ids") or []).issubset(request.get("component_ids") or []):
            raise ValueError("修复指令超出当前共享修订的组件范围；范围变更应先交回 Planner。")
        recovery = None
        if attempts.get("foundation", 0) > 1:
            recovery = decision
        result = run_codex_foundation_writer_workflow(
            facts=facts, tasks=tasks, experiment_index=experiment_index,
            scientific_architecture=scientific_architecture, paper=paper,
            paper_path=paper_path, paper_images=paper_images, paper_thesis=paper_thesis,
            output_dir=output_dir, audit_dir=audit_dir,
            resume=options.resume or attempts.get("foundation", 0) > 1,
            case_runtime=case_runtime, execution_plan=execution_plan,
            **({"revision_request": request, "revision_evidence_root": Path(request["evidence_root"]),
                "previous_foundation": foundation} if request else {}),
            **({"recovery_instructions": recovery} if recovery else {}))
        if result is None and _shared_foundation_is_material(execution_plan, scientific_architecture):
            raise NodeFailure("执行契约需要共享快照，但 Foundation 工具未返回快照。")
        context.mark("foundation")
        return result

    def run_writers(decision):
        context.begin("generation")
        return run_codex_task_writer_workflow(
            facts=facts, tasks=tasks, execution_plan=execution_plan, experiment_index=experiment_index,
            paper=paper, paper_path=paper_path, paper_context_json=analysis.paper_context,
            paper_images=paper_images, paper_thesis=paper_thesis, output_dir=output_dir,
            audit_dir=audit_dir, repro_project_dir=repro_project_dir,
            run_repro=options.run_repro,
            resume=options.resume or attempts.get("writers", 0) > 1,
            task_review_callback=_review_one_task, foundation=foundation, case_runtime=case_runtime,
            force_task_ids=forced_task_ids, review_feedback={},
            declined_foundation_revision_ids=declined_revision_ids)

    def preserve_revision(decision):
        # The old version remains old. Consumers receive the unresolved request,
        # never a fabricated revision or a successful scientific conclusion.
        request_id = str(pending_revision["request_id"])
        write_json(audit_dir / "03b_foundation_revision_failures" / (request_id + ".json"), {
            "request": pending_revision, "moderation": decision,
            "decision": "retain_previous_version_and_report_unresolved_science"})
        return request_id

    def available():
        if complete:
            return []
        offered = []
        def add(name, description, operation, inputs=None):
            def invoke(decision):
                attempts[name] = attempts.get(name, 0) + 1
                return operation(decision)
            def resume_call(decision):
                attempts[name] = max(attempts.get(name, 0), 1)
                return invoke(decision)
            offered.append(SupervisorTool(name, description, invoke,
                inputs={"task_goals": [{"task_id": item.get("task_id"), "goal": item.get("goal")}
                                       for item in tasks.get("repro_tasks", [])], "previous_failure": failures.get(name),
                        "attempts": attempts.get(name, 0), **(inputs or {})},
                resume=resume_call, resources=("execution_workspace",),
                evidence={"failures": audit_dir / "execution_tool_failures.json",
                          "environment_report": output_dir / "03a_environment_report.json",
                          "foundation_manifest": output_dir / "foundation_manifest.json",
                          "foundation_validation": audit_dir / "03b_foundation_validation.json"}))
        if case_runtime is None or pending_environment is not None:
            add("environment", "准备或扩展所需环境。已有环境不变也是事实，不自动判定无法恢复。",
                prepare_environment, {"requested": [str(item.requirement) for item in
                    (pending_environment.requests if pending_environment else [])]})
        if pending_revision is not None and foundation is not None:
            add("retain_foundation", "保留当前共享版本，撤回本次未成功的修订及其额外依赖，记录未解决原因。",
                preserve_revision, {"revision": pending_revision})
        if case_runtime is not None and pending_environment is None:
            if not foundation_ready or pending_revision is not None:
                add("foundation", "创建或修订共享实现。失败后的修复原因与做法由本次指令指定。",
                    build_foundation, {"revision": pending_revision,
                                       "environment_hash": case_runtime.environment_hash})
            if foundation_ready and pending_revision is None:
                add("writers", "启动/恢复独立 Writer 与 Reporter，按有效收据复用成果；不会为了组装重复 full。",
                    run_writers, {"foundation_hash": (foundation or {}).get("snapshot_hash"),
                                  "environment_hash": case_runtime.environment_hash,
                                  "forced_task_ids": sorted(forced_task_ids)})
        if agentic_result is not None:
            add("deliver_partial", "停止新增计算，保留本次已有任务结果与原始审查，交给报告工具说明未完成事项。",
                lambda decision: decision)
        return offered

    def received(name, value):
        nonlocal case_runtime, foundation, foundation_ready, pending_environment, pending_revision
        nonlocal agentic_result, complete, forced_task_ids
        nonlocal environment_extension_count
        failures.pop(name, None)
        if name == "environment":
            if pending_environment is not None:
                refresh_only = pending_environment.source == "shared_runtime_refresh"
                environment_extension_count += 1
                environment_requests.extend(pending_environment.requests)
                failures.pop("writers" if pending_environment.source in {"task_writers", "shared_runtime_refresh"} else "foundation", None)
                if refresh_only:
                    # The partial Writer records remain on disk; the next
                    # dispatch resumes them against the refreshed runtime.
                    agentic_result = None
                write_json(audit_dir / "03a_environment_extensions.json", {
                    "extension_count": environment_extension_count,
                    "latest_source": pending_environment.source,
                    "requirements": [item.requirement for item in environment_requests],
                    "environment_lock_hash": value.environment_hash,
                    "previous_environment_lock_hash": case_runtime.environment_hash if case_runtime else None,
                    "environment_changed": case_runtime is None or value.environment_hash != case_runtime.environment_hash})
            pending_environment = None
            pending_path = audit_dir / _PENDING_ENVIRONMENT_PATH
            if pending_path.is_file():
                write_json(pending_path, {
                    "schema_version": 1,
                    "state": "resolved",
                    "source": "host_environment",
                    "environment_hash": value.environment_hash,
                })
            case_runtime = value
        elif name == "foundation":
            foundation = value
            foundation_ready = True
            if pending_revision is not None:
                write_json(audit_dir / "03b_foundation_revision_applied.json", pending_revision)
                request_path = Path(pending_revision["evidence_root"]) / "foundation_revision_request.json"
                # The immutable incident and revision history retain the request.
                request_path.unlink(missing_ok=True)
            pending_revision = None
            failures.pop("writers", None)
        elif name == "retain_foundation":
            declined_revision_ids.add(value)
            pending_revision = None
            forced_task_ids = set()
            foundation_ready = True
            if pending_environment is not None and pending_environment.source == "foundation_writer":
                pending_environment = None
                failures.pop("environment", None)
            failures.pop("foundation", None)
            failures.pop("writers", None)
        elif name == "writers":
            agentic_result, complete = value, True
        elif name == "deliver_partial":
            complete = True

    def failed(name, exc):
        nonlocal pending_environment, pending_revision, forced_task_ids, agentic_result
        failure = {"tool": name, "node_id": name, "error": f"{type(exc).__name__}: {exc}",
                   "category": getattr(exc, "category", type(exc).__name__),
                   "decision": getattr(exc, "decision", None), "report": getattr(exc, "report", None)}
        failures[name] = failure
        if isinstance(exc, EnvironmentRequestRequired):
            pending_environment = exc
            _write_pending_environment(audit_dir, exc)
            if isinstance(exc.partial_result, dict):
                agentic_result = exc.partial_result
        if isinstance(exc, FoundationRevisionRequired):
            pending_revision = exc.request
            forced_task_ids = set(pending_revision.get("affected_task_ids") or [])
            if isinstance(getattr(exc, "partial_result", None), dict):
                agentic_result = exc.partial_result
        if name == "environment":
            failure["node_id"] = "environment:extension" if case_runtime is not None else "environment"
            write_json(audit_dir / "03a_environment_blocked.json", {
                **failure, "decision": "awaiting_supervisor", "pipeline_can_continue": agentic_result is not None,
                "preserved_task_ids": [r.get("task_id") for r in (agentic_result or {}).get("task_records", [])]})
        if name == "foundation" and pending_revision is not None:
            write_json(audit_dir / "03b_foundation_revision_failures" / str(pending_revision["request_id"])
                       / f"attempt_{attempts.get('foundation', 0):03d}.json",
                       {**failure, "request": pending_revision, "preserved_snapshot": (foundation or {}).get("snapshot_hash")})
        write_json(audit_dir / "execution_tool_failures.json", failures)

    def routine_route(snapshot: dict) -> dict | None:
        # The normal prerequisite chain is mechanical. A failed capability,
        # dependency request, or shared revision still needs moderator routing.
        if pending_environment is not None and pending_environment.source == "shared_runtime_refresh":
            if "environment" in snapshot["ready"]:
                return {"action": "start", "next_nodes": ["environment"], "status": "routine",
                        "diagnosis": "Shared Python changed; refresh the case environment before resuming Writers."}
        if failures or pending_revision is not None:
            return None
        ready = set(snapshot["ready"])
        if complete:
            return {"action": "finish", "status": "routine",
                    "diagnosis": "Execution handoffs have been recorded."}
        if case_runtime is None:
            next_name = "environment"
        elif not foundation_ready:
            next_name = "foundation"
        elif agentic_result is None:
            next_name = "writers"
        else:
            return None
        if next_name not in ready:
            return None
        return {"action": "start", "next_nodes": [next_name], "status": "routine",
                "diagnosis": f"Execution prerequisites are ready for {next_name}."}

    with supervisor_scope(supervisor):
        decision = supervisor.tools.run("execution", available,
            state=lambda: {"failures": failures, "pending_revision": pending_revision,
                "pending_environment": str(pending_environment) if pending_environment else None,
                "environment_ready": case_runtime is not None, "foundation_ready": foundation_ready,
                "preserved_task_ids": [r.get("task_id") for r in (agentic_result or {}).get("task_records", [])],
                "execution_complete": complete}, on_result=received, on_error=failed,
            routine_selector=routine_route)
    if agentic_result is None:
        raise StageBlocked("execution", decision)
    if not complete or pending_environment is not None or pending_revision is not None:
        runtime = agentic_result["runtime_result"]
        runtime["delivery_status"] = "partial"
        runtime.setdefault("engineering_failures", []).extend(failures.values())
        agentic_result["delivery_blocked"] = {"decision": decision, "failures": failures}
        write_json(output_dir / "runtime_result.json", runtime)

    manifest = agentic_result["manifest"]
    written_files = [
        Path(path) for path in agentic_result.get("written_files", [])
    ]
    runtime_result = agentic_result["runtime_result"]
    if isinstance(runtime_result.get("validation"), dict):
        validation = runtime_result["validation"]
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
        "enabled": False,
        "passed": None,
        "mode": "task_writer_task_reporter_loops",
        "decision_authority": "reporter_and_supervisor",
    }
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
