from __future__ import annotations

import base64
import binascii
import json
from pathlib import Path
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

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
EvidenceKind = Literal["paper_explicit", "paper_derived", "visual_estimate"]
TaskSpecificationStatus = Literal["evidenced", "assumed", "not_applicable", "unresolved"]
BackfillFieldStatus = Literal[
    "resolved_explicit",
    "resolved_derived",
    "resolved_visual_estimate",
    "not_found_in_paper",
    "ambiguous_or_conflicting",
]

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
    evidence_kind: EvidenceKind = "paper_explicit"
    derivation: str | None = None


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
    request_id: str | None = None
    field_ids: list[str] = Field(default_factory=list)
    sensitivity_check: str = ""


class ExpectedTrend(StrictModel):
    x_axis: str
    y_axis: str
    direction: TrendDirection
    reason: str


class Comparison(StrictModel):
    baselines: list[str]
    curve_groups: list[str]
    tolerance: NonEmptyStr


class RequestedFactField(StrictModel):
    field_id: NonEmptyStr
    description: NonEmptyStr
    affects: list[NonEmptyStr] = Field(default_factory=list)


class MissingFactRequest(StrictModel):
    request_id: NonEmptyStr
    type: FactType
    name: NonEmptyStr
    why_needed: NonEmptyStr
    impact: MissingImpact
    search_targets: list[str]
    required_fields: list[RequestedFactField] = Field(default_factory=list)


class TaskSpecificationItem(StrictModel):
    name: NonEmptyStr
    value: Any = None
    status: TaskSpecificationStatus
    evidence_facts: list[RequiredFactRef] = Field(default_factory=list)
    note: str = ""


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
    missing_fact_requests: list[MissingFactRequest] = Field(default_factory=list)
    assumptions: list[Assumption]
    risk_if_unreproducible: NonEmptyStr
    formula_chain: list[TaskSpecificationItem] = Field(default_factory=list)
    parameter_matrix: list[TaskSpecificationItem] = Field(default_factory=list)
    baseline_definitions: list[TaskSpecificationItem] = Field(default_factory=list)
    statistical_protocol: list[TaskSpecificationItem] = Field(default_factory=list)
    validation_anchors: list[TaskSpecificationItem] = Field(default_factory=list)


class BackfillFieldResolution(StrictModel):
    field_id: NonEmptyStr
    status: BackfillFieldStatus
    fact_refs: list[RequiredFactRef] = Field(default_factory=list)
    searched_locations: list[str] = Field(default_factory=list)
    note: NonEmptyStr


class BackfillRequestResolution(StrictModel):
    request_id: NonEmptyStr
    field_results: list[BackfillFieldResolution] = Field(min_length=1)


class TargetedFactBackfillDocument(EngineeringFactsDocument):
    request_resolutions: list[BackfillRequestResolution]


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


class ExperimentIndexItem(StrictModel):
    experiment_id: NonEmptyStr
    title: NonEmptyStr
    figure_or_table: str | None = None
    task_id: NonEmptyStr
    metric: str | None = None
    source_pages: list[int]
    source_chunk_ids: list[NonEmptyStr]
    required_facts: list[RequiredFactRef]
    limitations: list[str]
    subfigure: str | None = None
    claim: str = ""
    methods: list[str] = Field(default_factory=list)
    baselines: list[str] = Field(default_factory=list)
    regimes: list[str] = Field(default_factory=list)
    parameters: dict[str, Any] = Field(default_factory=dict)


class ExperimentIndexDocument(StrictModel):
    schema_version: Literal["2.0"] = "2.0"
    experiments: list[ExperimentIndexItem]


class ArchitectureBasis(StrictModel):
    status: Literal["paper_explicit", "paper_derived", "assumed", "unresolved"]
    evidence_facts: list[RequiredFactRef] = Field(default_factory=list)
    assumption_refs: list[str] = Field(default_factory=list)
    note: str = ""


