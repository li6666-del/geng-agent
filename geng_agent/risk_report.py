from __future__ import annotations

import re
from pathlib import Path
from typing import Any

"""Reproducibility-risk scoring: the multi-dimensional risk report, scientific/semantic
checks on the task list, nondeterminism detection, and per-stage run-cost ledger."""


def build_scientific_check(tasks: dict[str, Any]) -> dict[str, Any]:
    issues: list[dict[str, str]] = []
    repro_tasks = tasks.get("repro_tasks", [])
    if not isinstance(repro_tasks, list):
        return {"ok": False, "issues": [{"path": "$.repro_tasks", "message": "repro_tasks is not a list"}]}

    for index, task in enumerate(repro_tasks):
        if not isinstance(task, dict):
            continue
        base = f"$.repro_tasks[{index}]"
        comparison = task.get("comparison")
        if isinstance(comparison, dict):
            baselines = comparison.get("baselines")
            if baselines in (None, [], ""):
                issues.append({"path": f"{base}.comparison.baselines", "message": "baseline is missing or empty"})
        expected_trend = task.get("expected_trend")
        if isinstance(expected_trend, dict) and not expected_trend:
            issues.append({"path": f"{base}.expected_trend", "message": "expected trend is empty"})
        output_columns = task.get("output_columns")
        if isinstance(output_columns, list) and "metric_value" in output_columns:
            issues.append({"path": f"{base}.output_columns", "message": "generic metric_value should be replaced by concrete CSV columns"})
        metric = str(task.get("metric", "")).lower()
        formula = str(task.get("metric_formula", "")).lower()
        if metric and metric not in formula and metric not in {"other"}:
            issues.append({"path": f"{base}.metric_formula", "message": "metric formula should explicitly name the metric"})

    return {"ok": not issues, "issues": issues}


_RANDOM_USE = re.compile(
    r"\b(?:np\.random|numpy\.random|default_rng|RandomState|SeedSequence)\b"
    r"|\brandom\.(?:random|randint|randrange|choice|choices|sample|shuffle|normal|uniform|gauss|seed)\b"
)
_SEED_SET = re.compile(
    r"np\.random\.seed\s*\(|numpy\.random\.seed\s*\(|\brandom\.seed\s*\("
    r"|default_rng\s*\(\s*[^)\s]|RandomState\s*\(\s*[^)\s]|SeedSequence\s*\(\s*[^)\s]"
    r"|set_seed|manual_seed"
)


def detect_nondeterminism_findings(repro_project_dir: Path) -> list[dict[str, Any]]:
    """Advisory (non-blocking) check: flag a generated project that draws random numbers
    but never sets a seed anywhere, because its results would not be reproducible
    run-to-run. Project-level (not per-file) to avoid false positives when the seed lives
    in a shared setup module — only emits when randomness is used and NO seed is detected."""
    try:
        py_files = sorted(repro_project_dir.rglob("*.py"))
    except OSError:
        return []
    uses_random = False
    sets_seed = False
    random_files: list[str] = []
    for path in py_files:
        if "__pycache__" in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if _RANDOM_USE.search(text):
            uses_random = True
            try:
                random_files.append(path.relative_to(repro_project_dir).as_posix())
            except ValueError:
                random_files.append(path.name)
        if _SEED_SET.search(text):
            sets_seed = True
    if uses_random and not sets_seed:
        return [
            {
                "type": "nondeterministic_randomness",
                "message": "生成项目使用了随机数但全项目未检测到固定随机种子，复现结果可能每次运行不同，难以与论文数值对照；建议在每个实验入口设置 numpy/random 种子并写入产物。",
                "files": random_files,
            }
        ]
    return []


