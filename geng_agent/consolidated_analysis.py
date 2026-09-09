"""Coupled reasoning outputs with separate, traceable scientific artifacts."""
from __future__ import annotations

from typing import Any

from .analysis_prompt_context import scientific_prompt_value
from .execution_plan import ExecutionPlanError, compile_execution_plan
from .experiment_index import build_local_experiment_index
from .facts_normalize import finalize_engineering_facts
from .heuristic_fallbacks import build_fallback_engineering_facts, build_fallback_repro_tasks
from .json_utils import pretty_json
from .pipeline_helpers import wrap_untrusted
from .schemas import ValidationIssue
from .tasks_normalize import finalize_repro_tasks
from .task_evidence_backfill import reconcile_final_tasks
from .scientific_architecture import partition_scientific_architecture_issues
from .scientific_architecture_normalize import (
    finalize_scientific_architecture, scientific_architecture_normalization_errors,
    validate_scientific_architecture_repair_preservation,
)
from .workflow_policy import _execution_plan_requires_shared_science


CONSOLIDATED_ANALYSIS_POLICY = "understanding-and-planning-v1"


def load_paper_understanding(pipeline: Any, context: Any, *, paper: dict, paper_context: str,
                             paper_images: list, valid_chunk_ids: set, valid_pages: set) -> dict:
    def normalize(raw: dict) -> dict:
        if not isinstance(raw.get("facts"), dict):
            return raw  # Missing/invalid documents must enter structure repair.
        return {**raw, "facts": finalize_engineering_facts(raw["facts"], valid_chunk_ids, valid_pages)}

    def fallback(exc: Exception) -> dict:
        reason = f"Paper understanding failed: {exc}"
        return {"facts": build_fallback_engineering_facts(paper=paper, reason=reason),
                "paper_thesis": None, "limitations": [reason], "_meta": {"fallback_reason": reason}}

    return pipeline._load_or_create_analysis_stage_json(
        output_path=context.output_dir / "paper_understanding.json", output_dir=context.output_dir,
        audit_dir=context.audit_dir, prompt=pipeline.prompt_book.render("understand_paper.md", paper_context=paper_context),
        stage_label="01_understand_paper", cleanup_stage="facts", schema_stage="paper_understanding",
        max_attempts=context.options.json_repair_attempts + 1, resume=context.options.resume,
        images=paper_images, candidate_normalizer=normalize, backend=context.options.analysis_backend,
        cache_inputs={"paper_source_sha256": paper.get("source_sha256"), "policy": CONSOLIDATED_ANALYSIS_POLICY},
        fallback_factory=fallback if context.options.analysis_fallback else None)


def plan_document_issues(document: dict, *, facts: dict, paper: dict, figure_index: dict) -> list[ValidationIssue]:
    tasks = document.get("tasks") or {}
    if not isinstance(tasks, dict) or not isinstance(tasks.get("repro_tasks"), list):
        return []  # The structural schema reports malformed documents.
    try:
        execution_plan = compile_execution_plan(tasks)
    except ExecutionPlanError as exc:
        return [ValidationIssue("$.tasks" + exc.path.removeprefix("$"), str(exc))]
    # A blocked plan requests paper evidence, not a speculative executable graph.
    if tasks.get("backfill_handoff", {}).get("ready_for_writer") is False:
        return []
    shared = _execution_plan_requires_shared_science(execution_plan)
    architecture = document.get("scientific_architecture")
    if architecture is None:
        return [ValidationIssue("$.scientific_architecture", "Shared scientific dependencies require an architecture") ] if shared else []
    index = build_local_experiment_index(facts, tasks, paper, figure_index)
    blockers, _warnings = partition_scientific_architecture_issues(
        architecture, facts=facts, tasks=tasks, experiment_index=index, execution_plan=execution_plan)
    issues = [*scientific_architecture_normalization_errors(architecture), *(blockers if shared else [])]
    return [ValidationIssue("$.scientific_architecture" + item.path.removeprefix("$"), item.message) for item in issues]


