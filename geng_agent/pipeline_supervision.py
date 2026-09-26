"""Run-level scheduling and honest partial delivery around the business nodes."""
from __future__ import annotations

from dataclasses import replace, is_dataclass
from copy import copy
from typing import Any, Callable

from .outputs import write_json
from .pipeline_models import ExecutionFlowResult, PipelineResult
from .security import redact_text
from .supervisor import RunSupervisor, StageBlocked, supervisor_scope
from .supervisor_tools import SupervisorTool
from .supervisor_repairs import register_delivery_repairs, write_delivery_index
from .writer_environment import cleanup_completed_writer_environments


def _failure(phase: str, exc: Exception) -> dict:
    return {"phase": phase, "node_id": getattr(exc, "node_id", phase), "decision": getattr(exc, "decision", None),
            "error": redact_text(str(exc)), "scientific_verdict": "not_assigned_by_host"}


def _blocked_execution(context, analysis, failure: dict) -> ExecutionFlowResult:
    # No disk record is presumed current merely because it lives in this case.
    # The execution flow itself retains verified in-memory records on packaging
    # failures; this fallback handles a failure before those records are returned.
    records = [{"index": index, "task_id": task.get("task_id"), "writer_completed": False,
                "task_writer_status": "blocked", "blocked_reason": failure["error"],
                "writer_error_kind": "stage_blocked", "host_execution": {"passed": False,
                    "issues": ["This run did not hand off validated execution evidence."]}}
               for index, task in enumerate(analysis.tasks.get("repro_tasks", []), 1)]
    runtime = {"enabled": context.options.run_repro, "passed": False,
               "delivery_status": "blocked", "engineering_failures": [failure],
               "tasks_total": len(records), "tasks_passed": 0,
               "coverage": f"0/{len(records)}", "per_task": records}
    write_json(context.output_dir / "runtime_result.json", runtime)
    return ExecutionFlowResult(validation={"required_files_present": False}, scientific_check={},
        agentic_result={"status": {"stop_class": "engineering_blocked"}}, manifest={},
        written_files=[], runtime_result=runtime, task_records=records,
        writer_review_document={}, writer_summary_result={}, risk_report={})


