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
ExperimentIndexStatus = Literal["ready", "ready_with_limitations", "blocked"]
ReproducibilityMode = Literal[
    "native_full", "scaled_full", "proxy_only", "environment_blocked", "upstream_patch_required"
]
PaperEntityKind = Literal["section", "figure", "table", "equation", "algorithm"]
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


class ThesisComparison(StrictModel):
    # One head-to-head the paper makes. The ORDER of methods_best_to_worst is the checkable
    # core: it encodes "who should beat who" so a reproduction whose curves come out in the
    # wrong order is caught (e.g. ZF>STAB when the paper claims STAB>ZF in a dense regime).
    claim_id: NonEmptyStr
    methods_best_to_worst: list[str]
    expected_ordering: NonEmptyStr
    metric: str
    regime: str
    figure_ref: str
    mechanism_note: str


class PaperThesisDocument(StrictModel):
    # The paper's "思路": its central claim, the protagonist method, WHY it works (mechanism,
    # in prose -- not a transcribed bound), the head-to-head orderings it asserts, and the
    # regime boundaries where the claim flips. This is the anchor downstream codegen and the
    # result-review check against, so a reproduction targets the paper's CONCLUSION rather
    # than just transcribing formulas.
    central_claim: NonEmptyStr
    proposed_method: NonEmptyStr
    mechanism: NonEmptyStr
    comparisons: list[ThesisComparison]
    headline_shape: str
    caveats: list[str]


class PaperMemorySource(StrictModel):
    path: str
    format: str
    sha256: str | None
    page_count: int | None


class PaperMemoryEntity(StrictModel):
    entity_id: NonEmptyStr
    kind: PaperEntityKind
    label: NonEmptyStr
    number: str | None
    subfigure: str | None
    page: int | None
    chunk_ids: list[NonEmptyStr]
    text: str
    parent_id: str | None


class PaperCrossReference(StrictModel):
    from_id: NonEmptyStr
    to_id: NonEmptyStr
    relation: Literal["references", "contains"]


class PaperMemoryMetadata(StrictModel):
    builder: NonEmptyStr
    chunk_count: int
    entity_count: int


class PaperMemoryDocument(StrictModel):
    schema_version: Literal["2.0"]
    source: PaperMemorySource
    entities: list[PaperMemoryEntity]
    cross_references: list[PaperCrossReference]
    metadata: PaperMemoryMetadata
    memory_hash: NonEmptyStr


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
    target_entity_ids: list[str] = Field(default_factory=list)
    subfigure: str | None = None
    claim: str = ""
    methods: list[str] = Field(default_factory=list)
    baselines: list[str] = Field(default_factory=list)
    regimes: list[str] = Field(default_factory=list)
    parameters: dict[str, Any] = Field(default_factory=dict)
    acceptance_criteria: list[str] = Field(default_factory=list)
    reproducibility_mode: ReproducibilityMode = "native_full"
    feasibility: dict[str, Any] = Field(default_factory=dict)


class ExperimentIndexDocument(StrictModel):
    schema_version: Literal["2.0"] = "2.0"
    experiments: list[ExperimentIndexItem]


class TaskContractInput(StrictModel):
    name: NonEmptyStr
    source: NonEmptyStr
    value: Any = None
    required: bool


class TaskContractOutput(StrictModel):
    path_pattern: NonEmptyStr
    kind: Literal["csv", "png", "json", "text", "other"]
    required: bool


class TaskContractBackend(StrictModel):
    requested: Literal["auto", "cpu", "gpu"]
    allow_cpu_fallback: bool


class TaskContractResources(StrictModel):
    execution_class: Literal["cpu_light", "cpu_heavy", "gpu", "unknown"]
    cpu_cores: int = Field(ge=1)
    ram_gb: float = Field(gt=0)
    gpu_count: int = Field(ge=0)
    vram_gb: float = Field(ge=0)
    confidence: Literal["low", "medium", "high"]


class TaskContractDocument(StrictModel):
    schema_version: Literal["1.0"]
    task_id: NonEmptyStr
    experiment_id: NonEmptyStr
    memory_snapshot_hash: NonEmptyStr
    reproducibility_mode: ReproducibilityMode
    inputs: list[TaskContractInput]
    outputs: list[TaskContractOutput] = Field(min_length=1)
    equations: list[str]
    algorithm_steps: list[NonEmptyStr] = Field(min_length=1)
    invariants: list[NonEmptyStr]
    backend: TaskContractBackend
    resources: TaskContractResources
    seed: int
    acceptance_criteria: list[NonEmptyStr] = Field(min_length=1)
    assumptions: list[str]


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


class ReproducibilityVerdictDocument(StrictModel):
    verdict: ReproducibilityVerdict
    confidence: Confidence
    reasons: list[NonEmptyStr]
    recommended_action: NonEmptyStr




SCHEMA_MODELS: dict[str, type[BaseModel]] = {
    "engineering_facts": EngineeringFactsDocument,
    "repro_tasks": ReproTasksDocument,
    "paper_thesis": PaperThesisDocument,
    "paper_memory": PaperMemoryDocument,
    "experiment_index": ExperimentIndexDocument,
    "task_contract": TaskContractDocument,
    "repro_project_manifest": ReproProjectManifest,
    "reproducibility_verdict": ReproducibilityVerdictDocument,
}


SCHEMA_FILENAMES: dict[str, str] = {
    "engineering_facts": "engineering_facts.schema.json",
    "repro_tasks": "repro_tasks.schema.json",
    "paper_thesis": "paper_thesis.schema.json",
    "paper_memory": "paper_memory.schema.json",
    "experiment_index": "experiment_index.schema.json",
    "task_contract": "task_contract.schema.json",
    "repro_project_manifest": "repro_project_manifest.schema.json",
    "reproducibility_verdict": "reproducibility_verdict.schema.json",
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
