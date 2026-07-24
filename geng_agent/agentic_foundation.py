from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from .codex_runner import DEFAULT_CODEX_TIMEOUT_SECONDS, run_codex_subprocess, run_python_unittest_subprocess
from .config import get_config_value
from .foundation_snapshot import (
    FOUNDATION_CONTRACT_VERSION,
    FOUNDATION_SCHEMA_VERSION,
    FOUNDATION_WORKFLOW_VERSION,
    file_sha256,
    foundation_snapshot_hash,
    is_foundation_frozen_path,
    path_is_foundation_link,
    resolve_foundation_path,
    scan_foundation_tree,
    validate_foundation_manifest,
    validate_foundation_relpath,
    validate_foundation_snapshot,
)
from .io_runtime import BACKEND_RUNTIME_PY, IO_RUNTIME_PY, inject_io_runtime
from .json_utils import pretty_json
from .outputs import _missing_local_imports, write_json, write_text
from .scientific_architecture import foundation_module_paths
from .security import (
    ALLOWED_REQUIREMENTS,
    FOUNDATION_STATIC_SECURITY_ADVISORY_CATEGORIES,
    split_requirement_issues,
    split_static_security_issues,
    static_scan_repro_project,
    validate_requirements,
)
from .task_writer_support import (
    PAPER_EVIDENCE_DIR,
    _analysis_snapshot_hash,
    _collect_writer_analysis_artifacts,
    _missing_required_analysis_artifacts,
    _write_paper_evidence_bundle,
)


FOUNDATION_RESULT_STATUS = "ready_for_tasks"
FOUNDATION_LABEL = "03b_foundation_writer"
FOUNDATION_CORE_MODULES = {
    "src/channel.py",
    "src/modulation.py",
    "src/transmitter.py",
    "src/receiver.py",
    "src/metrics.py",
    "src/simulator.py",
    "src/simulation.py",
    "src/algorithms/__init__.py",
    "src/baselines/__init__.py",
}


def run_codex_foundation_writer_workflow(
    *,
    facts: dict[str, Any],
    tasks: dict[str, Any],
    experiment_index: dict[str, Any],
    scientific_architecture: dict[str, Any],
    paper: dict[str, Any],
    paper_path: Path,
    paper_images: list[Any] | None,
    paper_thesis: dict[str, Any] | None,
    output_dir: Path,
    audit_dir: Path,
    timeout: float = DEFAULT_CODEX_TIMEOUT_SECONDS,
    resume: bool = True,
) -> dict[str, Any]:
    """Build and freeze the shared scientific layer before parallel task writers.

    The Foundation Writer owns shared ``src`` modules and their contract tests,
    but never task modules or outputs.  Its content-addressed snapshot is the
    sole shared layer installed into every writer sandbox and the final project.
    """

    analysis_artifacts = _collect_writer_analysis_artifacts(output_dir=output_dir)
    missing = _missing_required_analysis_artifacts(analysis_artifacts)
    if missing:
        raise RuntimeError("foundation writer requires finalized analysis artifacts: " + ", ".join(missing))
    if "scientific_architecture.json" not in analysis_artifacts:
        raise RuntimeError("foundation writer requires finalized analysis artifact: scientific_architecture.json")
    analysis_hash = _analysis_snapshot_hash(paper_path=paper_path, artifacts=analysis_artifacts)
    input_hash = _foundation_input_hash(analysis_hash, scientific_architecture)
    required_modules = _required_foundation_modules(scientific_architecture)
    manifest_path = output_dir / "foundation_manifest.json"
    sandbox = audit_dir / "03b_foundation_writer_sandbox"
    snapshot_dir = audit_dir / "03b_foundation_snapshot"

    if resume:
        cached = _load_cached_foundation(
            manifest_path=manifest_path,
            snapshot_dir=snapshot_dir,
            expected_input_hash=input_hash,
            expected_required_modules=required_modules,
        )
        if cached is not None:
            write_json(audit_dir / "03b_foundation_writer_resume.json", {"ok": True, "source": "content_addressed_snapshot"})
            return cached

    for path in (sandbox, snapshot_dir):
        if path.exists():
            shutil.rmtree(path)
    sandbox.mkdir(parents=True, exist_ok=True)
    write_text(sandbox / "requirements.txt", _initial_foundation_requirements(scientific_architecture))
    inject_io_runtime(sandbox)
    _write_paper_evidence_bundle(
        repro_project_dir=sandbox,
        paper_path=paper_path,
        paper=paper,
        facts=facts,
        tasks=tasks,
        paper_thesis=paper_thesis,
        analysis_snapshot_hash=analysis_hash,
        analysis_artifacts=analysis_artifacts,
        full_paper_images=paper_images,
    )
    write_text(
        sandbox / "README.foundation.md",
        "# Frozen scientific foundation\n\nGenerated once before isolated task writers.\n",
    )
    trusted_before = _trusted_hashes(sandbox)
    prompt = _foundation_brief(scientific_architecture)
    write_text(audit_dir / f"{FOUNDATION_LABEL}_brief.md", prompt)
    python_dir = Path(sys.executable).resolve().parent
    status = run_codex_subprocess(
        role="foundation_writer",
        work_dir=sandbox,
        prompt=prompt,
        audit_dir=audit_dir,
        label=FOUNDATION_LABEL,
        sandbox="workspace-write",
        timeout=timeout,
        command_override=get_config_value("GENG_CODEX_FOUNDATION_WRITER_CMD"),
        image_paths=sorted(
            path.resolve()
            for path in (sandbox / PAPER_EVIDENCE_DIR / "full_paper_pages").glob("paper_page_*.png")
            if path.is_file()
        ),
        extra_env={"GENG_PYTHON_EXECUTABLE": sys.executable},
        path_prepend=[python_dir],
    )
    if not status.get("ok"):
        raise RuntimeError(f"foundation writer failed: {status.get('error') or status.get('blocked_reason') or 'unknown error'}")

    try:
        # This must be the first inspection after the agent returns. Nothing
        # below may read or replace an agent-controlled path until the complete
        # sandbox has been walked without following links or reparse points.
        _assert_foundation_sandbox_layout_safe(sandbox)
        trusted_after = _trusted_hashes(sandbox)
        trusted_changed = sorted(path for path, digest in trusted_before.items() if trusted_after.get(path) != digest)
        _restore_trusted_runtime_atomically(sandbox)
    except (OSError, RuntimeError) as exc:
        raise RuntimeError(f"foundation writer produced an unsafe filesystem layout: {exc}") from exc
    issues, test_result = _validate_foundation_delivery(
        sandbox=sandbox,
        architecture=scientific_architecture,
        trusted_changed=trusted_changed,
    )
    write_json(
        audit_dir / "03b_foundation_validation.json",
        {"ok": not issues, "issues": issues, "tests": test_result, "input_hash": input_hash},
    )
    if issues:
        raise RuntimeError("foundation validation failed: " + "; ".join(str(item.get("message")) for item in issues[:8]))

    files = _copy_foundation_snapshot(sandbox=sandbox, snapshot_dir=snapshot_dir)
    frozen_files = [item for item in files if is_foundation_frozen_path(str(item.get("path") or ""))]
    snapshot_hash = foundation_snapshot_hash(files)
    manifest = {
        "schema_version": FOUNDATION_SCHEMA_VERSION,
        "workflow_version": FOUNDATION_WORKFLOW_VERSION,
        "contract_version": FOUNDATION_CONTRACT_VERSION,
        "input_hash": input_hash,
        "analysis_snapshot_hash": analysis_hash,
        "snapshot_hash": snapshot_hash,
        "files": files,
        "frozen_files": frozen_files,
        "required_modules": sorted(required_modules),
        "validation": {"tests_passed": bool(test_result.get("passed")), "local_imports_resolve": True},
    }
    manifest_issues = validate_foundation_manifest(
        manifest,
        expected_input_hash=input_hash,
        expected_required_modules=required_modules,
    )
    if manifest_issues:
        raise RuntimeError(f"internal Foundation manifest validation failed: {manifest_issues[:5]}")
    _write_foundation_manifest(manifest_path, manifest)
    return {"manifest": manifest, "manifest_path": str(manifest_path), "snapshot_dir": str(snapshot_dir), "snapshot_hash": snapshot_hash}


def validate_foundation_bundle(
    foundation: Any,
    *,
    expected_input_hash: str | None = None,
    expected_required_modules: set[str] | None = None,
) -> list[dict[str, str]]:
    """Validate both the outer hand-off record and its immutable snapshot."""

    if not isinstance(foundation, dict):
        return [{"path": "$", "message": "Foundation hand-off must be an object"}]
    manifest = foundation.get("manifest") if isinstance(foundation.get("manifest"), dict) else {}
    issues: list[dict[str, str]] = []
    if foundation.get("snapshot_hash") != manifest.get("snapshot_hash"):
        issues.append(
            {
                "path": "$.snapshot_hash",
                "message": "outer Foundation snapshot hash does not match its manifest",
            }
        )
    raw_snapshot_dir = foundation.get("snapshot_dir")
    if not isinstance(raw_snapshot_dir, str) or not raw_snapshot_dir.strip():
        issues.append({"path": "$.snapshot_dir", "message": "must be a non-empty path string"})
        return issues
    issues.extend(
        validate_foundation_snapshot(
            manifest,
            Path(raw_snapshot_dir),
            expected_input_hash=expected_input_hash,
            expected_required_modules=expected_required_modules,
        )
    )
    return issues


