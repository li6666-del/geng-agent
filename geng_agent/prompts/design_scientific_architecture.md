# Scientific architecture guidance for the combined experiment planner

Describe the shared science needed to implement the finalized tasks. The project supervisor reviews its suitability with the paper evidence; the host does not complete descriptive fields, infer scientific equivalence or rewrite defaults. Keep private tasks local when no shared implementation is scientifically needed.

## Contract rules

1. Use stable IDs for scientific quantities, components and experiments. Describe role, shape, units, scale, normalization, default and evidence basis where they affect the experiment. An unknown value stays unresolved; omit inapplicable metadata rather than inventing it.
2. Distinguish quantities that are fixed across the whole plan, fixed within a consistency group, varied by experiment, or chosen only at runtime. Explain scientifically meaningful overrides and their applicable regimes. Detect contradictory normalization or units by reading the evidence, not by matching strings.
3. Split the scientific system into components such as datasets, preprocessing, models, channels, solvers, trainers, baselines and metrics. Choose the actual scientific role rather than a fixed taxonomy. Name a real public callable when known; the Writer can complete implementation details without changing the paper method.
4. Declare each implementation `module` as a safe relative Python path under `src/`. Never use an absolute path or `..`. Use different files for shared and private components so their ownership remains unambiguous. `inputs`, `outputs`, `parameters` and `depends_on` refer to the IDs you declare.
5. State only execution requirements relevant to the component: framework/runtime, required libraries, CPU/GPU policy, precision, gradients, trainability and checkpoint behavior. A short analytical computation does not need a neural-training metadata table. Conversely, an unavailable accelerator or autodifferentiation dependency is an environment gap, never permission to replace the scientific algorithm.
6. Reused code and shared state are different. The host derives Foundation ownership from task bindings and transitive component dependencies. Shared deterministic definitions can normally run in independent Writers through `weak` relationships. Shared checkpoints, fitted preprocessing or the same random realization require the explicitly declared producer/consumer artifacts and `strong` relationships. Do not introduce strong coupling just to save inexpensive computation; do not drop scientifically necessary state to gain concurrency.
7. Bind tasks to their actual components and outputs using `task_id`. Use distinct `experiment_id` values and multiple bindings when one task contains scientifically distinct experiments. Consistency groups may overlap; a binding's primary group is not an exhaustive membership list. Express the real sharing and dependencies rather than adding empty groups to satisfy a template.
8. Optional `acceptance_bindings` connect a task's criterion ID to a measurable output; they do not redefine acceptance. Qualitative claims need no invented numerical proxy. Reporter independently checks implementation, comparability and task outcome from the paper and execution evidence. The supervisor decides the handoff.
9. Explain the evidence basis of scientifically consequential choices: `paper_explicit`, a justified `paper_derived` interpretation, an explicit assumption, or unresolved. Preserve original paper wording and its location when correcting a typo or resolving conflicting formulas. Different source names or incomplete provenance are reasons for contextual review, not reasons to delete useful content.
10. Include useful scientific invariants, such as normalization, baseline identity or required state sharing, when they help implement or audit the tasks. Avoid report formatting, figure styling and incidental helper details. No fixed inventory of invariants or execution capabilities is required for every paper.

## Document navigation

Use `quantities`, `components`, `bindings` and `consistency_groups` when applicable. A nested `basis` can contain status, evidence references and notes. Arrays for symbolic shapes and explicit ID references make the document easier to consume, but missing non-runtime description is not a host rejection rule. Keep the task's original conditions, assumptions, numeric targets and method identities intact across revisions; the supervisor can compare the original and revised documents.

## Host capability inventory (execution feasibility only)

Use this inventory to assess whether the selected scientific implementation is immediately runnable. It is not paper evidence and must not change a paper-derived algorithm. Package importability and visible accelerator hardware do not prove that a framework can use a device; the Foundation must verify that at runtime. If the scientifically required stack is unavailable, preserve the required contract and request case-environment resolution instead of silently choosing a weaker implementation. Read `python_runtime_registry[*].resolution_supported/installed/usable_now` and `external_runtime_registry[*].available` explicitly; a non-ready entry is a resolvable host gap, not permission to change the algorithm.

{{ host_capabilities_json }}

## Finalized engineering facts

{{ engineering_facts_json }}

## Finalized reproduction tasks

{{ repro_tasks_json }}

## Deterministic execution plan

This plan is compiled by the host from the Task Designer's relationships. It fixes Writer co-location and producer-before-consumer order. Architecture maps those decisions to real quantities, components, checkpoint/data interfaces, and consistency groups; it must not reinterpret the relationship strength or merge unrelated logical tasks.

{{ execution_plan_json }}

## Paper thesis

{{ paper_thesis_json }}

## Experiment index

{{ experiment_index_json }}

## Paper context

{{ paper_chunks_json }}
