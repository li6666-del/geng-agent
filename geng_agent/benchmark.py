from __future__ import annotations

import csv
import json
import math
import statistics
from pathlib import Path
from typing import Any, Iterable

from pydantic import ValidationError

from geng_agent.benchmark_models import (
    BenchmarkCase,
    BenchmarkCaseScore,
    BenchmarkDimensionScore,
    BenchmarkReport,
    BenchmarkRunScore,
    BenchmarkSuite,
    CurveCheck,
)


DIMENSION_WEIGHTS = {
    "paper_understanding": 15.0,
    "task_design": 15.0,
    "implementation_faithfulness": 20.0,
    "execution_artifacts": 15.0,
    "scientific_result_fidelity": 25.0,
    "stability": 5.0,
    "efficiency": 5.0,
}
QUALIFICATION_ORDER = {
    "invalid": 0,
    "no_valid_reproduction": 1,
    "partial_reproduction": 2,
    "basic_reproduction": 3,
    "high_reproduction": 4,
    "correctly_limited": 5,
}


class BenchmarkError(ValueError):
    pass


def load_suite(suite_path: Path) -> tuple[BenchmarkSuite, list[tuple[BenchmarkCase, Path]]]:
    suite_path = suite_path.resolve()
    suite = BenchmarkSuite.model_validate(_read_json(suite_path))
    root = suite_path.parent
    loaded: list[tuple[BenchmarkCase, Path]] = []
    seen_ids: set[str] = set()
    for relative in suite.cases:
        case_path = _resolve_inside(root, relative)
        case = BenchmarkCase.model_validate(_read_json(case_path))
        if case.case_id in seen_ids:
            raise BenchmarkError(f"duplicate case_id: {case.case_id}")
        seen_ids.add(case.case_id)
        loaded.append((case, case_path.parent))
    return suite, loaded


def validate_suite(suite_path: Path) -> dict[str, Any]:
    suite, cases = load_suite(suite_path)
    pending = [case.case_id for case, _ in cases if case.gold_status == "pending"]
    curated = [case.case_id for case, _ in cases if case.gold_status == "curated"]
    return {
        "ok": True,
        "suite_id": suite.suite_id,
        "total_cases": len(cases),
        "curated_cases": curated,
        "pending_cases": pending,
    }


def evaluate_suite(suite_path: Path, runs_root: Path) -> BenchmarkReport:
    suite, cases = load_suite(suite_path)
    case_scores: list[BenchmarkCaseScore] = []
    for case, case_dir in cases:
        case_scores.append(evaluate_case(case, case_dir, runs_root.resolve()))

    scored = [case for case in case_scores if case.status == "scored" and case.score is not None]
    qualification_counts: dict[str, int] = {}
    for case in scored:
        key = case.qualification or "unknown"
        qualification_counts[key] = qualification_counts.get(key, 0) + 1
    dimension_values: dict[str, list[float]] = {name: [] for name in DIMENSION_WEIGHTS}
    for case in scored:
        for run in case.runs:
            for dimension in run.dimensions:
                dimension_values[dimension.dimension].append(dimension.score)
        if case.stability_score is not None:
            dimension_values["stability"].append(case.stability_score)
    dimension_scores = {
        name: round(statistics.fmean(values), 2) if values else 0.0
        for name, values in dimension_values.items()
    }
    score = round(statistics.fmean(case.score for case in scored if case.score is not None), 2) if scored else None
    repeated = sum(1 for case in scored if len(case.runs) >= 2)
    confidence = "high" if len(scored) >= 15 and repeated >= 5 else "medium" if len(scored) >= 6 else "low"
    return BenchmarkReport(
        suite_id=suite.suite_id,
        score=score,
        scored_cases=len(scored),
        total_cases=len(case_scores),
        pending_cases=sum(case.status == "pending" for case in case_scores),
        missing_run_cases=sum(case.status == "missing_runs" for case in case_scores),
        qualification_counts=qualification_counts,
        dimension_scores=dimension_scores,
        cases=case_scores,
        confidence=confidence,
    )