def install_foundation_snapshot(target: Path, foundation: dict[str, Any]) -> set[str]:
    """Install a validated snapshot without following manifest-controlled links."""

    issues = validate_foundation_bundle(foundation)
    if issues:
        raise RuntimeError(f"foundation snapshot validation failed: {issues[:5]}")
    manifest = foundation["manifest"]
    snapshot_dir = Path(foundation["snapshot_dir"])

    target.mkdir(parents=True, exist_ok=True)
    installed: set[str] = set()
    for item in manifest["files"]:
        relative = validate_foundation_relpath(item["path"])
        source = resolve_foundation_path(snapshot_dir, relative, require_file=True)
        destination = resolve_foundation_path(target, relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination = resolve_foundation_path(target, relative)
        if path_is_foundation_link(destination) or (destination.exists() and not destination.is_file()):
            raise RuntimeError(f"unsafe Foundation destination: {relative}")

        descriptor, temp_name = tempfile.mkstemp(
            prefix=f".{destination.name}.foundation-",
            dir=destination.parent,
        )
        os.close(descriptor)
        temp_path = Path(temp_name)
        try:
            shutil.copy2(source, temp_path)
            if temp_path.stat().st_size != item["bytes"] or file_sha256(temp_path) != item["sha256"]:
                raise RuntimeError(f"Foundation file changed during installation: {relative}")
            if path_is_foundation_link(destination) or (destination.exists() and not destination.is_file()):
                raise RuntimeError(f"unsafe Foundation destination: {relative}")
            os.replace(temp_path, destination)
        finally:
            temp_path.unlink(missing_ok=True)
        installed.add(relative)
    return installed


_TRUSTED_PROJECT_FILES = {"src/_io.py", "src/_backend.py"}


def _is_restricted_project_path(relative: str) -> bool:
    parts = relative.split("/")
    if relative in {"src", "tests"} or relative.startswith(("src/", "tests/")):
        return True
    return len(parts) >= 2 and parts[0] == "configs" and parts[1].startswith("foundation")


def _scan_restricted_project(
    project_dir: Path,
) -> tuple[dict[str, Path], list[Path], list[dict[str, str]]]:
    actual_files: dict[str, Path] = {}
    directories: list[Path] = []
    issues: list[dict[str, str]] = []
    try:
        if path_is_foundation_link(project_dir):
            return {}, [], [{"file": ".", "message": "project root is a link or reparse point"}]
        if project_dir.exists() and not project_dir.is_dir():
            return {}, [], [{"file": ".", "message": "project root is not a directory"}]
    except OSError as exc:
        return {}, [], [{"file": ".", "message": f"cannot inspect project root: {exc}"}]

    for root_name in ("src", "tests", "configs"):
        root = project_dir / root_name
        try:
            if path_is_foundation_link(root):
                issues.append({"file": root_name, "message": "Foundation-owned directory is a link or reparse point"})
                continue
            if not root.exists():
                continue
            if not root.is_dir():
                issues.append({"file": root_name, "message": "Foundation-owned path is not a directory"})
                continue
            files, found_directories, links, special = scan_foundation_tree(root)
        except OSError as exc:
            issues.append({"file": root_name, "message": f"cannot scan Foundation-owned directory: {exc}"})
            continue

        for path in found_directories:
            relative = path.relative_to(project_dir).as_posix()
            if _is_restricted_project_path(relative):
                directories.append(path)
        for path in links:
            relative = path.relative_to(project_dir).as_posix()
            if _is_restricted_project_path(relative):
                issues.append({"file": relative, "message": "frozen Foundation tree contains a link or reparse point"})
        for path in special:
            relative = path.relative_to(project_dir).as_posix()
            if _is_restricted_project_path(relative):
                issues.append({"file": relative, "message": "frozen Foundation tree contains a non-regular entry"})
        for path in files:
            relative = path.relative_to(project_dir).as_posix()
            if _is_restricted_project_path(relative):
                actual_files[relative] = path
    return actual_files, directories, issues


def foundation_violations(project_dir: Path, foundation: dict[str, Any]) -> list[dict[str, str]]:
    bundle_issues = validate_foundation_bundle(foundation)
    if bundle_issues:
        return [
            {"file": str(item.get("path") or "foundation_manifest.json"), "message": str(item.get("message") or "invalid manifest")}
            for item in bundle_issues
        ]

    manifest = foundation["manifest"]
    frozen = {str(item["path"]): str(item["sha256"]) for item in manifest["frozen_files"]}
    actual_files, _, scan_issues = _scan_restricted_project(project_dir)
    issues = list(scan_issues)
    for relative, expected in sorted(frozen.items()):
        try:
            path = resolve_foundation_path(project_dir, relative)
            if not path.is_file():
                issues.append({"file": relative, "message": "frozen foundation file was deleted"})
            elif file_sha256(path) != expected:
                issues.append({"file": relative, "message": "frozen foundation file was modified"})
        except (OSError, ValueError) as exc:
            issues.append({"file": relative, "message": str(exc)})

    allowed_files = set(frozen) | _TRUSTED_PROJECT_FILES
    for relative in sorted(set(actual_files) - allowed_files):
        issues.append({"file": relative, "message": "task writer created a file inside frozen Foundation ownership"})
    return issues


def restore_foundation_snapshot(project_dir: Path, foundation: dict[str, Any]) -> None:
    bundle_issues = validate_foundation_bundle(foundation)
    if bundle_issues:
        raise RuntimeError(f"cannot restore invalid Foundation snapshot: {bundle_issues[:5]}")

    manifest = foundation["manifest"]
    frozen_paths = {str(item["path"]) for item in manifest["frozen_files"]}
    actual_files, directories, scan_issues = _scan_restricted_project(project_dir)
    if scan_issues:
        raise RuntimeError(f"cannot restore Foundation through unsafe project paths: {scan_issues[:5]}")

    root_resolved = project_dir.resolve(strict=False)
    unexpected = sorted(set(actual_files) - frozen_paths - _TRUSTED_PROJECT_FILES)
    for relative in unexpected:
        path = actual_files[relative]
        try:
            if path_is_foundation_link(path):
                raise ValueError("restricted file became a link during restore")
            path.resolve(strict=False).relative_to(root_resolved)
            path.unlink()
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"cannot safely remove unexpected Foundation file {relative}: {exc}") from exc

    for directory in sorted(directories, key=lambda path: len(path.parts), reverse=True):
        try:
            if path_is_foundation_link(directory):
                raise ValueError("restricted directory became a link during restore")
            directory.resolve(strict=False).relative_to(root_resolved)
            directory.rmdir()
        except FileNotFoundError:
            continue
        except OSError:
            # Expected parent directories and non-empty task-owned config trees remain.
            continue
        except ValueError as exc:
            raise RuntimeError(f"cannot safely clean Foundation directory {directory}: {exc}") from exc

    install_foundation_snapshot(project_dir, foundation)