def plan_repair_preservation(before: dict, after: dict) -> list[ValidationIssue]:
    """Keep the existing architecture repair protection across the new envelope."""
    original = before.get("scientific_architecture")
    if not isinstance(original, dict):
        return []
    current = after.get("scientific_architecture")
    return [ValidationIssue("$.scientific_architecture" + issue.path.removeprefix("$"), issue.message)
            for issue in validate_scientific_architecture_repair_preservation(original, current if isinstance(current, dict) else {})]


def load_experiment_plan(pipeline: Any, context: Any, *, facts: dict, paper_thesis: dict | None,
                         paper: dict, paper_context: str, paper_images: list, figure_index: dict,
                         host_capabilities: dict, previous_plan: dict | None = None,
                         resolution: dict | None = None, ledger: dict | None = None,
                         round_index: int = 0) -> dict:
    def normalize(raw: dict) -> dict:
        if not isinstance(raw.get("tasks"), dict):
            return raw
        architecture = raw.get("scientific_architecture")
        tasks = finalize_repro_tasks(raw["tasks"], facts)
        if previous_plan is not None:
            tasks = reconcile_final_tasks(previous_plan["tasks"], tasks, resolution or {}, relationship_snapshot=True)
            tasks = finalize_repro_tasks(tasks, facts)
        tasks.setdefault("_meta", {})["experiment_plan_snapshot"] = True
        if isinstance(architecture, dict):
            architecture = finalize_scientific_architecture(architecture)
            # Experiment IDs are host-owned index addresses derived from task IDs.
            experiments = build_local_experiment_index(facts, tasks, paper, figure_index)
            ids = {item["task_id"]: item["experiment_id"] for item in experiments["experiments"]}
            for binding in architecture.get("bindings", []):
                if isinstance(binding, dict) and binding.get("task_id") in ids:
                    binding["experiment_id"] = ids[binding["task_id"]]
        result = {**raw, "tasks": tasks}
        if "scientific_architecture" in raw:
            result["scientific_architecture"] = architecture
        return result

    def fallback(exc: Exception) -> dict:
        # A failed revision must not silently mix the new tasks and old architecture.
        if previous_plan is not None:
            raise exc
        reason = f"Experiment planning failed: {exc}"
        return {"tasks": build_fallback_repro_tasks(facts=facts, paper=paper, reason=reason),
                "scientific_architecture": None, "_meta": {"fallback_reason": reason}}

    architecture_rules = "## Contract rules" + pipeline.prompt_book.load("design_scientific_architecture.md").split("## Contract rules", 1)[1].split("## Host capability inventory", 1)[0]
    prompt = pipeline.prompt_book.render("plan_experiments.md", architecture_rules=architecture_rules,
        paper_context=paper_context,
        understanding=wrap_untrusted("understanding", pretty_json(scientific_prompt_value({"facts": facts, "paper_thesis": paper_thesis}))),
        revision_context=wrap_untrusted("revision", pretty_json(scientific_prompt_value({
            "previous_plan": previous_plan, "resolution": resolution, "search_ledger": ledger}))),
        host_capabilities=wrap_untrusted("host_capabilities", pretty_json(host_capabilities)))
    label = "02a_plan_experiments" if round_index == 0 else f"02c_round_{round_index:02d}_revise_experiment_plan"
    return pipeline._load_or_create_analysis_stage_json(
        output_path=context.audit_dir / f"{label}.json", output_dir=context.output_dir, audit_dir=context.audit_dir,
        prompt=prompt, stage_label=label, cleanup_stage="tasks" if round_index == 0 else "tasks_finalize",
        schema_stage="experiment_plan", max_attempts=context.options.json_repair_attempts + 1,
        resume=context.options.resume, images=paper_images, request_timeout=context.options.tasks_timeout,
        candidate_normalizer=normalize,
        final_extra_validation=lambda raw: plan_document_issues(raw, facts=facts, paper=paper, figure_index=figure_index),
        repair_preservation_validator=plan_repair_preservation,
        backend=context.options.analysis_backend,
        cache_inputs={"paper_source_sha256": paper.get("source_sha256"), "facts": facts, "paper_thesis": paper_thesis,
                      "previous_plan": previous_plan, "resolution": resolution, "ledger": ledger,
                      "figure_index": figure_index, "host_capabilities": host_capabilities,
                      "policy": CONSOLIDATED_ANALYSIS_POLICY},
        fallback_factory=fallback if context.options.analysis_fallback else None)