def evaluate_case(case: BenchmarkCase, case_dir: Path, runs_root: Path) -> BenchmarkCaseScore:
    if case.gold_status == "pending":
        return BenchmarkCaseScore(
            case_id=case.case_id, split=case.split, difficulty=case.difficulty,
            negative_case=case.negative_case, status="pending",
            notes=["gold_status=pending; excluded from aggregate score"],
        )
    run_dirs = _discover_runs(runs_root / case.case_id)
    if not run_dirs:
        return BenchmarkCaseScore(
            case_id=case.case_id, split=case.split, difficulty=case.difficulty,
            negative_case=case.negative_case, status="missing_runs",
            notes=[f"no run artifacts under {runs_root / case.case_id}"],
        )
    run_scores = [score_run(case, case_dir, path) for path in run_dirs[: case.repeat_runs]]
    stability = _stability_score(run_scores, expected=case.repeat_runs)
    average = statistics.fmean(run.raw_score for run in run_scores)
    score = round(max(0.0, average - (100.0 - stability) * DIMENSION_WEIGHTS["stability"] / 100.0), 2)
    qualification = min(run_scores, key=lambda run: QUALIFICATION_ORDER.get(run.qualification, 0)).qualification
    notes = []
    if len(run_scores) < case.repeat_runs:
        notes.append(f"expected {case.repeat_runs} runs, found {len(run_scores)}")
    return BenchmarkCaseScore(
        case_id=case.case_id, split=case.split, difficulty=case.difficulty,
        negative_case=case.negative_case, status="scored", score=score,
        stability_score=round(stability, 2), qualification=qualification,
        runs=run_scores, notes=notes,
    )


def score_run(case: BenchmarkCase, case_dir: Path, run_dir: Path) -> BenchmarkRunScore:
    facts = _read_json_optional(run_dir / "engineering_facts.json")
    tasks = _read_json_optional(run_dir / "repro_tasks.json")
    runtime = _read_json_optional(run_dir / "runtime_result.json")
    result_review = _read_json_optional(run_dir / "result_review.json")
    verdict = _read_json_optional(run_dir / "reproducibility_verdict.json")
    risk = _read_json_optional(run_dir / "risk_report.json")
    cost = _read_json_optional(run_dir / "run_cost.json")

    understanding, understanding_ev = _score_understanding(case, facts)
    task_design, task_ev = _score_tasks(case, tasks)
    faithfulness, faith_ev = _score_faithfulness(case, run_dir)
    execution, execution_ev = _score_execution(case, run_dir, runtime)
    result_score, result_ev = _score_results(case, case_dir, run_dir, result_review, verdict)
    efficiency, efficiency_ev = _score_efficiency(case, cost, execution)
    values = {
        "paper_understanding": (understanding, understanding_ev),
        "task_design": (task_design, task_ev),
        "implementation_faithfulness": (faithfulness, faith_ev),
        "execution_artifacts": (execution, execution_ev),
        "scientific_result_fidelity": (result_score, result_ev),
        "stability": (100.0, ["computed across repeated runs at case level"]),
        "efficiency": (efficiency, efficiency_ev),
    }
    dimensions = [
        BenchmarkDimensionScore(dimension=name, score=round(score, 2), weight=DIMENSION_WEIGHTS[name], evidence=evidence)
        for name, (score, evidence) in values.items()
    ]
    raw_score = round(sum(item.score * item.weight for item in dimensions) / 100.0, 2)
    invalid, gates, qualification = _apply_gates(
        case, runtime, risk, verdict, faithfulness, execution, result_score, raw_score
    )
    return BenchmarkRunScore(
        run_id=run_dir.name, raw_score=0.0 if invalid else raw_score,
        qualification=qualification, invalid=invalid, gates=gates, dimensions=dimensions,
    )