_EXECUTION_CONTRACT_FIELDS = (
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
_MATERIAL_EXECUTION_FIELDS = tuple(field for field in _EXECUTION_CONTRACT_FIELDS if field != "rationale")
_FRAMEWORK_EXEMPTIONS = {
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
_LIBRARY_CANONICAL_NAMES = {
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
_CAPABILITY_GROUPS = {
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
_TRUSTED_CAPABILITY_PROBE_FRAMEWORKS = {"torch"}
_TRUSTED_EXTERNAL_RUNTIME_ADAPTERS: dict[str, str] = {}
_FRAMEWORK_SEMANTIC_LABELS = {
    "real parameter update",
    "gradient/back-propagation",
    "checkpoint round-trip",
    "accelerator availability",
    "accelerator tensor placement",
}


def _architecture_requires_execution_contracts(architecture: dict[str, Any]) -> bool:
    match = re.fullmatch(r"\s*(\d+)(?:\.(\d+))?\s*", str(architecture.get("schema_version") or ""))
    if match is None:
        return False
    return (int(match.group(1)), int(match.group(2) or 0)) >= (1, 1)


def _architecture_components(architecture: dict[str, Any]) -> list[dict[str, Any]]:
    raw = architecture.get("components")
    return [component for component in raw if isinstance(component, dict)] if isinstance(raw, list) else []


def _normalized_library_name(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip().casefold()
    text = re.split(r"[<>=!~;\[]", text, maxsplit=1)[0].strip()
    return re.sub(r"[-_.\s]+", "-", text)


def _library_keys(value: Any) -> set[str]:
    normalized = _normalized_library_name(value)
    if not normalized:
        return set()
    keys = {normalized}
    raw = str(value).strip().casefold()
    raw = re.split(r"[<>=!~;\[]", raw, maxsplit=1)[0].strip()
    if "." in raw:
        keys.add(_normalized_library_name(raw.split(".", 1)[0]))
    canonical = _LIBRARY_CANONICAL_NAMES.get(normalized)
    if canonical:
        keys.add(canonical)
    for alias, target in _LIBRARY_CANONICAL_NAMES.items():
        if normalized == target:
            keys.add(alias)
    return keys


def _requirement_name_for_library(value: Any) -> str | None:
    keys = _library_keys(value)
    for candidate in sorted(ALLOWED_REQUIREMENTS):
        if keys & _library_keys(candidate):
            return candidate
    return None


def _initial_foundation_requirements(architecture: dict[str, Any]) -> str:
    if not _architecture_requires_execution_contracts(architecture):
        return "numpy\nmatplotlib\n"
    requirements: set[str] = set()
    for component in _architecture_components(architecture):
        execution = component.get("execution")
        if not isinstance(execution, dict):
            continue
        libraries = [execution.get("primary_framework")]
        supporting = execution.get("supporting_libraries")
        if isinstance(supporting, list):
            libraries.extend(supporting)
        for library in libraries:
            requirement = _requirement_name_for_library(library)
            if requirement is not None:
                requirements.add(requirement)
    return "".join(f"{requirement}\n" for requirement in sorted(requirements))


def _dependency_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for child in value for item in _dependency_strings(child)]
    if isinstance(value, dict):
        result = list(value)
        for child in value.values():
            result.extend(_dependency_strings(child))
        return result
    return []


def _architecture_dependency_names(architecture: dict[str, Any]) -> set[str]:
    dependency_fields = (
        "dependencies",
        "dependency_declarations",
        "libraries",
        "requirements",
        "supporting_libraries",
    )
    values: list[str] = []
    for container in [architecture, *_architecture_components(architecture)]:
        for field in dependency_fields:
            values.extend(_dependency_strings(container.get(field)))
        execution = container.get("execution")
        if isinstance(execution, dict):
            values.extend(_dependency_strings(execution.get("supporting_libraries")))
            for field in ("dependencies", "dependency_declarations", "requirements"):
                values.extend(_dependency_strings(execution.get(field)))
    return {key for value in values for key in _library_keys(value)}


def _declared_requirement_keys(sandbox: Path) -> set[str]:
    path = sandbox / "requirements.txt"
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return set()
    result: set[str] = set()
    for raw in lines:
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        match = re.match(r"([A-Za-z0-9_.-]+)", line)
        if match:
            result.update(_library_keys(match.group(1)))
    return result


def _foundation_source_trees(
    sandbox: Path,
) -> tuple[dict[str, ast.Module], list[dict[str, str]]]:
    trees: dict[str, ast.Module] = {}
    issues: list[dict[str, str]] = []
    source_root = sandbox / "src"
    if not source_root.is_dir():
        return trees, issues
    for path in sorted(source_root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        relative = path.relative_to(sandbox).as_posix()
        try:
            trees[relative] = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        except (OSError, SyntaxError, UnicodeError) as exc:
            issues.append({"file": relative, "message": f"cannot statically inspect Python source: {exc}"})
    return trees, issues


def _component_framework_import_keys(
    trees: dict[str, ast.Module],
    declared_module: str,
) -> set[str]:
    """Collect frameworks reachable from one component's local static import graph."""

    imported: set[str] = set()
    pending = [declared_module]
    visited: set[str] = set()
    while pending:
        relative = pending.pop()
        if relative in visited or relative in _TRUSTED_PROJECT_FILES:
            continue
        visited.add(relative)
        tree = trees.get(relative)
        if tree is None:
            continue
        backend_aliases = {"_backend"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    local_module = _module_candidate(trees, alias.name.split("."))
                    if local_module is not None:
                        pending.append(local_module)
                    else:
                        imported.add(alias.name.split(".", 1)[0])
                    if alias.name.endswith("._backend"):
                        backend_aliases.add(alias.asname or alias.name.split(".", 1)[0])
            elif isinstance(node, ast.ImportFrom):
                local_module = _imported_module_path(
                    current_module=relative,
                    imported_module=node.module,
                    level=node.level,
                    trees=trees,
                )
                if local_module is not None:
                    pending.append(local_module)
                elif node.module:
                    imported.add(node.module.split(".", 1)[0])
                for alias in node.names:
                    child_module = _imported_module_path(
                        current_module=relative,
                        imported_module=node.module,
                        level=node.level,
                        trees=trees,
                        child_module=alias.name,
                    )
                    if child_module is not None:
                        pending.append(child_module)
                    if alias.name == "_backend":
                        backend_aliases.add(alias.asname or alias.name)
                    if node.module and node.module.endswith("._backend") and alias.name == "torch":
                        imported.add("torch")

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            call_name = _ast_dotted_name(node.func)
            if call_name.endswith("._backend.torch"):
                imported.add("torch")
                continue
            if any(call_name == f"{alias}.torch" for alias in backend_aliases):
                imported.add("torch")
    return {key for name in imported for key in _library_keys(name)}


def _foundation_project_import_keys(sandbox: Path) -> set[str]:
    names = {"src"}
    source_root = sandbox / "src"
    if source_root.is_dir():
        for path in source_root.iterdir():
            if path.is_dir():
                names.add(path.name)
            elif path.suffix == ".py":
                names.add(path.stem)
    return {key for name in names for key in _library_keys(name)}


def _framework_is_external(value: Any, project_keys: set[str]) -> bool:
    keys = _library_keys(value)
    if not keys or keys & _FRAMEWORK_EXEMPTIONS:
        return False
    known_external_keys = {
        key
        for library in _LIBRARY_CANONICAL_NAMES
        for key in _library_keys(library)
    }
    if keys & known_external_keys:
        return True
    if keys & project_keys:
        return False
    stdlib_names = getattr(sys, "stdlib_module_names", set())
    stdlib_keys = {key for name in stdlib_names for key in _library_keys(name)}
    return not bool(keys & stdlib_keys)


_Binding = tuple[str, ast.AST | None, str]


def _module_candidate(trees: dict[str, ast.Module], parts: list[str]) -> str | None:
    normalized = [part for part in parts if part]
    candidates = [
        "/".join(normalized) + ".py",
        "/".join([*normalized, "__init__.py"]),
    ]
    if normalized and normalized[0] != "src":
        candidates.extend(
            [
                "/".join(["src", *normalized]) + ".py",
                "/".join(["src", *normalized, "__init__.py"]),
            ]
        )
    return next((candidate for candidate in candidates if candidate in trees), None)


def _imported_module_path(
    *,
    current_module: str,
    imported_module: str | None,
    level: int,
    trees: dict[str, ast.Module],
    child_module: str | None = None,
) -> str | None:
    current_parts = current_module.removesuffix(".py").split("/")
    package_parts = current_parts[:-1]
    if current_parts[-1:] == ["__init__"]:
        package_parts = current_parts[:-1]
    module_parts = imported_module.split(".") if imported_module else []
    if level:
        ascend = max(level - 1, 0)
        if ascend > len(package_parts):
            return None
        base = package_parts[: len(package_parts) - ascend]
        parts = [*base, *module_parts]
    else:
        parts = module_parts
    if child_module:
        parts.append(child_module)
    return _module_candidate(trees, parts)


def _top_level_binding(
    *,
    trees: dict[str, ast.Module],
    module: str,
    name: str,
    seen: set[tuple[str, str]],
) -> _Binding:
    key = (module, name)
    if key in seen:
        return ("missing", None, module)
    seen = {*seen, key}
    tree = trees.get(module)
    if tree is None:
        return ("missing", None, module)

    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ("found", node, module)

    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                binding_name = alias.asname or alias.name
                if binding_name != name:
                    continue
                if node.module is None:
                    child = _imported_module_path(
                        current_module=module,
                        imported_module=None,
                        level=node.level,
                        trees=trees,
                        child_module=alias.name,
                    )
                    if child is not None:
                        return ("module", None, child)
                    return ("external", None, module)
                imported = _imported_module_path(
                    current_module=module,
                    imported_module=node.module,
                    level=node.level,
                    trees=trees,
                )
                if imported is None:
                    return ("external", None, module)
                resolved = _top_level_binding(
                    trees=trees,
                    module=imported,
                    name=alias.name,
                    seen=seen,
                )
                return resolved
        elif isinstance(node, ast.Import):
            for alias in node.names:
                binding_name = alias.asname or alias.name.split(".", 1)[0]
                if binding_name != name:
                    continue
                imported = _module_candidate(trees, alias.name.split("."))
                return ("module", None, imported) if imported is not None else ("external", None, module)

    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if not any(isinstance(target, ast.Name) and target.id == name for target in targets):
                continue
            value = node.value
            if value is None:
                return ("missing", None, module)
            return _expression_binding(trees=trees, module=module, expression=value, seen=seen)
    return ("missing", None, module)


def _expression_binding(
    *,
    trees: dict[str, ast.Module],
    module: str,
    expression: ast.AST,
    seen: set[tuple[str, str]],
) -> _Binding:
    if isinstance(expression, ast.Name):
        return _top_level_binding(trees=trees, module=module, name=expression.id, seen=seen)
    if isinstance(expression, ast.Subscript):
        return _expression_binding(trees=trees, module=module, expression=expression.value, seen=seen)
    if isinstance(expression, ast.Attribute):
        segments: list[str] = []
        current: ast.AST = expression
        while isinstance(current, ast.Attribute):
            segments.append(current.attr)
            current = current.value
        if not isinstance(current, ast.Name):
            return ("missing", None, module)
        binding = _top_level_binding(trees=trees, module=module, name=current.id, seen=seen)
        for segment in reversed(segments):
            binding = _advance_binding(
                trees=trees,
                binding=binding,
                segment=segment,
                seen_bindings=seen,
                seen_members=set(),
            )
        return binding
    return ("missing", None, module)


def _class_member_binding(
    *,
    trees: dict[str, ast.Module],
    module: str,
    class_node: ast.ClassDef,
    member: str,
    seen_bindings: set[tuple[str, str]],
    seen_members: set[tuple[str, str, str]],
) -> _Binding:
    key = (module, class_node.name, member)
    if key in seen_members:
        return ("missing", None, module)
    seen_members = {*seen_members, key}
    for node in class_node.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == member:
            return ("found", node, module)
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(target, ast.Name) and target.id == member for target in targets):
                if node.value is None:
                    return ("missing", None, module)
                return _expression_binding(
                    trees=trees,
                    module=module,
                    expression=node.value,
                    seen=seen_bindings,
                )

    uncertain_external_base = False
    for base in class_node.bases:
        binding = _expression_binding(trees=trees, module=module, expression=base, seen=seen_bindings)
        if binding[0] == "found" and isinstance(binding[1], ast.ClassDef):
            inherited = _class_member_binding(
                trees=trees,
                module=binding[2],
                class_node=binding[1],
                member=member,
                seen_bindings=seen_bindings,
                seen_members=seen_members,
            )
            if inherited[0] != "missing":
                return inherited
        elif binding[0] == "external":
            uncertain_external_base = True
    return ("external", None, module) if uncertain_external_base else ("missing", None, module)


def _advance_binding(
    *,
    trees: dict[str, ast.Module],
    binding: _Binding,
    segment: str,
    seen_bindings: set[tuple[str, str]],
    seen_members: set[tuple[str, str, str]],
) -> _Binding:
    state, node, module = binding
    if state == "module":
        return _top_level_binding(trees=trees, module=module, name=segment, seen=seen_bindings)
    if state == "external":
        return binding
    if state == "found" and isinstance(node, ast.ClassDef):
        return _class_member_binding(
            trees=trees,
            module=module,
            class_node=node,
            member=segment,
            seen_bindings=seen_bindings,
            seen_members=seen_members,
        )
    return ("missing", None, module)


def _declared_callable_binding(
    *,
    component: dict[str, Any],
    trees: dict[str, ast.Module],
) -> _Binding:
    module = str(component.get("module") or "").strip().replace(chr(92), "/")
    callable_name = str(component.get("callable") or "").strip()
    if (
        module not in trees
        or re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", callable_name) is None
    ):
        return ("missing", None, module)
    segments = callable_name.split(".")
    binding = _top_level_binding(trees=trees, module=module, name=segments[0], seen=set())
    for segment in segments[1:]:
        binding = _advance_binding(
            trees=trees,
            binding=binding,
            segment=segment,
            seen_bindings=set(),
            seen_members=set(),
        )
    return binding


def _validate_declared_callable(
    *,
    component: dict[str, Any],
    trees: dict[str, ast.Module],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    component_id = str(component.get("id") or "<unknown>")
    module = str(component.get("module") or "").strip().replace(chr(92), "/")
    callable_name = str(component.get("callable") or "").strip()
    if not callable_name:
        return (
            [{"file": module or "scientific_architecture.json", "message": f"component {component_id} has no declared callable"}],
            [],
        )
    if re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", callable_name) is None:
        return (
            [{"file": module or "scientific_architecture.json", "message": f"component {component_id} callable is not a simple dotted Python name: {callable_name}"}],
            [],
        )
    if module not in trees:
        return (
            [
                {
                    "file": module or "scientific_architecture.json",
                    "message": (
                        f"component {component_id} declared module is missing or cannot be statically inspected"
                    ),
                }
            ],
            [],
        )

    segments = callable_name.split(".")
    binding = _declared_callable_binding(component=component, trees=trees)
    if binding[0] == "found":
        return [], []
    if binding[0] == "external" and len(segments) > 1:
        return (
            [],
            [
                {
                    "file": module,
                    "message": (
                        f"component {component_id} callable {callable_name} is exposed through an "
                        "external base/import whose final method cannot be proven statically"
                    ),
                    "severity": "warning",
                }
            ],
        )
    return (
        [
            {
                "file": module,
                "message": (
                    f"component {component_id} callable {callable_name} is absent from its declared "
                    "module or reachable local re-export/inheritance chain"
                ),
            }
        ],
        [],
    )


def _external_runtime_command(execution: dict[str, Any]) -> str:
    framework = str(execution.get("primary_framework") or "").strip()
    tokens = set(_normalized_capability(framework).split("-"))
    token_commands = {
        "julia": "julia",
        "matlab": "matlab",
        "octave": "octave",
        "rscript": "Rscript",
        "wolfram": "wolframscript",
    }
    for token, command in token_commands.items():
        if token in tokens:
            return command
    if tokens == {"r"}:
        return "R"
    return framework


def _external_runtime_available(execution: dict[str, Any]) -> tuple[str, str | None]:
    command = _external_runtime_command(execution)
    if not command:
        return "", None
    try:
        return command, shutil.which(command)
    except (OSError, TypeError, ValueError):
        return command, None


def _trusted_external_runtime_adapter(execution: dict[str, Any]) -> str | None:
    framework = _normalized_capability(execution.get("primary_framework"))
    return _TRUSTED_EXTERNAL_RUNTIME_ADAPTERS.get(framework)


def _framework_has_trusted_capability_probe(value: Any) -> bool:
    keys = _library_keys(value)
    return any(keys & _library_keys(framework) for framework in _TRUSTED_CAPABILITY_PROBE_FRAMEWORKS)


def _execution_requires_trusted_capability_probe(execution: dict[str, Any]) -> bool:
    if execution.get("trainable") is True:
        return True
    if str(execution.get("gradient_mode") or "").strip().casefold() == "required":
        return True
    if str(execution.get("checkpoint_policy") or "").strip().casefold() == "required":
        return True
    if str(execution.get("device_policy") or "").strip().casefold() == "accelerator_required":
        return True
    raw_capabilities = execution.get("required_capabilities")
    if not isinstance(raw_capabilities, list):
        return False
    probe_groups = (
        _CAPABILITY_GROUPS["parameter update"],
        _CAPABILITY_GROUPS["gradient/back-propagation"],
        _CAPABILITY_GROUPS["checkpoint round-trip"],
        _CAPABILITY_GROUPS["accelerator availability"],
        _CAPABILITY_GROUPS["accelerator tensor placement"],
    )
    return any(
        any(_capability_matches_group(capability, aliases) for aliases in probe_groups)
        for capability in raw_capabilities
    )


def _binding_is_trivial_external_stub(binding: _Binding) -> bool:
    state, node, _ = binding
    if state != "found" or not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    statements = [
        statement
        for statement in node.body
        if not (
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Constant)
            and isinstance(statement.value.value, str)
        )
    ]
    if len(statements) != 1 or not isinstance(statements[0], ast.Return):
        return False
    value = statements[0].value
    if isinstance(value, ast.Constant) and (
        value.value is None or isinstance(value.value, bool)
    ):
        return True
    argument_names = {
        argument.arg
        for argument in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
        if argument.arg not in {"self", "cls"}
    }
    return isinstance(value, ast.Name) and value.id in argument_names


def _contract_values_equal(expected: Any, actual: Any) -> bool:
    if isinstance(expected, bool) or isinstance(actual, bool):
        return type(expected) is type(actual) and expected == actual
    if isinstance(expected, list) and isinstance(actual, list):
        expected_items = sorted({str(item).strip().casefold() for item in expected})
        actual_items = sorted({str(item).strip().casefold() for item in actual})
        return expected_items == actual_items
    if isinstance(expected, str) and isinstance(actual, str):
        return expected.strip().casefold() == actual.strip().casefold()
    return expected == actual


def _normalized_capability(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().casefold()).strip("-")


def _capability_matches_group(value: Any, aliases: set[str]) -> bool:
    capability = _normalized_capability(value)
    tokens = capability.split("-") if capability else []
    negative_tokens = {
        "absent",
        "disabled",
        "failed",
        "failure",
        "missing",
        "no",
        "not",
        "unavailable",
        "unsupported",
        "untested",
        "without",
    }
    if negative_tokens & set(tokens):
        return False
    for alias in aliases:
        alias_tokens = alias.split("-")
        width = len(alias_tokens)
        if any(tokens[index:index + width] == alias_tokens for index in range(len(tokens) - width + 1)):
            return True
    return False


def _ast_dotted_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _ast_dotted_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    if isinstance(node, ast.Call):
        return _ast_dotted_name(node.func)
    return ""


def _static_bool(node: ast.AST) -> bool | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        value = _static_bool(node.operand)
        return None if value is None else not value
    return None


def _decorator_skips_test(decorator: ast.expr) -> bool:
    target = decorator.func if isinstance(decorator, ast.Call) else decorator
    name = _ast_dotted_name(target).casefold().split(".")[-1].replace("_", "")
    if name in {"skipif", "skipunless"}:
        if not isinstance(decorator, ast.Call) or not decorator.args:
            return True
        condition = _static_bool(decorator.args[0])
        if condition is None:
            return True
        return condition if name == "skipif" else not condition
    return name.startswith(("skip", "expectedfailure", "xfail"))


def _test_is_skipped(node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    if any(_decorator_skips_test(decorator) for decorator in node.decorator_list):
        return True
    for child in ast.walk(node):
        name = _ast_dotted_name(child).casefold()
        if (
            name.endswith(".skiptest")
            or name in {"skiptest", "pytest.skip", "pytest.xfail"}
        ):
            return True
        if isinstance(child, ast.Raise) and "skiptest" in _ast_dotted_name(child.exc).casefold():
            return True
    return False


def _test_has_substantive_body(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    statements = [
        statement
        for statement in node.body
        if not (
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Constant)
            and isinstance(statement.value.value, str)
        )
    ]
    if not statements or all(isinstance(statement, ast.Pass) for statement in statements):
        return False
    for statement in statements:
        for child in ast.walk(statement):
            if isinstance(child, ast.Assert):
                if not isinstance(child.test, ast.Constant):
                    return True
                continue
            if not isinstance(child, ast.Call):
                continue
            name = _ast_dotted_name(child.func).split(".")[-1].casefold()
            if not (name.startswith("assert") or name.startswith("failunless")):
                continue
            if name in {"asserttrue", "assertfalse"} and child.args:
                if isinstance(child.args[0], ast.Constant):
                    continue
            if name in {"assertequal", "assertnotequal"} and len(child.args) >= 2:
                if isinstance(child.args[0], ast.Constant) and isinstance(child.args[1], ast.Constant):
                    continue
            return True
    return False


def _normalized_test_reference(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    reference = value.strip().replace(chr(92), "/").replace("::", ".").replace("/", ".")
    reference = reference.replace(".py.", ".")
    if reference.endswith(".py"):
        reference = reference[:-3]
    return re.sub(r"\.+", ".", reference).strip(".")


_DeliveredTest = tuple[
    ast.FunctionDef | ast.AsyncFunctionDef,
    ast.Module,
    ast.ClassDef,
]


def _delivered_test_references(sandbox: Path) -> dict[str, _DeliveredTest]:
    references: dict[str, _DeliveredTest] = {}
    tests_root = sandbox / "tests"
    if not tests_root.is_dir():
        return references
    for path in sorted(tests_root.rglob("test*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        except (OSError, SyntaxError, UnicodeError):
            continue
        module = path.relative_to(sandbox).with_suffix("").as_posix().replace("/", ".")
        module_names = {module}
        if module.startswith("tests."):
            module_names.add(module[len("tests."):])
        testcase_aliases = {"TestCase"}
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module == "unittest":
                testcase_aliases.update(
                    alias.asname or alias.name
                    for alias in node.names
                    if alias.name == "TestCase"
                )
        testcase_classes: set[str] = set()
        changed = True
        while changed:
            changed = False
            for node in tree.body:
                if not isinstance(node, ast.ClassDef) or node.name in testcase_classes:
                    continue
                base_names = {
                    base.id if isinstance(base, ast.Name) else base.attr
                    for base in node.bases
                    if isinstance(base, (ast.Name, ast.Attribute))
                }
                if not (
                    base_names & testcase_aliases
                    or base_names & testcase_classes
                    or any(name.endswith("TestCase") for name in base_names)
                ):
                    continue
                testcase_classes.add(node.name)
                changed = True
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name in testcase_classes:
                if _test_is_skipped(node):
                    continue
                for method in node.body:
                    if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        continue
                    if not method.name.startswith("test"):
                        continue
                    if _test_is_skipped(method) or not _test_has_substantive_body(method):
                        continue
                    names = {
                        method.name,
                        f"{node.name}.{method.name}",
                        *(f"{name}.{node.name}.{method.name}" for name in module_names),
                    }
                    for reference in names:
                        references[_normalized_test_reference(reference)] = (method, tree, node)
    return references


def _capability_status_passed(item: dict[str, Any]) -> bool:
    status = item.get("status")
    if status is True:
        return True
    return isinstance(status, str) and status.strip().casefold() in {"ok", "passed", "success", "verified"}


def _test_import_targets(tree: ast.Module) -> dict[str, str]:
    targets: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name.split(".", 1)[0]
                targets[local] = alias.name if alias.asname else local
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                if alias.name == "*":
                    continue
                targets[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return targets


def _resolve_test_name(name: str, import_targets: dict[str, str]) -> str:
    if not name:
        return ""
    first, separator, remainder = name.partition(".")
    target = import_targets.get(first)
    if target is None:
        return name
    return f"{target}.{remainder}" if separator else target


def _flow_reference(node: ast.AST | None) -> str:
    if isinstance(node, ast.Name):
        return "@fixture" if node.id in {"self", "cls"} else node.id
    if isinstance(node, ast.Attribute):
        prefix = _flow_reference(node.value)
        return f"{prefix}.{node.attr}" if prefix else ""
    return ""


def _component_test_target(component: dict[str, Any]) -> tuple[str, bool]:
    module = str(component.get("module") or "").strip().replace(chr(92), "/")
    if module.endswith(".py"):
        module = module[:-3]
    module = module.replace("/", ".").strip(".")
    callable_name = str(component.get("callable") or "").strip()
    segments = callable_name.split(".") if callable_name else []
    if not module or not segments:
        return "", False
    # A dotted Class.method contract has an explicit constructor root. A single
    # name is intentionally ambiguous: it may itself be a class or a factory that
    # returns the bound component, so its call result must retain instance binding.
    return f"{module}.{segments[0]}", len(segments) > 1


def _factory_expression_tags(
    node: ast.AST | None,
    *,
    local_values: dict[str, set[str]],
    import_targets: dict[str, str],
    component_target: str,
    component_is_class: bool,
    known_factories: dict[str, set[str]],
) -> set[str]:
    if isinstance(node, ast.Name):
        return set(local_values.get(node.id, set()))
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return set().union(
            *(
                _factory_expression_tags(
                    item,
                    local_values=local_values,
                    import_targets=import_targets,
                    component_target=component_target,
                    component_is_class=component_is_class,
                    known_factories=known_factories,
                )
                for item in node.elts
            ),
            set(),
        )
    if isinstance(node, ast.IfExp):
        return _factory_expression_tags(
            node.body,
            local_values=local_values,
            import_targets=import_targets,
            component_target=component_target,
            component_is_class=component_is_class,
            known_factories=known_factories,
        ) | _factory_expression_tags(
            node.orelse,
            local_values=local_values,
            import_targets=import_targets,
            component_target=component_target,
            component_is_class=component_is_class,
            known_factories=known_factories,
        )
    if not isinstance(node, ast.Call):
        return set()
    call_name = _ast_dotted_name(node.func)
    resolved = _resolve_test_name(call_name, import_targets)
    if resolved == component_target:
        return {"instance"} if component_is_class else {"instance", "output"}
    factory = call_name.split(".")[-1]
    return set(known_factories.get(factory, set()))


def _component_factory_tags(
    tree: ast.Module,
    test_class: ast.ClassDef,
    *,
    import_targets: dict[str, str],
    component_target: str,
    component_is_class: bool,
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    functions = [
        node
        for node in [*tree.body, *test_class.body]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and not node.name.startswith("test")
    ]
    factories: dict[str, set[str]] = {}
    fixture_parameters: dict[str, set[str]] = {}
    for _ in range(len(functions) + 1):
        changed = False
        for function in functions:
            local_values: dict[str, set[str]] = {}
            returned: set[str] = set()
            for statement in function.body:
                if isinstance(statement, (ast.Assign, ast.AnnAssign)):
                    value = statement.value
                    tags = _factory_expression_tags(
                        value,
                        local_values=local_values,
                        import_targets=import_targets,
                        component_target=component_target,
                        component_is_class=component_is_class,
                        known_factories=factories,
                    )
                    targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                    for target in targets:
                        if isinstance(target, ast.Name) and tags:
                            local_values[target.id] = set(tags)
                elif isinstance(statement, ast.Return):
                    returned |= _factory_expression_tags(
                        statement.value,
                        local_values=local_values,
                        import_targets=import_targets,
                        component_target=component_target,
                        component_is_class=component_is_class,
                        known_factories=factories,
                    )
            if returned and returned != factories.get(function.name):
                factories[function.name] = returned
                changed = True
        if not changed:
            break
    for function in functions:
        if any("fixture" in _ast_dotted_name(decorator).casefold() for decorator in function.decorator_list):
            tags = factories.get(function.name)
            if tags:
                fixture_parameters[function.name] = set(tags)
    return factories, fixture_parameters


def _new_component_flow(
    *,
    tree: ast.Module,
    test_class: ast.ClassDef,
    component: dict[str, Any],
) -> dict[str, Any]:
    import_targets = _test_import_targets(tree)
    component_target, component_is_class = _component_test_target(component)
    factories, fixture_parameters = _component_factory_tags(
        tree,
        test_class,
        import_targets=import_targets,
        component_target=component_target,
        component_is_class=component_is_class,
    )
    callable_name = str(component.get("callable") or "").strip()
    return {
        "values": {},
        "import_targets": import_targets,
        "component_target": component_target,
        "component_is_class": component_is_class,
        "component_method": callable_name.split(".")[-1] if "." in callable_name else "",
        "factories": factories,
        "fixture_parameters": fixture_parameters,
        "actions": set(),
        "assertion_tags": set(),
        "change_assertion": False,
        "component_interactions": 0,
        "checkpoint_saved": False,
    }


def _assign_flow_target(target: ast.AST, tags: set[str], flow: dict[str, Any]) -> None:
    if isinstance(target, (ast.Tuple, ast.List)):
        for item in target.elts:
            _assign_flow_target(item, tags, flow)
        return
    reference = _flow_reference(target)
    if reference and tags:
        flow["values"][reference] = set(tags)


def _flow_expression_tags(
    node: ast.AST | None,
    flow: dict[str, Any],
    *,
    record_actions: bool,
) -> set[str]:
    if node is None:
        return set()
    reference = _flow_reference(node)
    if reference in flow["values"]:
        return set(flow["values"][reference])
    if isinstance(node, ast.Name):
        return set()
    if isinstance(node, ast.Subscript):
        return _flow_expression_tags(node.value, flow, record_actions=record_actions)
    if isinstance(node, ast.Attribute):
        base_tags = _flow_expression_tags(node.value, flow, record_actions=record_actions)
        if node.attr in {"__class__", "__name__", "__qualname__", "__module__"}:
            return set()
        if "instance" in base_tags:
            flow["component_interactions"] += 1
            if node.attr.casefold() in {"grad", "gradient", "gradients"}:
                return {"gradient"}
            if node.attr.casefold() in {"device", "is_cuda"}:
                return {"device"}
            return {"parameter"}
        if "parameter" in base_tags and node.attr.casefold() in {"grad", "gradient", "gradients"}:
            return {"gradient"}
        if base_tags & {"instance", "parameter", "output"} and node.attr.casefold() in {"device", "is_cuda"}:
            return {"device"}
        return set(base_tags)
    if isinstance(node, ast.Call):
        call_name = _ast_dotted_name(node.func)
        resolved = _resolve_test_name(call_name, flow["import_targets"])
        if resolved == flow["component_target"]:
            if flow["component_is_class"]:
                return {"instance"}
            flow["component_interactions"] += 1
            return {"instance", "output"}
        factory = call_name.split(".")[-1]
        if factory in flow["factories"]:
            return set(flow["factories"][factory])

        callable_reference = _flow_reference(node.func)
        callable_tags = set(flow["values"].get(callable_reference, set()))
        if "instance" in callable_tags:
            flow["component_interactions"] += 1
            return {"output"}
        receiver_tags = (
            _flow_expression_tags(node.func.value, flow, record_actions=record_actions)
            if isinstance(node.func, ast.Attribute)
            else _flow_expression_tags(node.func, flow, record_actions=record_actions)
        )
        argument_tags = set().union(
            *(
                _flow_expression_tags(argument, flow, record_actions=record_actions)
                for argument in [*node.args, *(keyword.value for keyword in node.keywords)]
            ),
            set(),
        )
        method = _normalized_capability(call_name.split(".")[-1])
        resolved_tokens = {
            _normalized_capability(part)
            for part in resolved.split(".")
            if _normalized_capability(part)
        }

        if "instance" in receiver_tags:
            flow["component_interactions"] += 1
            if method in {"parameter", "parameters", "named-parameters"}:
                return {"parameter"}
            if method in {"state-dict", "get-state", "pack", "serialize"}:
                if record_actions:
                    flow["actions"].add("checkpoint_save")
                    flow["checkpoint_saved"] = True
                return {"checkpoint"}
            if method in {"load-state-dict", "set-state", "unpack", "restore", "deserialize"}:
                if record_actions and "checkpoint" in argument_tags:
                    flow["actions"].add("checkpoint_load")
                return {"instance"}
            if method in {"to", "cuda", "xpu", "mps", "put", "place"}:
                if record_actions:
                    flow["actions"].add("accelerator_placement")
                return {"instance", "device"}
            if method in {"apply", "fit", "minimise", "minimize", "step", "train-step", "update"}:
                if record_actions:
                    flow["actions"].add("parameter_update")
                return {"output"}
            return {"output"}

        if "optimizer" in receiver_tags:
            if record_actions and method in {"apply", "step", "train-step", "update"}:
                flow["actions"].add("parameter_update")
            return set()
        if receiver_tags & {"output", "parameter"}:
            if record_actions and method in {"backward", "grad", "gradient", "vjp"}:
                flow["actions"].add("gradient")
                return {"gradient"}
            return set(receiver_tags)

        if "parameter" in argument_tags and (
            "optim" in resolved_tokens
            or method in {"adagrad", "adam", "adamw", "optimizer", "rmsprop", "sgd"}
        ):
            return {"optimizer"}
        if record_actions and method in {"backward", "grad", "gradient", "vjp"} and argument_tags & {
            "output",
            "parameter",
        }:
            flow["actions"].add("gradient")
            return {"gradient"}
        if record_actions and method in {"available", "availability", "is-available"}:
            flow["actions"].add("accelerator_availability")
        trusted_namespace = resolved.split(".", 1)[0].casefold() in {"torch", "pickle", "joblib"}
        standalone_helper = isinstance(node.func, ast.Name)
        if method in {"dump", "save", "save-file", "serialize", "write"}:
            if argument_tags & {"checkpoint", "parameter", "instance"} and (
                trusted_namespace or standalone_helper
            ):
                if record_actions:
                    flow["actions"].add("checkpoint_save")
                    flow["checkpoint_saved"] = True
                return {"checkpoint"}
        if method in {"deserialize", "load", "load-file", "read", "restore"}:
            if flow["checkpoint_saved"] and (trusted_namespace or standalone_helper):
                if record_actions:
                    flow["actions"].add("checkpoint_load")
                return {"checkpoint"}
        if argument_tags:
            return set(argument_tags)
        return set().union(
            *(
                _flow_expression_tags(child, flow, record_actions=record_actions)
                for child in ast.iter_child_nodes(node)
                if child is not node.func
            ),
            set(),
        )
    if isinstance(node, ast.NamedExpr):
        tags = _flow_expression_tags(node.value, flow, record_actions=record_actions)
        _assign_flow_target(node.target, tags, flow)
        return tags
    return set().union(
        *(
            _flow_expression_tags(child, flow, record_actions=record_actions)
            for child in ast.iter_child_nodes(node)
        ),
        set(),
    )


def _assertion_operand_tags(call: ast.Call, flow: dict[str, Any]) -> set[str]:
    name = _ast_dotted_name(call.func).split(".")[-1].casefold()
    if not (name.startswith("assert") or name.startswith("failunless")):
        return set()
    operands = call.args[:1] if name in {
        "assertfalse",
        "assertisnone",
        "assertisnotnone",
        "asserttrue",
        "failunless",
    } else call.args[:2]
    return set().union(
        *(
            _flow_expression_tags(operand, flow, record_actions=True)
            for operand in operands
        ),
        set(),
    )


def _component_change_pair(
    left: ast.AST,
    right: ast.AST,
    flow: dict[str, Any],
) -> bool:
    if ast.dump(left, include_attributes=False) == ast.dump(right, include_attributes=False):
        return False
    material = {"checkpoint", "parameter"}
    left_tags = _flow_expression_tags(left, flow, record_actions=True)
    right_tags = _flow_expression_tags(right, flow, record_actions=True)
    return bool(left_tags & material and right_tags & material)


def _negated_equality_has_component_change(node: ast.AST, flow: dict[str, Any]) -> bool:
    if isinstance(node, ast.Call):
        name = _normalized_capability(_ast_dotted_name(node.func).split(".")[-1])
        if name in {"allclose", "equal", "isclose"} and len(node.args) >= 2:
            return _component_change_pair(node.args[0], node.args[1], flow)
    if isinstance(node, ast.Compare) and len(node.ops) == 1 and len(node.comparators) == 1:
        if isinstance(node.ops[0], ast.Eq):
            return _component_change_pair(node.left, node.comparators[0], flow)
    return False


def _assert_expression_has_component_change(node: ast.AST, flow: dict[str, Any]) -> bool:
    if isinstance(node, ast.Compare) and len(node.ops) == 1 and len(node.comparators) == 1:
        if isinstance(node.ops[0], ast.NotEq):
            return _component_change_pair(node.left, node.comparators[0], flow)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return _negated_equality_has_component_change(node.operand, flow)
    return False


def _assertion_call_has_component_change(call: ast.Call, flow: dict[str, Any]) -> bool:
    name = _normalized_capability(_ast_dotted_name(call.func).split(".")[-1])
    if name in {"assertnotalmostequal", "assertnotequal", "failifequal"}:
        return len(call.args) >= 2 and _component_change_pair(call.args[0], call.args[1], flow)
    if name == "assertfalse" and call.args:
        return _negated_equality_has_component_change(call.args[0], flow)
    if name == "asserttrue" and call.args:
        return _assert_expression_has_component_change(call.args[0], flow)
    return False


def _analyze_flow_statements(
    statements: list[ast.stmt],
    flow: dict[str, Any],
    *,
    record_actions: bool,
) -> None:
    for statement in statements:
        if isinstance(statement, (ast.Assign, ast.AnnAssign)):
            tags = _flow_expression_tags(statement.value, flow, record_actions=record_actions)
            targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
            for target in targets:
                _assign_flow_target(target, tags, flow)
        elif isinstance(statement, ast.AugAssign):
            target_tags = _flow_expression_tags(statement.target, flow, record_actions=record_actions)
            value_tags = _flow_expression_tags(statement.value, flow, record_actions=record_actions)
            if record_actions and "parameter" in target_tags:
                flow["actions"].add("parameter_update")
            _assign_flow_target(statement.target, target_tags | value_tags, flow)
        elif isinstance(statement, ast.For):
            iter_tags = _flow_expression_tags(statement.iter, flow, record_actions=record_actions)
            _assign_flow_target(statement.target, iter_tags, flow)
            _analyze_flow_statements(statement.body, flow, record_actions=record_actions)
            _analyze_flow_statements(statement.orelse, flow, record_actions=record_actions)
        elif isinstance(statement, ast.Assert):
            tags = _flow_expression_tags(
                statement.test,
                flow,
                record_actions=record_actions,
            )
            if record_actions:
                flow["assertion_tags"] |= tags
                if _assert_expression_has_component_change(statement.test, flow):
                    flow["change_assertion"] = True
        elif isinstance(statement, ast.Expr):
            if record_actions and isinstance(statement.value, ast.Call):
                flow["assertion_tags"] |= _assertion_operand_tags(statement.value, flow)
                if _assertion_call_has_component_change(statement.value, flow):
                    flow["change_assertion"] = True
            _flow_expression_tags(statement.value, flow, record_actions=record_actions)
        elif isinstance(statement, (ast.If, ast.While)):
            _flow_expression_tags(statement.test, flow, record_actions=record_actions)
            _analyze_flow_statements(statement.body, flow, record_actions=record_actions)
            _analyze_flow_statements(statement.orelse, flow, record_actions=record_actions)
        elif isinstance(statement, (ast.With, ast.AsyncWith)):
            for item in statement.items:
                tags = _flow_expression_tags(item.context_expr, flow, record_actions=record_actions)
                if item.optional_vars is not None:
                    _assign_flow_target(item.optional_vars, tags, flow)
            _analyze_flow_statements(statement.body, flow, record_actions=record_actions)
        elif isinstance(statement, ast.Try):
            _analyze_flow_statements(statement.body, flow, record_actions=record_actions)
            for handler in statement.handlers:
                _analyze_flow_statements(handler.body, flow, record_actions=record_actions)
            _analyze_flow_statements(statement.orelse, flow, record_actions=record_actions)
            _analyze_flow_statements(statement.finalbody, flow, record_actions=record_actions)
        elif isinstance(statement, (ast.Return, ast.Raise)):
            _flow_expression_tags(
                statement.value if isinstance(statement, ast.Return) else statement.exc,
                flow,
                record_actions=record_actions,
            )


def _component_test_flow(
    delivered: _DeliveredTest,
    component: dict[str, Any],
) -> dict[str, Any]:
    method, tree, test_class = delivered
    flow = _new_component_flow(tree=tree, test_class=test_class, component=component)
    module_assignments = [
        node for node in tree.body if isinstance(node, (ast.Assign, ast.AnnAssign))
    ]
    class_assignments = [
        node for node in test_class.body if isinstance(node, (ast.Assign, ast.AnnAssign))
    ]
    _analyze_flow_statements(module_assignments, flow, record_actions=False)
    _analyze_flow_statements(class_assignments, flow, record_actions=False)
    for node in test_class.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        is_setup = node.name in {"setUp", "setUpClass", "asyncSetUp", "asyncSetUpClass"}
        is_fixture = any(
            "fixture" in _ast_dotted_name(decorator).casefold()
            for decorator in node.decorator_list
        )
        if is_setup or is_fixture:
            _analyze_flow_statements(node.body, flow, record_actions=False)
    arguments = [*method.args.posonlyargs, *method.args.args, *method.args.kwonlyargs]
    for argument in arguments:
        fixture_tags = flow["fixture_parameters"].get(argument.arg)
        if fixture_tags:
            flow["values"][argument.arg] = set(fixture_tags)
    _analyze_flow_statements(method.body, flow, record_actions=True)
    return flow


def _method_references_component(flow: dict[str, Any]) -> bool:
    material_tags = {"checkpoint", "device", "gradient", "output", "parameter"}
    return bool(
        flow["component_interactions"]
        and flow["assertion_tags"] & material_tags
    )


def _method_evidences_capability(flow: dict[str, Any], label: str) -> bool:
    assertion_tags = flow["assertion_tags"]
    actions = flow["actions"]
    if label == "real parameter update":
        return "parameter_update" in actions and flow["change_assertion"] is True
    if label == "gradient/back-propagation":
        return "gradient" in actions and bool(assertion_tags & {"gradient", "parameter"})
    if label == "checkpoint round-trip":
        return {
            "checkpoint_save",
            "checkpoint_load",
        }.issubset(actions) and bool(assertion_tags & {"checkpoint", "parameter"})
    if label == "accelerator availability":
        return "accelerator_availability" in actions and bool(assertion_tags & {"device", "parameter"})
    if label == "accelerator tensor placement":
        return "accelerator_placement" in actions and "device" in assertion_tags
    return True


def _capability_test_passed(
    item: dict[str, Any],
    delivered_tests: dict[str, _DeliveredTest],
    *,
    component: dict[str, Any] | None = None,
    label: str | None = None,
) -> bool:
    reference = item.get("test") or item.get("test_id") or item.get("test_name")
    if not isinstance(reference, str) or not reference.strip():
        return False
    delivered = delivered_tests.get(_normalized_test_reference(reference))
    if not _capability_status_passed(item) or delivered is None:
        return False
    if component is None:
        return True
    if not _contract_values_equal(component.get("module"), item.get("module")):
        return False
    if not _contract_values_equal(component.get("callable"), item.get("callable")):
        return False
    flow = _component_test_flow(delivered, component)
    if not _method_references_component(flow):
        return False
    if label is None:
        return True
    if label not in _FRAMEWORK_SEMANTIC_LABELS:
        return True
    execution = component.get("execution")
    framework = execution.get("primary_framework") if isinstance(execution, dict) else ""
    if not _framework_has_trusted_capability_probe(framework):
        return False
    return _method_evidences_capability(flow, label)


def _required_component_capabilities(component: dict[str, Any]) -> list[tuple[str, set[str], bool]]:
    execution = component.get("execution")
    if not isinstance(execution, dict):
        return []
    requirements: list[tuple[str, set[str], bool]] = []
    raw_capabilities = execution.get("required_capabilities")
    if isinstance(raw_capabilities, list):
        for capability in raw_capabilities:
            normalized = _normalized_capability(capability)
            if normalized:
                requirements.append((f"required capability {capability}", {normalized}, False))
    if execution.get("trainable") is True:
        requirements.append(("real parameter update", _CAPABILITY_GROUPS["parameter update"], True))
    if str(execution.get("gradient_mode") or "").strip().casefold() == "required":
        requirements.append(("gradient/back-propagation", _CAPABILITY_GROUPS["gradient/back-propagation"], True))
    if str(execution.get("checkpoint_policy") or "").strip().casefold() == "required":
        requirements.append(("checkpoint round-trip", _CAPABILITY_GROUPS["checkpoint round-trip"], True))
    if str(execution.get("device_policy") or "").strip().casefold() == "accelerator_required":
        requirements.append(("accelerator availability", _CAPABILITY_GROUPS["accelerator availability"], True))
        requirements.append(
            ("accelerator tensor placement", _CAPABILITY_GROUPS["accelerator tensor placement"], True)
        )
    if str(execution.get("device_policy") or "").strip().casefold() == "external_runtime":
        requirements.append(
            ("external runtime availability", _CAPABILITY_GROUPS["external runtime availability"], True)
        )
        requirements.append(
            (
                "external runtime invocation interface",
                _CAPABILITY_GROUPS["external runtime invocation interface"],
                True,
            )
        )
    return requirements


def _validate_foundation_execution_contracts(
    *,
    sandbox: Path,
    architecture: dict[str, Any],
    result: dict[str, Any],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Statically enforce schema 1.1 execution contracts without importing generated code."""

    if not _architecture_requires_execution_contracts(architecture):
        return [], []

    issues: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    components = _architecture_components(architecture)
    trees, tree_issues = _foundation_source_trees(sandbox)
    issues.extend(tree_issues)
    project_keys = _foundation_project_import_keys(sandbox)
    declared_keys = _declared_requirement_keys(sandbox) | _architecture_dependency_names(architecture)

    for component in components:
        component_id = str(component.get("id") or "<unknown>")
        execution = component.get("execution")
        if not isinstance(execution, dict):
            issues.append(
                {
                    "file": "scientific_architecture.json",
                    "message": f"component {component_id} has no schema 1.1 execution contract",
                }
            )
            continue
        callable_issues, callable_warnings = _validate_declared_callable(
            component=component,
            trees=trees,
        )
        issues.extend(callable_issues)
        warnings.extend(callable_warnings)
        if str(execution.get("device_policy") or "").strip().casefold() == "external_runtime":
            declared_capabilities = (
                execution.get("required_capabilities")
                if isinstance(execution.get("required_capabilities"), list)
                else []
            )
            for label in ("external runtime availability", "external runtime invocation interface"):
                if any(
                    _capability_matches_group(capability, _CAPABILITY_GROUPS[label])
                    for capability in declared_capabilities
                ):
                    continue
                issues.append(
                    {
                        "file": "scientific_architecture.json",
                        "message": (
                            f"component {component_id} external_runtime contract must declare {label} "
                            "in required_capabilities"
                        ),
                    }
                )
            runtime_command, resolved_runtime = _external_runtime_available(execution)
            if resolved_runtime is None:
                issues.append(
                    {
                        "file": "scientific_architecture.json",
                        "message": (
                            f"component {component_id} has an external runtime host capability gap: "
                            f"command {runtime_command or '<unspecified>'!r} was not found by the "
                            "trusted host executable probe"
                        ),
                    }
                )
            if _trusted_external_runtime_adapter(execution) is None:
                issues.append(
                    {
                        "file": "scientific_architecture.json",
                        "message": (
                            f"component {component_id} environment_extension_required: "
                            "no trusted host invocation adapter is registered for external runtime "
                            f"{execution.get('primary_framework')!r}"
                        ),
                    }
                )
            callable_binding = _declared_callable_binding(component=component, trees=trees)
            if _binding_is_trivial_external_stub(callable_binding):
                issues.append(
                    {
                        "file": str(component.get("module") or "src"),
                        "message": (
                            f"component {component_id} external runtime callable is a constant/identity "
                            "stub and cannot prove runtime invocation"
                        ),
                    }
                )
            continue
        framework = execution.get("primary_framework")
        if (
            _execution_requires_trusted_capability_probe(execution)
            and not _framework_has_trusted_capability_probe(framework)
        ):
            issues.append(
                {
                    "file": "scientific_architecture.json",
                    "message": (
                        f"component {component_id} environment_extension_required: no trusted "
                        f"training/gradient/checkpoint/device capability probe is registered for "
                        f"framework {framework!r}"
                    ),
                }
            )
        if not _framework_is_external(framework, project_keys):
            continue
        if _requirement_name_for_library(framework) is None:
            issues.append(
                {
                    "file": "requirements.txt",
                    "message": (
                        f"component {component_id} environment_extension_required: framework "
                        f"{framework!r} has no allowlisted Python requirement/runtime registry entry"
                    ),
                }
            )
            continue
        framework_keys = _library_keys(framework)
        if not framework_keys & declared_keys:
            issues.append(
                {
                    "file": "requirements.txt",
                    "message": (
                        f"component {component_id} primary framework {framework!r} is not declared in "
                        "requirements.txt or architecture dependency metadata"
                    ),
                }
            )
        component_import_keys = _component_framework_import_keys(
            trees,
            str(component.get("module") or "").strip().replace(chr(92), "/"),
        )
        if not framework_keys & component_import_keys:
            issues.append(
                {
                    "file": str(component.get("module") or "src"),
                    "message": (
                        f"component {component_id} primary framework {framework!r} is never imported "
                        "by Foundation-owned source"
                    ),
                }
            )

    raw_contracts = result.get("execution_contracts")
    contracts = [item for item in raw_contracts if isinstance(item, dict)] if isinstance(raw_contracts, list) else []
    if not isinstance(raw_contracts, list):
        issues.append(
            {
                "file": "foundation_result.json",
                "message": "schema 1.1 Foundation hand-off must contain an execution_contracts array",
            }
        )
    contracts_by_component: dict[str, list[dict[str, Any]]] = {}
    for contract in contracts:
        contracts_by_component.setdefault(str(contract.get("component_id") or ""), []).append(contract)

    for component in components:
        component_id = str(component.get("id") or "")
        matches = contracts_by_component.get(component_id, [])
        if not matches:
            issues.append(
                {
                    "file": "foundation_result.json",
                    "message": f"missing execution_contracts record for component {component_id or '<unknown>'}",
                }
            )
            continue
        if len(matches) > 1:
            issues.append(
                {
                    "file": "foundation_result.json",
                    "message": f"duplicate execution_contracts records for component {component_id}",
                }
            )
        contract = matches[0]
        for field in ("module", "callable"):
            if not _contract_values_equal(component.get(field), contract.get(field)):
                issues.append(
                    {
                        "file": "foundation_result.json",
                        "message": f"component {component_id} execution contract does not match architecture {field}",
                    }
                )
        expected_execution = component.get("execution")
        actual_execution = contract.get("execution")
        if not isinstance(expected_execution, dict) or not isinstance(actual_execution, dict):
            issues.append(
                {
                    "file": "foundation_result.json",
                    "message": f"component {component_id} execution contract must contain an execution object",
                }
            )
            continue
        for field in _MATERIAL_EXECUTION_FIELDS:
            if not _contract_values_equal(expected_execution.get(field), actual_execution.get(field)):
                issues.append(
                    {
                        "file": "foundation_result.json",
                        "message": f"component {component_id} execution contract weakens or changes {field}",
                    }
                )
        if not _contract_values_equal(expected_execution.get("rationale"), actual_execution.get("rationale")):
            warnings.append(
                {
                    "file": "foundation_result.json",
                    "message": f"component {component_id} execution rationale was not copied exactly",
                    "severity": "warning",
                }
            )

    known_ids = {str(component.get("id") or "") for component in components}
    for component_id in sorted(set(contracts_by_component) - known_ids):
        warnings.append(
            {
                "file": "foundation_result.json",
                "message": f"execution_contracts contains unknown component {component_id or '<empty>'}",
                "severity": "warning",
            }
        )

    raw_capability_tests = result.get("capability_tests")
    capability_tests = (
        [item for item in raw_capability_tests if isinstance(item, dict)]
        if isinstance(raw_capability_tests, list)
        else []
    )
    delivered_tests = _delivered_test_references(sandbox)
    components_by_id = {str(component.get("id") or ""): component for component in components}
    for item in capability_tests:
        component = components_by_id.get(str(item.get("component_id") or ""))
        if _capability_status_passed(item) and not _capability_test_passed(
            item,
            delivered_tests,
            component=component,
        ):
            issues.append(
                {
                    "file": "foundation_result.json",
                    "message": (
                        "passing capability_tests record is not bound to its declared "
                        "component module/callable and a substantive delivered unittest method"
                    ),
                }
            )
    expected_capability_count = sum(len(_required_component_capabilities(component)) for component in components)
    if expected_capability_count and not isinstance(raw_capability_tests, list):
        issues.append(
            {
                "file": "foundation_result.json",
                "message": "required execution capabilities need a capability_tests array",
            }
        )

    for component in components:
        component_id = str(component.get("id") or "")
        component_tests = [
            item
            for item in capability_tests
            if str(item.get("component_id") or "") == component_id
        ]
        for label, accepted, group_match in _required_component_capabilities(component):
            matching = [
                item
                for item in component_tests
                if (
                    _capability_matches_group(item.get("capability"), accepted)
                    if group_match
                    else _normalized_capability(item.get("capability")) in accepted
                )
            ]
            if not any(
                _capability_test_passed(
                    item,
                    delivered_tests,
                    component=component,
                    label=label,
                )
                for item in matching
            ):
                issues.append(
                    {
                        "file": "foundation_result.json",
                        "message": (
                            f"component {component_id} lacks passing capability_tests evidence for {label}"
                        ),
                    }
                )

    for item in capability_tests:
        component_id = str(item.get("component_id") or "")
        if component_id not in known_ids:
            warnings.append(
                {
                    "file": "foundation_result.json",
                    "message": f"capability_tests contains unknown component {component_id or '<empty>'}",
                    "severity": "warning",
                }
            )
    return issues, warnings


def _foundation_brief(architecture: dict[str, Any]) -> str:
    modules = sorted(_required_foundation_modules(architecture))
    component_contracts = [
        {
            "component_id": str(component.get("id") or ""),
            "kind": str(component.get("kind") or ""),
            "module": str(component.get("module") or ""),
            "callable": str(component.get("callable") or ""),
            "execution": component.get("execution") if isinstance(component.get("execution"), dict) else {},
        }
        for component in _architecture_components(architecture)
    ]
    acceptance_output_contracts: list[dict[str, Any]] = []
    raw_bindings = architecture.get("bindings")
    for binding in raw_bindings if isinstance(raw_bindings, list) else []:
        if not isinstance(binding, dict):
            continue
        binding_outputs = {
            str(output_id)
            for output_id in binding.get("outputs", [])
        } if isinstance(binding.get("outputs"), list) else set()
        raw_acceptance = binding.get("acceptance_bindings")
        for acceptance in raw_acceptance if isinstance(raw_acceptance, list) else []:
            if not isinstance(acceptance, dict):
                continue
            criterion_id = str(acceptance.get("criterion_id") or "")
            output_ids = [
                str(output_id)
                for output_id in acceptance.get("output_quantity_ids", [])
                if str(output_id) in binding_outputs
            ] if isinstance(acceptance.get("output_quantity_ids"), list) else []
            if not criterion_id or not output_ids:
                continue
            acceptance_output_contracts.append(
                {
                    "task_id": str(binding.get("task_id") or ""),
                    "criterion_id": criterion_id,
                    "criterion_kind": str(acceptance.get("criterion_kind") or ""),
                    "output_quantity_ids": output_ids,
                }
            )
    execution_contract_required = _architecture_requires_execution_contracts(architecture)
    result_template: dict[str, Any] = {
        "status": "ready_for_tasks",
        "summary": "one concise Chinese sentence",
        "tests_command": "python -m unittest discover -s tests -v",
        "tested_invariants": ["invariant ids"],
        "remaining_uncertainties": ["explicit unresolved items only"],
    }
    if execution_contract_required:
        result_template["execution_contracts"] = [
            {
                "component_id": item["component_id"],
                "module": item["module"],
                "callable": item["callable"],
                "execution": item["execution"],
            }
            for item in component_contracts
        ]
        capability_templates: list[dict[str, str]] = []
        for component in _architecture_components(architecture):
            execution = component.get("execution") if isinstance(component.get("execution"), dict) else {}
            capabilities = [
                str(capability)
                for capability in execution.get("required_capabilities", [])
                if str(capability).strip()
            ]
            if execution.get("trainable") is True:
                capabilities.append("training_step")
            if str(execution.get("gradient_mode") or "").strip().casefold() == "required":
                capabilities.append("gradient_flow")
            if str(execution.get("checkpoint_policy") or "").strip().casefold() == "required":
                capabilities.append("checkpoint_roundtrip")
            device_policy = str(execution.get("device_policy") or "").strip().casefold()
            if device_policy == "accelerator_required":
                capabilities.extend(["accelerator_availability", "tensor_device_placement"])
            elif device_policy == "external_runtime":
                capabilities.extend(["runtime_availability", "runtime_invocation"])
            for capability in dict.fromkeys(capabilities):
                capability_templates.append(
                    {
                        "component_id": str(component.get("id") or ""),
                        "module": str(component.get("module") or ""),
                        "callable": str(component.get("callable") or ""),
                        "capability": capability,
                        "test": "tests.test_component.ComponentTests.test_capability",
                        "status": "passed",
                    }
                )
        result_template["capability_tests"] = capability_templates
    execution_result_note = (
        """
6. Because this is scientific architecture schema 1.1 or newer, `foundation_result.json`
   must use the single complete template above. Keep one `execution_contracts`
   record for every component, copy its architecture `execution` object without
   weakening it, and replace every capability `test` placeholder with the real
   delivered test method. Every capability record must retain the component's
   exact `module` and `callable`, and its test must construct/call that public
   component while asserting the relevant state transition.
   A component with `trainable: true` needs evidence of a real parameter update.
   `gradient_mode: required` needs a gradient/back-propagation test, and
   `checkpoint_policy: required` needs a save/load round-trip test.
   `device_policy: accelerator_required` needs tests for accelerator availability
   and actual tensor placement on that accelerator. Every `test` reference must
   identify a discoverable `unittest.TestCase` method actually delivered under
   `tests/test*.py`. `status: passed` is metadata, not proof: the host reruns the
   complete delivered suite and freezes the Foundation only after a real zero-exit
   test outcome. Hard training/gradient/checkpoint/device claims are certified only
   for frameworks in the trusted probe registry; otherwise report
   `environment_extension_required` instead of copying PyTorch method names or
   claiming a pass.
"""
        if _architecture_requires_execution_contracts(architecture)
        else """
6. This is a legacy schema 1.0 architecture. `execution_contracts` and
   `capability_tests` are encouraged when useful but are not required for compatibility.
"""
    )
    return f"""# Role: Foundation Writer

Build the shared scientific foundation for all reproduction tasks. The architecture contract is mandatory and already validated. You own shared source modules and contract tests only; you do not own any figure-specific task, experiment output, report, or runtime result.

## Mandatory inputs
- `paper_evidence/analysis_artifacts/scientific_architecture.json`
- all other finalized artifacts in `paper_evidence/analysis_artifacts/`
- copied source paper and rendered pages under `paper_evidence/`

## Required modules
Create every module below and implement the interfaces assigned by the architecture:
```json
{pretty_json(modules)}
```

## Per-component implementation contract
The architecture designer, not the Foundation Writer, has already selected the
technical stack for each component. Follow each `module`, `callable`, and
`execution` object below. Components may intentionally use different frameworks;
a mixed-framework Foundation is valid and must not be flattened into one preferred
stack.
```json
{pretty_json(component_contracts)}
```

## Measurable acceptance output interfaces
The optional mappings below are routing hints from task criterion IDs to shared
quantities. Implement the listed quantity interfaces so Task Writers can measure
them. Do not decide whether a paper conclusion is supported, compute an acceptance
verdict, or restate the task's scientific contract. Unknown or absent mappings do
not create Foundation work.
```json
{pretty_json(acceptance_output_contracts)}
```
## Ownership and safety
- You may create/edit `src/**/*.py` except `src/_io.py` and `src/_backend.py`.
- You may create `tests/**/*.py`, `configs/foundation*.json|yaml`, `requirements.txt`, and `README.foundation.md`.
- Do not create or edit `tasks/`, `outputs/`, reports, task configs, or harness/runtime files.
- Foundation tests verify interfaces, shapes, units, execution capabilities, and reusable scientific mechanics only. Never add tests for paper-claim success, paper-value closeness, plot styling, crop geometry, or pixel similarity; those are downstream observations, not Foundation invariants.
- Do not duplicate paper-explicit channel, normalization, metric, baseline, or shape logic inside separate modules. Implement one shared definition and expose a clear callable interface.
- Keep unresolved paper details explicit in arguments/defaults and comments. Never hard-code target curves or fabricate paper values.
- Declare only allowlisted Python dependencies in `requirements.txt`.
- Import and use every external `primary_framework` selected by the architecture,
  and declare it in `requirements.txt` or the architecture dependency metadata.
- When no third-party framework is needed, the architecture must use
  `primary_framework: standard_library` or `primary_framework: project_local`;
  an algorithm or component name is not a Python package. Request architecture
  revision if this convention was not followed.
- For `device_policy: external_runtime` (for example MATLAB, Julia, or a custom
  binary), do not add the runtime name as a Python requirement and do not fake a
  Python import. The architecture must declare runtime-availability and invocation-
  interface capabilities, each backed by a delivered unittest. The host must
  resolve the real runtime executable and provide a registered trusted invocation
  adapter. If either is absent, report `environment_extension_required`. A constant
  availability function or identity callable is not evidence. The static validator
  will not launch untrusted external code, but the host will run every delivered
  unittest before freezing the Foundation.
- A NumPy-only, analytic, mock, placeholder, or otherwise non-trainable reference
  is not an implementation of a component that requires training, gradients, or
  checkpoints. Do not replace those requirements with a look-alike interface.
- If an architecture execution contract cannot be implemented in the allowed
  environment, stop and explicitly request an architecture revision. Do not
  downgrade the framework/capability and do not claim `ready_for_tasks`.

## Verification
1. Read the complete scientific architecture; implement each component and exposed binding output. Treat acceptance mappings only as output-routing hints.
2. Implement the required modules, using package `__init__.py` files where needed.
3. Add focused `unittest` tests under `tests/` for dimensions, units/normalization, deterministic seeds, component composition, and applicable cross-task interface invariants. Do not test the paper-result verdict.
4. Run `python -m unittest discover -s tests -v` and fix every failure.
5. Write `foundation_result.json` only after tests pass:
```json
{pretty_json(result_template)}
```
{execution_result_note}

Do not implement any `tasks/<figure>.py`. Parallel task writers will consume this foundation as a frozen, read-only dependency.
"""


def _validate_foundation_delivery(
    *,
    sandbox: Path,
    architecture: dict[str, Any],
    trusted_changed: list[str],
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """Validate executable Foundation content and retain metadata debt as warnings."""

    issues: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    if trusted_changed:
        issues.extend({"file": path, "message": "Foundation Writer modified a host-trusted runtime file"} for path in trusted_changed)
    try:
        # Perform the no-follow layout gate before reading foundation_result.json
        # or invoking any validator that reads generated source/config files.
        _foundation_project_files(sandbox)
    except (OSError, RuntimeError, ValueError) as exc:
        issues.append({"file": ".", "message": str(exc)})
        return issues, {
            "passed": False,
            "skipped": True,
            "reason": "unsafe Foundation filesystem layout",
            "warnings": warnings,
        }

    result_path = sandbox / "foundation_result.json"
    result: dict[str, Any] = {}
    try:
        loaded_result = json.loads(result_path.read_text(encoding="utf-8-sig"))
        if not isinstance(loaded_result, dict):
            raise ValueError("hand-off JSON must be an object")
        result = loaded_result
    except Exception as exc:
        warnings.append(
            {
                "file": "foundation_result.json",
                "message": f"missing or invalid hand-off JSON: {type(exc).__name__}: {exc}",
                "severity": "warning",
            }
        )
    if result.get("status") != FOUNDATION_RESULT_STATUS:
        warnings.append(
            {
                "file": "foundation_result.json",
                "message": f"status is not {FOUNDATION_RESULT_STATUS}; host validation decides usability",
                "severity": "warning",
            }
        )

    execution_issues, execution_warnings = _validate_foundation_execution_contracts(
        sandbox=sandbox,
        architecture=architecture,
        result=result,
    )
    # Execution-contract findings are static proof gaps, not observed runtime
    # failures. Preserve them in the audit, but let the concrete syntax/import
    # checks and host unittests below decide whether the Foundation is usable.
    for item in execution_issues:
        message = str(item.get("message") or "")
        warning = {str(k): str(v) for k, v in item.items()}
        warning["severity"] = "warning"
        warning["category"] = (
            "execution_capability_advisory"
            if (
                "environment_extension_required" in message
                or "host capability gap" in message
                or "trusted host" in message
                or "external runtime" in message
            )
            else "static_contract_advisory"
        )
        warnings.append(warning)
    warnings.extend(execution_warnings)
    required_modules = _required_foundation_modules(architecture)
    for relative in sorted(required_modules):
        if not (sandbox / Path(relative)).is_file():
            issues.append({"file": relative, "message": "required architecture module is missing"})
    tests = sorted((sandbox / "tests").rglob("test*.py")) if (sandbox / "tests").is_dir() else []
    if not tests:
        warnings.append(
            {
                "file": "tests",
                "message": "no Foundation contract test was delivered; host import checks still apply",
                "severity": "warning",
            }
        )

    for path in sandbox.rglob("*.py"):
        if PAPER_EVIDENCE_DIR in path.parts or "__pycache__" in path.parts:
            continue
        relative = path.relative_to(sandbox).as_posix()
        if not (relative.startswith("src/") or relative.startswith("tests/")):
            warnings.append(
                {
                    "file": relative,
                    "message": "Python file is outside Foundation ownership and will be excluded from the snapshot",
                    "severity": "warning",
                }
            )
            continue
        try:
            compile(path.read_text(encoding="utf-8-sig"), str(path), "exec")
        except (OSError, SyntaxError, UnicodeError) as exc:
            issues.append({"file": relative, "message": f"syntax error: {exc}"})

    issues.extend(_missing_local_imports(sandbox))
    requirement_issues = validate_requirements(sandbox)
    if _architecture_requires_execution_contracts(architecture):
        blocking_requirements, requirement_warnings = split_requirement_issues(requirement_issues)
        issues.extend({str(k): str(v) for k, v in item.items()} for item in blocking_requirements)
        warnings.extend({str(k): str(v) for k, v in item.items()} for item in requirement_warnings)
    else:
        for raw in requirement_issues:
            item = {str(k): str(v) for k, v in raw.items()}
            item["severity"] = "warning"
            warnings.append(item)
    blocking_security, security_warnings = split_static_security_issues(
        static_scan_repro_project(sandbox),
        advisory_categories=FOUNDATION_STATIC_SECURITY_ADVISORY_CATEGORIES,
    )
    issues.extend(blocking_security)
    warnings.extend(security_warnings)
    if issues:
        return issues, {
            "passed": False,
            "skipped": True,
            "reason": "pre-test validation failed",
            "warnings": warnings,
        }
    test_result = dict(_run_foundation_tests(sandbox))
    test_result["warnings"] = warnings
    if not test_result.get("passed"):
        issues.append({"file": "tests", "message": "Foundation contract tests failed or timed out"})
    return issues, test_result

def _run_foundation_tests(sandbox: Path) -> dict[str, Any]:
    return run_python_unittest_subprocess(work_dir=sandbox, start_dir="tests", timeout=120.0)


def _copy_foundation_snapshot(*, sandbox: Path, snapshot_dir: Path) -> list[dict[str, Any]]:
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    files: list[dict[str, Any]] = []
    for path in _foundation_project_files(sandbox):
        relative = validate_foundation_relpath(path.relative_to(sandbox).as_posix())
        target = resolve_foundation_path(snapshot_dir, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target = resolve_foundation_path(snapshot_dir, relative)
        shutil.copy2(path, target)
        files.append({"path": relative, "sha256": file_sha256(target), "bytes": target.stat().st_size})
    return files


def _foundation_project_files(sandbox: Path) -> list[Path]:
    _assert_foundation_sandbox_layout_safe(sandbox)
    candidates: set[Path] = set()
    for root_name in ("src", "tests", "configs"):
        root = sandbox / root_name
        if path_is_foundation_link(root):
            raise RuntimeError(f"Foundation-owned directory is a link or reparse point: {root_name}")
        if not root.is_dir():
            continue
        files, _, links, special = scan_foundation_tree(root)
        if links:
            relative = links[0].relative_to(sandbox).as_posix()
            raise RuntimeError(f"Foundation output contains a link or reparse point: {relative}")
        if special:
            relative = special[0].relative_to(sandbox).as_posix()
            raise RuntimeError(f"Foundation output contains a non-regular entry: {relative}")
        for path in files:
            if "__pycache__" in path.parts:
                continue
            relative = path.relative_to(sandbox).as_posix()
            if relative in _TRUSTED_PROJECT_FILES:
                continue
            validate_foundation_relpath(relative)
            candidates.add(path)
    for name in ("requirements.txt", "README.foundation.md"):
        path = sandbox / name
        if path_is_foundation_link(path):
            raise RuntimeError(f"Foundation output contains a link or reparse point: {name}")
        if path.is_file():
            validate_foundation_relpath(name)
            candidates.add(path)
    return sorted(
        candidates,
        key=lambda path: path.relative_to(sandbox).as_posix(),
    )


def _assert_foundation_sandbox_layout_safe(sandbox: Path) -> None:
    """Reject links, special files, and agent-owned hardlinks without traversal."""

    if path_is_foundation_link(sandbox):
        raise RuntimeError("Foundation sandbox root is a link or reparse point")
    if not sandbox.is_dir():
        raise RuntimeError("Foundation sandbox root is not a directory")
    files, _, links, special = scan_foundation_tree(sandbox)
    if links:
        relative = links[0].relative_to(sandbox).as_posix()
        raise RuntimeError(f"Foundation output contains a link or reparse point: {relative}")
    if special:
        relative = special[0].relative_to(sandbox).as_posix()
        raise RuntimeError(f"Foundation output contains a non-regular entry: {relative}")
    for path in files:
        relative_path = path.relative_to(sandbox)
        if relative_path.parts and relative_path.parts[0] == PAPER_EVIDENCE_DIR:
            continue
        if path.lstat().st_nlink > 1:
            relative = relative_path.as_posix()
            raise RuntimeError(f"Foundation output contains a hard-linked regular file: {relative}")


def _restore_trusted_runtime_atomically(sandbox: Path) -> None:
    """Restore host-owned runtime files without ever opening their targets."""

    if path_is_foundation_link(sandbox):
        raise RuntimeError("Foundation sandbox root is a link or reparse point")
    src_dir = sandbox / "src"
    if path_is_foundation_link(src_dir):
        raise RuntimeError("Foundation-owned directory is a link or reparse point: src")
    if not src_dir.is_dir():
        raise RuntimeError("Foundation-owned path is not a directory: src")

    for name, content in (
        ("_io.py", IO_RUNTIME_PY),
        ("_backend.py", BACKEND_RUNTIME_PY),
    ):
        target = src_dir / name
        if path_is_foundation_link(target):
            raise RuntimeError(f"Foundation output contains a link or reparse point: src/{name}")
        if target.exists() and not target.is_file():
            raise RuntimeError(f"Foundation output contains a non-regular entry: src/{name}")
        if target.exists() and target.lstat().st_nlink > 1:
            raise RuntimeError(f"Foundation output contains a hard-linked regular file: src/{name}")
        if path_is_foundation_link(src_dir) or not src_dir.is_dir():
            raise RuntimeError("Foundation-owned directory changed into an unsafe path: src")

        descriptor, temp_name = tempfile.mkstemp(prefix=f".{name}.trusted-", dir=src_dir)
        temp_path = Path(temp_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            if path_is_foundation_link(src_dir):
                raise RuntimeError("Foundation-owned directory changed into a link or reparse point: src")
            if path_is_foundation_link(target):
                raise RuntimeError(f"Foundation output changed into a link or reparse point: src/{name}")
            os.replace(temp_path, target)
        except Exception:
            try:
                os.close(descriptor)
            except OSError:
                pass
            raise
        finally:
            temp_path.unlink(missing_ok=True)


def _is_frozen_path(relative: str) -> bool:
    return is_foundation_frozen_path(relative)


def _write_foundation_manifest(path: Path, manifest: dict[str, Any]) -> None:
    """Replace the manifest atomically so an existing link cannot write through."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if path_is_foundation_link(path.parent):
        raise RuntimeError("Foundation manifest parent directory is a link or reparse point")
    descriptor, temp_name = tempfile.mkstemp(prefix=".foundation-manifest-", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(pretty_json(manifest))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            try:
                os.close(descriptor)
            except OSError:
                pass
            raise
        if path_is_foundation_link(path.parent):
            raise RuntimeError("Foundation manifest parent changed into a link or reparse point")
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def _load_cached_foundation(
    *,
    manifest_path: Path,
    snapshot_dir: Path,
    expected_input_hash: str,
    expected_required_modules: set[str] | None = None,
) -> dict[str, Any] | None:
    try:
        if path_is_foundation_link(manifest_path) or path_is_foundation_link(manifest_path.parent):
            return None
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except Exception:
        return None
    if validate_foundation_snapshot(
        manifest,
        snapshot_dir,
        expected_input_hash=expected_input_hash,
        expected_required_modules=expected_required_modules,
    ):
        return None
    return {
        "manifest": manifest,
        "manifest_path": str(manifest_path),
        "snapshot_dir": str(snapshot_dir),
        "snapshot_hash": str(manifest.get("snapshot_hash") or ""),
    }


def _foundation_input_hash(analysis_hash: str, architecture: dict[str, Any]) -> str:
    payload = {
        "analysis_snapshot_hash": analysis_hash,
        "contract_version": FOUNDATION_CONTRACT_VERSION,
        "architecture": architecture,
        "role": "foundation_writer",
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _required_foundation_modules(architecture: dict[str, Any]) -> set[str]:
    architecture_modules = foundation_module_paths(architecture)
    if _architecture_requires_execution_contracts(architecture):
        return architecture_modules
    return set(FOUNDATION_CORE_MODULES) | architecture_modules


def _snapshot_hash(files: list[dict[str, Any]]) -> str:
    return foundation_snapshot_hash(files)


def _trusted_hashes(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for relative in ("src/_io.py", "src/_backend.py"):
        path = root / Path(relative)
        if path.is_file():
            result[relative] = _sha256(path)
    return result


def _sha256(path: Path) -> str:
    return file_sha256(path)
