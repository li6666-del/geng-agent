from __future__ import annotations

import copy
import unittest

from geng_agent.schemas import validate_stage
from geng_agent.scientific_architecture import foundation_module_paths


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


def test_architecture_science_is_preserved_without_host_equivalence_judgment():
    facts, tasks, experiments, architecture = _inputs()
    architecture["quantities"][0]["unit"] = "dBm (paper notation)"
    architecture["bindings"][0]["overrides"] = {"snr_db": [99]}
    before = copy.deepcopy(architecture)
    assert validate_stage("scientific_architecture", architecture) == []
    assert architecture == before


def test_foundation_paths_are_explicit_safe_owned_python_files():
    architecture = {"components": [{"module": "src/model.py"}, {"module": "../escape.py"},
                                    {"module": "C:/escape.py"}, {"module": "src/_private.py"}]}
    assert foundation_module_paths(architecture) == {"src/model.py", "src/_private.py"}