class ArchitectureQuantity(StrictModel):
    id: NonEmptyStr
    role: NonEmptyStr
    dtype: str = ""
    shape: list[str] = Field(default_factory=list)
    unit: str = ""
    scale: str = ""
    normalization: str = ""
    scope: Literal["global", "consistency_group", "experiment", "runtime"]
    default: Any = None
    basis: ArchitectureBasis


class ArchitectureExecutionContract(StrictModel):
    execution_kind: NonEmptyStr
    primary_framework: NonEmptyStr
    supporting_libraries: list[NonEmptyStr]
    device_policy: Literal[
        "cpu",
        "framework_default",
        "accelerator_preferred",
        "accelerator_required",
        "external_runtime",
    ]
    precision: str
    trainable: bool
    gradient_mode: Literal["required", "not_required", "not_applicable"]
    checkpoint_policy: Literal["required", "optional", "not_applicable"]
    shared_implementation: bool
    required_capabilities: list[NonEmptyStr]
    rationale: NonEmptyStr


class ArchitectureComponent(StrictModel):
    id: NonEmptyStr
    kind: NonEmptyStr
    module: NonEmptyStr
    callable: str = ""
    execution: ArchitectureExecutionContract | None = None
    inputs: list[NonEmptyStr] = Field(default_factory=list)
    outputs: list[NonEmptyStr] = Field(default_factory=list)
    parameters: list[NonEmptyStr] = Field(default_factory=list)
    depends_on: list[NonEmptyStr] = Field(default_factory=list)
    basis: ArchitectureBasis


class ArchitectureConsistencyGroup(StrictModel):
    id: NonEmptyStr
    task_ids: list[NonEmptyStr] = Field(default_factory=list)
    shared_quantity_ids: list[NonEmptyStr] = Field(default_factory=list)


class ArchitectureBinding(StrictModel):
    task_id: NonEmptyStr
    experiment_id: NonEmptyStr
    consistency_group: NonEmptyStr
    components: list[NonEmptyStr] = Field(min_length=1)
    allowed_overrides: list[NonEmptyStr] = Field(default_factory=list)
    overrides: dict[str, Any] = Field(default_factory=dict)
    outputs: list[NonEmptyStr] = Field(default_factory=list)


class ArchitectureInvariant(StrictModel):
    id: NonEmptyStr
    kind: Literal["reference", "shape", "unit", "normalization", "global_override", "consistency", "foundation_ownership", "other"] = "other"
    subjects: list[NonEmptyStr] = Field(default_factory=list)
    task_ids: list[NonEmptyStr] = Field(default_factory=list)
    severity: Literal["error", "warning"]
    description: str = ""
    expression: str = ""
    basis: ArchitectureBasis


