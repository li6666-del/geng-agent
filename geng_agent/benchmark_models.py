from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from geng_agent.schema_models import Confidence, FactType, MetricName, NonEmptyStr, ReproducibilityVerdict, StrictModel, TrendDirection

BenchmarkSplit = Literal["development", "regression", "hidden"]
GoldStatus = Literal["curated", "pending"]
CurveScale = Literal["linear", "log10"]


class BenchmarkFact(StrictModel):
    type: FactType
    name: NonEmptyStr
    value: Any | None = None
    required: bool = True


class BenchmarkTask(StrictModel):
    task_id: NonEmptyStr
    figure_or_claim: NonEmptyStr
    metric: MetricName
    output_columns: list[NonEmptyStr] = Field(default_factory=list)
    baselines: list[NonEmptyStr] = Field(default_factory=list)
    expected_trend: TrendDirection = "unknown"
    expected_artifacts: list[NonEmptyStr] = Field(default_factory=list)


class StaticCodeCheck(StrictModel):
    check_id: NonEmptyStr
    path: NonEmptyStr
    contains: list[NonEmptyStr] = Field(default_factory=list)
    absent: list[NonEmptyStr] = Field(default_factory=list)
    weight: float = Field(default=1.0, gt=0)

    @model_validator(mode="after")
    def has_assertion(self) -> "StaticCodeCheck":
        if not self.contains and not self.absent:
            raise ValueError("a static code check needs at least one assertion")
        return self


class CurveCheck(StrictModel):
    check_id: NonEmptyStr
    task_id: NonEmptyStr
    actual_csv: NonEmptyStr
    reference_csv: NonEmptyStr
    x_column: NonEmptyStr
    y_columns: list[NonEmptyStr] = Field(min_length=1)
    scale: CurveScale = "linear"
    nmae_tolerance: float = Field(default=0.1, gt=0)
    min_rank_correlation: float = Field(default=0.9, ge=-1, le=1)
    weight: float = Field(default=1.0, gt=0)


class BenchmarkBudgets(StrictModel):
    wall_clock_s: float = Field(default=3600.0, gt=0)
    total_tokens: int = Field(default=500_000, gt=0)


class BenchmarkCase(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    case_id: NonEmptyStr
    title: NonEmptyStr
    paper: NonEmptyStr
    split: BenchmarkSplit
    difficulty: int = Field(ge=1, le=3)
    archetype: NonEmptyStr
    gold_status: GoldStatus = "curated"
    negative_case: bool = False
    repeat_runs: int = Field(default=1, ge=1, le=3)
    gold_facts: list[BenchmarkFact] = Field(default_factory=list)
    gold_tasks: list[BenchmarkTask] = Field(default_factory=list)
    implementation_checks: list[StaticCodeCheck] = Field(default_factory=list)
    curve_checks: list[CurveCheck] = Field(default_factory=list)
    expected_missing_information: list[NonEmptyStr] = Field(default_factory=list)
    expected_verdicts: list[ReproducibilityVerdict] = Field(default_factory=list)
    budgets: BenchmarkBudgets = Field(default_factory=BenchmarkBudgets)
    notes: str = ""

    @model_validator(mode="after")
    def curated_case_has_gold(self) -> "BenchmarkCase":
        signals = (self.gold_facts, self.gold_tasks, self.implementation_checks, self.curve_checks, self.expected_missing_information, self.expected_verdicts)
        if self.gold_status == "curated" and not any(signals):
            raise ValueError("a curated case must define at least one gold signal")
        if self.negative_case and self.gold_status == "curated" and not self.expected_verdicts:
            raise ValueError("a curated negative case must define expected_verdicts")
        return self


class BenchmarkSuite(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    suite_id: NonEmptyStr
    title: NonEmptyStr
    domain: Literal["communication"] = "communication"
    cases: list[NonEmptyStr] = Field(min_length=1)
    description: str = ""

    @field_validator("cases")
    @classmethod
    def unique_cases(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("case manifest paths must be unique")
        return value


class BenchmarkDimensionScore(StrictModel):
    dimension: NonEmptyStr
    score: float = Field(ge=0, le=100)
    weight: float = Field(gt=0)
    evidence: list[str] = Field(default_factory=list)


class BenchmarkRunScore(StrictModel):
    run_id: NonEmptyStr
    raw_score: float = Field(ge=0, le=100)
    qualification: NonEmptyStr
    invalid: bool = False
    gates: list[str] = Field(default_factory=list)
    dimensions: list[BenchmarkDimensionScore]


class BenchmarkCaseScore(StrictModel):
    case_id: NonEmptyStr
    split: BenchmarkSplit
    difficulty: int
    negative_case: bool
    status: Literal["scored", "pending", "missing_runs"]
    score: float | None = Field(default=None, ge=0, le=100)
    stability_score: float | None = Field(default=None, ge=0, le=100)
    qualification: str | None = None
    runs: list[BenchmarkRunScore] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class BenchmarkReport(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    suite_id: NonEmptyStr
    score: float | None = Field(default=None, ge=0, le=100)
    scored_cases: int = Field(ge=0)
    total_cases: int = Field(ge=0)
    pending_cases: int = Field(ge=0)
    missing_run_cases: int = Field(ge=0)
    qualification_counts: dict[str, int]
    dimension_scores: dict[str, float]
    cases: list[BenchmarkCaseScore]
    confidence: Confidence = "low"
