from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from .status import inspect_case_status
from .benchmark_quality import assess_quality_baseline, quality_counts, scientific_outcome_counts, SCIENTIFIC_OUTCOMES
from .codex_cost import summarize_codex_usage


OUTCOME_STATUSES = ("matched", "explained_gap", "failed")
COST_FIELDS = ("llm_calls", "prompt_tokens", "completion_tokens", "total_tokens")
_COVERAGE_RE = re.compile(r"^\s*(\d+)\s*/\s*(\d+)\s*$")


def build_case_summary(output_dir: str | Path) -> dict[str, Any]:
    """Collect one case's offline benchmark fields from its output directory."""
    case_dir = Path(output_dir).expanduser().resolve()
    status = inspect_case_status(case_dir)
    facts = _read_json_object(case_dir / "engineering_facts.json")
    tasks = _read_json_object(case_dir / "repro_tasks.json")
    runtime = _read_json_object(case_dir / "runtime_result.json")
    run_cost = _read_json_object(case_dir / "run_cost.json")

    stages = [
        {
            "stage": str(item.get("stage") or ""),
            "ok": item.get("ok") is True,
            "reason": str(item.get("reason") or ""),
        }
        for item in status.get("stages", [])
        if isinstance(item, dict)
    ]
    runtime_fields = _runtime_fields(runtime)
    outcomes = _outcome_counts(case_dir, runtime)
    cumulative_cost = run_cost.get("cumulative") if run_cost else None
    measured_cost = cumulative_cost if isinstance(cumulative_cost, dict) else run_cost
    cost_fields = _cost_fields(measured_cost)
    codex_cost = summarize_codex_usage(case_dir / "audit")
    cost_scope = "cumulative" if isinstance(cumulative_cost, dict) else "legacy_latest_invocation"
    if not isinstance(cumulative_cost, dict) and codex_cost["llm_calls"]:
        cost_scope = "legacy_api_latest_plus_observed_codex_history"
        cost_fields["llm_calls"] = (cost_fields.get("llm_calls") or 0) + codex_cost["llm_calls"]
        for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = codex_cost.get(field)
            cost_fields[field] = (cost_fields.get(field) or 0) + value if value is not None else None
        cost_fields["cost_usd"] = None

    return {
        "case": case_dir.name,
        "output_dir": str(case_dir),
        "exists": case_dir.exists(),
        "next_stage": status.get("next_stage"),
        "resume_from": status.get("resume_from"),
        "stages": stages,
        "stages_ok": sum(1 for item in stages if item["ok"]),
        "stages_total": len(stages),
        "facts_count": _list_count(facts, "engineering_facts"),
        "tasks_count": _list_count(tasks, "repro_tasks"),
        **runtime_fields,
        **outcomes,
        "wall_clock_s": _number(measured_cost.get("wall_clock_s")) if measured_cost else None,
        "cost_scope": cost_scope,
        **cost_fields,
        "codex_cumulative": codex_cost,
        "scientific_outcomes": scientific_outcome_counts(case_dir, tasks),
        "quality": assess_quality_baseline(case_dir),
    }