class ScientificArchitectureDocument(StrictModel):
    model_config = ConfigDict(
        json_schema_extra={
            "allOf": [
                {
                    "if": {
                        "properties": {"schema_version": {"const": "1.1"}},
                        "required": ["schema_version"],
                    },
                    "then": {
                        "properties": {
                            "components": {
                                "items": {
                                    "required": ["callable", "execution"],
                                    "properties": {
                                        "callable": {"type": "string", "minLength": 1, "pattern": r"\S"},
                                        "execution": {
                                            "$ref": "#/$defs/ArchitectureExecutionContract"
                                        },
                                    },
                                }
                            }
                        }
                    },
                }
            ]
        }
    )

    schema_version: Literal["1.0", "1.1"]
    workflow_version: Literal["2"] = "2"
    quantities: list[ArchitectureQuantity]
    components: list[ArchitectureComponent] = Field(min_length=1)
    consistency_groups: list[ArchitectureConsistencyGroup] = Field(default_factory=list)
    bindings: list[ArchitectureBinding] = Field(min_length=1)
    invariants: list[ArchitectureInvariant] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_v11_execution_contract(self) -> ScientificArchitectureDocument:
        if self.schema_version != "1.1":
            return self

        violations: list[str] = []
        components_by_id = {component.id: component for component in self.components}
        consistency_group_ids = [group.id for group in self.consistency_groups]
        declared_consistency_groups = set(consistency_group_ids)
        duplicate_consistency_groups = sorted(
            group_id
            for group_id in declared_consistency_groups
            if consistency_group_ids.count(group_id) > 1
        )
        for group_id in duplicate_consistency_groups:
            violations.append(f"consistency group {group_id!r} is declared more than once")
        tasks_by_component: dict[str, set[str]] = {}
        for component in self.components:
            if not component.callable.strip():
                violations.append(f"component {component.id!r} needs a non-empty callable")
            if component.execution is None:
                violations.append(f"component {component.id!r} needs an execution contract")
        for binding in self.bindings:
            if binding.consistency_group not in declared_consistency_groups:
                violations.append(
                    f"binding for task {binding.task_id!r} refers to undeclared "
                    f"consistency group {binding.consistency_group!r}"
                )
            for component_id in binding.components:
                tasks_by_component.setdefault(component_id, set()).add(binding.task_id)
        for component_id, task_ids in tasks_by_component.items():
            component = components_by_id.get(component_id)
            if (
                len(task_ids) > 1
                and component is not None
                and component.execution is not None
                and not component.execution.shared_implementation
            ):
                violations.append(
                    f"component {component_id!r} is bound to multiple tasks and needs "
                    "shared_implementation=true"
                )
        if violations:
            raise ValueError("scientific architecture 1.1: " + "; ".join(violations))
        return self


class TaskVerificationResult(StrictModel):
    task_id: NonEmptyStr
    verdict: Literal["accepted", "revise"]
    comparison_summary: NonEmptyStr
    differences: list[str]
    evidence_files: list[NonEmptyStr] = Field(min_length=1)
    feedback: list[str]
    confidence: Confidence


class IsolatedTaskVerificationDocument(StrictModel):
    schema_version: Literal["1.0"]
    task_id: NonEmptyStr
    verdict: Literal["accepted", "revise"]
    revision_target: Literal["none", "writer", "reporter"]
    comparison_summary: NonEmptyStr
    differences: list[str]
    non_material_differences: list[str] = Field(default_factory=list)
    evidence_files: list[NonEmptyStr] = Field(min_length=1)
    feedback: list[str]
    confidence: Confidence
    local_assets: list[str] = Field(default_factory=list)
    paper_assets: list[str] = Field(default_factory=list)
    remaining_uncertainties: list[str] = Field(default_factory=list)


class VerificationResultDocument(StrictModel):
    schema_version: Literal["1.0"]
    all_accepted: bool
    tasks: list[TaskVerificationResult] = Field(min_length=1)


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
    "targeted_fact_backfill": TargetedFactBackfillDocument,
    "repro_tasks": ReproTasksDocument,
    "paper_thesis": PaperThesisDocument,
    "experiment_index": ExperimentIndexDocument,
    "scientific_architecture": ScientificArchitectureDocument,
    "task_verification_result": IsolatedTaskVerificationDocument,
    "verification_result": VerificationResultDocument,
    "repro_project_manifest": ReproProjectManifest,
    "reproducibility_verdict": ReproducibilityVerdictDocument,
}


SCHEMA_FILENAMES: dict[str, str] = {
    "engineering_facts": "engineering_facts.schema.json",
    "targeted_fact_backfill": "targeted_fact_backfill.schema.json",
    "repro_tasks": "repro_tasks.schema.json",
    "paper_thesis": "paper_thesis.schema.json",
    "experiment_index": "experiment_index.schema.json",
    "scientific_architecture": "scientific_architecture.schema.json",
    "task_verification_result": "task_verification_result.schema.json",
    "verification_result": "verification_result.schema.json",
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