def _score_understanding(case: BenchmarkCase, facts: dict[str, Any]) -> tuple[float, list[str]]:
    actual = facts.get("engineering_facts", []) if isinstance(facts, dict) else []
    actual_keys = {_fact_key(item) for item in actual if isinstance(item, dict)}
    gold_keys = {_fact_key(item.model_dump()) for item in case.gold_facts if item.required}
    fact_f1 = _set_f1(actual_keys, gold_keys) if gold_keys else 1.0
    missing_recall = _missing_recall(case.expected_missing_information, facts)
    score = 100.0 * (0.8 * fact_f1 + 0.2 * missing_recall) if case.expected_missing_information else 100.0 * fact_f1
    return score, [f"required_fact_f1={fact_f1:.3f}", f"missing_information_recall={missing_recall:.3f}"]


def _score_tasks(case: BenchmarkCase, tasks_doc: dict[str, Any]) -> tuple[float, list[str]]:
    gold = case.gold_tasks
    if not gold:
        return 100.0, ["no gold task rubric for this case"]
    actual = [item for item in tasks_doc.get("repro_tasks", []) if isinstance(item, dict)] if isinstance(tasks_doc, dict) else []
    unused = set(range(len(actual)))
    task_scores: list[float] = []
    matched = 0
    for expected in gold:
        candidates = [(index, _task_match(expected, item)) for index, item in enumerate(actual) if index in unused]
        if not candidates:
            task_scores.append(0.0)
            continue
        index, match_score = max(candidates, key=lambda pair: pair[1])
        if match_score < 0.35:
            task_scores.append(0.0)
            continue
        unused.remove(index)
        matched += 1
        item = actual[index]
        checks = [float(_norm(item.get("metric")) == _norm(expected.metric))]
        checks.append(_set_recall({_norm(v) for v in item.get("output_columns", [])}, {_norm(v) for v in expected.output_columns}))
        comparison = item.get("comparison") if isinstance(item.get("comparison"), dict) else {}
        checks.append(_set_recall({_norm(v) for v in comparison.get("baselines", [])}, {_norm(v) for v in expected.baselines}))
        direction = item.get("expected_trend", {}).get("direction") if isinstance(item.get("expected_trend"), dict) else None
        checks.append(1.0 if expected.expected_trend == "unknown" else float(direction == expected.expected_trend))
        task_scores.append(100.0 * statistics.fmean(checks))
    coverage = matched / len(gold)
    score = 0.4 * 100.0 * coverage + 0.6 * statistics.fmean(task_scores)
    return score, [f"gold_task_coverage={matched}/{len(gold)}", f"unmatched_actual_tasks={len(unused)}"]


def _score_faithfulness(case: BenchmarkCase, run_dir: Path) -> tuple[float, list[str]]:
    project = run_dir / "repro_project"
    if not project.is_dir():
        project = run_dir
    checks: list[tuple[float, float]] = []
    evidence: list[str] = []
    for check in case.implementation_checks:
        path = _resolve_run_artifact(project, check.path)
        if not path.is_file():
            checks.append((0.0, check.weight))
            evidence.append(f"{check.check_id}=missing_file")
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        assertions = [token in text for token in check.contains] + [token not in text for token in check.absent]
        passed = sum(assertions) / len(assertions)
        checks.append((100.0 * passed, check.weight))
        evidence.append(f"{check.check_id}={sum(assertions)}/{len(assertions)}")
    code_review = _read_json_optional(run_dir / "code_review.json")
    review_score = 100.0 if code_review.get("passed") is True else 40.0 if code_review else 50.0
    if checks:
        static_score = _weighted_mean(checks)
        return 0.8 * static_score + 0.2 * review_score, evidence + [f"code_review_score={review_score:.0f}"]
    return review_score, ["no static gold checks", f"code_review_score={review_score:.0f}"]


def _score_execution(case: BenchmarkCase, run_dir: Path, runtime: dict[str, Any]) -> tuple[float, list[str]]:
    runtime_score = 100.0 if runtime.get("passed") is True else 50.0 if runtime.get("partial_success", {}).get("has_partial_output") else 0.0
    expected = [artifact for task in case.gold_tasks for artifact in task.expected_artifacts]
    found = sum(_resolve_run_artifact(run_dir, artifact).exists() for artifact in expected)
    artifact_score = 100.0 * found / len(expected) if expected else runtime_score
    return 0.6 * runtime_score + 0.4 * artifact_score, [f"runtime_passed={runtime.get('passed')}", f"expected_artifacts={found}/{len(expected)}"]