def _build_run_cost(
    marks: list[dict[str, Any]],
    *,
    total_wall_s: float,
    by_model: dict[str, dict[str, int]],
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
    return {
        "wall_clock_s": total_wall_s,
        "totals": {key: int(totals.get(key, 0)) for key in keys},
        "by_stage": by_stage,
        "by_model": by_model,
        "note": "墙钟为各阶段实测耗时；token 由 LLM API 的 usage 字段汇总，缺失字段按 0 计（部分服务商不返回 usage，此时 token 显示为 0 但调用次数仍准确）。",
    }


def build_risk_report(
    facts: dict[str, Any],
    tasks: dict[str, Any],
    validation: dict[str, Any],
    runtime_result: dict[str, Any] | None = None,
    scientific_check: dict[str, Any] | None = None,
    manifest_meta: dict[str, Any] | None = None,
    result_review_result: dict[str, Any] | None = None,
    paper_format: str | None = None,
) -> dict[str, Any]:
    missing = facts.get("missing_information", [])
    repro_tasks = tasks.get("repro_tasks", [])
    assumptions = []
    for task in repro_tasks if isinstance(repro_tasks, list) else []:
        if isinstance(task, dict) and isinstance(task.get("assumptions"), list):
            assumptions.extend(task["assumptions"])

    findings: list[dict[str, Any]] = []

    # === 自动注入已知局限（最高优先改进项）===
    # PDF text extraction 必然丢失图片/图表视觉信息。这是通信论文复现中最常见的根本性限制。
    if paper_format == "pdf":
        findings.append({
            "type": "pdf_images_lost",
            "message": "PDF 转 text chunks 时，嵌入的图片、图表、坐标轴、星座图等视觉信息已丢失。source_kind=figure 的事实仅能通过后续多模态页面 PNG 部分补偿（① facts 抽取、③a 计划、④ 结果审查阶段）。逐文件代码生成等阶段看不到图片内容，强烈建议人工对照原始 PDF 核对所有图相关结论。",
            "severity": "high",
            "mitigation": "多模态模型在支持阶段会收到页面渲染 PNG；非 PDF 或无图论文不受此影响。",
            "always_injected_for_pdfs": True,
        })

    if missing:
        findings.append({"type": "missing_information", "message": "论文存在复现需要但未明确说明的信息。", "count": len(missing)})
    if assumptions:
        findings.append({"type": "assumptions_required", "message": "复现任务需要使用假设参数。", "count": len(assumptions)})
    if not validation.get("required_files_present"):
        findings.append({"type": "generated_project_incomplete", "message": "LLM 生成的复现项目缺少必要文件。", "missing_files": validation.get("missing_files", [])})
    if not validation.get("python_compiles"):
        findings.append({"type": "generated_code_compile_error", "message": "LLM 生成的 Python 文件存在语法错误。", "compile_errors": validation.get("compile_errors", [])})
    if runtime_result and runtime_result.get("enabled") and not runtime_result.get("passed"):
        findings.append(
            {
                "type": "generated_project_runtime_failed",
                "message": "生成的复现项目自动运行失败，已记录结构化日志。",
                "repair_attempts_used": runtime_result.get("repair_attempts_used"),
                "logs_dir": runtime_result.get("logs_dir"),
                "pipeline_error": runtime_result.get("pipeline_error"),
            }
        )
    partial_success = runtime_result.get("partial_success") if isinstance(runtime_result, dict) else None
    if isinstance(partial_success, dict) and partial_success.get("has_partial_output"):
        findings.append(
            {
                "type": "generated_project_partial_success",
                "message": "生成项目未整体通过，但产出了部分有效结果；已保留生成项目而非退模板，失败的实验需人工核对。",
                "valid_csv_files": partial_success.get("valid_csv_files", []),
            }
        )
    if manifest_meta and manifest_meta.get("loose_recovery_used"):
        findings.append({"type": "loose_json_recovery_used", "message": "文件 manifest 曾使用宽松恢复，代码内容需要人工抽查。"})
    if manifest_meta and manifest_meta.get("template_fallback_used"):
        findings.append(
            {
                "type": "template_fallback_used",
                "message": "自由生成复现项目不稳定，本次使用了本地确定性模板兜底。",
                "reason": manifest_meta.get("template_fallback_reason"),
                "template": manifest_meta.get("template_name"),
            }
        )
    stage_fallbacks = _local_stage_fallbacks(facts, tasks)
    if stage_fallbacks:
        findings.append(
            {
                "type": "local_stage_fallback_used",
                "message": "部分 LLM 结构化阶段失败，本次使用了本地启发式兜底，事实和任务需要人工复核。",
                "stages": stage_fallbacks,
            }
        )
    facts_meta = facts.get("_meta") if isinstance(facts, dict) else None
    if isinstance(facts_meta, dict):
        if facts_meta.get("truncation_recovered"):
            findings.append(
                {
                    "type": "facts_truncation_recovered",
                    "message": "工程事实抽取输出疑似被截断，已从可解析前缀抢救事实，完整性需人工抽查。",
                    "recovered_fact_count": facts_meta.get("recovered_fact_count"),
                }
            )
        if facts_meta.get("partial_acceptance_used"):
            findings.append(
                {
                    "type": "facts_partial_acceptance_used",
                    "message": "部分工程事实未通过本地校验已被丢弃，仅保留可追溯的事实。",
                    "dropped_fact_count": facts_meta.get("dropped_fact_count"),
                }
            )
        if facts_meta.get("normalization_used"):
            findings.append(
                {
                    "type": "facts_normalized",
                    "message": "工程事实存在枚举/字段近义偏差，已本地归一化，建议人工抽查事实标签。",
                    "coercion_count": facts_meta.get("coercion_count"),
                }
            )
    if scientific_check and scientific_check.get("issues"):
        findings.append({"type": "scientific_check_issues", "message": "复现任务缺少部分通信实验语义约束。", "count": len(scientific_check["issues"])})
    if result_review_result and result_review_result.get("enabled") and not result_review_result.get("passed"):
        findings.append({"type": "result_review_failed", "message": "结果级多模态审查未完成。", "error": result_review_result.get("error")})

    dimensions = build_risk_dimensions(
        missing=missing if isinstance(missing, list) else [],
        assumptions=assumptions,
        validation=validation,
        runtime_result=runtime_result or {},
        scientific_check=scientific_check or {},
        tasks=tasks,
        result_review_result=result_review_result or {},
        manifest_meta=manifest_meta or {},
        stage_fallback_used=bool(stage_fallbacks),
    )
    return {
        "risk_level": combine_risk_dimensions(dimensions),
        "judgement_style": "reproducibility_risk_only",
        "risk_dimensions": dimensions,
        "missing_information_count": len(missing) if isinstance(missing, list) else 0,
        "assumptions_count": len(assumptions),
        "findings": findings,
        "scientific_check": scientific_check or {},
        "result_review": result_review_result or {},
        "note": "风险等级只表示工程复现风险，不等同于造假结论；smoke 通过也不等于论文完全复现成功。",
    }


def build_risk_dimensions(
    missing: list[Any],
    assumptions: list[Any],
    validation: dict[str, Any],
    runtime_result: dict[str, Any],
    scientific_check: dict[str, Any],
    tasks: dict[str, Any],
    result_review_result: dict[str, Any],
    manifest_meta: dict[str, Any],
    stage_fallback_used: bool = False,
) -> dict[str, dict[str, Any]]:
    runtime_enabled = bool(runtime_result.get("enabled"))
    runtime_passed = runtime_result.get("passed")
    scientific_issues = scientific_check.get("issues", []) if isinstance(scientific_check, dict) else []
    security_issues = runtime_result.get("security_issues", []) if isinstance(runtime_result, dict) else []
    requirement_issues = runtime_result.get("requirements_issues", []) if isinstance(runtime_result, dict) else []
    result_review_enabled = bool(result_review_result.get("enabled"))
    result_review_passed = result_review_result.get("passed")
    template_fallback_used = bool(manifest_meta.get("template_fallback_used"))

    return {
        "information_completeness": _dimension(
            "high" if stage_fallback_used or len(missing) >= 5 else "medium" if missing or assumptions else "low",
            [f"missing_information={len(missing)}", f"assumptions={len(assumptions)}", f"stage_fallback_used={stage_fallback_used}"],
        ),
        "implementation_fidelity": _dimension(
            "high"
            if not validation.get("required_files_present") or not validation.get("python_compiles") or template_fallback_used
            else "medium"
            if scientific_issues
            else "low",
            [
                f"required_files_present={validation.get('required_files_present')}",
                f"python_compiles={validation.get('python_compiles')}",
                f"scientific_issues={len(scientific_issues)}",
                f"template_fallback_used={template_fallback_used}",
            ],
        ),
        "runtime_reliability": _dimension(
            "medium" if not runtime_enabled else "low" if runtime_passed else "high",
            [
                f"runtime_enabled={runtime_enabled}",
                f"runtime_passed={runtime_passed}",
                f"attempts={len(runtime_result.get('attempts', [])) if isinstance(runtime_result.get('attempts'), list) else 0}",
            ],
        ),
        "result_alignment": _dimension(
            _result_alignment_level(
                runtime_enabled,
                runtime_passed,
                result_review_enabled,
                result_review_passed,
                scientific_issues,
                template_fallback_used,
            ),
            [
                f"result_review_enabled={result_review_enabled}",
                f"result_review_passed={result_review_passed}",
                f"scientific_issues={len(scientific_issues)}",
                f"template_fallback_used={template_fallback_used}",
            ],
        ),
        "baseline_fairness": _dimension("high" if _count_missing_baselines(tasks) else "low", [f"tasks_without_baseline={_count_missing_baselines(tasks)}"]),
        "security_isolation": _dimension(
            "high" if security_issues or requirement_issues else "medium" if runtime_enabled else "low",
            [
                f"security_issues={len(security_issues)}",
                f"requirements_issues={len(requirement_issues)}",
                f"host_execution_requested={runtime_enabled}",
            ],
        ),
    }


def _local_stage_fallbacks(facts: dict[str, Any], tasks: dict[str, Any]) -> list[dict[str, Any]]:
    fallbacks: list[dict[str, Any]] = []
    for label, data in (("engineering_facts", facts), ("repro_tasks", tasks)):
        meta = data.get("_meta") if isinstance(data, dict) else None
        if isinstance(meta, dict) and meta.get("local_fallback_used"):
            fallbacks.append(
                {
                    "stage": label,
                    "fallback_name": meta.get("fallback_name"),
                    "reason": meta.get("fallback_reason"),
                }
            )
    return fallbacks


def _result_alignment_level(
    runtime_enabled: bool,
    runtime_passed: Any,
    result_review_enabled: bool,
    result_review_passed: Any,
    scientific_issues: list[Any],
    template_fallback_used: bool = False,
) -> str:
    if template_fallback_used:
        # A generic template was run, not the paper's method: its outputs cannot align
        # with the paper, regardless of whether the template itself executed cleanly.
        return "high"
    if not runtime_enabled:
        return "medium"
    if runtime_enabled and not runtime_passed:
        return "high"
    if result_review_enabled and not result_review_passed:
        return "high"
    if scientific_issues:
        return "medium"
    return "low"


def _dimension(level: str, evidence: list[str]) -> dict[str, Any]:
    return {"level": level, "evidence": evidence}


def combine_risk_dimensions(dimensions: dict[str, dict[str, Any]]) -> str:
    levels = [dimension.get("level") for dimension in dimensions.values()]
    if "high" in levels:
        return "high"
    if "medium" in levels:
        return "medium"
    return "low"


def _count_missing_baselines(tasks: dict[str, Any]) -> int:
    count = 0
    repro_tasks = tasks.get("repro_tasks", [])
    if not isinstance(repro_tasks, list):
        return 0
    for task in repro_tasks:
        if not isinstance(task, dict):
            continue
        comparison = task.get("comparison")
        if not isinstance(comparison, dict) or comparison.get("baselines") in (None, [], ""):
            count += 1
    return count
