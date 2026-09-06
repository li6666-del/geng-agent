from __future__ import annotations

import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from geng_agent.agentic_foundation import (
    FOUNDATION_CORE_MODULES,
    _foundation_brief,
    _initial_foundation_requirements,
    _required_foundation_modules,
    _validate_foundation_delivery,
    _validate_foundation_execution_contracts,
)
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


def _result(architecture: dict, capability_tests: list[dict] | None = None) -> dict:
    component = architecture["components"][0]
    result = {
        "status": "ready_for_tasks",
        "execution_contracts": [
            {
                "component_id": component["id"],
                "module": component["module"],
                "callable": component["callable"],
                "execution": copy.deepcopy(component["execution"]),
            }
        ],
    }
    if capability_tests is not None:
        bound_tests = copy.deepcopy(capability_tests)
        for item in bound_tests:
            item.setdefault("module", component["module"])
            item.setdefault("callable", component["callable"])
        result["capability_tests"] = bound_tests
    return result


def _passing_capability_tests() -> list[dict]:
    return [
        {
            "component_id": "encoder",
            "capability": "batched_inference",
            "test": "tests.test_model.ModelTests.test_batched_inference",
            "status": "passed",
        },
        {
            "component_id": "encoder",
            "capability": "training_step",
            "test": "tests.test_model.ModelTests.test_parameter_update",
            "status": "passed",
        },
        {
            "component_id": "encoder",
            "capability": "gradient_flow",
            "test": "tests.test_model.ModelTests.test_gradient_flow",
            "status": "passed",
        },
        {
            "component_id": "encoder",
            "capability": "checkpoint_roundtrip",
            "test": "tests.test_model.ModelTests.test_checkpoint_roundtrip",
            "status": "passed",
        },
    ]


def _torch_model_source() -> str:
    return (
        "import torch\n\n"
        "class Encoder(torch.nn.Module):\n"
        "    def __init__(self):\n"
        "        super().__init__()\n"
        "        self.linear = torch.nn.Linear(1, 1, bias=False)\n"
        "    def forward(self, value):\n"
        "        return self.linear(value)\n"
    )


def _external_runtime_source() -> str:
    return (
        "class Encoder:\n"
        "    def __init__(self, runtime_client=None):\n"
        "        self.runtime_client = runtime_client\n"
        "    def runtime_available(self):\n"
        "        return self.runtime_client is not None and self.runtime_client.available()\n"
        "    def forward(self, value):\n"
        "        if self.runtime_client is None:\n"
        "            raise RuntimeError('trusted runtime adapter required')\n"
        "        return self.runtime_client.invoke(value)\n"
    )


def _write_sandbox(root: Path, *, source: str, requirements: str = "numpy\ntorch\n") -> None:
    module = root / "src" / "model.py"
    module.parent.mkdir(parents=True)
    module.write_text(source, encoding="utf-8")
    (root / "requirements.txt").write_text(requirements, encoding="utf-8")
    test_module = root / "tests" / "test_model.py"
    test_module.parent.mkdir()
    test_module.write_text(
        "import unittest\n"
        "import torch\n"
        "from src.model import Encoder\n\n"
        "class ModelTests(unittest.TestCase):\n"
        "    def test_batched_inference(self):\n"
        "        model = Encoder()\n"
        "        output = model(torch.ones(2, 1))\n"
        "        self.assertEqual(output.shape[0], 2)\n"
        "    def test_parameter_update(self):\n"
        "        model = Encoder()\n"
        "        optimizer = torch.optim.SGD(model.parameters(), lr=0.1)\n"
        "        before = next(model.parameters()).detach().clone()\n"
        "        loss = model(torch.ones(2, 1)).sum()\n"
        "        optimizer.zero_grad()\n"
        "        loss.backward()\n"
        "        optimizer.step()\n"
        "        after = next(model.parameters()).detach()\n"
        "        self.assertFalse(torch.equal(before, after))\n"
        "    def test_gradient_flow(self):\n"
        "        model = Encoder()\n"
        "        model(torch.ones(2, 1)).sum().backward()\n"
        "        gradient = next(model.parameters()).grad\n"
        "        self.assertIsNotNone(gradient)\n"
        "    def test_checkpoint_roundtrip(self):\n"
        "        model = Encoder()\n"
        "        state = model.state_dict()\n"
        "        restored = Encoder()\n"
        "        restored.load_state_dict(state)\n"
        "        self.assertTrue(all(torch.equal(state[key], restored.state_dict()[key]) for key in state))\n"
        "    def test_device_available(self):\n"
        "        model = Encoder()\n"
        "        parameter_device = next(model.parameters()).device.type\n"
        "        self.assertTrue(torch.cuda.is_available() and parameter_device == 'cpu')\n"
        "    def test_tensor_device(self):\n"
        "        model = Encoder().to('cuda')\n"
        "        value = torch.ones(2, 1, device='cuda')\n"
        "        output = model(value)\n"
        "        self.assertEqual(output.device.type, 'cuda')\n"
        "    def test_external_runtime_available(self):\n"
        "        model = Encoder()\n"
        "        self.assertTrue(model.runtime_available())\n"
        "    def test_external_runtime_invocation(self):\n"
        "        model = Encoder()\n"
        "        result = model.forward([1.0])\n"
        "        self.assertIsNotNone(result)\n"
        "    def test_name_only_evidence(self):\n"
        "        model = Encoder()\n"
        "        self.assertEqual(model.__class__.__name__, 'Encoder')\n"
        "    def test_empty_evidence(self):\n"
        "        pass\n"
        "    @unittest.skip('not executable evidence')\n"
        "    def test_skipped_evidence(self):\n"
        "        self.assertTrue(True)\n"
        "    @unittest.expectedFailure\n"
        "    def test_expected_failure_evidence(self):\n"
        "        self.assertEqual(1, 2)\n"
        "    @unittest.skipIf(True, 'constant skip')\n"
        "    def test_skipif_true_evidence(self):\n"
        "        self.assertEqual(1, 1)\n"
        "    @unittest.skipIf(False, 'must run')\n"
        "    def test_skipif_false_evidence(self):\n"
        "        model = Encoder()\n"
        "        self.assertIsNotNone(model.forward([1.0]))\n"
        "    @unittest.skipUnless(True, 'must run')\n"
        "    def test_skipunless_true_evidence(self):\n"
        "        model = Encoder()\n"
        "        self.assertIsNotNone(model.forward([1.0]))\n",
        encoding="utf-8",
    )


