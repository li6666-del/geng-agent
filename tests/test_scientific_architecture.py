from __future__ import annotations

import copy
import unittest

from geng_agent.schemas import validate_stage
from geng_agent.scientific_architecture import (
    foundation_module_paths,
    partition_scientific_architecture_issues,
    validate_scientific_architecture,
)
from geng_agent.scientific_architecture_normalize import (
    finalize_scientific_architecture,
    scientific_architecture_normalization_errors,
    scientific_architecture_normalization_warnings,
    validate_scientific_architecture_repair_preservation,
)


def _execution(*, shared: bool = True) -> dict:
    return {
        "execution_kind": "array_simulation",
        "primary_framework": "numpy",
        "supporting_libraries": ["scipy"],
        "device_policy": "cpu",
        "precision": "float64",
        "trainable": False,
        "gradient_mode": "not_applicable",
        "checkpoint_policy": "not_applicable",
        "shared_implementation": shared,
        "required_capabilities": ["batched_sampling"],
        "rationale": "The component is a deterministic shared numerical simulation.",
    }


def _declare_ber_consistency_group(architecture: dict) -> None:
    architecture["consistency_groups"] = [
        {
            "id": "ber",
            "task_ids": ["fig_1", "fig_2"],
            "shared_quantity_ids": ["snr_db"],
        }
    ]


def _scientific_acceptance(task_id: str) -> dict:
    return {
        "contract_version": "1.0",
        "core_conclusions": [
            {
                "claim_id": f"{task_id}.ber_decreases",
                "statement": "BER decreases as SNR increases.",
                "kind": "trend",
                "regime": "the declared SNR sweep",
                "paper_anchor": "Fig. 1",
            }
        ],
        "key_numeric_targets": [
            {
                "target_id": f"{task_id}.ber_at_10db",
                "name": "BER at 10 dB",
                "paper_magnitude": 0.01,
                "unit": "1",
                "regime": "SNR = 10 dB",
                "evidence_quality": "visual_estimate",
            }
        ],
        "information_gaps": [],
    }


def _acceptance_bindings(task_id: str) -> list[dict]:
    return [
        {
            "criterion_id": f"{task_id}.ber_decreases",
            "criterion_kind": "core_conclusion",
            "output_quantity_ids": ["ber"],
        },
        {
            "criterion_id": f"{task_id}.ber_at_10db",
            "criterion_kind": "key_numeric_target",
            "output_quantity_ids": ["ber"],
        },
    ]


def _inputs() -> tuple[dict, dict, dict, dict]:
    facts = {
        "engineering_facts": [
            {"type": "channel_model", "name": "AWGN"},
            {"type": "metric", "name": "BER"},
        ]
    }
    tasks = {
        "repro_tasks": [
            {"task_id": "fig_1", "assumptions": [{"name": "sample_count"}], "scientific_acceptance": _scientific_acceptance("fig_1")},
            {"task_id": "fig_2", "assumptions": [{"name": "sample_count"}], "scientific_acceptance": _scientific_acceptance("fig_2")},
        ]
    }
    experiments = {
        "experiments": [
            {"task_id": "fig_1", "experiment_id": "exp_1"},
            {"task_id": "fig_2", "experiment_id": "exp_2"},
        ]
    }
    basis = {
        "status": "paper_explicit",
        "evidence_facts": [{"type": "channel_model", "name": "AWGN"}],
        "assumption_refs": [],
        "note": "",
    }
    architecture = {
        "schema_version": "1.0",
        "workflow_version": "2",
        "quantities": [
            {
                "id": "snr_db", "role": "sweep", "dtype": "float64", "shape": ["n_snr"],
                "unit": "dB", "scale": "log_power", "normalization": "none", "scope": "global",
                "default": [0, 5, 10], "basis": basis,
            },
            {
                "id": "ber", "role": "metric", "dtype": "float64", "shape": ["n_snr"],
                "unit": "1", "scale": "log10_plot", "normalization": "errors/bits", "scope": "experiment",
                "default": None,
                "basis": {**basis, "evidence_facts": [{"type": "metric", "name": "BER"}]},
            },
        ],
        "components": [
            {
                "id": "channel", "kind": "channel", "module": "src/channel.py", "callable": "apply_awgn",
                "inputs": ["snr_db"], "outputs": [], "parameters": [], "depends_on": [], "basis": basis,
            },
            {
                "id": "metric", "kind": "metric", "module": "src/metrics.py", "callable": "bit_error_rate",
                "inputs": [], "outputs": ["ber"], "parameters": [], "depends_on": ["channel"],
                "basis": {**basis, "evidence_facts": [{"type": "metric", "name": "BER"}]},
            },
        ],
        "bindings": [
            {"task_id": "fig_1", "experiment_id": "exp_1", "consistency_group": "ber", "components": ["channel", "metric"], "overrides": {}, "outputs": ["ber"], "acceptance_bindings": _acceptance_bindings("fig_1")},
            {"task_id": "fig_2", "experiment_id": "exp_2", "consistency_group": "ber", "components": ["channel", "metric"], "overrides": {}, "outputs": ["ber"], "acceptance_bindings": _acceptance_bindings("fig_2")},
        ],
        "invariants": [
            {"id": "same_snr", "kind": "consistency", "subjects": ["snr_db"], "task_ids": ["fig_1", "fig_2"], "severity": "error", "basis": basis}
        ],
    }
    return facts, tasks, experiments, architecture


