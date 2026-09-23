from __future__ import annotations


import copy


import json


from pathlib import Path


from tempfile import TemporaryDirectory


import unittest


from geng_agent.agentic_foundation import (_foundation_brief, _initial_foundation_requirements, _required_foundation_modules)


from geng_agent.io_runtime import inject_io_runtime


def _execution(**overrides: object) -> dict:
    execution = {
        "execution_kind": "neural_model",
        "primary_framework": "PyTorch",
        "supporting_libraries": ["numpy"],
        "device_policy": "accelerator_preferred",
        "precision": "float32",
        "trainable": True,
        "gradient_mode": "required",
        "checkpoint_policy": "required",
        "shared_implementation": True,
        "required_capabilities": ["batched_inference"],
        "rationale": "The paper trains a shared neural encoder.",
    }
    execution.update(overrides)
    return execution


def _architecture(*, schema_version: str = "1.1", execution: dict | None = None) -> dict:
    component = {
        "id": "encoder",
        "kind": "transmitter",
        "module": "src/model.py",
        "callable": "Encoder.forward",
    }
    if execution is not None or schema_version == "1.1":
        component["execution"] = copy.deepcopy(execution or _execution())
    return {"schema_version": schema_version, "components": [component]}


class FoundationPromptContractTests(unittest.TestCase):
    def test_brief_preserves_component_stack_and_rejects_reference_substitution(self) -> None:
        architecture = _architecture()

        brief = _foundation_brief(architecture)

        self.assertIn('"module": "src/model.py"', brief)
        self.assertIn('"callable": "Encoder.forward"', brief)
        self.assertIn('"primary_framework": "PyTorch"', brief)
        self.assertIn("mixed-framework Foundation is valid", brief)
        self.assertIn("non-trainable reference", brief)
        self.assertIn("test_evidence", brief)
        self.assertNotIn("trusted probe registry", brief)
        self.assertIn("Report conflicts to the supervisor", brief)

    def test_v11_initial_files_follow_architecture_instead_of_legacy_communication_defaults(self) -> None:
        architecture = _architecture(
            execution=_execution(supporting_libraries=["numpy", "tensorflow", "not-a-package"])
        )

        self.assertEqual(_required_foundation_modules(architecture), {"src/model.py"})
        self.assertEqual(
            _initial_foundation_requirements(architecture),
            "matplotlib\nnot-a-package\nnumpy\ntensorflow\ntorch\n",
        )
        with TemporaryDirectory() as temp:
            sandbox = Path(temp)
            (sandbox / "requirements.txt").write_text(
                _initial_foundation_requirements(architecture),
                encoding="utf-8",
            )
            inject_io_runtime(sandbox)
            final_requirements = set(
                (sandbox / "requirements.txt").read_text(encoding="utf-8").splitlines()
            )
        self.assertEqual(
            final_requirements,
            {"matplotlib", "not-a-package", "numpy", "tensorflow", "torch"},
        )

        legacy = _architecture(schema_version="1.0")
        self.assertEqual(
            _required_foundation_modules(legacy),
            {"src/model.py"},
        )
        self.assertEqual(_initial_foundation_requirements(legacy), "matplotlib\nnumpy\n")
