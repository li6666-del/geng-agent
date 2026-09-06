from __future__ import annotations

import re
from pathlib import Path
from typing import Any

"""Reproducibility-risk scoring: the multi-dimensional risk report, scientific/semantic
checks on the task list, nondeterminism detection, and per-stage run-cost ledger."""


def build_scientific_check(tasks: dict[str, Any]) -> dict[str, Any]:
    warnings: list[dict[str, str]] = []
    repro_tasks = tasks.get("repro_tasks", [])
    if not isinstance(repro_tasks, list):
        return {
            "ok": True,
            "issues": [],
            "warnings": [{"path": "$.repro_tasks", "message": "repro_tasks is unavailable for advisory risk review"}],
            "policy": "advisory_only",
        }

    for index, task in enumerate(repro_tasks):
        if not isinstance(task, dict):
            continue
        base = f"$.repro_tasks[{index}]"
        acceptance = task.get("scientific_acceptance")
        if not isinstance(acceptance, dict):
            warnings.append(
                {
                    "path": f"{base}.scientific_acceptance",
                    "message": "conclusion-level acceptance contract is unavailable; report this as an audit limitation",
                }
            )
            continue
        conclusions = acceptance.get("core_conclusions")
        numeric_targets = acceptance.get("key_numeric_targets")
        if not conclusions and not numeric_targets:
            warnings.append(
                {
                    "path": f"{base}.scientific_acceptance",
                    "message": "no conclusion or key numeric target is available for criterion-level reporting",
                }
            )

    return {"ok": True, "issues": [], "warnings": warnings, "policy": "advisory_only"}


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
        from .codex_cost import summarize_codex_usage
        delta = summarize_codex_usage(Path(audit_dir), since=codex_since if codex_since is not None else time.time() - total_wall_s)
        cumulative = summarize_codex_usage(Path(audit_dir))
        result["codex"] = {"delta": delta, "cumulative": cumulative}
        result["llm_api_totals"] = dict(result["totals"])
        for key in keys:
            value = delta.get(key)
            result["totals"][key] = result["totals"][key] + value if value is not None else None
    return result


