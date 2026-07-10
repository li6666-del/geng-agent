from __future__ import annotations

from .outputs import REQUIRED_REPRO_FILES


# Task writers own task-private scripts. The host injects the dispatcher and trusted
# runtime files after merging, so those files must not be claimed as writer output.
SHARED_GENERATED_FILES = REQUIRED_REPRO_FILES - {"run_experiment.py"}


def expected_generated_paths(task_scripts: list[str] | None) -> set[str]:
    return set(SHARED_GENERATED_FILES) | set(task_scripts or [])