class ScientificArchitectureTests(unittest.TestCase):
    def test_valid_contract_passes_schema_and_cross_document_gate(self) -> None:
        facts, tasks, experiments, architecture = _inputs()
        self.assertEqual(validate_stage("scientific_architecture", architecture), [])
        self.assertEqual(
            validate_scientific_architecture(architecture, facts=facts, tasks=tasks, experiment_index=experiments),
            [],
        )
        self.assertEqual(foundation_module_paths(architecture), {"src/channel.py", "src/metrics.py"})

    def test_same_criterion_id_in_different_acceptance_kinds_is_not_conflated(self) -> None:
        facts, tasks, experiments, architecture = _inputs()
        acceptance = tasks["repro_tasks"][0]["scientific_acceptance"]
        shared_id = acceptance["core_conclusions"][0]["claim_id"]
        acceptance["key_numeric_targets"][0]["target_id"] = shared_id
        architecture["bindings"][0]["acceptance_bindings"][1]["criterion_id"] = shared_id

        self.assertEqual(
            validate_scientific_architecture(
                architecture,
                facts=facts,
                tasks=tasks,
                experiment_index=experiments,
            ),
            [],
        )


    def test_private_style_module_names_are_allowed_under_src(self) -> None:
        facts, tasks, experiments, architecture = _inputs()
        architecture["components"][0]["module"] = "src/_io.py"

        self.assertFalse(
            any(
                issue.path == "$.components[0].module"
                for issue in validate_scientific_architecture(
                    architecture, facts=facts, tasks=tasks, experiment_index=experiments
                )
            )
        )
        self.assertIn("src/_io.py", foundation_module_paths(architecture))


    def test_acceptance_mapping_shape_debt_is_advisory(self) -> None:
        facts, tasks, experiments, architecture = _inputs()
        binding = architecture["bindings"][0]
        mapped_claim = copy.deepcopy(binding["acceptance_bindings"][0])
        mapped_claim["criterion_kind"] = "key_numeric_target"
        binding["acceptance_bindings"] = [
            mapped_claim,
            copy.deepcopy(mapped_claim),
            {
                "criterion_id": "fig_1.unknown",
                "criterion_kind": "core_conclusion",
                "output_quantity_ids": ["ghost_quantity"],
            },
        ]

        blockers, warnings = partition_scientific_architecture_issues(
            architecture,
            facts=facts,
            tasks=tasks,
            experiment_index=experiments,
        )

        self.assertEqual(blockers, [])
        warning_paths = {issue.path for issue in warnings}
        self.assertIn("$.bindings[0].acceptance_bindings[0].criterion_kind", warning_paths)
        self.assertIn("$.bindings[0].acceptance_bindings[1].criterion_id", warning_paths)
        self.assertIn("$.bindings[0].acceptance_bindings[2].criterion_id", warning_paths)
        self.assertIn("$.bindings[0].acceptance_bindings", warning_paths)

    def test_unresolved_acceptance_outputs_are_advisory(self) -> None:
        facts, tasks, experiments, architecture = _inputs()
        architecture["bindings"][0]["acceptance_bindings"][0]["output_quantity_ids"] = [
            "ghost_quantity",
            "snr_db",
        ]

        blockers, warnings = partition_scientific_architecture_issues(
            architecture,
            facts=facts,
            tasks=tasks,
            experiment_index=experiments,
        )

        self.assertEqual(blockers, [])
        warning_paths = {issue.path for issue in warnings}
        self.assertIn(
            "$.bindings[0].acceptance_bindings[0].output_quantity_ids[0]",
            warning_paths,
        )
        self.assertIn(
            "$.bindings[0].acceptance_bindings[0].output_quantity_ids[1]",
            warning_paths,
        )

    def test_acceptance_mapping_format_noise_is_normalized_without_blocking(self) -> None:
        facts, tasks, experiments, architecture = _inputs()
        architecture["bindings"][0]["acceptance_bindings"][0] = {
            "criterion_id": 123,
            "criterion_kind": "narrative",
            "output_quantity_ids": ["ber", None],
            "comment": "advisory metadata",
        }

        normalized = finalize_scientific_architecture(architecture)
        blockers, warnings = partition_scientific_architecture_issues(
            normalized,
            facts=facts,
            tasks=tasks,
            experiment_index=experiments,
        )

        self.assertEqual(validate_stage("scientific_architecture", normalized), [])
        self.assertEqual(blockers, [])
        normalized_mapping = normalized["bindings"][0]["acceptance_bindings"][0]
        self.assertEqual(normalized_mapping["criterion_id"], "123")
        self.assertEqual(normalized_mapping["criterion_kind"], "core_conclusion")
        self.assertEqual(normalized_mapping["output_quantity_ids"], ["ber"])
        self.assertNotIn("comment", normalized_mapping)
        self.assertTrue(any("mapping is ignored" in issue.message for issue in warnings))
    def test_schema_version_must_be_explicit(self) -> None:
        _facts, _tasks, _experiments, architecture = _inputs()
        architecture.pop("schema_version")

        self.assertTrue(
            any(
                issue.path == "$.schema_version"
                for issue in validate_stage("scientific_architecture", architecture)
            )
        )

    def test_component_kind_is_not_limited_to_communication_taxonomy(self) -> None:
        _facts, _tasks, _experiments, architecture = _inputs()
        architecture["components"][0]["kind"] = "neural_model"

        self.assertEqual(validate_stage("scientific_architecture", architecture), [])

    def test_v11_execution_contract_passes_for_shared_components(self) -> None:
        facts, tasks, experiments, architecture = _inputs()
        architecture["schema_version"] = "1.1"
        _declare_ber_consistency_group(architecture)
        for component in architecture["components"]:
            component["execution"] = _execution(shared=True)

        self.assertEqual(validate_stage("scientific_architecture", architecture), [])
        self.assertEqual(
            validate_scientific_architecture(
                architecture,
                facts=facts,
                tasks=tasks,
                experiment_index=experiments,
            ),
            [],
        )

    def test_v11_requires_execution_callable_but_normalizes_shared_bookkeeping(self) -> None:
        facts, tasks, experiments, architecture = _inputs()
        architecture["schema_version"] = "1.1"
        _declare_ber_consistency_group(architecture)
        architecture["components"][0]["callable"] = ""
        architecture["components"][0]["execution"] = _execution(shared=False)
        architecture["components"][1]["execution"] = _execution(shared=True)

        schema_messages = [
            issue.message
            for issue in validate_stage("scientific_architecture", architecture)
        ]
        self.assertTrue(any("non-empty callable" in message for message in schema_messages))
        self.assertFalse(any("shared_implementation=true" in message for message in schema_messages))

        cross_issues = validate_scientific_architecture(
            architecture,
            facts=facts,
            tasks=tasks,
            experiment_index=experiments,
        )
        self.assertTrue(
            any(issue.path == "$.components[0].callable" for issue in cross_issues)
        )
        self.assertTrue(
            any(
                issue.path == "$.components[0].execution.shared_implementation"
                for issue in cross_issues
            )
        )
        blockers, warnings = partition_scientific_architecture_issues(
            architecture,
            facts=facts,
            tasks=tasks,
            experiment_index=experiments,
        )
        self.assertNotIn(
            "$.components[0].execution.shared_implementation",
            {issue.path for issue in blockers},
        )
        self.assertIn(
            "$.components[0].execution.shared_implementation",
            {issue.path for issue in warnings},
        )
        normalized = finalize_scientific_architecture(architecture)
        self.assertTrue(
            normalized["components"][0]["execution"]["shared_implementation"]
        )


        del architecture["components"][1]["execution"]
        self.assertTrue(
            any(
                issue.path == "$.components[1].execution"
                for issue in validate_scientific_architecture(
                    architecture,
                    facts=facts,
                    tasks=tasks,
                    experiment_index=experiments,
                )
            )
        )

    def test_v11_normalizes_consistency_group_bookkeeping(self) -> None:
        facts, tasks, experiments, architecture = _inputs()
        architecture["schema_version"] = "1.1"
        for component in architecture["components"]:
            component["execution"] = _execution(shared=True)

        self.assertEqual(validate_stage("scientific_architecture", architecture), [])
        blockers, warnings = partition_scientific_architecture_issues(
            architecture,
            facts=facts,
            tasks=tasks,
            experiment_index=experiments,
        )
        self.assertEqual(blockers, [])
        self.assertIn(
            "$.bindings[0].consistency_group", {issue.path for issue in warnings}
        )

        normalized = finalize_scientific_architecture(architecture)
        self.assertEqual(validate_stage("scientific_architecture", normalized), [])
        self.assertEqual(normalized["consistency_groups"][0]["id"], "ber")
        self.assertEqual(
            normalized["consistency_groups"][0]["task_ids"], ["fig_1", "fig_2"]
        )

        _declare_ber_consistency_group(architecture)
        architecture["consistency_groups"].append(
            copy.deepcopy(architecture["consistency_groups"][0])
        )
        normalized_duplicate = finalize_scientific_architecture(architecture)
        self.assertEqual(
            [group["id"] for group in normalized_duplicate["consistency_groups"]],
            ["ber"],
        )

    def test_missing_binding_unsafe_module_and_global_override_are_rejected(self) -> None:
        facts, tasks, experiments, architecture = _inputs()
        broken = copy.deepcopy(architecture)
        broken["bindings"] = broken["bindings"][:1]
        broken["bindings"][0]["allowed_overrides"] = ["ber"]
        broken["bindings"][0]["overrides"] = {"snr_db": [1, 2]}
        broken["components"][0]["module"] = "../channel.py"
        broken["invariants"][0]["subjects"] = ["ghost_quantity"]

        messages = [
            f"{issue.path}: {issue.message}"
            for issue in validate_scientific_architecture(
                broken, facts=facts, tasks=tasks, experiment_index=experiments
            )
        ]

        self.assertTrue(any("safe relative Python path" in message for message in messages))
        self.assertTrue(any("not listed in allowed_overrides" in message for message in messages))
        self.assertTrue(any("global quantities cannot be overridden" in message for message in messages))
        self.assertTrue(any("missing architecture binding for task: fig_2" in message for message in messages))
        self.assertTrue(any("declared quantity or component" in message for message in messages))

        no_whitelist = copy.deepcopy(architecture)
        no_whitelist["bindings"][0]["overrides"] = {"ber": 0.1}
        no_whitelist_issues = validate_scientific_architecture(
            no_whitelist, facts=facts, tasks=tasks, experiment_index=experiments
        )
        self.assertTrue(any("not listed in allowed_overrides" in issue.message for issue in no_whitelist_issues))
        whitelist_blockers, whitelist_warnings = partition_scientific_architecture_issues(
            no_whitelist,
            facts=facts,
            tasks=tasks,
            experiment_index=experiments,
        )
        self.assertNotIn(
            "$.bindings[0].overrides.ber",
            {issue.path for issue in whitelist_blockers},
        )
        self.assertIn(
            "$.bindings[0].overrides.ber",
            {issue.path for issue in whitelist_warnings},
        )
        normalized_whitelist = finalize_scientific_architecture(no_whitelist)
        self.assertEqual(normalized_whitelist["bindings"][0]["allowed_overrides"], ["ber"])


    def test_partition_blocks_only_contract_defects_needed_for_execution(self) -> None:
        facts, tasks, experiments, architecture = _inputs()

        cases = [
            (
                "missing component id",
                lambda item: item["components"][0].pop("id"),
                "$.components[0].id",
            ),
            (
                "duplicate component id",
                lambda item: item["components"][1].update(id="channel"),
                "$.components[1].id",
            ),
            (
                "missing quantity id",
                lambda item: item["quantities"][0].pop("id"),
                "$.quantities[0].id",
            ),
            (
                "duplicate quantity id",
                lambda item: item["quantities"][1].update(id="snr_db"),
                "$.quantities[1].id",
            ),
            (
                "missing module",
                lambda item: item["components"][0].update(module=""),
                "$.components[0].module",
            ),
            (
                "unsafe module",
                lambda item: item["components"][0].update(module="../channel.py"),
                "$.components[0].module",
            ),
            (
                "unknown component input",
                lambda item: item["components"][0].update(inputs=["ghost_quantity"]),
                "$.components[0].inputs[0]",
            ),
            (
                "unknown component output",
                lambda item: item["components"][0].update(outputs=["ghost_quantity"]),
                "$.components[0].outputs[0]",
            ),
            (
                "unknown component parameter",
                lambda item: item["components"][0].update(parameters=["ghost_quantity"]),
                "$.components[0].parameters[0]",
            ),
            (
                "unknown dependency",
                lambda item: item["components"][1].update(depends_on=["ghost_component"]),
                "$.components[1].depends_on[0]",
            ),
            (
                "unknown bound component",
                lambda item: item["bindings"][0].update(components=["ghost_component"]),
                "$.bindings[0].components[0]",
            ),
            (
                "experiment mismatch",
                lambda item: item["bindings"][0].update(experiment_id="wrong_experiment"),
                "$.bindings[0].experiment_id",
            ),
            (
                "missing task binding",
                lambda item: item.update(bindings=item["bindings"][:1]),
                "$.bindings",
            ),
            (
                "duplicate task binding",
                lambda item: item["bindings"].append(copy.deepcopy(item["bindings"][0])),
                "$.bindings[2].task_id",
            ),
        ]
        for label, mutate, expected_path in cases:
            with self.subTest(label=label):
                candidate = copy.deepcopy(architecture)
                mutate(candidate)
                blockers, _warnings = partition_scientific_architecture_issues(
                    candidate,
                    facts=facts,
                    tasks=tasks,
                    experiment_index=experiments,
                )
                self.assertIn(expected_path, {issue.path for issue in blockers})

    def test_common_architecture_dialect_is_normalized_without_changing_science(self) -> None:
        facts, tasks, experiments, architecture = _inputs()
        dialect = copy.deepcopy(architecture)
        dialect["consistency_groups"] = [
            {"id": "ber", "task_ids": ["fig_1", "fig_2"], "shared_quantity_ids": ["snr_db"]}
        ]
        dialect["task_bindings"] = dialect.pop("bindings")

        def flatten_basis(item: dict) -> None:
            basis = item.pop("basis")
            item["basis"] = basis["status"]
            item["evidence_facts"] = basis["evidence_facts"]
            item["assumption_refs"] = basis["assumption_refs"]
            item["note"] = basis["note"]

        dialect["quantities"][0]["shape"] = ["B×L×E"]
        original_defaults = [copy.deepcopy(item["default"]) for item in dialect["quantities"]]
        for quantity in dialect["quantities"]:
            quantity["shape"] = quantity["shape"][0]
            flatten_basis(quantity)
        for component in dialect["components"]:
            component.pop("callable")
            flatten_basis(component)
        for binding in dialect["task_bindings"]:
            binding["consistency_group_id"] = binding.pop("consistency_group")
            binding["component_ids"] = binding.pop("components")
            binding["output_quantity_ids"] = binding.pop("outputs")
            binding["allowed_overrides"] = ["ber"]
            for acceptance in binding["acceptance_bindings"]:
                criterion_kind = acceptance.pop("criterion_kind")
                criterion_id = acceptance.pop("criterion_id")
                if criterion_kind == "core_conclusion":
                    acceptance["claim_id"] = criterion_id
                else:
                    acceptance["target_id"] = criterion_id
                acceptance["outputs"] = acceptance.pop("output_quantity_ids")
        for invariant in dialect["invariants"]:
            invariant.pop("kind")
            invariant.pop("subjects")
            invariant["description"] = "The SNR grid is shared."
            invariant["expression"] = "same(snr_db)"
            flatten_basis(invariant)

        original = copy.deepcopy(dialect)
        normalized = finalize_scientific_architecture(dialect)

        self.assertEqual(dialect, original, "normalization must not mutate the candidate")
        self.assertEqual(finalize_scientific_architecture(normalized), normalized, "normalization must be idempotent")
        self.assertEqual([item["default"] for item in normalized["quantities"]], original_defaults)
        self.assertEqual(normalized["quantities"][0]["shape"], ["B×L×E"])
        self.assertEqual(normalized["components"][0]["callable"], "")
        self.assertEqual(normalized["bindings"][0]["allowed_overrides"], ["ber"])
        normalized_acceptance = normalized["bindings"][0]["acceptance_bindings"]
        self.assertEqual(normalized_acceptance[0]["criterion_id"], "fig_1.ber_decreases")
        self.assertEqual(normalized_acceptance[0]["criterion_kind"], "core_conclusion")
        self.assertEqual(normalized_acceptance[0]["output_quantity_ids"], ["ber"])
        self.assertEqual(normalized_acceptance[1]["criterion_id"], "fig_1.ber_at_10db")
        self.assertEqual(normalized_acceptance[1]["criterion_kind"], "key_numeric_target")
        self.assertEqual(validate_stage("scientific_architecture", normalized), [])
        self.assertEqual(
            validate_scientific_architecture(normalized, facts=facts, tasks=tasks, experiment_index=experiments),
            [],
        )
        self.assertGreater(len(scientific_architecture_normalization_warnings(normalized)), 0)
        self.assertEqual(scientific_architecture_normalization_errors(normalized), [])

    def test_alias_conflicts_warn_and_repair_still_protects_existing_science(self) -> None:
        _, _, _, architecture = _inputs()
        conflicting = copy.deepcopy(architecture)
        conflicting["task_bindings"] = [{"task_id": "different"}]
        normalized = finalize_scientific_architecture(conflicting)
        self.assertEqual(scientific_architecture_normalization_errors(normalized), [])
        self.assertTrue(any("kept canonical value" in issue.message for issue in scientific_architecture_normalization_warnings(normalized)))

        repaired = copy.deepcopy(architecture)
        repaired["quantities"][0]["default"] = [1, 2, 3]
        issues = validate_scientific_architecture_repair_preservation(architecture, repaired)
        self.assertTrue(any(issue.path == "$.quantities[0].default" for issue in issues))

    def test_repair_may_add_fields_and_fix_references(self) -> None:
        _, _, _, architecture = _inputs()
        repaired = copy.deepcopy(architecture)
        repaired["components"][0]["execution"] = _execution(shared=True)
        repaired["components"][0]["inputs"] = ["snr_db", "ber"]
        repaired["bindings"][0]["consistency_group"] = "repaired_group"
        repaired["quantities"].append(
            {
                **copy.deepcopy(repaired["quantities"][1]),
                "id": "new_diagnostic_quantity",
            }
        )

        self.assertEqual(
            validate_scientific_architecture_repair_preservation(
                architecture, repaired
            ),
            [],
        )

    def test_normalization_maps_python_runtime_labels_to_standard_library(self) -> None:
        _, _, _, architecture = _inputs()
        architecture["schema_version"] = "1.1"
        for component in architecture["components"]:
            component["execution"] = _execution(shared=True)
            component["execution"]["primary_framework"] = "python_3.11"

        normalized = finalize_scientific_architecture(architecture)

        self.assertTrue(all(
            component["execution"]["primary_framework"] == "standard_library"
            for component in normalized["components"]
        ))

    def test_normalization_and_repair_preserve_existing_execution_contract(self) -> None:
        _, _, _, architecture = _inputs()
        architecture["schema_version"] = "1.1"
        for component in architecture["components"]:
            component["execution"] = _execution(shared=True)

        normalized = finalize_scientific_architecture(architecture)
        self.assertEqual(
            normalized["components"][0]["execution"],
            architecture["components"][0]["execution"],
        )
        self.assertEqual(finalize_scientific_architecture(normalized), normalized)

        changed = copy.deepcopy(normalized)
        changed["components"][0]["execution"]["primary_framework"] = "jax"
        issues = validate_scientific_architecture_repair_preservation(normalized, changed)
        self.assertTrue(
            any(
                issue.path == "$.components[0].execution.primary_framework"
                for issue in issues
            )
        )

    def test_partition_keeps_evidence_and_invariant_completeness_advisory(self) -> None:
        facts, tasks, experiments, architecture = _inputs()
        architecture["components"][0]["basis"] = {
            "status": "assumed",
            "evidence_facts": [{"type": "channel_model", "name": "missing_fact"}],
            "assumption_refs": ["missing_assumption"],
            "note": "Evidence debt must remain visible without stopping execution.",
        }
        architecture["invariants"][0]["subjects"] = ["missing_subject"]
        architecture["invariants"][0]["task_ids"] = ["missing_task"]

        blockers, warnings = partition_scientific_architecture_issues(
            architecture,
            facts=facts,
            tasks=tasks,
            experiment_index=experiments,
        )

        self.assertEqual(blockers, [])
        warning_paths = {issue.path for issue in warnings}
        self.assertIn("$.components[0].basis.evidence_facts[0]", warning_paths)
        self.assertIn("$.components[0].basis.assumption_refs[0]", warning_paths)
        self.assertIn("$.invariants[0].subjects[0]", warning_paths)
        self.assertIn("$.invariants[0].task_ids[0]", warning_paths)

    def test_non_shared_overrides_may_differ_inside_consistency_group(self) -> None:
        facts, tasks, experiments, architecture = _inputs()
        for index, binding in enumerate(architecture["bindings"]):
            binding["allowed_overrides"] = ["ber"]
            binding["overrides"] = {"ber": 0.1 + index}

        blockers, _warnings = partition_scientific_architecture_issues(
            architecture,
            facts=facts,
            tasks=tasks,
            experiment_index=experiments,
        )

        self.assertFalse(any("conflicts" in issue.message for issue in blockers))

    def test_numerically_equivalent_shared_overrides_do_not_conflict(self) -> None:
        facts, tasks, experiments, architecture = _inputs()
        _declare_ber_consistency_group(architecture)
        architecture["quantities"][0]["scope"] = "consistency_group"
        architecture["bindings"][0]["allowed_overrides"] = ["snr_db"]
        architecture["bindings"][0]["overrides"] = {"snr_db": [0, 5, 10]}
        architecture["bindings"][1]["allowed_overrides"] = ["snr_db"]
        architecture["bindings"][1]["overrides"] = {
            "snr_db": [0.0, 5.0, 10.0]
        }

        blockers, _warnings = partition_scientific_architecture_issues(
            architecture,
            facts=facts,
            tasks=tasks,
            experiment_index=experiments,
        )

        self.assertFalse(
            any("conflicts inside one consistency_group" in issue.message for issue in blockers)
        )

    def test_conflicting_shared_overrides_are_execution_blockers(self) -> None:
        facts, tasks, experiments, architecture = _inputs()
        _declare_ber_consistency_group(architecture)
        architecture["quantities"][0]["scope"] = "consistency_group"
        for index, binding in enumerate(architecture["bindings"]):
            binding["allowed_overrides"] = ["snr_db"]
            binding["overrides"] = {"snr_db": [0, 5 + index, 10]}

        blockers, _warnings = partition_scientific_architecture_issues(
            architecture,
            facts=facts,
            tasks=tasks,
            experiment_index=experiments,
        )

        self.assertTrue(
            any(
                issue.path == "$.bindings[1].overrides.snr_db"
                and "conflicts" in issue.message
                for issue in blockers
            )
        )


if __name__ == "__main__":
    unittest.main()