def build_risk_report(
    facts: dict[str, Any],
    tasks: dict[str, Any],
    validation: dict[str, Any],
    runtime_result: dict[str, Any] | None = None,
    scientific_check: dict[str, Any] | None = None,
    result_review_result: dict[str, Any] | None = None,
    paper_format: str | None = None,
) -> dict[str, Any]:
    missing = facts.get("missing_information", [])
    repro_tasks = tasks.get("repro_tasks", [])
    assumptions = []
    task_evidence_gaps: list[dict[str, Any]] = []
    for task in repro_tasks if isinstance(repro_tasks, list) else []:
        if isinstance(task, dict) and isinstance(task.get("assumptions"), list):
            assumptions.extend(task["assumptions"])
        if isinstance(task, dict) and isinstance(task.get("missing_fact_requests"), list):
            for request in task["missing_fact_requests"]:
                if isinstance(request, dict):
                    task_evidence_gaps.append({**request, "task_id": task.get("task_id")})
        acceptance = task.get("scientific_acceptance") if isinstance(task, dict) else None
        information_gaps = acceptance.get("information_gaps") if isinstance(acceptance, dict) else None
        for gap in information_gaps if isinstance(information_gaps, list) else []:
            if isinstance(gap, dict):
                task_evidence_gaps.append({**gap, "task_id": task.get("task_id")})

    findings: list[dict[str, Any]] = []

    # === 自动注入已知局限（最高优先改进项）===
    # PDF text extraction 必然丢失图片/图表视觉信息。这是通信论文复现中最常见的根本性限制。
    if paper_format == "pdf":
        findings.append({
            "type": "pdf_images_lost",
            "message": "PDF 文本抽取可能省略图表视觉信息；系统会优先使用页面渲染、结构化数值与任务证据补偿。缺少裁图只作为证据包装限制记录。",
            "severity": "advisory",
            "mitigation": "结论级验收可使用可读结果图或等价 CSV、表格、summary 与文本证据，不要求像素级或裁图级一致。",
            "always_injected_for_pdfs": True,
        })

    if missing:
        findings.append({"type": "missing_information", "message": "论文存在复现需要但未明确说明的信息。", "count": len(missing)})
    if assumptions:
        findings.append({"type": "assumptions_required", "message": "复现任务需要使用假设参数。", "count": len(assumptions)})
    if task_evidence_gaps:
        findings.append(
            {
                "type": "task_evidence_gaps",
                "message": "任务定稿后仍有论文未明确的执行证据；这些缺口不是运行前复现评级。",
                "count": len(task_evidence_gaps),
                "items": task_evidence_gaps,
            }
        )
    if not validation.get("required_files_present"):
        findings.append({"type": "generated_project_incomplete", "message": "Codex task writer 交付的复现项目缺少必要文件。", "missing_files": validation.get("missing_files", [])})
    if not validation.get("python_compiles"):
        findings.append({"type": "generated_code_compile_error", "message": "Codex task writer 交付的 Python 文件存在语法错误。", "compile_errors": validation.get("compile_errors", [])})
    if runtime_result and runtime_result.get("enabled") and not runtime_result.get("passed"):
        findings.append(
            {
                "type": "generated_project_runtime_failed",
                "message": "部分 Codex task writer 未完成可验收的 full 运行或交付。",
                "coverage": runtime_result.get("coverage"),
                "delivery_coverage": runtime_result.get("delivery_coverage"),
                "per_task": runtime_result.get("per_task", []),
                "pipeline_error": runtime_result.get("pipeline_error"),
            }
        )
    requirement_warnings = runtime_result.get("requirements_warnings") if isinstance(runtime_result, dict) else None
    if isinstance(requirement_warnings, list) and requirement_warnings:
        findings.append(
            {
                "type": "dependency_warnings",
                "message": "复现项目存在非阻断依赖告警；本次不影响自动运行通过，但建议整理 requirements.txt 以提升可移植性。",
                "count": len(requirement_warnings),
                "examples": requirement_warnings[:3],
            }
        )
    partial_success = runtime_result.get("partial_success") if isinstance(runtime_result, dict) else None
    if isinstance(partial_success, dict) and partial_success.get("has_partial_output"):
        findings.append(
            {
                "type": "generated_project_partial_success",
                "message": "任务级项目未整体通过，但产出了部分有效结果；已保留可验收任务产物，失败任务需人工核对。",
                "valid_csv_files": partial_success.get("valid_csv_files", []),
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
    if scientific_check and scientific_check.get("warnings"):
        findings.append(
            {
                "type": "scientific_check_warnings",
                "message": "部分任务的结论级验收信息不完整；该项只作为报告审计提示，不阻断流程。",
                "count": len(scientific_check["warnings"]),
            }
        )
    outcome_counts = _terminal_outcome_counts(result_review_result or {})
    terminal_count = sum(outcome_counts.values())
    all_terminal = bool((result_review_result or {}).get("all_terminal")) or (
        bool(repro_tasks) and terminal_count >= len(repro_tasks)
    )
    if outcome_counts.get("inconclusive_missing_information"):
        findings.append(
            {
                "type": "inconclusive_missing_information",
                "message": "部分任务因论文缺少影响核心结论的信息而进入可报告的不确定终态。",
                "count": outcome_counts["inconclusive_missing_information"],
            }
        )
    if outcome_counts.get("not_reproduced"):
        findings.append(
            {
                "type": "not_reproduced",
                "message": "部分任务已完成有效且忠实的运行，但核心结论未得到支持；这是科学结果而不是流水线错误。",
                "count": outcome_counts["not_reproduced"],
            }
        )
    if result_review_result and result_review_result.get("enabled") and not result_review_result.get("passed") and not all_terminal:
        findings.append({"type": "writer_results_incomplete", "message": "至少一个任务尚未进入可报告终态。", "error": result_review_result.get("error")})

    combined_missing = list(missing) if isinstance(missing, list) else []
    known_missing_names = {
        str(item.get("name") or "").strip().casefold()
        for item in combined_missing if isinstance(item, dict)
    }
    for item in task_evidence_gaps:
        name_key = str(item.get("name") or "").strip().casefold()
        if name_key in known_missing_names:
            continue
        known_missing_names.add(name_key)
        combined_missing.append(item)
    dimensions = build_risk_dimensions(
        missing=combined_missing,
        assumptions=assumptions,
        validation=validation,
        runtime_result=runtime_result or {},
        scientific_check=scientific_check or {},
        tasks=tasks,
        result_review_result=result_review_result or {},
        stage_fallback_used=bool(stage_fallbacks),
    )
    engineering_risk_level = combine_risk_dimensions(dimensions)
    scientific_risk_level = _scientific_risk_level(
        dimensions,
        result_review_result or {},
    )
    return {
        "risk_level": engineering_risk_level,
        "engineering_risk_level": engineering_risk_level,
        "scientific_risk_level": scientific_risk_level,
        "judgement_style": "separated_scientific_and_engineering_risk",
        "risk_dimensions": dimensions,
        "missing_information_count": len(missing) if isinstance(missing, list) else 0,
        "task_evidence_gap_count": len(task_evidence_gaps),
        "assumptions_count": len(assumptions),
        "findings": findings,
        "scientific_check": scientific_check or {},
        "result_review": result_review_result or {},
        "note": "风险等级只表示工程复现风险，不等同于造假结论；smoke 通过也不等于论文完全复现成功。",
    }


def _terminal_outcome_counts(result_review_result: dict[str, Any]) -> dict[str, int]:
    counts = {outcome: 0 for outcome in (
        "reproduced",
        "reproduced_with_assumptions",
        "inconclusive_missing_information",
        "not_reproduced",
        "execution_failed",
    )}
    nested = result_review_result.get("verification_result")
    source = nested if isinstance(nested, dict) else result_review_result
    declared_counts = source.get("outcome_counts")
    if isinstance(declared_counts, dict):
        for outcome in counts:
            value = declared_counts.get(outcome)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                counts[outcome] = value
    task_items: list[Any] = []
    for key in ("tasks", "task_verifications", "per_task"):
        values = source.get(key)
        if isinstance(values, list):
            task_items = values
            break
    if any(counts.values()) and not task_items:
        return counts
    counts = {outcome: 0 for outcome in counts}
    for item in task_items:
        if not isinstance(item, dict):
            continue
        outcome = ""
        for key in ("terminal_outcome", "outcome", "scientific_outcome"):
            value = str(item.get(key) or "").strip()
            if value:
                outcome = value
                break
        if outcome in counts:
            counts[outcome] += 1
    return counts


def build_risk_dimensions(
    missing: list[Any],
    assumptions: list[Any],
    validation: dict[str, Any],
    runtime_result: dict[str, Any],
    scientific_check: dict[str, Any],
    tasks: dict[str, Any],
    result_review_result: dict[str, Any],
    stage_fallback_used: bool = False,
) -> dict[str, dict[str, Any]]:
    runtime_enabled = bool(runtime_result.get("enabled"))
    runtime_passed = runtime_result.get("passed")
    scientific_issues = scientific_check.get("issues", []) if isinstance(scientific_check, dict) else []
    security_issues = runtime_result.get("security_issues", []) if isinstance(runtime_result, dict) else []
    requirement_issues = runtime_result.get("requirements_issues", []) if isinstance(runtime_result, dict) else []
    requirement_warnings = runtime_result.get("requirements_warnings", []) if isinstance(runtime_result, dict) else []
    result_review_enabled = bool(result_review_result.get("enabled"))
    result_review_passed = result_review_result.get("passed")
    outcome_counts = _terminal_outcome_counts(result_review_result)
    material_missing = _high_impact_count(missing, field="impact")
    material_assumptions = _high_impact_count(assumptions, field="risk")
    all_terminal = bool(result_review_result.get("all_terminal")) or (
        bool(repro_tasks := tasks.get("repro_tasks"))
        and sum(outcome_counts.values()) >= len(repro_tasks)
    )
    return {
        "information_completeness": _dimension(
            "high"
            if outcome_counts["inconclusive_missing_information"]
            else "medium"
            if stage_fallback_used or material_missing or material_assumptions
            else "low",
            [
                f"material_missing_information={material_missing}",
                f"material_assumptions={material_assumptions}",
                f"missing_information={len(missing)}",
                f"assumptions={len(assumptions)}",
                f"stage_fallback_used={stage_fallback_used}",
                f"inconclusive_tasks={outcome_counts['inconclusive_missing_information']}",
            ],
        ),
        "implementation_fidelity": _dimension(
            "high"
            if not validation.get("required_files_present") or not validation.get("python_compiles")
            else "medium"
            if scientific_issues
            else "low",
            [
                f"required_files_present={validation.get('required_files_present')}",
                f"python_compiles={validation.get('python_compiles')}",
                f"scientific_issues={len(scientific_issues)}",
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
                outcome_counts,
                all_terminal,
            ),
            [
                f"result_review_enabled={result_review_enabled}",
                f"result_review_passed={result_review_passed}",
                f"scientific_issues={len(scientific_issues)}",
                f"terminal_outcomes={outcome_counts}",
            ],
        ),
        "baseline_fairness": _dimension(
            "medium" if _count_missing_baselines(tasks) else "low",
            [f"declared_comparisons_without_baseline={_count_missing_baselines(tasks)}"],
        ),
        "security_isolation": _dimension(
            "high" if security_issues else "low",
            [
                f"security_issues={len(security_issues)}",
                f"requirements_issues={len(requirement_issues)}",
                f"requirements_warnings={len(requirement_warnings)}",
                f"host_execution_requested={runtime_enabled}",
            ],
        ),
        "dependency_portability": _dimension(
            "high" if requirement_issues else "medium" if requirement_warnings else "low",
            [
                f"requirements_issues={len(requirement_issues)}",
                f"requirements_warnings={len(requirement_warnings)}",
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
    outcome_counts: dict[str, int] | None = None,
    all_terminal: bool = False,
) -> str:
    outcome_counts = outcome_counts or {}
    if not runtime_enabled:
        return "medium"
    if runtime_enabled and not runtime_passed:
        return "high"
    if outcome_counts.get("not_reproduced"):
        return "high"
    if outcome_counts.get("inconclusive_missing_information"):
        return "medium"
    if result_review_enabled and not result_review_passed and not all_terminal:
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

def _high_impact_count(items: list[Any], *, field: str) -> int:
    return sum(
        1
        for item in items
        if isinstance(item, dict)
        and (
            str(item.get(field) or "").strip().lower()
            in {"high", "critical", "severe"}
            or bool(item.get("affects_claim_ids"))
        )
    )


def _scientific_risk_level(
    dimensions: dict[str, dict[str, Any]],
    result_review_result: dict[str, Any],
) -> str:
    outcomes = _terminal_outcome_counts(result_review_result)
    if outcomes["not_reproduced"] or outcomes["execution_failed"]:
        return "high"
    if outcomes["inconclusive_missing_information"] or outcomes["reproduced_with_assumptions"]:
        return "medium"
    if sum(outcomes.values()):
        return "low"
    alignment = dimensions.get("result_alignment")
    level = str(alignment.get("level") or "") if isinstance(alignment, dict) else ""
    return level if level in {"low", "medium", "high"} else "medium"




def _count_missing_baselines(tasks: dict[str, Any]) -> int:
    count = 0
    repro_tasks = tasks.get("repro_tasks", [])
    if not isinstance(repro_tasks, list):
        return 0
    for task in repro_tasks:
        if not isinstance(task, dict):
            continue
        comparison = task.get("comparison")
        if not isinstance(comparison, dict) or "baselines" not in comparison:
            continue
        if comparison.get("baselines") in (None, [], ""):
            count += 1
    return count
