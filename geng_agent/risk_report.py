"""Execution observations, preserved Agent assessments and measured cost."""
from __future__ import annotations
from pathlib import Path
from typing import Any


def _build_run_cost(
    marks: list[dict[str, Any]],
    *,
    total_wall_s: float,
    by_model: dict[str, dict[str, int]],
    audit_dir: Path | None = None,
    codex_since: float | None = None,
) -> dict[str, Any]:
    """Turn the cumulative stage marks into a per-stage cost ledger (time + tokens)."""
    keys = ("llm_calls", "prompt_tokens", "completion_tokens", "total_tokens")
    by_stage: list[dict[str, Any]] = []
    for prev, cur in zip(marks, marks[1:]):
        entry: dict[str, Any] = {
            "stage": cur.get("stage"),
            "seconds": round(float(cur.get("elapsed_s", 0)) - float(prev.get("elapsed_s", 0)), 3),
        }
        for key in keys:
            entry[key] = int(cur.get(key, 0)) - int(prev.get(key, 0))
        by_stage.append(entry)
    totals = marks[-1] if marks else {}
    result = {
        "wall_clock_s": total_wall_s,
        "totals": {key: max(0, int(totals.get(key, 0)) - int((marks[0] if len(marks) > 1 else {}).get(key, 0))) for key in keys},
        "by_stage": by_stage,
        "by_model": by_model,
        "note": "墙钟为本次调用实测耗时；Codex token 来自已完成 turn 的 usage，缺失时为 null；累计成本保留此前调用。",
    }
    if audit_dir is not None:
        import time
        from .codex_cost import summarize_codex_usage, summarize_execution_time
        delta = summarize_codex_usage(Path(audit_dir), since=codex_since if codex_since is not None else time.time() - total_wall_s)
        cumulative = summarize_codex_usage(Path(audit_dir))
        result["codex"] = {"delta": delta, "cumulative": cumulative}
        result["observed_execution"] = summarize_execution_time(Path(audit_dir), since=codex_since)
        origin = codex_since if codex_since is not None else time.time() - total_wall_s
        for entry, prev, cur in zip(by_stage, marks, marks[1:]):
            entry["llm_api_usage"] = {key: entry[key] for key in keys}
            usage = summarize_codex_usage(Path(audit_dir), since=origin,
                completed_after=origin + float(prev.get("elapsed_s", 0)),
                completed_before=origin + float(cur.get("elapsed_s", 0)))
            entry["codex_usage"] = usage
            entry["attribution"] = "Codex calls completing within this stage interval; concurrent/nested sessions remain individually listed"
            for key in keys:
                entry[key] = entry[key] + usage[key] if usage.get(key) is not None else None
        result["llm_api_totals"] = dict(result["totals"])
        for key in keys:
            value = delta.get(key)
            result["totals"][key] = result["totals"][key] + value if value is not None else None
    return result


def build_risk_report(
    facts: dict[str, Any], tasks: dict[str, Any], validation: dict[str, Any],
    runtime_result: dict[str, Any] | None = None,
    scientific_check: dict[str, Any] | None = None,
    result_review_result: dict[str, Any] | None = None,
    paper_format: str | None = None,
) -> dict[str, Any]:
    """Collect attributed records without assigning a second scientific verdict."""
    runtime = runtime_result if isinstance(runtime_result, dict) else {}
    review = result_review_result if isinstance(result_review_result, dict) else {}
    missing = facts.get("missing_information", [])
    assumptions, task_evidence_gaps = [], []
    entries = tasks.get("repro_tasks")
    for task in entries if isinstance(entries, list) else []:
        if not isinstance(task, dict):
            continue
        declared = task.get("assumptions")
        if isinstance(declared, list):
            assumptions.extend(declared)
        requests = task.get("missing_fact_requests")
        for request in requests if isinstance(requests, list) else []:
            task_evidence_gaps.append({"task_id": task.get("task_id"), "source": "missing_fact_requests", "record": request})
        acceptance = task.get("scientific_acceptance")
        gaps = acceptance.get("information_gaps") if isinstance(acceptance, dict) else None
        for gap in gaps if isinstance(gaps, list) else []:
            task_evidence_gaps.append({"task_id": task.get("task_id"), "source": "scientific_acceptance.information_gaps", "record": gap})

    findings = []
    # None means not checked. Only an explicitly recorded mechanical failure
    # may become an execution finding; no source-code regex or semantic rating.
    if validation.get("required_files_present") is False:
        findings.append({"type": "generated_project_incomplete", "message": "文件清单检查记录了未找到的声明文件。",
                         "missing_files": validation.get("missing_files", [])})
    if validation.get("python_compiles") is False:
        findings.append({"type": "generated_code_compile_error", "message": "已有编译记录报告失败。",
                         "compile_errors": validation.get("compile_errors", [])})
    if runtime.get("enabled") and runtime.get("passed") is False:
        findings.append({"type": "generated_project_runtime_failed", "message": "本次执行记录未全部有效，具体原因保留在任务记录中。",
                         "coverage": runtime.get("coverage"), "per_task": runtime.get("per_task", [])})
    warnings = runtime.get("requirements_warnings")
    if isinstance(warnings, list) and warnings:
        findings.append({"type": "dependency_warnings", "message": "运行阶段保留的依赖观测。",
                         "count": len(warnings), "items": warnings})
    partial = runtime.get("partial_success")
    if isinstance(partial, dict) and partial.get("has_partial_output"):
        findings.append({"type": "generated_project_partial_success", "message": "运行阶段保留了部分任务产物。", "record": partial})
    return {
        "risk_level": None, "engineering_risk_level": None, "scientific_risk_level": None,
        "judgement_style": "agent_decisions_with_host_records", "risk_dimensions": {},
        "missing_information_count": len(missing) if isinstance(missing, list) else 0,
        "task_evidence_gap_count": len(task_evidence_gaps), "assumptions_count": len(assumptions),
        "analysis_records": {"missing_information": missing, "planned_assumptions": assumptions,
                             "task_evidence_gaps": task_evidence_gaps,
                             "facts_metadata": facts.get("_meta", {}), "tasks_metadata": tasks.get("_meta", {})},
        "paper_format": paper_format, "validation": validation, "runtime_result": runtime,
        "findings": findings, "scientific_check": scientific_check or {}, "result_review": review,
        "note": "宿主只汇总实际记录，不判定实现忠实度、数值一致性、信息缺口的重要性或风险等级；科学解释来自 Reporter 与最终编辑智能体。",
    }