def _write_project_local_sandbox(root: Path) -> None:
    module = root / "src" / "model.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "class Encoder:\n"
        "    def __init__(self):\n"
        "        self.weight = 1.0\n"
        "    def forward(self, value):\n"
        "        return self.weight * value\n"
        "    def learn_once(self):\n"
        "        self.weight += 0.5\n"
        "    def derivative(self):\n"
        "        return self.weight\n"
        "    def pack(self):\n"
        "        return {'weight': self.weight}\n"
        "    def unpack(self, payload):\n"
        "        self.weight = payload['weight']\n",
        encoding="utf-8",
    )
    (root / "requirements.txt").write_text("", encoding="utf-8")
    test_module = root / "tests" / "test_model.py"
    test_module.parent.mkdir()
    test_module.write_text(
        "import unittest\n"
        "from src.model import Encoder\n\n"
        "class ModelTests(unittest.TestCase):\n"
        "    def test_custom_learning(self):\n"
        "        model = Encoder()\n"
        "        before = model.weight\n"
        "        model.learn_once()\n"
        "        after = model.weight\n"
        "        self.assertNotEqual(before, after)\n"
        "    def test_custom_derivative(self):\n"
        "        model = Encoder()\n"
        "        derivative = model.derivative()\n"
        "        self.assertNotEqual(derivative, 0.0)\n"
        "    def test_custom_pack_unpack(self):\n"
        "        model = Encoder()\n"
        "        payload = model.pack()\n"
        "        restored = Encoder()\n"
        "        restored.unpack(payload)\n"
        "        self.assertEqual(restored.weight, model.weight)\n",
        encoding="utf-8",
    )