def run_supervised_pipeline(*, context, supervisor: RunSupervisor,
                            analyze: Callable[[], Any], execute: Callable[[Any], Any],
                            report: Callable[[Any, Any], Any],
                            finish_analysis: Callable[[Any], Any]) -> PipelineResult:
    completed: list[str] = []
    blocked: dict[str, dict] = {}
    analysis = execution = result = None
    phase_attempts: dict[str, int] = {}

    def resume_options():
        if is_dataclass(context.options):
            return replace(context.options, resume=True)
        value = copy(context.options)
        value.resume = True
        return value

    def invoke(name, operation, decision):
        phase_attempts[name] = phase_attempts.get(name, 0) + 1
        supervisor.record_phase(name, "running")
        supervisor._instructions["phase:" + name] = decision
        if name == "revise_plan":
            supervisor.assign("revision:experiment_planning", decision)
        elif name == "revise_understanding":
            supervisor.assign("revision:paper_understanding", decision)
        original = context.options
        if phase_attempts[name] > 1 or name.startswith("revise_"):
            context.options = resume_options()
        try:
            return operation()
        finally:
            context.options = original

    def offer():
        capabilities = []
        evidence = {}
        if analysis is not None:
            for stem in ("engineering_facts", "repro_tasks", "scientific_architecture", "paper_thesis"):
                path = context.output_dir / (stem + ".json")
                if path.is_file():
                    evidence[stem] = path
        if execution is not None:
            path = context.output_dir / "runtime_result.json"
            if path.is_file():
                evidence["runtime_result"] = path
        if result is not None:
            for stem in ("result_review", "reproduction_report"):
                path = context.output_dir / (stem + ".md")
                if path.is_file():
                    evidence[stem] = path
        def add(name, description, operation):
            call = lambda decision: invoke(name, operation, decision)
            def resume_call(decision):
                original = context.options
                context.options = resume_options()
                try:
                    return call(decision)
                finally:
                    context.options = original
            capabilities.append(SupervisorTool(name, description, call,
                inputs={"failure": blocked.get(name), "run_id": context.run_id},
                resume=resume_call, evidence=evidence, resources=("case_phase",)))
        if analysis is None:
            add("analysis", "解析、理解论文并规划；子工具按主持人决定补查或定稿。恢复时核对原角色缓存。", analyze)
        if analysis is not None:
            add("revise_plan", "仅在发现规划错误时交回 Planner；不以未复现为理由改目标。修订后下游重新对账。", analyze)
            add("revise_understanding", "仅在原文理解存在具体错误时回到理解角色；旧执行记录保留，按新材料重新对账。", analyze)
            if context.options.analysis_only:
                if result is None:
                    add("analysis_delivery", "交付已有分析材料；用户只授权分析。", lambda: finish_analysis(analysis))
            else:
                execution_incomplete = getattr(execution, "runtime_result", {}).get("delivery_status") in {"partial", "blocked"}
                if "execution" not in completed or execution_incomplete:
                    add("execution", "调用环境、并行 Writer/Reporter 与组装工具；保留已有执行证据。", lambda: execute(analysis))
                if execution is not None and (result is None or not getattr(result, "result_review_passed", False)):
                    add("reports", "用本次已有证据生成报告，可交付未复现或工程受阻的结果。", lambda: report(analysis, execution))
        return capabilities

    def received(name, value):
        nonlocal analysis, execution, result
        if name in {"analysis", "revise_plan", "revise_understanding"}:
            analysis = value
            if name != "analysis":
                execution = result = None
                completed[:] = [item for item in completed if item not in {"execution", "reports"}]
                blocked.clear()
        elif name == "execution":
            execution = value
            result = None
            completed[:] = [item for item in completed if item != "reports"]
            register_delivery_repairs(supervisor, context, getattr(value, "task_records", []))
        else:
            result = value
        blocked.pop(name, None)
        if name not in completed:
            completed.append(name)
        supervisor.record_phase(name, "completed")

    def failed(name, exc):
        nonlocal execution
        failure = _failure(name, exc)
        blocked[name] = failure
        supervisor.record_phase(name, "blocked", error=failure)
        if name == "execution" and execution is None:
            execution = _blocked_execution(context, analysis, failure)
            register_delivery_repairs(supervisor, context, [])

    def routine_route(snapshot: dict) -> dict | None:
        # Stage order follows actual data availability. Replanning, failed
        # stages, and unsuccessful report delivery remain moderator decisions.
        if blocked:
            return None
        ready = set(snapshot["ready"])
        if analysis is None:
            next_name = "analysis"
        elif context.options.analysis_only:
            next_name = "analysis_delivery" if result is None else ""
        elif execution is None:
            next_name = "execution"
        elif result is None:
            next_name = "reports"
        elif result.result_review_passed is True:
            next_name = ""
        else:
            return None
        if next_name:
            if next_name not in ready:
                return None
            return {"action": "start", "next_nodes": [next_name], "status": "routine",
                    "diagnosis": f"Required inputs are ready for {next_name}."}
        return {"action": "finish", "status": "routine",
                "diagnosis": "Available work and report delivery are recorded."}

    with supervisor_scope(supervisor):
        try:
            final_decision = supervisor.tools.run("project", offer,
                state=lambda: {"completed": completed, "failures": blocked,
                               "analysis_available": analysis is not None,
                               "execution_available": execution is not None,
                               "delivery_available": result is not None,
                               "execution_delivery_status": getattr(execution, "runtime_result", {}).get("delivery_status"),
                               "reports_accepted": getattr(result, "result_review_passed", None),
                               "delivery_status": getattr(result, "delivery_status", None)},
                on_result=received, on_error=failed, routine_selector=routine_route)
        except StageBlocked as exc:
            failed("dispatch", exc)
            final_decision = exc.decision

    status = "complete"
    if blocked or result is None:
        status = "partial" if result is not None or execution is not None else "blocked"
    if execution is not None and getattr(execution, "runtime_result", {}).get("delivery_status") in {"partial", "blocked"}:
        status = "partial"
    if isinstance(result, PipelineResult) and result.delivery_status in {"partial", "blocked"}:
        status = result.delivery_status
    if result is None:
        result = PipelineResult(output_dir=context.output_dir,
            review_path=context.output_dir / "review.md", repro_project_dir=context.output_dir / "repro_project",
            risk_report_path=context.output_dir / "risk_report.json")
    if isinstance(result, PipelineResult):
        result = replace(result, delivery_status=status,
                         supervision_path=context.audit_dir / "supervisor" / "outcome.json")
    outcome = {"run_id": context.run_id, "goal_id": supervisor.goal_id,
               "final_decision": final_decision,
               "delivery_status": status, "completed_phases": completed, "blocked_phases": blocked,
               "analysis_only": context.options.analysis_only,
               "reports_accepted": isinstance(result, PipelineResult) and result.result_review_passed is True,
               "accepted_docx_paths": [path.name for path in (
                   getattr(result, "result_review_docx_path", None),
                   getattr(result, "reproduction_report_docx_path", None)) if path is not None],
               "runtime_recorded": execution is not None,
               "project_accepted": bool(getattr(execution, "validation", {}).get("required_files_present"))
                   and getattr(execution, "runtime_result", {}).get("delivery_status") not in {"partial", "blocked"}}
    write_json(context.audit_dir / "supervisor" / "outcome.json", outcome)
    write_delivery_index(context.output_dir, outcome)
    if (status == "complete" and execution is not None and not context.options.analysis_only
            and isinstance(result, PipelineResult) and result.result_review_passed is True):
        try:
            cleanup = cleanup_completed_writer_environments(
                output_dir=context.output_dir, task_records=execution.task_records,
            )
        except Exception as exc:
            cleanup = {"removed": [], "skipped": [f"{type(exc).__name__}: {exc}"]}
        write_json(context.audit_dir / "03c_writer_environment_cleanup.json", cleanup)
    return result