def aggregate_benchmark(case_summaries: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Purely aggregate already-collected case summaries into one report."""
    cases = [dict(summary) for summary in case_summaries]
    runtime_tasks_passed = _sum_complete(cases, "runtime_tasks_passed")
    runtime_tasks_total = _sum_complete(cases, "runtime_tasks_total")

    totals: dict[str, Any] = {
        "facts_count": sum(_integer(case.get("facts_count"), default=0) for case in cases),
        "tasks_count": sum(_integer(case.get("tasks_count"), default=0) for case in cases),
        "runtime_tasks_passed": runtime_tasks_passed,
        "runtime_tasks_total": runtime_tasks_total,
        "runtime_coverage": (
            f"{runtime_tasks_passed}/{runtime_tasks_total}"
            if runtime_tasks_passed is not None and runtime_tasks_total is not None
            else None
        ),
        **{
            outcome: sum(_integer(case.get(outcome), default=0) for case in cases)
            for outcome in OUTCOME_STATUSES
        },
        "wall_clock_s": _sum_complete(cases, "wall_clock_s"),
        **{field: _sum_complete(cases, field) for field in COST_FIELDS},
        "cost_usd": _sum_complete(cases, "cost_usd"),
    }

    quality_rows = [row for case in cases for row in case.get("quality", {}).get("tasks", [])]
    return {
        "schema_version": "1.0",
        "case_count": len(cases),
        "totals": totals,
        "stage_totals": _aggregate_stages(cases),
        "cases": cases,
        "quality": {**quality_counts(quality_rows), "by_paper_family": {
            family: quality_counts([row for row in quality_rows if str(row.get("paper_family") or "unspecified") == family])
            for family in sorted({str(row.get("paper_family") or "unspecified") for row in quality_rows})}},
        "scientific_outcomes": {name: sum(case.get("scientific_outcomes", {}).get(name, 0) for case in cases)
                                for name in (*SCIENTIFIC_OUTCOMES, "unassessed")},
        "codex_cumulative": {
            "llm_calls": sum(case.get("codex_cumulative", {}).get("llm_calls", 0) for case in cases),
            "total_tokens": _sum_complete([case.get("codex_cumulative", {}) for case in cases], "total_tokens"),
            "calls_missing_usage": sum(case.get("codex_cumulative", {}).get("calls_missing_usage", 0) for case in cases),
        },
    }


def build_benchmark(case_output_dirs: Iterable[str | Path]) -> dict[str, Any]:
    """Collect and aggregate several case output directories without network access."""
    return aggregate_benchmark(build_case_summary(path) for path in case_output_dirs)


def render_benchmark_json(report: Mapping[str, Any]) -> str:
    """Pure JSON rendering for a benchmark report."""
    return json.dumps(report, ensure_ascii=False, indent=2) + "\n"


def render_benchmark_markdown(report: Mapping[str, Any]) -> str:
    """Pure Markdown rendering for a benchmark report."""
    cases = report.get("cases")
    case_rows = cases if isinstance(cases, list) else []
    totals = report.get("totals") if isinstance(report.get("totals"), Mapping) else {}

    lines = [
        "# Offline Benchmark",
        "",
        f"Cases: {report.get('case_count', len(case_rows))}",
        "",
        "Cost columns use cumulative case costs when a run ledger is available. Older cases combine observed Codex history with only the last recorded API/time invocation; missing usage is unknown.",
        "",
        "| Case | Stages | Facts | Tasks | Runtime | Matched | Explained gap | Failed | Wall clock (s) | LLM calls | Total tokens | Cost (USD) |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for case in case_rows:
        if not isinstance(case, Mapping):
            continue
        lines.append(_markdown_case_row(case))

    total_row = {
        "case": "**Total**",
        "stages_ok": "-",
        "stages_total": "-",
        **totals,
    }
    lines.extend([_markdown_case_row(total_row), "", "## Stage Status", ""])
    lines.extend(
        [
            "| Stage | OK | Not OK | Total |",
            "| --- | ---: | ---: | ---: |",
        ]
    )
    stage_totals = report.get("stage_totals")
    if isinstance(stage_totals, list):
        for item in stage_totals:
            if not isinstance(item, Mapping):
                continue
            lines.append(
                "| {stage} | {ok} | {not_ok} | {total} |".format(
                    stage=_markdown_text(item.get("stage")),
                    ok=_markdown_value(item.get("ok")),
                    not_ok=_markdown_value(item.get("not_ok")),
                    total=_markdown_value(item.get("total")),
                )
            )
    quality = report.get("quality") or {}
    cumulative = report.get("codex_cumulative") or {}
    lines += ["", "## Scientific outcomes", "",
              "Runtime coverage measures process completion. These terminal scientific results are recorded separately.", "",
              "| Case | Reproduced | With assumptions | Missing information | Not reproduced | Execution failed | Unassessed |",
              "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for case in [*case_rows, {"case": "**Total**", "scientific_outcomes": report.get("scientific_outcomes", {})}]:
        outcomes = case.get("scientific_outcomes", {})
        lines.append("| " + " | ".join([_markdown_text(case.get("case")),
                     *[str(outcomes.get(name, 0)) for name in (*SCIENTIFIC_OUTCOMES, "unassessed")]]) + " |")
    lines += ["", "## Scientific quality and cumulative Codex cost", "",
              "Independent labels are optional. Missing labels/results are unassessed, never counted as correct.", "",
              f"Assessed/labeled: {quality.get('assessed', 0)}/{quality.get('labeled', 0)}; "
              f"false success: {quality.get('false_success', 0)}; false failure: {quality.get('false_failure', 0)}; "
              f"unjustified/missed reruns: {quality.get('rerun_errors', 0)}.", "",
              f"Cumulative Codex calls: {cumulative.get('llm_calls', 0)}; tokens: {cumulative.get('total_tokens') if cumulative.get('total_tokens') is not None else 'unknown'}; "
              f"calls without complete usage: {cumulative.get('calls_missing_usage', 0)}."]
    return "\n".join(lines) + "\n"


def write_benchmark_json(path: str | Path, report: Mapping[str, Any]) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_benchmark_json(report), encoding="utf-8", newline="\n")
    return target


def write_benchmark_markdown(path: str | Path, report: Mapping[str, Any]) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_benchmark_markdown(report), encoding="utf-8", newline="\n")
    return target


def write_benchmark_reports(
    report: Mapping[str, Any],
    *,
    json_path: str | Path,
    markdown_path: str | Path,
) -> tuple[Path, Path]:
    """Write both serializations and return their paths."""
    return (
        write_benchmark_json(json_path, report),
        write_benchmark_markdown(markdown_path, report),
    )


def _read_json_object(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _list_count(document: dict[str, Any] | None, key: str) -> int:
    items = document.get(key) if document else None
    return len(items) if isinstance(items, list) else 0


def _runtime_fields(runtime: dict[str, Any] | None) -> dict[str, Any]:
    if runtime is None:
        return {
            "runtime_enabled": None,
            "runtime_passed": None,
            "runtime_tasks_passed": None,
            "runtime_tasks_total": None,
            "runtime_coverage": None,
        }

    per_task = runtime.get("per_task")
    task_items = [item for item in per_task if isinstance(item, dict)] if isinstance(per_task, list) else []
    tasks_passed = _integer_or_none(runtime.get("tasks_passed"))
    tasks_total = _integer_or_none(runtime.get("tasks_total"))

    coverage = runtime.get("coverage")
    coverage_text = str(coverage).strip() if coverage is not None else ""
    match = _COVERAGE_RE.match(coverage_text)
    if match:
        if tasks_passed is None:
            tasks_passed = int(match.group(1))
        if tasks_total is None:
            tasks_total = int(match.group(2))

    if task_items:
        if tasks_total is None:
            tasks_total = len(task_items)
        if tasks_passed is None:
            tasks_passed = sum(_runtime_task_passed(item) for item in task_items)

    if tasks_passed is not None and tasks_total is not None:
        coverage_text = f"{tasks_passed}/{tasks_total}"
    elif not coverage_text:
        coverage_text = ""

    return {
        "runtime_enabled": runtime.get("enabled") if isinstance(runtime.get("enabled"), bool) else None,
        "runtime_passed": runtime.get("passed") if isinstance(runtime.get("passed"), bool) else None,
        "runtime_tasks_passed": tasks_passed,
        "runtime_tasks_total": tasks_total,
        "runtime_coverage": coverage_text or None,
    }


def _runtime_task_passed(item: Mapping[str, Any]) -> int:
    if isinstance(item.get("passed"), bool):
        return int(item["passed"])
    status = str(item.get("task_writer_status") or item.get("status") or "")
    return int(status == "matched")


def _outcome_counts(case_dir: Path, runtime: dict[str, Any] | None) -> dict[str, int]:
    sources: list[Any] = [runtime.get("per_task") if runtime else None]

    result_review = _read_json_object(case_dir / "result_review.json")
    sources.append(result_review.get("task_writer_reviews") if result_review else None)

    generated = _read_json_object(case_dir / "generated_files.json")
    generated_review = generated.get("result_review") if generated else None
    sources.append(generated_review.get("task_statuses") if isinstance(generated_review, dict) else None)

    audit_records = _read_json_object(case_dir / "audit" / "03c_task_writers_records.json")
    sources.append(audit_records.get("tasks") if audit_records else None)

    statuses: list[str] = []
    for source in sources:
        statuses = _extract_statuses(source)
        if statuses:
            break
    return {status: statuses.count(status) for status in OUTCOME_STATUSES}


def _extract_statuses(items: Any) -> list[str]:
    if not isinstance(items, list):
        return []
    statuses = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        status = str(item.get("task_writer_status") or item.get("status") or "").strip()
        if status:
            statuses.append(status)
    return statuses


def _cost_fields(run_cost: dict[str, Any] | None) -> dict[str, int | float | None]:
    totals = run_cost.get("totals") if run_cost else None
    totals = totals if isinstance(totals, dict) else {}
    fields = {
        field: _first_number(totals.get(field), run_cost.get(field) if run_cost else None)
        for field in COST_FIELDS
    }
    fields["cost_usd"] = _first_number(
        totals.get("cost_usd"),
        totals.get("estimated_cost_usd"),
        totals.get("total_cost_usd"),
        run_cost.get("cost_usd") if run_cost else None,
        run_cost.get("estimated_cost_usd") if run_cost else None,
        run_cost.get("total_cost_usd") if run_cost else None,
    )
    return fields


def _aggregate_stages(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: dict[str, dict[str, int]] = {}
    for case in cases:
        stages = case.get("stages")
        if not isinstance(stages, list):
            continue
        for item in stages:
            if not isinstance(item, Mapping):
                continue
            stage = str(item.get("stage") or "")
            if not stage:
                continue
            current = counts.setdefault(stage, {"ok": 0, "not_ok": 0, "total": 0})
            current["total"] += 1
            current["ok" if item.get("ok") is True else "not_ok"] += 1
    return [{"stage": stage, **values} for stage, values in counts.items()]


def _sum_complete(cases: list[dict[str, Any]], field: str) -> int | float | None:
    if not cases:
        return None
    values = [_number(case.get(field)) for case in cases]
    if any(value is None for value in values):
        return None
    total = sum(value for value in values if value is not None)
    return round(total, 6) if any(isinstance(value, float) for value in values) else total


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _first_number(*values: Any) -> int | float | None:
    for value in values:
        number = _number(value)
        if number is not None:
            return number
    return None


def _integer_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _integer(value: Any, *, default: int) -> int:
    parsed = _integer_or_none(value)
    return parsed if parsed is not None else default


def _markdown_case_row(case: Mapping[str, Any]) -> str:
    stages_ok = case.get("stages_ok")
    stages_total = case.get("stages_total")
    stages = "-" if stages_ok == "-" else f"{_markdown_value(stages_ok)}/{_markdown_value(stages_total)}"
    values = [
        _markdown_text(case.get("case")),
        stages,
        _markdown_value(case.get("facts_count")),
        _markdown_value(case.get("tasks_count")),
        _markdown_value(case.get("runtime_coverage")),
        _markdown_value(case.get("matched")),
        _markdown_value(case.get("explained_gap")),
        _markdown_value(case.get("failed")),
        _markdown_value(case.get("wall_clock_s")),
        _markdown_value(case.get("llm_calls")),
        _markdown_value(case.get("total_tokens")) if case.get("total_tokens") is not None else "unknown",
        _markdown_value(case.get("cost_usd")) if case.get("cost_usd") is not None else "unknown",
    ]
    return "| " + " | ".join(values) + " |"


def _markdown_text(value: Any) -> str:
    return str(value if value is not None else "-").replace("|", "\\|").replace("\n", " ")


def _markdown_value(value: Any) -> str:
    return _markdown_text(value)