class FoundationExecutionContractTests(unittest.TestCase):
    def test_brief_preserves_component_stack_and_rejects_reference_substitution(self) -> None:
        architecture = _architecture()

        brief = _foundation_brief(architecture)

        self.assertIn('"module": "src/model.py"', brief)
        self.assertIn('"callable": "Encoder.forward"', brief)
        self.assertIn('"primary_framework": "PyTorch"', brief)
        self.assertIn("mixed-framework Foundation is valid", brief)
        self.assertIn("non-trainable reference", brief)
        self.assertIn("request an architecture revision", brief)
        self.assertIn("execution_contracts", brief)
        self.assertIn("capability_tests", brief)
        self.assertEqual(brief.count('"execution_contracts"'), 1)
        self.assertEqual(brief.count('"capability_tests"'), 1)

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
            set(FOUNDATION_CORE_MODULES) | {"src/model.py"},
        )
        self.assertEqual(_initial_foundation_requirements(legacy), "matplotlib\nnumpy\n")

    def test_valid_contract_accepts_dotted_callable_and_never_executes_source(self) -> None:
        architecture = _architecture()
        result = _result(architecture, _passing_capability_tests())
        with TemporaryDirectory() as temp:
            sandbox = Path(temp)
            _write_sandbox(
                sandbox,
                source=(
                    _torch_model_source()
                    + "\nraise RuntimeError('static validation must not execute this module')\n"
                ),
            )

            issues, warnings = _validate_foundation_execution_contracts(
                sandbox=sandbox,
                architecture=architecture,
                result=result,
            )

        self.assertEqual(issues, [])
        self.assertEqual(warnings, [])

    def test_missing_top_level_callable_is_blocking(self) -> None:
        architecture = _architecture()
        with TemporaryDirectory() as temp:
            sandbox = Path(temp)
            _write_sandbox(sandbox, source="import torch\n\nclass DifferentEncoder:\n    pass\n")

            issues, _ = _validate_foundation_execution_contracts(
                sandbox=sandbox,
                architecture=architecture,
                result=_result(architecture, _passing_capability_tests()),
            )

        self.assertTrue(any("callable Encoder.forward is absent" in item["message"] for item in issues))

    def test_missing_dotted_method_is_blocking_even_when_class_exists(self) -> None:
        architecture = _architecture()
        with TemporaryDirectory() as temp:
            sandbox = Path(temp)
            _write_sandbox(sandbox, source="import torch\n\nclass Encoder:\n    pass\n")

            issues, _ = _validate_foundation_execution_contracts(
                sandbox=sandbox,
                architecture=architecture,
                result=_result(architecture, _passing_capability_tests()),
            )

        self.assertTrue(any("callable Encoder.forward is absent" in item["message"] for item in issues))

    def test_callable_accepts_local_reexport_alias_and_inherited_method(self) -> None:
        execution = _execution(
            primary_framework="standard_library",
            supporting_libraries=[],
            device_policy="cpu",
            trainable=False,
            gradient_mode="not_applicable",
            checkpoint_policy="not_applicable",
            required_capabilities=[],
        )
        architecture = _architecture(execution=execution)
        cases = [
            (
                "from src.impl import Model as Encoder\n",
                "class Model:\n    def forward(self, value):\n        return value\n",
            ),
            (
                "class Model:\n"
                "    def forward(self, value):\n"
                "        return value\n"
                "Encoder = Model\n",
                None,
            ),
            (
                "class BaseEncoder:\n"
                "    def forward(self, value):\n"
                "        return value\n"
                "class Encoder(BaseEncoder):\n"
                "    pass\n",
                None,
            ),
        ]
        for source, implementation in cases:
            with self.subTest(source=source), TemporaryDirectory() as temp:
                sandbox = Path(temp)
                _write_sandbox(sandbox, source=source, requirements="")
                if implementation is not None:
                    (sandbox / "src" / "__init__.py").write_text("", encoding="utf-8")
                    (sandbox / "src" / "impl.py").write_text(implementation, encoding="utf-8")

                issues, warnings = _validate_foundation_execution_contracts(
                    sandbox=sandbox,
                    architecture=architecture,
                    result=_result(architecture),
                )

                self.assertEqual(issues, [])
                self.assertEqual(warnings, [])

    def test_external_base_method_uncertainty_warns_but_direct_absence_blocks(self) -> None:
        execution = _execution(
            primary_framework="standard_library",
            supporting_libraries=[],
            device_policy="cpu",
            trainable=False,
            gradient_mode="not_applicable",
            checkpoint_policy="not_applicable",
            required_capabilities=[],
        )
        architecture = _architecture(execution=execution)
        with TemporaryDirectory() as temp:
            sandbox = Path(temp)
            _write_sandbox(
                sandbox,
                source=(
                    "from external_package import ExternalBase\n\n"
                    "class Encoder(ExternalBase):\n"
                    "    pass\n"
                ),
                requirements="",
            )

            issues, warnings = _validate_foundation_execution_contracts(
                sandbox=sandbox,
                architecture=architecture,
                result=_result(architecture),
            )

        self.assertEqual(issues, [])
        self.assertTrue(any("cannot be proven statically" in item["message"] for item in warnings))

    def test_non_src_declared_module_cannot_disappear_from_static_gate(self) -> None:
        execution = _execution(
            primary_framework="standard_library",
            supporting_libraries=[],
            device_policy="cpu",
            trainable=False,
            gradient_mode="not_applicable",
            checkpoint_policy="not_applicable",
            required_capabilities=[],
        )
        architecture = _architecture(execution=execution)
        architecture["components"][0]["module"] = "../model.py"
        with TemporaryDirectory() as temp:
            sandbox = Path(temp)
            _write_sandbox(
                sandbox,
                source=(
                    "class Encoder:\n"
                    "    def runtime_available(self):\n"
                    "        return True\n"
                    "    def forward(self, value):\n"
                    "        return value\n"
                ),
                requirements="",
            )

            issues, _ = _validate_foundation_execution_contracts(
                sandbox=sandbox,
                architecture=architecture,
                result=_result(architecture),
            )

        self.assertTrue(any("declared module is missing" in item["message"] for item in issues))

    def test_external_framework_must_be_declared_and_imported(self) -> None:
        architecture = _architecture(
            execution=_execution(
                device_policy="cpu",
                trainable=False,
                gradient_mode="not_applicable",
                checkpoint_policy="not_applicable",
                required_capabilities=[],
            )
        )
        result = _result(architecture)
        cases = [
            (
                "import torch\n\nclass Encoder:\n    def forward(self, value):\n        return value\n",
                "numpy\n",
                "not declared",
            ),
            (
                "class Encoder:\n    def forward(self, value):\n        return value\n",
                "numpy\ntorch\n",
                "never imported",
            ),
        ]
        for source, requirements, expected_message in cases:
            with self.subTest(expected_message=expected_message), TemporaryDirectory() as temp:
                sandbox = Path(temp)
                _write_sandbox(sandbox, source=source, requirements=requirements)

                issues, _ = _validate_foundation_execution_contracts(
                    sandbox=sandbox,
                    architecture=architecture,
                    result=result,
                )

                self.assertTrue(
                    any(expected_message in item["message"] for item in issues),
                    issues,
                )

    def test_unknown_safe_python_framework_is_not_blocked_by_a_registry(self) -> None:
        architecture = _architecture(
            execution=_execution(
                primary_framework="JAX",
                supporting_libraries=[],
                device_policy="cpu",
                trainable=False,
                gradient_mode="not_applicable",
                checkpoint_policy="not_applicable",
                required_capabilities=[],
            )
        )
        self.assertEqual(
            _initial_foundation_requirements(architecture),
            "jax\nmatplotlib\nnumpy\n",
        )
        with TemporaryDirectory() as temp:
            sandbox = Path(temp)
            _write_sandbox(
                sandbox,
                source=(
                    "import jax.numpy as jnp\n\n"
                    "class Encoder:\n"
                    "    def forward(self, value):\n"
                    "        return jnp.asarray(value)\n"
                ),
                requirements="jax\nmatplotlib\nnumpy\n",
            )
            issues, _ = _validate_foundation_execution_contracts(
                sandbox=sandbox,
                architecture=architecture,
                result=_result(architecture),
            )

        self.assertEqual(issues, [])

    def test_framework_import_must_be_reachable_but_trusted_backend_use_counts(self) -> None:
        architecture = _architecture(
            execution=_execution(
                device_policy="cpu",
                trainable=False,
                gradient_mode="not_applicable",
                checkpoint_policy="not_applicable",
                required_capabilities=[],
            )
        )
        result = _result(architecture)
        with TemporaryDirectory() as temp:
            sandbox = Path(temp)
            _write_sandbox(
                sandbox,
                source="class Encoder:\n    def forward(self, value):\n        return value\n",
            )
            (sandbox / "src" / "unrelated.py").write_text("import torch\n", encoding="utf-8")

            issues, _ = _validate_foundation_execution_contracts(
                sandbox=sandbox,
                architecture=architecture,
                result=result,
            )

        self.assertTrue(any("never imported" in item["message"] for item in issues))

        with TemporaryDirectory() as temp:
            sandbox = Path(temp)
            _write_sandbox(
                sandbox,
                source=(
                    "from src import _backend\n\n"
                    "class Encoder:\n"
                    "    def forward(self, value):\n"
                    "        torch = _backend.torch()\n"
                    "        return torch.as_tensor(value)\n"
                ),
            )
            (sandbox / "src" / "__init__.py").write_text("", encoding="utf-8")
            (sandbox / "src" / "_backend.py").write_text(
                "def torch():\n    raise RuntimeError('trusted host stub is never executed here')\n",
                encoding="utf-8",
            )

            issues, warnings = _validate_foundation_execution_contracts(
                sandbox=sandbox,
                architecture=architecture,
                result=result,
            )

        self.assertEqual(issues, [])
        self.assertEqual(warnings, [])

    def test_missing_training_gradient_and_checkpoint_evidence_is_blocking(self) -> None:
        architecture = _architecture()
        only_declared_capability = [_passing_capability_tests()[0]]
        with TemporaryDirectory() as temp:
            sandbox = Path(temp)
            _write_sandbox(
                sandbox,
                source=_torch_model_source(),
            )

            issues, _ = _validate_foundation_execution_contracts(
                sandbox=sandbox,
                architecture=architecture,
                result=_result(architecture, only_declared_capability),
            )

        messages = [item["message"] for item in issues]
        self.assertTrue(any("real parameter update" in message for message in messages))
        self.assertTrue(any("gradient/back-propagation" in message for message in messages))
        self.assertTrue(any("checkpoint round-trip" in message for message in messages))

    def test_accelerator_required_needs_availability_and_tensor_placement_tests(self) -> None:
        architecture = _architecture(execution=_execution(device_policy="accelerator_required"))
        with TemporaryDirectory() as temp:
            sandbox = Path(temp)
            _write_sandbox(
                sandbox,
                source=_torch_model_source(),
            )

            issues, _ = _validate_foundation_execution_contracts(
                sandbox=sandbox,
                architecture=architecture,
                result=_result(architecture, _passing_capability_tests()),
            )

        messages = [item["message"] for item in issues]
        self.assertTrue(any("accelerator availability" in message for message in messages))
        self.assertTrue(any("accelerator tensor placement" in message for message in messages))

    def test_accelerator_capability_evidence_uses_real_availability_and_device_assertions(self) -> None:
        architecture = _architecture(execution=_execution(device_policy="accelerator_required"))
        capability_tests = [
            *_passing_capability_tests(),
            {
                "component_id": "encoder",
                "capability": "accelerator_availability",
                "test": "tests.test_model.ModelTests.test_device_available",
                "status": "passed",
            },
            {
                "component_id": "encoder",
                "capability": "tensor_device_placement",
                "test": "tests.test_model.ModelTests.test_tensor_device",
                "status": "passed",
            },
        ]
        with TemporaryDirectory() as temp:
            sandbox = Path(temp)
            _write_sandbox(sandbox, source=_torch_model_source())

            issues, warnings = _validate_foundation_execution_contracts(
                sandbox=sandbox,
                architecture=architecture,
                result=_result(architecture, capability_tests),
            )

        self.assertEqual(issues, [])
        self.assertEqual(warnings, [])

    def test_passing_capability_metadata_must_reference_a_delivered_test(self) -> None:
        architecture = _architecture()
        capability_tests = _passing_capability_tests()
        capability_tests[0]["test"] = "tests.test_model.ModelTests.test_not_delivered"
        with TemporaryDirectory() as temp:
            sandbox = Path(temp)
            _write_sandbox(
                sandbox,
                source=_torch_model_source(),
            )

            issues, _ = _validate_foundation_execution_contracts(
                sandbox=sandbox,
                architecture=architecture,
                result=_result(architecture, capability_tests),
            )

        self.assertTrue(any("not bound to its declared" in item["message"] for item in issues))
        self.assertTrue(any("required capability batched_inference" in item["message"] for item in issues))

    def test_component_name_only_is_not_callable_binding_evidence(self) -> None:
        architecture = _architecture(
            execution=_execution(
                primary_framework="standard_library",
                supporting_libraries=[],
                device_policy="cpu",
                trainable=False,
                gradient_mode="not_applicable",
                checkpoint_policy="not_applicable",
                required_capabilities=["runtime_check"],
            )
        )
        capability_tests = [
            {
                "component_id": "encoder",
                "capability": "runtime_check",
                "test": "tests.test_model.ModelTests.test_name_only_evidence",
                "status": "passed",
            }
        ]
        with TemporaryDirectory() as temp:
            sandbox = Path(temp)
            _write_sandbox(
                sandbox,
                source="class Encoder:\n    def forward(self, value):\n        return value\n",
                requirements="",
            )

            issues, _ = _validate_foundation_execution_contracts(
                sandbox=sandbox,
                architecture=architecture,
                result=_result(architecture, capability_tests),
            )

        self.assertTrue(any("not bound to its declared" in item["message"] for item in issues))

    def test_passing_capability_metadata_must_keep_component_module_and_callable(self) -> None:
        architecture = _architecture()
        result = _result(architecture, _passing_capability_tests())
        result["capability_tests"][0].pop("module")
        result["capability_tests"][1]["callable"] = "OtherEncoder.forward"
        with TemporaryDirectory() as temp:
            sandbox = Path(temp)
            _write_sandbox(sandbox, source=_torch_model_source())

            issues, _ = _validate_foundation_execution_contracts(
                sandbox=sandbox,
                architecture=architecture,
                result=result,
            )

        binding_issues = [
            item
            for item in issues
            if "not bound to its declared" in item["message"]
        ]
        self.assertEqual(len(binding_issues), 2)

    def test_parameter_update_evidence_cannot_reuse_an_inference_only_test(self) -> None:
        architecture = _architecture()
        capability_tests = _passing_capability_tests()
        capability_tests[1]["test"] = "tests.test_model.ModelTests.test_batched_inference"
        with TemporaryDirectory() as temp:
            sandbox = Path(temp)
            _write_sandbox(sandbox, source=_torch_model_source())

            issues, _ = _validate_foundation_execution_contracts(
                sandbox=sandbox,
                architecture=architecture,
                result=_result(architecture, capability_tests),
            )

        self.assertTrue(any("real parameter update" in item["message"] for item in issues))

    def test_component_flow_supports_setup_fixture_and_import_alias(self) -> None:
        architecture = _architecture()
        with TemporaryDirectory() as temp:
            sandbox = Path(temp)
            _write_sandbox(sandbox, source=_torch_model_source())
            (sandbox / "tests" / "test_model.py").write_text(
                "import unittest\n"
                "import torch\n"
                "from src.model import Encoder as Subject\n\n"
                "def encoder_fixture():\n"
                "    instance = Subject()\n"
                "    return instance\n\n"
                "class ModelTests(unittest.TestCase):\n"
                "    def setUp(self):\n"
                "        self.model = encoder_fixture()\n"
                "        self.optimizer = torch.optim.SGD(self.model.parameters(), lr=0.1)\n"
                "    def test_batched_inference(self):\n"
                "        output = self.model(torch.ones(2, 1))\n"
                "        self.assertEqual(output.shape[0], 2)\n"
                "    def test_parameter_update(self):\n"
                "        before = next(self.model.parameters()).detach().clone()\n"
                "        loss = self.model(torch.ones(2, 1)).sum()\n"
                "        self.optimizer.zero_grad()\n"
                "        loss.backward()\n"
                "        self.optimizer.step()\n"
                "        after = next(self.model.parameters()).detach()\n"
                "        self.assertFalse(torch.equal(before, after))\n"
                "    def test_gradient_flow(self):\n"
                "        self.model(torch.ones(2, 1)).sum().backward()\n"
                "        gradient = next(self.model.parameters()).grad\n"
                "        self.assertIsNotNone(gradient)\n"
                "    def test_checkpoint_roundtrip(self):\n"
                "        state = self.model.state_dict()\n"
                "        restored = encoder_fixture()\n"
                "        restored.load_state_dict(state)\n"
                "        self.assertTrue(all(torch.equal(state[key], restored.state_dict()[key]) for key in state))\n",
                encoding="utf-8",
            )

            issues, warnings = _validate_foundation_execution_contracts(
                sandbox=sandbox,
                architecture=architecture,
                result=_result(architecture, _passing_capability_tests()),
            )

        self.assertEqual(issues, [])
        self.assertEqual(warnings, [])

    def test_single_segment_factory_callable_keeps_component_instance_binding(self) -> None:
        architecture = _architecture()
        architecture["components"][0]["callable"] = "build_model"
        capability_tests = _passing_capability_tests()
        with TemporaryDirectory() as temp:
            sandbox = Path(temp)
            _write_sandbox(
                sandbox,
                source=(
                    "import torch\n\n"
                    "def build_model():\n"
                    "    return torch.nn.Linear(1, 1, bias=False)\n"
                ),
            )
            (sandbox / "tests" / "test_model.py").write_text(
                "import unittest\n"
                "import torch\n"
                "from src.model import build_model\n\n"
                "class ModelTests(unittest.TestCase):\n"
                "    def test_batched_inference(self):\n"
                "        output = build_model()(torch.ones(2, 1))\n"
                "        self.assertEqual(output.shape[0], 2)\n"
                "    def test_parameter_update(self):\n"
                "        model = build_model()\n"
                "        optimizer = torch.optim.SGD(model.parameters(), lr=0.1)\n"
                "        before = next(model.parameters()).detach().clone()\n"
                "        model(torch.ones(2, 1)).sum().backward()\n"
                "        optimizer.step()\n"
                "        after = next(model.parameters()).detach()\n"
                "        self.assertFalse(torch.equal(before, after))\n"
                "    def test_gradient_flow(self):\n"
                "        model = build_model()\n"
                "        model(torch.ones(2, 1)).sum().backward()\n"
                "        gradient = next(model.parameters()).grad\n"
                "        self.assertIsNotNone(gradient)\n"
                "    def test_checkpoint_roundtrip(self):\n"
                "        model = build_model()\n"
                "        state = model.state_dict()\n"
                "        restored = build_model()\n"
                "        restored.load_state_dict(state)\n"
                "        self.assertTrue(all(torch.equal(state[key], restored.state_dict()[key]) for key in state))\n",
                encoding="utf-8",
            )

            issues, warnings = _validate_foundation_execution_contracts(
                sandbox=sandbox,
                architecture=architecture,
                result=_result(architecture, capability_tests),
            )

        self.assertEqual(issues, [])
        self.assertEqual(warnings, [])

    def test_parameter_update_needs_change_oriented_component_assertion(self) -> None:
        architecture = _architecture()
        source = _torch_model_source().replace(
            "    def forward(self, value):\n",
            "    def step(self):\n"
            "        return None\n"
            "    def forward(self, value):\n",
        )
        with TemporaryDirectory() as temp:
            sandbox = Path(temp)
            _write_sandbox(sandbox, source=source)
            test_path = sandbox / "tests" / "test_model.py"
            delivered = test_path.read_text(encoding="utf-8")
            old = (
                "    def test_parameter_update(self):\n"
                "        model = Encoder()\n"
                "        optimizer = torch.optim.SGD(model.parameters(), lr=0.1)\n"
                "        before = next(model.parameters()).detach().clone()\n"
                "        loss = model(torch.ones(2, 1)).sum()\n"
                "        optimizer.zero_grad()\n"
                "        loss.backward()\n"
                "        optimizer.step()\n"
                "        after = next(model.parameters()).detach()\n"
                "        self.assertFalse(torch.equal(before, after))\n"
            )
            replacement = (
                "    def test_parameter_update(self):\n"
                "        model = Encoder()\n"
                "        before = next(model.parameters()).detach().clone()\n"
                "        model.step()\n"
                "        self.assertEqual(before, before)\n"
            )
            self.assertIn(old, delivered)
            test_path.write_text(delivered.replace(old, replacement, 1), encoding="utf-8")

            issues, _ = _validate_foundation_execution_contracts(
                sandbox=sandbox,
                architecture=architecture,
                result=_result(architecture, _passing_capability_tests()),
            )

        self.assertTrue(any("real parameter update" in item["message"] for item in issues))

    def test_unrelated_dummy_actions_cannot_certify_component_capabilities(self) -> None:
        architecture = _architecture()
        with TemporaryDirectory() as temp:
            sandbox = Path(temp)
            _write_sandbox(sandbox, source=_torch_model_source())
            (sandbox / "tests" / "test_model.py").write_text(
                "import unittest\n"
                "import torch\n"
                "from src.model import Encoder\n\n"
                "class Dummy:\n"
                "    def step(self):\n"
                "        return None\n"
                "    def backward(self):\n"
                "        return None\n"
                "    def save(self, value):\n"
                "        self.value = value\n"
                "    def load(self):\n"
                "        return self.value\n\n"
                "class ModelTests(unittest.TestCase):\n"
                "    def test_batched_inference(self):\n"
                "        output = Encoder()(torch.ones(2, 1))\n"
                "        self.assertEqual(output.shape[0], 2)\n"
                "    def test_parameter_update(self):\n"
                "        model = Encoder()\n"
                "        before = model(torch.ones(2, 1))\n"
                "        Dummy().step()\n"
                "        after = model(torch.ones(2, 1))\n"
                "        self.assertFalse(torch.equal(before, after))\n"
                "    def test_gradient_flow(self):\n"
                "        model = Encoder()\n"
                "        output = model(torch.ones(2, 1))\n"
                "        Dummy().backward()\n"
                "        gradient = output\n"
                "        self.assertIsNotNone(gradient)\n"
                "    def test_checkpoint_roundtrip(self):\n"
                "        model = Encoder()\n"
                "        state = model.state_dict()\n"
                "        dummy = Dummy()\n"
                "        dummy.save(state)\n"
                "        restored = dummy.load()\n"
                "        self.assertEqual(restored, state)\n",
                encoding="utf-8",
            )

            issues, _ = _validate_foundation_execution_contracts(
                sandbox=sandbox,
                architecture=architecture,
                result=_result(architecture, _passing_capability_tests()),
            )

        messages = [item["message"] for item in issues]
        self.assertTrue(any("real parameter update" in message for message in messages))
        self.assertTrue(any("gradient/back-propagation" in message for message in messages))
        self.assertTrue(any("checkpoint round-trip" in message for message in messages))

    def test_non_torch_training_requires_a_registered_trusted_probe(self) -> None:
        architecture = _architecture(
            execution=_execution(
                primary_framework="project_local",
                supporting_libraries=[],
                device_policy="cpu",
                required_capabilities=[],
            )
        )
        capability_tests = [
            {
                "component_id": "encoder",
                "capability": "training_step",
                "test": "tests.test_model.ModelTests.test_custom_learning",
                "status": "passed",
            },
            {
                "component_id": "encoder",
                "capability": "gradient_flow",
                "test": "tests.test_model.ModelTests.test_custom_derivative",
                "status": "passed",
            },
            {
                "component_id": "encoder",
                "capability": "checkpoint_roundtrip",
                "test": "tests.test_model.ModelTests.test_custom_pack_unpack",
                "status": "passed",
            },
        ]
        with TemporaryDirectory() as temp:
            sandbox = Path(temp)
            _write_project_local_sandbox(sandbox)

            issues, warnings = _validate_foundation_execution_contracts(
                sandbox=sandbox,
                architecture=architecture,
                result=_result(architecture, capability_tests),
            )

        self.assertTrue(
            any("environment_extension_required" in item["message"] for item in issues)
        )
        self.assertEqual(warnings, [])

    def test_empty_skipped_and_negated_capabilities_are_not_positive_evidence(self) -> None:
        execution = _execution(
            primary_framework="standard_library",
            supporting_libraries=[],
            device_policy="cpu",
            trainable=False,
            gradient_mode="not_applicable",
            checkpoint_policy="not_applicable",
            required_capabilities=["runtime_check"],
        )
        architecture = _architecture(execution=execution)
        for method in (
            "test_empty_evidence",
            "test_skipped_evidence",
            "test_expected_failure_evidence",
            "test_skipif_true_evidence",
        ):
            with self.subTest(method=method), TemporaryDirectory() as temp:
                sandbox = Path(temp)
                _write_sandbox(
                    sandbox,
                    source="class Encoder:\n    def forward(self, value):\n        return value\n",
                    requirements="",
                )
                capability_tests = [
                    {
                        "component_id": "encoder",
                        "capability": "runtime_check",
                        "test": f"tests.test_model.ModelTests.{method}",
                        "status": "passed",
                    }
                ]

                issues, _ = _validate_foundation_execution_contracts(
                    sandbox=sandbox,
                    architecture=architecture,
                    result=_result(architecture, capability_tests),
                )

                self.assertTrue(any("not bound to its declared" in item["message"] for item in issues))
                self.assertTrue(any("required capability runtime_check" in item["message"] for item in issues))

        for method in ("test_skipif_false_evidence", "test_skipunless_true_evidence"):
            with self.subTest(method=method), TemporaryDirectory() as temp:
                sandbox = Path(temp)
                _write_sandbox(
                    sandbox,
                    source="class Encoder:\n    def forward(self, value):\n        return value\n",
                    requirements="",
                )
                capability_tests = [
                    {
                        "component_id": "encoder",
                        "capability": "runtime_check",
                        "test": f"tests.test_model.ModelTests.{method}",
                        "status": "passed",
                    }
                ]
                issues, _ = _validate_foundation_execution_contracts(
                    sandbox=sandbox,
                    architecture=architecture,
                    result=_result(architecture, capability_tests),
                )
                self.assertEqual(issues, [])

        architecture = _architecture()
        capability_tests = _passing_capability_tests()
        capability_tests[2]["capability"] = "gradient_not_supported"
        with TemporaryDirectory() as temp:
            sandbox = Path(temp)
            _write_sandbox(
                sandbox,
                source=_torch_model_source(),
            )
            issues, _ = _validate_foundation_execution_contracts(
                sandbox=sandbox,
                architecture=architecture,
                result=_result(architecture, capability_tests),
            )
        self.assertTrue(any("gradient/back-propagation" in item["message"] for item in issues))

    def test_execution_mismatch_blocks_but_rationale_only_warns(self) -> None:
        architecture = _architecture()
        result = _result(architecture, _passing_capability_tests())
        result["execution_contracts"][0]["execution"]["precision"] = "float64"
        result["execution_contracts"][0]["execution"]["rationale"] = "Equivalent explanation."
        with TemporaryDirectory() as temp:
            sandbox = Path(temp)
            _write_sandbox(
                sandbox,
                source=_torch_model_source(),
            )

            issues, warnings = _validate_foundation_execution_contracts(
                sandbox=sandbox,
                architecture=architecture,
                result=result,
            )

        self.assertTrue(any("changes precision" in item["message"] for item in issues))
        self.assertTrue(any("rationale" in item["message"] for item in warnings))

    def test_non_trainable_numpy_component_needs_no_training_evidence(self) -> None:
        numpy_execution = _execution(
            primary_framework="numpy",
            supporting_libraries=[],
            device_policy="cpu",
            precision="float64",
            trainable=False,
            gradient_mode="not_applicable",
            checkpoint_policy="not_applicable",
            required_capabilities=[],
        )
        architecture = _architecture(execution=numpy_execution)
        self.assertEqual(
            _initial_foundation_requirements(architecture),
            "matplotlib\nnumpy\n",
        )
        with TemporaryDirectory() as temp:
            sandbox = Path(temp)
            _write_sandbox(
                sandbox,
                source=(
                    "import numpy as np\n\n"
                    "class Encoder:\n"
                    "    def forward(self, value):\n"
                    "        return np.asarray(value)\n"
                ),
                requirements="numpy\n",
            )

            issues, warnings = _validate_foundation_execution_contracts(
                sandbox=sandbox,
                architecture=architecture,
                result=_result(architecture),
            )

        self.assertEqual(issues, [])
        self.assertEqual(warnings, [])

    def test_external_runtime_requires_host_proof_and_a_trusted_adapter(self) -> None:
        external_execution = _execution(
            primary_framework="MATLAB",
            supporting_libraries=[],
            device_policy="external_runtime",
            precision="float64",
            trainable=False,
            gradient_mode="not_applicable",
            checkpoint_policy="not_applicable",
            required_capabilities=["runtime_availability", "runtime_invocation"],
        )
        architecture = _architecture(execution=external_execution)
        self.assertEqual(
            _initial_foundation_requirements(architecture),
            "matplotlib\nnumpy\n",
        )
        capability_tests = [
            {
                "component_id": "encoder",
                "capability": "runtime_availability",
                "test": "tests.test_model.ModelTests.test_external_runtime_available",
                "status": "passed",
            },
            {
                "component_id": "encoder",
                "capability": "runtime_invocation",
                "test": "tests.test_model.ModelTests.test_external_runtime_invocation",
                "status": "passed",
            },
        ]
        with TemporaryDirectory() as temp:
            sandbox = Path(temp)
            _write_sandbox(
                sandbox,
                source=_external_runtime_source(),
                requirements="",
            )
            with patch(
                "geng_agent.agentic_foundation.shutil.which",
                return_value="/trusted/bin/matlab",
            ):
                issues, warnings = _validate_foundation_execution_contracts(
                    sandbox=sandbox,
                    architecture=architecture,
                    result=_result(architecture, capability_tests),
                )

        self.assertTrue(
            any("environment_extension_required" in item["message"] for item in issues)
        )
        self.assertEqual(warnings, [])

        with TemporaryDirectory() as temp:
            sandbox = Path(temp)
            _write_sandbox(
                sandbox,
                source=_external_runtime_source(),
                requirements="",
            )
            (sandbox / "foundation_result.json").write_text(
                json.dumps(_result(architecture, capability_tests)),
                encoding="utf-8",
            )
            with patch(
                "geng_agent.agentic_foundation.shutil.which",
                return_value="/trusted/bin/matlab",
            ), patch(
                "geng_agent.agentic_foundation._run_foundation_tests",
                return_value={"passed": True, "returncode": 0},
            ):
                delivery_issues, test_result = _validate_foundation_delivery(
                    sandbox=sandbox,
                    architecture=architecture,
                    trusted_changed=[],
                )
        self.assertEqual(delivery_issues, [])
        self.assertFalse(test_result.get("skipped", False))
        self.assertTrue(
            any(
                "environment_extension_required" in item["message"]
                for item in test_result.get("warnings", [])
            )
        )

        missing_interface = _architecture(
            execution={**external_execution, "required_capabilities": ["runtime_availability"]}
        )
        with TemporaryDirectory() as temp:
            sandbox = Path(temp)
            _write_sandbox(
                sandbox,
                source=_external_runtime_source(),
                requirements="",
            )
            with patch(
                "geng_agent.agentic_foundation.shutil.which",
                return_value="/trusted/bin/matlab",
            ):
                issues, _ = _validate_foundation_execution_contracts(
                    sandbox=sandbox,
                    architecture=missing_interface,
                    result=_result(missing_interface, capability_tests[:1]),
                )
        self.assertTrue(any("must declare external runtime invocation" in item["message"] for item in issues))

        with TemporaryDirectory() as temp:
            sandbox = Path(temp)
            _write_sandbox(
                sandbox,
                source=_external_runtime_source(),
                requirements="",
            )
            with patch("geng_agent.agentic_foundation.shutil.which", return_value=None):
                issues, _ = _validate_foundation_execution_contracts(
                    sandbox=sandbox,
                    architecture=architecture,
                    result=_result(architecture, capability_tests),
                )
        self.assertTrue(any("external runtime host capability gap" in item["message"] for item in issues))

    def test_external_runtime_identity_callable_is_not_invocation_evidence(self) -> None:
        execution = _execution(
            primary_framework="MATLAB",
            supporting_libraries=[],
            device_policy="external_runtime",
            precision="float64",
            trainable=False,
            gradient_mode="not_applicable",
            checkpoint_policy="not_applicable",
            required_capabilities=["runtime_availability", "runtime_invocation"],
        )
        architecture = _architecture(execution=execution)
        capability_tests = [
            {
                "component_id": "encoder",
                "capability": "runtime_availability",
                "test": "tests.test_model.ModelTests.test_external_runtime_available",
                "status": "passed",
            },
            {
                "component_id": "encoder",
                "capability": "runtime_invocation",
                "test": "tests.test_model.ModelTests.test_external_runtime_invocation",
                "status": "passed",
            },
        ]
        with TemporaryDirectory() as temp:
            sandbox = Path(temp)
            _write_sandbox(
                sandbox,
                source=(
                    "class Encoder:\n"
                    "    def runtime_available(self):\n"
                    "        return True\n"
                    "    def forward(self, value):\n"
                    "        return value\n"
                ),
                requirements="",
            )
            with patch(
                "geng_agent.agentic_foundation.shutil.which",
                return_value="/trusted/bin/matlab",
            ):
                issues, _ = _validate_foundation_execution_contracts(
                    sandbox=sandbox,
                    architecture=architecture,
                    result=_result(architecture, capability_tests),
                )

        self.assertTrue(any("constant/identity stub" in item["message"] for item in issues))

    def test_standard_library_framework_and_schema_10_remain_compatible(self) -> None:
        standard_execution = _execution(
            primary_framework="standard_library",
            supporting_libraries=[],
            trainable=False,
            gradient_mode="not_applicable",
            checkpoint_policy="not_applicable",
            required_capabilities=[],
        )
        architecture = _architecture(execution=standard_execution)
        with TemporaryDirectory() as temp:
            sandbox = Path(temp)
            _write_sandbox(
                sandbox,
                source="class Encoder:\n    def forward(self, value):\n        return value\n",
                requirements="",
            )
            issues, _ = _validate_foundation_execution_contracts(
                sandbox=sandbox,
                architecture=architecture,
                result=_result(architecture),
            )
        self.assertEqual(issues, [])

        legacy = _architecture(schema_version="1.0")
        issues, warnings = _validate_foundation_execution_contracts(
            sandbox=Path("does-not-need-to-exist"),
            architecture=legacy,
            result={},
        )
        self.assertEqual(issues, [])
        self.assertEqual(warnings, [])


if __name__ == "__main__":
    unittest.main()
