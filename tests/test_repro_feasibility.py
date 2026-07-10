from __future__ import annotations

import copy
import unittest

from geng_agent.repro_feasibility import FEASIBILITY_MODES, classify_repro_feasibility


def _task(**updates) -> dict:
    task = {
        "task_id": "ber_awgn",
        "metric": "bit_error_rate",
        "output_columns": ["snr_db", "ber"],
        "required_facts": [
            {"type": "modulation", "name": "QPSK"},
            {"type": "channel_model", "name": "AWGN"},
        ],
        "required_packages": ["numpy"],
    }
    task.update(updates)
    return task


def _facts(**updates) -> dict:
    facts = {
        "engineering_facts": [
            {"type": "modulation", "name": "QPSK", "confidence": "high"},
            {"type": "channel_model", "name": "AWGN", "confidence": "medium"},
        ],
        "missing_information": [],
    }
    facts.update(updates)
    return facts


def _environment(**updates) -> dict:
    environment = {
        "ready": True,
        "full_scale_supported": True,
        "packages": {"numpy": "installed"},
    }
    environment.update(updates)
    return environment


class ReproFeasibilityTests(unittest.TestCase):
    def test_native_full_requires_resolved_facts_and_environment(self) -> None:
        profile = classify_repro_feasibility(_task(), _facts(), _environment())

        self.assertEqual(profile["mode"], "native_full")
        self.assertEqual(set(profile), {"mode", "reasons", "requirements", "evidence"})
        self.assertTrue(profile["reasons"])
        self.assertTrue(all(item["status"] == "available" for item in profile["requirements"]))

    def test_scaled_full_requires_preserved_method_semantics(self) -> None:
        profile = classify_repro_feasibility(
            _task(scale_reduced=True, scale_preserves_method=True),
            _facts(),
            _environment(full_scale_supported=False),
        )

        self.assertEqual(profile["mode"], "scaled_full")
        self.assertTrue(any("fidelity_preserved" in reason for reason in profile["reasons"]))

    def test_unproven_scaled_fidelity_is_proxy_only(self) -> None:
        profile = classify_repro_feasibility(
            _task(scale_reduced=True, allow_scaled=True),
            _facts(),
            _environment(full_scale_supported=False),
        )

        self.assertEqual(profile["mode"], "proxy_only")
        self.assertTrue(any("scale_fidelity_unproven" in reason for reason in profile["reasons"]))

    def test_high_impact_missing_information_is_proxy_only(self) -> None:
        profile = classify_repro_feasibility(
            _task(),
            _facts(
                missing_information=[
                    {"name": "decoder stopping rule", "why_needed": "controls BER", "impact": "high"}
                ]
            ),
            _environment(),
        )

        self.assertEqual(profile["mode"], "proxy_only")
        self.assertTrue(any("high_impact_information_missing" in reason for reason in profile["reasons"]))

    def test_missing_runtime_requirement_is_environment_blocked(self) -> None:
        profile = classify_repro_feasibility(
            _task(required_hardware=["gpu"]),
            _facts(),
            _environment(hardware={"gpu": False}),
        )

        self.assertEqual(profile["mode"], "environment_blocked")
        gpu = next(item for item in profile["requirements"] if item["name"] == "gpu")
        self.assertEqual(gpu["status"], "missing")

    def test_missing_packages_field_resolves_declared_package_as_missing(self) -> None:
        profile = classify_repro_feasibility(
            _task(),
            _facts(),
            _environment(packages={}, missing_packages=["numpy"]),
        )

        self.assertEqual(profile["mode"], "environment_blocked")
        numpy_requirements = [item for item in profile["requirements"] if item["name"] == "numpy"]
        self.assertEqual(numpy_requirements, [
            {
                "name": "numpy",
                "kind": "software",
                "status": "missing",
                "source": "environment.missing_packages",
            }
        ])

    def test_unknown_runtime_requirement_is_not_optimistically_full(self) -> None:
        profile = classify_repro_feasibility(
            _task(required_hardware=["fpga"]),
            _facts(),
            _environment(),
        )

        self.assertEqual(profile["mode"], "environment_blocked")
        self.assertTrue(any("environment_requirement_unknown" in reason for reason in profile["reasons"]))

    def test_upstream_patch_has_precedence_over_environment_blocker(self) -> None:
        profile = classify_repro_feasibility(
            _task(upstream_patch_required=True),
            _facts(),
            _environment(ready=False),
        )

        self.assertEqual(profile["mode"], "upstream_patch_required")
        self.assertTrue(any(item["name"] == "upstream_patch" for item in profile["requirements"]))

    def test_applied_patch_allows_native_classification(self) -> None:
        profile = classify_repro_feasibility(
            _task(upstream_patch_required=True),
            _facts(),
            _environment(upstream_patch_applied=True),
        )

        self.assertEqual(profile["mode"], "native_full")

    def test_other_metric_stays_proxy_even_with_executable_flag(self) -> None:
        profile = classify_repro_feasibility(
            _task(metric="other", executable=True),
            _facts(),
            _environment(),
        )

        self.assertEqual(profile["mode"], "proxy_only")
        self.assertTrue(any("metric_unspecified" in reason for reason in profile["reasons"]))

    def test_inputs_are_not_mutated_and_mode_vocabulary_is_complete(self) -> None:
        task = _task()
        facts = _facts()
        environment = _environment()
        before = copy.deepcopy((task, facts, environment))

        profile = classify_repro_feasibility(task, facts, environment)

        self.assertEqual((task, facts, environment), before)
        self.assertIn(profile["mode"], FEASIBILITY_MODES)
        self.assertEqual(
            FEASIBILITY_MODES,
            {
                "native_full",
                "scaled_full",
                "proxy_only",
                "environment_blocked",
                "upstream_patch_required",
            },
        )

    def test_non_dict_input_fails_fast(self) -> None:
        with self.assertRaisesRegex(TypeError, "task must be a dict"):
            classify_repro_feasibility([], {}, {})  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