def _score_results(case: BenchmarkCase, case_dir: Path, run_dir: Path, review: dict[str, Any], verdict: dict[str, Any]) -> tuple[float, list[str]]:
    if case.negative_case:
        expected = set(case.expected_verdicts)
        actual = verdict.get("verdict")
        verdict_score = 100.0 if actual in expected else 0.0
        missing_score = 100.0 * _missing_recall(case.expected_missing_information, _read_json_optional(run_dir / "engineering_facts.json"))
        return 0.6 * verdict_score + 0.4 * missing_score, [f"verdict={actual}", f"expected_verdict={actual in expected}"]
    if case.curve_checks:
        results = [_score_curve(check, case_dir, run_dir) for check in case.curve_checks]
        return _weighted_mean([(score, check.weight) for check, (score, _) in zip(case.curve_checks, results)]), [message for _, messages in results for message in messages]
    ratings = []
    rating_score = {"strong": 100.0, "acceptable": 75.0, "weak": 35.0, "missing": 0.0, "unknown": 25.0}
    for experiment in review.get("experiment_reviews", []) if isinstance(review, dict) else []:
        for dimension in experiment.get("dimension_reviews", []) if isinstance(experiment, dict) else []:
            if dimension.get("dimension") in {"trend_shape", "metric_axis_scale", "baseline_comparison", "statistical_reliability", "conclusion_support"}:
                ratings.append(rating_score.get(dimension.get("rating"), 0.0))
    score = statistics.fmean(ratings) if ratings else 0.0
    return score, [f"gold_curve_checks=0", f"review_dimension_ratings={len(ratings)}"]


def _score_curve(check: CurveCheck, case_dir: Path, run_dir: Path) -> tuple[float, list[str]]:
    actual_path = _resolve_run_artifact(run_dir, check.actual_csv)
    reference_path = _resolve_inside(case_dir.resolve(), check.reference_csv)
    if not actual_path.is_file() or not reference_path.is_file():
        return 0.0, [f"{check.check_id}=missing_csv"]
    actual = _read_numeric_csv(actual_path, check.x_column, check.y_columns)
    reference = _read_numeric_csv(reference_path, check.x_column, check.y_columns)
    common = sorted(set(actual) & set(reference))
    if len(common) < 2:
        return 0.0, [f"{check.check_id}=insufficient_common_x"]
    similarities: list[float] = []
    for column in check.y_columns:
        av = [_scale(actual[x][column], check.scale) for x in common]
        rv = [_scale(reference[x][column], check.scale) for x in common]
        denom = max(max(rv) - min(rv), statistics.fmean(abs(v) for v in rv), 1e-12)
        nmae = statistics.fmean(abs(a - r) for a, r in zip(av, rv)) / denom
        error_similarity = max(0.0, 1.0 - nmae / check.nmae_tolerance)
        corr = _spearman(av, rv)
        corr_similarity = max(0.0, min(1.0, (corr + 1.0) / (check.min_rank_correlation + 1.0)))
        similarities.append(100.0 * (0.7 * error_similarity + 0.3 * corr_similarity))
    score = statistics.fmean(similarities)
    return score, [f"{check.check_id}_score={score:.2f}", f"{check.check_id}_points={len(common)}"]


def _score_efficiency(case: BenchmarkCase, cost: dict[str, Any], execution: float) -> tuple[float, list[str]]:
    if execution < 60.0:
        return 0.0, ["efficiency suppressed because execution score < 60"]
    wall = float(cost.get("wall_clock_s", 0) or 0)
    tokens = float(cost.get("totals", {}).get("total_tokens", 0) or 0)
    wall_score = _budget_score(wall, case.budgets.wall_clock_s)
    token_score = _budget_score(tokens, float(case.budgets.total_tokens)) if tokens else 50.0
    return statistics.fmean([wall_score, token_score]), [f"wall_clock_s={wall}", f"total_tokens={tokens}"]


