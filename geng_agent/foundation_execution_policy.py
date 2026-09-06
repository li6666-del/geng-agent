"""Shared constants for Foundation execution-contract validation.

This module intentionally contains data only.  Keeping policy constants below
the architecture, binding, evidence, and validator layers prevents the
Foundation modules from importing their high-level workflow facade.
"""

from __future__ import annotations


EXECUTION_CONTRACT_FIELDS = (
    "execution_kind",
    "primary_framework",
    "supporting_libraries",
    "device_policy",
    "precision",
    "trainable",
    "gradient_mode",
    "checkpoint_policy",
    "shared_implementation",
    "required_capabilities",
    "rationale",
)
MATERIAL_EXECUTION_FIELDS = tuple(
    field for field in EXECUTION_CONTRACT_FIELDS if field != "rationale"
)
FRAMEWORK_EXEMPTIONS = {
    "",
    "built-in",
    "builtin",
    "builtins",
    "custom",
    "custom-python",
    "framework-agnostic",
    "frameworkagnostic",
    "in-house",
    "in-project",
    "local",
    "native",
    "native-python",
    "none",
    "not-applicable",
    "project",
    "project-local",
    "python",
    "python-standard-library",
    "standard-library",
    "standardlibrary",
    "stdlib",
}
LIBRARY_CANONICAL_NAMES = {
    "commpy": "scikit-commpy",
    "jax": "jax",
    "matplotlib": "matplotlib",
    "numpy": "numpy",
    "pil": "pillow",
    "pillow": "pillow",
    "pytorch": "torch",
    "scikit-commpy": "scikit-commpy",
    "scikit-learn": "scikit-learn",
    "scipy": "scipy",
    "sklearn": "scikit-learn",
    "tensorflow": "tensorflow",
    "torch": "torch",
}
CAPABILITY_GROUPS = {
    "parameter update": {
        "optimizer-step",
        "parameter-update",
        "trainable",
        "training-step",
    },
    "gradient/back-propagation": {
        "autograd",
        "backpropagation",
        "backward",
        "gradient",
        "gradient-flow",
        "gradients",
    },
    "checkpoint round-trip": {
        "checkpoint",
        "checkpoint-roundtrip",
        "save-load",
        "state-dict-roundtrip",
    },
    "accelerator availability": {
        "accelerator-availability",
        "cuda-available",
        "device-availability",
        "gpu-available",
    },
    "accelerator tensor placement": {
        "actual-tensor-device",
        "device-placement",
        "tensor-device",
        "tensor-device-placement",
    },
    "external runtime availability": {
        "binary-available",
        "engine-available",
        "external-runtime-available",
        "external-runtime-availability",
        "matlab-available",
        "julia-available",
        "runtime-availability",
        "runtime-available",
    },
    "external runtime invocation interface": {
        "engine-invocation",
        "external-interface",
        "external-runtime-interface",
        "external-runtime-invocation",
        "invocation-interface",
        "julia-invocation",
        "matlab-invocation",
        "runtime-interface",
        "runtime-invocation",
    },
}
TRUSTED_CAPABILITY_PROBE_FRAMEWORKS = {"torch"}
TRUSTED_EXTERNAL_RUNTIME_ADAPTERS: dict[str, str] = {}
FRAMEWORK_SEMANTIC_LABELS = {
    "real parameter update",
    "gradient/back-propagation",
    "checkpoint round-trip",
    "accelerator availability",
    "accelerator tensor placement",
}
TRUSTED_PROJECT_FILES = {"src/_io.py", "src/_backend.py"}
