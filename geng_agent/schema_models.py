from __future__ import annotations

import base64
import binascii
import json
from pathlib import Path
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator


MAX_MANIFEST_FILES = 64
MAX_FILE_CHARS = 2_000_000

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

PaperReproType = Literal[
    "signal_chain",
    "modulation_recognition",
    "channel_coding",
    "mimo_ofdm",
    "network_protocol",
    "ml_communication",
    "optimization_algorithm",
    "hardware_dataset",
    "other",
]

FactType = Literal[
    "channel_model",
    "modulation",
    "coding",
    "metric",
    "simulation_parameter",
    "baseline",
    "figure_claim",
    "algorithm",
    "dataset",
    "topology",
    "hardware",
    "other",
]

Confidence = Literal["high", "medium", "low"]
MissingImpact = Literal["low", "medium", "high"]

MetricName = Literal[
    "bit_error_rate",
    "symbol_error_rate",
    "throughput",
    "delay",
    "spectral_efficiency",
    "outage_probability",
    "energy_efficiency",
    "accuracy",
    "loss",
    "other",
]

TrendDirection = Literal["decreasing", "increasing", "flat", "unknown"]
AssumptionRisk = Literal["low", "medium", "high"]
ResultCredibility = Literal["high", "medium", "low", "unknown"]
ResultAlignment = Literal["match", "partial_match", "mismatch", "inconclusive"]
ReviewDimension = Literal[
    "artifact_coverage",
    "reproduction_logic",
    "trend_shape",
    "metric_axis_scale",
    "baseline_comparison",
    "statistical_reliability",
    "conclusion_support",
]
DimensionRating = Literal["strong", "acceptable", "weak", "missing", "unknown"]
ScientificVerdict = Literal[
    "supports_paper_claim",
    "partially_supports_paper_claim",
    "does_not_support_paper_claim",
    "cannot_assess",
]
SupervisorAction = Literal["continue", "retry_stage", "use_fallback", "repair_code", "ask_human", "stop"]
SupervisorRiskLevel = Literal["low", "medium", "high"]
CodeReviewVerdict = Literal["pass", "revise"]
ReviewSeverity = Literal["blocking", "minor"]
SpecRefKind = Literal["fact", "task"]
ExperimentIndexStatus = Literal["ready", "ready_with_limitations", "blocked"]
ReproducibilityVerdict = Literal[
    "fully_reproduced",
    "mostly_reproduced",
    "partially_reproduced",
    "inconclusive",
    "high_reproducibility_risk",
    "failed_to_reproduce",
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FactSource(StrictModel):
    # A fact may be sourced from a text chunk OR from a figure/diagram the model saw in the
    # rendered page image. source_kind selects which: "text" -> chunk_id must be a real
    # paper_chunks id; "figure" -> chunk_id is null and page must be the page the figure is on.
    source_kind: Literal["text", "figure"]
    chunk_id: str | None
    page: int | None
    section: str
    quote: NonEmptyStr
    figure_ref: str


class EngineeringFact(StrictModel):
    type: FactType
    name: NonEmptyStr
    value: dict[str, Any]
    source: FactSource
    confidence: Confidence
    used_for_reproduction: bool


class MissingInformation(StrictModel):
    name: NonEmptyStr
    why_needed: NonEmptyStr
    impact: MissingImpact


class EngineeringFactsDocument(StrictModel):
    paper_domain: Literal["communication"]
    paper_repro_type: PaperReproType
    engineering_facts: list[EngineeringFact]
    missing_information: list[MissingInformation]


class RequiredFactRef(StrictModel):
    type: FactType
    name: NonEmptyStr


class Assumption(StrictModel):
    name: NonEmptyStr
    default_value: Any
    reason: NonEmptyStr
    risk: AssumptionRisk


class ExpectedTrend(StrictModel):
    x_axis: str
    y_axis: str
    direction: TrendDirection
    reason: str


class Comparison(StrictModel):
    baselines: list[str]
    curve_groups: list[str]
    tolerance: NonEmptyStr


class ReproTask(StrictModel):
    task_id: NonEmptyStr
    target: NonEmptyStr
    metric: MetricName
    metric_formula: NonEmptyStr
    figure_or_claim: NonEmptyStr
    expected_artifacts: list[NonEmptyStr]
    output_columns: list[NonEmptyStr]
    expected_trend: ExpectedTrend
    comparison: Comparison
    required_facts: list[RequiredFactRef]
    assumptions: list[Assumption]
    risk_if_unreproducible: NonEmptyStr


class ReproTasksDocument(StrictModel):
    repro_tasks: list[ReproTask] = Field(min_length=1)


class ExperimentIndexItem(StrictModel):
    experiment_id: NonEmptyStr
    title: NonEmptyStr
    figure_or_table: str | None = None
    task_id: NonEmptyStr
    metric: str | None = None
    source_pages: list[int]
    source_chunk_ids: list[NonEmptyStr]
    required_facts: list[RequiredFactRef]
    status: ExperimentIndexStatus
    limitations: list[str]


class ExperimentIndexDocument(StrictModel):
    experiments: list[ExperimentIndexItem]


class ManifestTextFile(StrictModel):
    path: NonEmptyStr
    content: str = Field(max_length=MAX_FILE_CHARS)


class ManifestLinesFile(StrictModel):
    path: NonEmptyStr
    content_lines: list[str]

    @field_validator("content_lines")
    @classmethod
    def content_lines_size(cls, value: list[str]) -> list[str]:
        if sum(len(line) + 1 for line in value) > MAX_FILE_CHARS:
            raise ValueError(f"must be at most {MAX_FILE_CHARS} characters total")
        return value


class ManifestBase64File(StrictModel):
    path: NonEmptyStr
    content_b64: str

    @field_validator("content_b64")
    @classmethod
    def valid_content_b64(cls, value: str) -> str:
        try:
            decoded = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("must be valid base64") from exc
        if len(decoded) > MAX_FILE_CHARS:
            raise ValueError(f"decoded file must be at most {MAX_FILE_CHARS} bytes")
        try:
            decoded.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("decoded file must be UTF-8 text") from exc
        return value


ManifestFile: TypeAlias = ManifestTextFile | ManifestLinesFile | ManifestBase64File


class ReproProjectManifest(StrictModel):
    files: list[ManifestFile] = Field(min_length=1, max_length=MAX_MANIFEST_FILES)


class ReproProjectPlanFile(StrictModel):
    path: NonEmptyStr
    purpose: NonEmptyStr
    key_interfaces: list[str]


class ReproProjectPlan(StrictModel):
    implementation_strategy: NonEmptyStr
    assumptions: list[str]
    files: list[ReproProjectPlanFile] = Field(min_length=1, max_length=MAX_MANIFEST_FILES)


class ReproProjectFile(StrictModel):
    path: NonEmptyStr
    content_lines: list[str]

    @field_validator("content_lines")
    @classmethod
    def content_lines_size(cls, value: list[str]) -> list[str]:
        if sum(len(line) + 1 for line in value) > MAX_FILE_CHARS:
            raise ValueError(f"must be at most {MAX_FILE_CHARS} characters total")
        return value


class RepairManifest(StrictModel):
    reason: NonEmptyStr
    touched_files: list[NonEmptyStr]
    scientific_changes: list[str]
    files: list[ManifestFile] = Field(min_length=1, max_length=MAX_MANIFEST_FILES)


REQUIRED_RESULT_REVIEW_DIMENSIONS = {
    "artifact_coverage",
    "reproduction_logic",
    "trend_shape",
    "metric_axis_scale",
    "baseline_comparison",
    "statistical_reliability",
    "conclusion_support",
}


class DimensionReview(StrictModel):
    dimension: ReviewDimension
    rating: DimensionRating
    finding: NonEmptyStr
    evidence: list[NonEmptyStr] = Field(min_length=1)


class ExperimentResultReview(StrictModel):
    task_id: NonEmptyStr
    local_result_credibility: ResultCredibility
    paper_alignment: ResultAlignment
    scientific_verdict: ScientificVerdict
    dimension_reviews: list[DimensionReview] = Field(min_length=len(REQUIRED_RESULT_REVIEW_DIMENSIONS))
    paper_result_summary: NonEmptyStr
    local_result_summary: NonEmptyStr
    differences: list[str]
    possible_causes: list[str]
    evidence: list[NonEmptyStr]
    limitations: list[str]
    confidence: Confidence

    @field_validator("dimension_reviews")
    @classmethod
    def dimension_reviews_cover_required_rubric(cls, value: list[DimensionReview]) -> list[DimensionReview]:
        dimensions = [item.dimension for item in value]
        dimension_set = set(dimensions)
        missing = sorted(REQUIRED_RESULT_REVIEW_DIMENSIONS - dimension_set)
        extra_duplicates = sorted({dimension for dimension in dimensions if dimensions.count(dimension) > 1})
        if missing:
            raise ValueError(f"must include all required dimensions; missing: {', '.join(missing)}")
        if extra_duplicates:
            raise ValueError(f"must not repeat dimensions: {', '.join(extra_duplicates)}")
        return value


class ResultReviewDocument(StrictModel):
    overall_result_credibility: ResultCredibility
    overall_alignment: ResultAlignment
    experiment_reviews: list[ExperimentResultReview] = Field(min_length=1)
    cross_experiment_findings: list[str]
    recommended_human_checks: list[str]
    note: NonEmptyStr


class SupervisorDecisionDocument(StrictModel):
    action: SupervisorAction
    target_stage: NonEmptyStr
    reason: NonEmptyStr
    evidence_paths: list[NonEmptyStr]
    risk_level: SupervisorRiskLevel
    confidence: Confidence
    prompt_adjustment: str | None = None
    human_question: str | None = None


class ReproducibilityVerdictDocument(StrictModel):
    verdict: ReproducibilityVerdict
    confidence: Confidence
    reasons: list[NonEmptyStr]
    recommended_action: NonEmptyStr


class CodeReviewFinding(StrictModel):
    spec_kind: SpecRefKind
    spec_ref: NonEmptyStr
    evidence_spec: NonEmptyStr
    code_location: NonEmptyStr
    evidence_code: NonEmptyStr
    severity: ReviewSeverity
    issue: NonEmptyStr
    suggested_fix: NonEmptyStr


class CodeFaithfulnessReviewDocument(StrictModel):
    verdict: CodeReviewVerdict
    findings: list[CodeReviewFinding]
    unverifiable: list[str]
    note: NonEmptyStr


SCHEMA_MODELS: dict[str, type[BaseModel]] = {
    "engineering_facts": EngineeringFactsDocument,
    "repro_tasks": ReproTasksDocument,
    "experiment_index": ExperimentIndexDocument,
    "repro_project_manifest": ReproProjectManifest,
    "repro_project_plan": ReproProjectPlan,
    "repro_project_file": ReproProjectFile,
    "repair_manifest": RepairManifest,
    "result_review_experiment": ExperimentResultReview,
    "result_review": ResultReviewDocument,
    "supervisor_decision": SupervisorDecisionDocument,
    "reproducibility_verdict": ReproducibilityVerdictDocument,
    "code_faithfulness_review": CodeFaithfulnessReviewDocument,
}


SCHEMA_FILENAMES: dict[str, str] = {
    "engineering_facts": "engineering_facts.schema.json",
    "repro_tasks": "repro_tasks.schema.json",
    "experiment_index": "experiment_index.schema.json",
    "repro_project_manifest": "repro_project_manifest.schema.json",
    "repro_project_plan": "repro_project_plan.schema.json",
    "repro_project_file": "repro_project_file.schema.json",
    "repair_manifest": "repair_manifest.schema.json",
    "result_review_experiment": "result_review_experiment.schema.json",
    "result_review": "result_review.schema.json",
    "supervisor_decision": "supervisor_decision.schema.json",
    "reproducibility_verdict": "reproducibility_verdict.schema.json",
    "code_faithfulness_review": "code_faithfulness_review.schema.json",
}


def model_for_stage(stage: str) -> type[BaseModel]:
    try:
        return SCHEMA_MODELS[stage]
    except KeyError as exc:
        raise ValueError(f"Unknown validation stage: {stage}") from exc


def response_format_for_stage(stage: str) -> dict[str, Any]:
    model = model_for_stage(stage)
    return {
        "type": "json_schema",
        "json_schema": {
            "name": stage,
            "strict": True,
            "schema": model.model_json_schema(),
        },
    }


def export_json_schemas(target_dir: Path) -> dict[str, Path]:
    target_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    for stage, model in SCHEMA_MODELS.items():
        path = target_dir / SCHEMA_FILENAMES[stage]
        path.write_text(
            json.dumps(model.model_json_schema(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        written[stage] = path
    return written
