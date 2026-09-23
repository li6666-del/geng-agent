"""Coupled reasoning outputs with separate, traceable scientific artifacts."""
from __future__ import annotations

from typing import Any

from .analysis_prompt_context import scientific_prompt_value
from .json_utils import pretty_json
from .pipeline_helpers import wrap_untrusted
from .progress import PipelineCancelled


CONSOLIDATED_ANALYSIS_POLICY = "supervisor-owned-understanding-and-planning-v2"


def _analysis_handoff_summary(result: dict) -> dict:
    """Cache observation is not a change to the accepted scientific handoff."""
    summary = dict(result)
    metadata = result.get("_meta")
    if isinstance(metadata, dict):
        summary["_meta"] = {key: value for key, value in metadata.items() if key != "cache_reused"}
    return summary


def supervised_analysis_stage(pipeline: Any, context: Any, *, node_id: str,
                              supervise: bool = True, recovery_guidance: dict | None = None,
                              recovery_resume: bool = False,
                              **kwargs: Any) -> dict:
    """Keep owner repair instructions and cache invalidation local to one node."""
    from .supervisor import REPLAY_REQUIRED, current_supervisor, supervised_call

    call = dict(kwargs)
    supervisor = current_supervisor()
    if recovery_guidance is None and supervisor is not None:
        recovery_guidance = supervisor.current_instruction("revision:" + node_id)
    if recovery_guidance:
        call["cache_inputs"] = {**call.get("cache_inputs", {}), "supervisor_assignment": recovery_guidance}
    original_prompt = str(call["prompt"])
    applied_instruction: dict | None = None
    reconcile_cache = False
    if current_supervisor() is not None:
        # The owner gets a chance to repair the original failure before a
        # heuristic document could hide it from the project supervisor.
        call["fallback_factory"] = None

    def repair(decision: dict[str, Any]) -> None:
        nonlocal applied_instruction
        call["resume"] = False
        applied_instruction = decision
        call["prompt"] = original_prompt + (
            "\n\n## Project supervisor recovery\n"
            "Address the diagnosed handoff problem while preserving the paper's "
            "claims, conditions and unresolved uncertainty. Do not invent evidence "
            "or shrink the task scope to make validation pass.\n"
            + wrap_untrusted("supervisor_recovery", pretty_json(decision))
        )

    if recovery_guidance:
        repair(recovery_guidance)
        call["resume"] = recovery_resume
    if not supervise:
        return pipeline._load_or_create_analysis_stage_json(**call)

    def run() -> dict:
        nonlocal reconcile_cache
        supervisor = current_supervisor()
        instruction = supervisor.current_instruction(node_id) if supervisor is not None else None
        if instruction and instruction.get("action") == "retry" and instruction != applied_instruction:
            repair(instruction)
            call["resume"] = True  # Original cache identity includes this guidance.
        if reconcile_cache:
            call["resume"] = True
            reconcile_cache = False
        return pipeline._load_or_create_analysis_stage_json(**call)

    def reconcile(_state: dict) -> Any:
        nonlocal reconcile_cache
        reconcile_cache = True
        return REPLAY_REQUIRED

    audit_dir = call.get("audit_dir", context.output_dir / "audit")
    return supervised_call(
        node_id, run,
        inputs={"owner": "paper_understanding" if node_id == "paper_understanding" else "experiment_planner",
                "stage": call["stage_label"], "source": call.get("cache_inputs", {}),
                "output_path": str(call["output_path"])},
        evidence_roots={"paper": context.output_dir / "paper_chunks.json",
                        "stage_result": call["output_path"],
                        "owner_documents": audit_dir / f"{call['stage_label']}_documents"},
        summarize=_analysis_handoff_summary,
        repair=repair,
        reconcile=reconcile, passthrough=(PipelineCancelled,),
    )


def load_paper_understanding(pipeline: Any, context: Any, *, paper: dict, paper_context: str,
                             paper_images: list, valid_chunk_ids: set, valid_pages: set) -> dict:
    return supervised_analysis_stage(pipeline, context, node_id="paper_understanding",
        output_path=context.output_dir / "paper_understanding.json", output_dir=context.output_dir,
        audit_dir=context.audit_dir, prompt=pipeline.prompt_book.render("understand_paper.md", paper_context=paper_context),
        stage_label="01_understand_paper", cleanup_stage="facts", schema_stage="paper_understanding",
        max_attempts=1, resume=context.options.resume,
        images=paper_images, backend=context.options.analysis_backend,
        cache_inputs={"paper_source_sha256": paper.get("source_sha256"), "policy": CONSOLIDATED_ANALYSIS_POLICY},
        fallback_factory=None)


def load_experiment_plan(pipeline: Any, context: Any, *, facts: dict, paper_thesis: dict | None,
                         paper: dict, paper_context: str, paper_images: list, figure_index: dict,
                         host_capabilities: dict, previous_plan: dict | None = None,
                         resolution: dict | None = None, ledger: dict | None = None,
                         round_index: int = 0, supervise: bool = True,
                         recovery_guidance: dict | None = None, recovery_resume: bool = False) -> dict:
    architecture_rules = "## Contract rules" + pipeline.prompt_book.load("design_scientific_architecture.md").split("## Contract rules", 1)[1].split("## Host capability inventory", 1)[0]
    prompt = pipeline.prompt_book.render("plan_experiments.md", architecture_rules=architecture_rules,
        paper_context=paper_context,
        understanding=wrap_untrusted("understanding", pretty_json(scientific_prompt_value({"facts": facts, "paper_thesis": paper_thesis}))),
        revision_context=wrap_untrusted("revision", pretty_json(scientific_prompt_value({
            "previous_plan": previous_plan, "resolution": resolution, "search_ledger": ledger}))),
        host_capabilities=wrap_untrusted("host_capabilities", pretty_json(host_capabilities)))
    label = "02a_plan_experiments" if round_index == 0 else f"02c_round_{round_index:02d}_revise_experiment_plan"
    return supervised_analysis_stage(pipeline, context,
        node_id="experiment_planning" if round_index == 0 else f"experiment_plan_revision_{round_index:02d}",
        supervise=supervise, recovery_guidance=recovery_guidance, recovery_resume=recovery_resume,
        output_path=context.audit_dir / f"{label}.json", output_dir=context.output_dir, audit_dir=context.audit_dir,
        prompt=prompt, stage_label=label, cleanup_stage="tasks" if round_index == 0 else "tasks_finalize",
        schema_stage="experiment_plan", max_attempts=1,
        resume=context.options.resume, images=paper_images, request_timeout=context.options.tasks_timeout,
        backend=context.options.analysis_backend,
        cache_inputs={"paper_source_sha256": paper.get("source_sha256"), "facts": facts, "paper_thesis": paper_thesis,
                      "previous_plan": previous_plan, "resolution": resolution, "ledger": ledger,
                      "figure_index": figure_index, "host_capabilities": host_capabilities,
                      "policy": CONSOLIDATED_ANALYSIS_POLICY},
        fallback_factory=None)