def _apply_gates(case: BenchmarkCase, runtime: dict[str, Any], risk: dict[str, Any], verdict: dict[str, Any], faithfulness: float, execution: float, results: float, raw: float) -> tuple[bool, list[str], str]:
    gates: list[str] = []
    security = runtime.get("security_issues", []) or runtime.get("requirements_issues", [])
    if security:
        return True, ["security_or_dependency_policy_violation"], "invalid"
    findings = risk.get("findings", []) if isinstance(risk, dict) else []
    fallback = bool(runtime.get("template_fallback_used")) or any(isinstance(item, dict) and item.get("type") == "template_fallback_used" for item in findings)
    if fallback:
        gates.append("template_fallback_used")
    if case.negative_case and verdict.get("verdict") in set(case.expected_verdicts) and results >= 60:
        return False, gates, "correctly_limited"
    if fallback or execution <= 0:
        return False, gates + (["no_valid_target_artifacts"] if execution <= 0 else []), "no_valid_reproduction"
    if faithfulness < 60 or execution < 60 or results < 60:
        if faithfulness < 60: gates.append("implementation_faithfulness_below_60")
        if execution < 60: gates.append("execution_artifacts_below_60")
        if results < 60: gates.append("scientific_result_fidelity_below_60")
        return False, gates, "partial_reproduction"
    if raw >= 85 and min(faithfulness, execution, results) >= 75:
        return False, gates, "high_reproduction"
    return False, gates, "basic_reproduction"


def render_report_markdown(report: BenchmarkReport) -> str:
    score = "N/A" if report.score is None else f"{report.score:.2f}"
    lines = [f"# Benchmark Report: {report.suite_id}", "", f"- Score: **{score}**", f"- Confidence: **{report.confidence}**", f"- Scored cases: **{report.scored_cases}/{report.total_cases}**", f"- Pending gold: **{report.pending_cases}**", f"- Missing runs: **{report.missing_run_cases}**", "", "## Dimension profile", "", "| Dimension | Score |", "|---|---:|"]
    lines.extend(f"| {name} | {value:.2f} |" for name, value in report.dimension_scores.items())
    lines.extend(["", "## Cases", "", "| Case | Split | Score | Qualification | Stability |", "|---|---|---:|---|---:|"])
    for case in report.cases:
        lines.append(f"| {case.case_id} | {case.split} | {_fmt(case.score)} | {case.qualification or case.status} | {_fmt(case.stability_score)} |")
    return "\n".join(lines) + "\n"


