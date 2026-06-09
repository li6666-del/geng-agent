"""Phase D of the science loop: feed the result-review's "this does not reproduce the paper's
claim" verdict back into a bounded, reversible regeneration of the offending science code.

The orchestration here is pure over injected effects (regenerate / evaluate / snapshot /
restore), so the gate-and-revert decision logic is unit-testable without a real subprocess or
multimodal review. The pipeline supplies the real effects.
"""

from __future__ import annotations

from typing import Any, Callable

DOES_NOT_SUPPORT = "does_not_support_paper_claim"


def collect_science_mismatches(result_review_doc: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Per-experiment reviews the model judged does_not_support_paper_claim, flattened into a
    compact repair record (the paper-vs-local summaries, differences, causes, and the two
    most actionable dimension findings). Empty when the review is missing or everything
    supports/partially-supports -- nothing to repair."""
    mismatches: list[dict[str, Any]] = []
    for review in (result_review_doc or {}).get("experiment_reviews", []):
        if not isinstance(review, dict):
            continue
        if str(review.get("scientific_verdict")) != DOES_NOT_SUPPORT:
            continue
        dims = {
            str(item.get("dimension")): item
            for item in review.get("dimension_reviews", [])
            if isinstance(item, dict)
        }
        mismatches.append(
            {
                "task_id": str(review.get("task_id") or ""),
                "paper_result_summary": str(review.get("paper_result_summary") or ""),
                "local_result_summary": str(review.get("local_result_summary") or ""),
                "differences": [str(x) for x in review.get("differences", []) if str(x).strip()],
                "possible_causes": [str(x) for x in review.get("possible_causes", []) if str(x).strip()],
                "baseline_finding": str((dims.get("baseline_comparison") or {}).get("finding") or ""),
                "reproduction_logic_finding": str((dims.get("reproduction_logic") or {}).get("finding") or ""),
            }
        )
    return mismatches


def diagnose_csv_symptoms(numeric_columns: dict[str, Any]) -> list[str]:
    """Paper-AGNOSTIC numeric-symptom checks on a results.csv's per-column stats (min/max).
    Turns observed output pathologies into concrete "where to look" hypotheses, driven only by
    the numbers (no paper-specific formula), so the repair gets a symptom->cause lead instead of
    only "the ordering is wrong". Empty when nothing looks off.

    ``numeric_columns`` is the {col: {min,max,...}} map that summarize_csv_file emits.
    """
    if not isinstance(numeric_columns, dict) or not numeric_columns:
        return []
    zero_cols: list[str] = []
    const_cols: list[str] = []
    varying_cols: list[str] = []
    extreme_cols: list[str] = []
    for col, stats in numeric_columns.items():
        if not isinstance(stats, dict):
            continue
        lo, hi = stats.get("min"), stats.get("max")
        if not isinstance(lo, (int, float)) or not isinstance(hi, (int, float)):
            continue
        amax = max(abs(lo), abs(hi))
        span = abs(hi - lo)
        if amax < 1e-9:
            zero_cols.append(col)
        elif span < 1e-9 * max(1.0, amax):
            const_cols.append(col)
        else:
            varying_cols.append(col)
        if amax > 1e6:
            extreme_cols.append(f"{col}∈[{lo:.3g},{hi:.3g}]")
    tips: list[str] = []
    if zero_cols:
        tips.append(
            f"列 {zero_cols} 整列≈0 → 信号被清零：查 SNR/噪声方差归一化（是否误用绝对链路预算×热噪声使 SINR≈0）、"
            "有无 `if v<eps: return 0` 或条件数阈值硬清零、预编码/等效信道矩阵是否退化为 0。"
        )
    if zero_cols and varying_cols:
        tips.append(
            f"部分列（{zero_cols}）≈0 而另一些（{varying_cols}）正常 → 那几条对应的方法/分支没算对，"
            "单独查它们各自的实现函数（选择/合并/预编码），不要动正常的那些。"
        )
    if const_cols:
        tips.append(
            f"列 {const_cols} 几乎是常数 → 该量没随自变量变化：查循环里是否复用了同一结果、自变量是否真正进入计算。"
        )
    if extreme_cols:
        tips.append(
            f"列量级/符号异常（{', '.join(extreme_cols)}） → 查单位与缩放、是否漏取实部 np.real、"
            "分母接近 0 被放大、是否未做归一化。"
        )
    return tips


def build_science_directive(
    mismatches: list[dict[str, Any]], symptoms_by_task: dict[str, list[str]] | None = None
) -> str:
    """A Chinese repair brief built from the review's diagnosis: WHAT came out wrong (paper vs
    local), the specific differences/causes, and a standing instruction to fix the MODEL rather
    than tune numbers to match. Shared src/ files get the union of all mismatch briefs (a
    channel/model fix usually helps every figure)."""
    if not mismatches:
        return ""
    blocks: list[str] = [
        "结果审查判定下列复现“未支持论文结论”（多为方法相对排序与论文相反/不一致）。"
        "请据此修正**科学建模**，不要把数值硬凑到与论文一致：",
    ]
    for mismatch in mismatches:
        lines = [f"\n## 任务 {mismatch['task_id']}"]
        if mismatch["paper_result_summary"]:
            lines.append(f"- 论文应有结果：{mismatch['paper_result_summary']}")
        if mismatch["local_result_summary"]:
            lines.append(f"- 本地实际结果：{mismatch['local_result_summary']}")
        for diff in mismatch["differences"][:4]:
            lines.append(f"- 差异：{diff}")
        for cause in mismatch["possible_causes"][:4]:
            lines.append(f"- 可能原因：{cause}")
        if mismatch["baseline_finding"]:
            lines.append(f"- baseline 对比维度发现：{mismatch['baseline_finding']}")
        if mismatch["reproduction_logic_finding"]:
            lines.append(f"- 复现逻辑维度发现：{mismatch['reproduction_logic_finding']}")
        for symptom in (symptoms_by_task or {}).get(mismatch["task_id"], [])[:4]:
            lines.append(f"- 本地数值症状 → 排查方向：{symptom}")
        blocks.append("\n".join(lines))
    blocks.append(
        "\n修正要求：定位让方法排序/趋势出错的**建模环节**（信道/等效信道构造、预编码、SINR 与和速率定义、"
        "归一化与 SNR 约定），改对它；若优势来自空时/多普勒维度的去相关与条件数改善，必须把该维度如实建出来。"
        "改完后方法的相对高低应自然与论文一致，而不是靠加偏置/裁剪去对齐。"
    )
    return "\n".join(blocks)


def review_score(result_review_doc: dict[str, Any] | None, runtime_result: dict[str, Any] | None) -> dict[str, int]:
    """Comparable score for the gate: how many tasks passed execution (coverage) and how many
    the review still judges does_not_support. Lower mismatch is better; coverage must not drop."""
    passed, total = _coverage(runtime_result)
    mismatch = len(collect_science_mismatches(result_review_doc))
    return {"coverage_passed": passed, "coverage_total": total, "mismatch_count": mismatch}


def is_improvement(before: dict[str, int], after: dict[str, int]) -> bool:
    """Keep a round only if it strictly reduced the mismatch count WITHOUT losing coverage."""
    return after["coverage_passed"] >= before["coverage_passed"] and after["mismatch_count"] < before["mismatch_count"]


def is_regression(before: dict[str, int], after: dict[str, int]) -> bool:
    """Hard revert trigger: a round that loses coverage or adds a new mismatch made things worse."""
    return after["coverage_passed"] < before["coverage_passed"] or after["mismatch_count"] > before["mismatch_count"]


def run_science_repair(
    *,
    result_review_doc: dict[str, Any] | None,
    runtime_result: dict[str, Any] | None,
    max_rounds: int,
    regenerate: Callable[[list[dict[str, Any]]], None],
    evaluate: Callable[[], tuple[dict[str, Any], dict[str, Any], dict[str, Any]]],
    snapshot: Callable[[], None],
    restore: Callable[[], None],
) -> dict[str, Any]:
    """Bounded, reversible repair loop. Each round regenerates the science files for the current
    does_not_support tasks, re-runs + re-reviews via ``evaluate`` (-> runtime, review_status,
    review_doc), and keeps the change ONLY if mismatch strictly dropped with no coverage loss;
    otherwise it restores the snapshot and stops. ``evaluate`` returning a falsy review_doc is
    treated as no-improvement (a failed re-review must not be scored as a win)."""
    mismatches = collect_science_mismatches(result_review_doc)
    if not mismatches or max_rounds <= 0:
        return {
            "applied": False,
            "kept": False,
            "reason": "no does_not_support tasks" if not mismatches else "science_repair_rounds<=0",
            "rounds": [],
            "runtime_result": runtime_result,
            "result_review_doc": result_review_doc,
            "result_review_result": None,
        }

    before = review_score(result_review_doc, runtime_result)
    snapshot()
    rounds: list[dict[str, Any]] = []
    current_runtime = runtime_result
    current_doc = result_review_doc
    kept_review_result: dict[str, Any] | None = None
    kept_any = False

    for round_no in range(1, max_rounds + 1):
        mismatches = collect_science_mismatches(current_doc)
        if not mismatches:
            break
        regenerate(mismatches)
        new_runtime, new_review_result, new_doc = evaluate()
        after = review_score(new_doc, new_runtime)
        record = {
            "round": round_no,
            "targets": [mismatch["task_id"] for mismatch in mismatches],
            "before": dict(before),
            "after": dict(after),
        }
        if not new_doc or is_regression(before, after) or not is_improvement(before, after):
            restore()
            record["decision"] = "reverted" if (new_doc and is_regression(before, after)) else "reverted_no_improvement"
            rounds.append(record)
            break
        record["decision"] = "kept"
        kept_any = True
        current_runtime, current_doc, kept_review_result = new_runtime, new_doc, new_review_result
        before = after
        snapshot()
        rounds.append(record)
        if after["mismatch_count"] == 0:
            break

    return {
        "applied": True,
        "kept": kept_any,
        "reason": "",
        "rounds": rounds,
        "runtime_result": current_runtime,
        "result_review_doc": current_doc,
        "result_review_result": kept_review_result,
    }


def _coverage(runtime_result: dict[str, Any] | None) -> tuple[int, int]:
    """Best-effort (passed, total) task coverage from a per-task runtime_result. Reads the
    "coverage": "N/M" string the per-task runner emits; falls back to (1,1)/(0,1) from the
    boolean ``passed`` for the legacy single-script shape."""
    if not isinstance(runtime_result, dict):
        return (0, 0)
    coverage = runtime_result.get("coverage")
    if isinstance(coverage, str) and "/" in coverage:
        try:
            passed_str, total_str = coverage.split("/", 1)
            return (int(passed_str), int(total_str))
        except ValueError:
            pass
    for key in ("smoke", "full"):
        profile = runtime_result.get(key)
        if isinstance(profile, dict) and isinstance(profile.get("coverage"), str) and "/" in profile["coverage"]:
            try:
                passed_str, total_str = profile["coverage"].split("/", 1)
                return (int(passed_str), int(total_str))
            except ValueError:
                continue
    if runtime_result.get("passed") is True:
        return (1, 1)
    return (0, 1)