def write_report(report: BenchmarkReport, output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "benchmark_report.json"
    md_path = output_dir / "benchmark_report.md"
    json_path.write_text(json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(render_report_markdown(report), encoding="utf-8")
    return json_path, md_path


def _discover_runs(case_root: Path) -> list[Path]:
    if (case_root / "engineering_facts.json").is_file() or (case_root / "runtime_result.json").is_file():
        return [case_root]
    if not case_root.is_dir():
        return []
    return [path for path in sorted(case_root.iterdir()) if path.is_dir() and ((path / "engineering_facts.json").is_file() or (path / "runtime_result.json").is_file())]


def _stability_score(runs: list[BenchmarkRunScore], expected: int) -> float:
    if not runs:
        return 0.0
    spread = statistics.pstdev(run.raw_score for run in runs) if len(runs) > 1 else 0.0
    disagreement = 0.0 if len({run.qualification for run in runs}) == 1 else 25.0
    missing = max(0, expected - len(runs)) * 15.0
    return max(0.0, 100.0 - 4.0 * spread - disagreement - missing)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BenchmarkError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise BenchmarkError(f"JSON document must be an object: {path}")
    return data


def _read_json_optional(path: Path) -> dict[str, Any]:
    try:
        return _read_json(path)
    except BenchmarkError:
        return {}


def _resolve_inside(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise BenchmarkError(f"path escapes benchmark root: {relative}") from exc
    return candidate


def _resolve_run_artifact(run_dir: Path, relative: str) -> Path:
    rel = Path(relative)
    candidates = [run_dir / rel]
    if rel.parts and rel.parts[0] != "repro_project":
        candidates.append(run_dir / "repro_project" / rel)
    for path in candidates:
        resolved = path.resolve()
        try:
            resolved.relative_to(run_dir.resolve())
        except ValueError:
            continue
        if resolved.exists():
            return resolved
    return candidates[0].resolve()


def _norm(value: Any) -> str:
    return "".join(ch.lower() for ch in str(value) if ch.isalnum())


def _fact_key(item: dict[str, Any]) -> tuple[str, str]:
    return _norm(item.get("type")), _norm(item.get("name"))


def _set_f1(actual: set[Any], expected: set[Any]) -> float:
    if not expected:
        return 1.0
    if not actual:
        return 0.0
    overlap = len(actual & expected)
    precision = overlap / len(actual)
    recall = overlap / len(expected)
    return 2 * precision * recall / (precision + recall) if precision + recall else 0.0


def _set_recall(actual: set[str], expected: set[str]) -> float:
    return 1.0 if not expected else len(actual & expected) / len(expected)


def _missing_recall(expected: Iterable[str], facts: dict[str, Any]) -> float:
    expected_norm = {_norm(value) for value in expected}
    if not expected_norm:
        return 1.0
    actual = {_norm(item.get("name")) for item in facts.get("missing_information", []) if isinstance(item, dict)}
    matched = sum(any(e in a or a in e for a in actual if a) for e in expected_norm)
    return matched / len(expected_norm)


def _task_match(expected: Any, actual: dict[str, Any]) -> float:
    task_id = float(_norm(expected.task_id) == _norm(actual.get("task_id")))
    figure = float(_norm(expected.figure_or_claim) == _norm(actual.get("figure_or_claim")))
    metric = float(_norm(expected.metric) == _norm(actual.get("metric")))
    return 0.45 * task_id + 0.35 * figure + 0.2 * metric


def _weighted_mean(values: list[tuple[float, float]]) -> float:
    total = sum(weight for _, weight in values)
    return sum(value * weight for value, weight in values) / total if total else 0.0


def _read_numeric_csv(path: Path, x_column: str, y_columns: list[str]) -> dict[float, dict[str, float]]:
    result: dict[float, dict[str, float]] = {}
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                x = float(row[x_column])
                result[x] = {column: float(row[column]) for column in y_columns}
    except (OSError, ValueError, KeyError) as exc:
        raise BenchmarkError(f"invalid numeric CSV {path}: {exc}") from exc
    return result


def _scale(value: float, scale: str) -> float:
    return math.log10(max(value, 1e-300)) if scale == "log10" else value


def _spearman(a: list[float], b: list[float]) -> float:
    if len(a) != len(b) or len(a) < 2:
        return 0.0
    ra, rb = _ranks(a), _ranks(b)
    ma, mb = statistics.fmean(ra), statistics.fmean(rb)
    numerator = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    denom = math.sqrt(sum((x - ma) ** 2 for x in ra) * sum((y - mb) ** 2 for y in rb))
    return numerator / denom if denom else (1.0 if ra == rb else 0.0)


def _ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=values.__getitem__)
    ranks = [0.0] * len(values)
    index = 0
    while index < len(order):
        end = index + 1
        while end < len(order) and values[order[end]] == values[order[index]]:
            end += 1
        rank = (index + end - 1) / 2.0
        for position in order[index:end]:
            ranks[position] = rank
        index = end
    return ranks


def _budget_score(actual: float, budget: float) -> float:
    if actual <= 0:
        return 50.0
    if actual <= budget:
        return 100.0
    return max(0.0, 100.0 * (2.0 - actual / budget))


def _fmt(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.2f}"


__all__ = ["BenchmarkError", "DIMENSION_WEIGHTS", "evaluate_case", "evaluate_suite", "load_suite", "render_report_markdown", "score_run", "validate_suite", "write_report"]
