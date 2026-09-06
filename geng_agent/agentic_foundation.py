from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Any

from .codex_runner import run_codex_subprocess, run_python_unittest_subprocess
from .config import get_config_value
from .execution_plan import compile_execution_plan
from .foundation_scope import scoped_foundation_architecture
from .foundation_revision import validate_foundation_revision_request
from .writer_lineage import foundation_cache_projection, foundation_consumed_runtime
from .foundation_snapshot import (
    FOUNDATION_CONTRACT_VERSION, FOUNDATION_SCHEMA_VERSION, FOUNDATION_WORKFLOW_VERSION,
    file_sha256, foundation_snapshot_hash, is_foundation_frozen_path,
    path_is_foundation_link, resolve_foundation_path, scan_foundation_tree,
    validate_foundation_manifest, validate_foundation_relpath, validate_foundation_snapshot,
)
from .io_runtime import BACKEND_RUNTIME_PY, IO_RUNTIME_PY, inject_io_runtime
from .case_runtime import (
    CaseRuntime, EnvironmentRequestRequired, environment_request_prompt,
    read_environment_request, requirements_from_scientific_architecture,
    requirements_missing_from_lock,
)
from .case_environment import EnvironmentPolicyError, normalize_requirement
from .json_utils import pretty_json
from .outputs import _missing_local_imports, write_json, write_text
from .scientific_architecture import foundation_module_paths
from .security import (
    FOUNDATION_STATIC_SECURITY_ADVISORY_CATEGORIES, split_requirement_issues,
    split_static_security_issues, static_scan_repro_project, validate_requirements,
)
from .task_writer_support import (
    PAPER_EVIDENCE_DIR, _analysis_snapshot_hash, _collect_writer_analysis_artifacts,
    _missing_required_analysis_artifacts, _write_paper_evidence_bundle,
)
from .foundation_architecture import (
    architecture_components as _architecture_components,
    architecture_dependency_names as _architecture_dependency_names,
    architecture_requires_execution_contracts as _architecture_requires_execution_contracts,
    declared_requirement_keys as _declared_requirement_keys,
    dependency_strings as _dependency_strings,
    initial_foundation_requirements as _initial_foundation_requirements,
    library_keys as _library_keys, normalized_library_name as _normalized_library_name,
    requirement_name_for_library as _requirement_name_for_library,
)
from .foundation_bindings import (
    _Binding, _advance_binding, _ast_dotted_name, _binding_is_trivial_external_stub,
    _class_member_binding, _component_framework_import_keys, _declared_callable_binding,
    _expression_binding, _foundation_project_import_keys, _foundation_source_trees,
    _framework_is_external, _imported_module_path, _module_candidate,
    _top_level_binding, _validate_declared_callable,
)
from .foundation_capability_evidence import (
    _analyze_flow_statements, _assert_expression_has_component_change,
    _assertion_call_has_component_change, _assertion_operand_tags, _assign_flow_target,
    _capability_test_passed, _component_change_pair, _component_factory_tags,
    _component_test_flow, _contract_values_equal,
    _execution_requires_trusted_capability_probe, _factory_expression_tags,
    _flow_expression_tags, _framework_has_trusted_capability_probe,
    _method_evidences_capability, _method_references_component,
    _negated_equality_has_component_change, _new_component_flow,
    _required_component_capabilities,
)
from .foundation_execution_contracts import (
    _external_runtime_available, _external_runtime_command,
    _trusted_external_runtime_adapter,
    validate_foundation_execution_contracts as _validate_foundation_execution_contracts,
)
from .foundation_execution_policy import (
    CAPABILITY_GROUPS as _CAPABILITY_GROUPS,
    EXECUTION_CONTRACT_FIELDS as _EXECUTION_CONTRACT_FIELDS,
    FRAMEWORK_EXEMPTIONS as _FRAMEWORK_EXEMPTIONS,
    FRAMEWORK_SEMANTIC_LABELS as _FRAMEWORK_SEMANTIC_LABELS,
    LIBRARY_CANONICAL_NAMES as _LIBRARY_CANONICAL_NAMES,
    MATERIAL_EXECUTION_FIELDS as _MATERIAL_EXECUTION_FIELDS,
    TRUSTED_CAPABILITY_PROBE_FRAMEWORKS as _TRUSTED_CAPABILITY_PROBE_FRAMEWORKS,
    TRUSTED_EXTERNAL_RUNTIME_ADAPTERS as _TRUSTED_EXTERNAL_RUNTIME_ADAPTERS,
    TRUSTED_PROJECT_FILES as _TRUSTED_PROJECT_FILES,
)
from .foundation_test_catalog import (
    _DeliveredTest, _capability_matches_group, _capability_status_passed,
    _component_test_target, _decorator_skips_test, _delivered_test_references,
    _flow_reference, _normalized_capability, _normalized_test_reference,
    _resolve_test_name, _static_bool, _test_has_substantive_body,
    _test_import_targets, _test_is_skipped,
)
from .foundation_prompt_cache import (
    FOUNDATION_CORE_MODULES, FOUNDATION_LABEL, FOUNDATION_RESULT_STATUS,
    _foundation_brief, _foundation_input_hash, _load_cached_foundation,
    _load_cached_foundation_failure, _required_foundation_modules,
)
from .foundation_snapshot_delivery import (
    _assert_foundation_sandbox_layout_safe,
    _foundation_project_files, _is_frozen_path, _is_restricted_project_path,
    _publish_foundation_snapshot, _restore_trusted_runtime_atomically,
    _scan_restricted_project, _sha256, _snapshot_hash, _trusted_hashes,
    _write_foundation_manifest, load_foundation_writer_delivery,
    persist_foundation_writer_delivery, restore_foundation_writer_delivery,
    foundation_violations, install_foundation_snapshot, restore_foundation_snapshot,
    validate_foundation_bundle,
)


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
    resume: bool = True,
    case_runtime: CaseRuntime | None = None,
    execution_plan: dict[str, Any] | None = None,
    revision_request: dict[str, Any] | None = None,
    revision_evidence_root: Path | None = None,
    previous_foundation: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Build and freeze the shared scientific layer before parallel task writers.

    The Foundation Writer owns shared ``src`` modules and their contract tests,
    but never task modules or outputs.  Its content-addressed snapshot is the
    sole shared layer installed into every writer sandbox and the final project.
    """

    original_architecture = scientific_architecture
    if execution_plan is None and isinstance(tasks.get("repro_tasks"), list):
        execution_plan = compile_execution_plan(tasks)
    if execution_plan is not None:
        scientific_architecture = scoped_foundation_architecture(
            scientific_architecture, execution_plan
        )
        write_json(
            audit_dir / "03b_foundation_scope.json",
            scientific_architecture["_foundation_scope"],
        )
        if not scientific_architecture["components"]:
            return None
    if revision_request is not None:
        if previous_foundation is None or revision_evidence_root is None:
            raise ValueError("a Foundation revision requires its prior snapshot and paper evidence root")
        previous_issues = validate_foundation_bundle(previous_foundation)
        if previous_issues:
            raise ValueError("cannot revise an invalid Foundation snapshot")
        revision_request = validate_foundation_revision_request(
            revision_request,
            architecture=original_architecture,
            execution_plan=execution_plan,
            evidence_root=revision_evidence_root,
        )
        scientific_architecture = dict(scientific_architecture)
        previous_revision = previous_foundation.get("manifest", {}).get("revision") or {}
        scientific_architecture["_foundation_revision"] = {
            **revision_request,
            "previous_snapshot_hash": previous_foundation["snapshot_hash"],
            "applied_request_ids": sorted({
                *previous_revision.get("applied_request_ids", []),
                *([previous_revision["request_id"]] if previous_revision.get("request_id") else []),
                revision_request["request_id"],
            }),
        }
        resume = False

    analysis_artifacts = _collect_writer_analysis_artifacts(output_dir=output_dir)
    missing = _missing_required_analysis_artifacts(analysis_artifacts)
    if missing:
        raise RuntimeError("foundation writer requires finalized analysis artifacts: " + ", ".join(missing))
    if "scientific_architecture.json" not in analysis_artifacts:
        raise RuntimeError("foundation writer requires finalized analysis artifact: scientific_architecture.json")
    analysis_hash = _analysis_snapshot_hash(paper_path=paper_path, artifacts=analysis_artifacts)
    environment_hash = case_runtime.environment_hash if case_runtime is not None else "host-runtime"
    cache_analysis_hash, cache_architecture, cache_environment_hash = analysis_hash, scientific_architecture, environment_hash
    if isinstance(scientific_architecture.get("_foundation_scope"), dict):
        cache_analysis_hash, cache_architecture, cache_environment_hash = foundation_cache_projection(
            architecture=scientific_architecture, facts=facts, paper_path=paper_path,
            case_runtime=case_runtime,
        )
    input_hash = _foundation_input_hash(
        cache_analysis_hash,
        cache_architecture,
        environment_hash=cache_environment_hash,
    )
    base_architecture = dict(cache_architecture)
    base_architecture.pop("_foundation_revision", None)
    base_input_hash = _foundation_input_hash(
        cache_analysis_hash, base_architecture,
        environment_hash=("runtime_validated_separately" if isinstance(scientific_architecture.get("_foundation_scope"), dict) else cache_environment_hash),
    )
    required_modules = _required_foundation_modules(scientific_architecture)
    manifest_path = output_dir / "foundation_manifest.json"
    sandbox = audit_dir / "03b_foundation_writer_sandbox"
    snapshot_dir = audit_dir / "03b_foundation_snapshot"
    if revision_request is not None:
        # Preserve the previously delivered generation until its replacement
        # has passed validation; active consumers never see in-place edits.
        revision_dir = audit_dir / "03b_foundation_revisions" / input_hash
        sandbox = revision_dir / "sandbox"
        snapshot_dir = revision_dir / "snapshot"
    writer_delivery_dir = (
        audit_dir / "03b_foundation_writer_deliveries" / input_hash
    )

    if resume:
        revised = _load_current_foundation_revision(
            audit_dir=audit_dir,
            manifest_path=manifest_path,
            expected_base_input_hash=base_input_hash,
            expected_required_modules=required_modules,
        )
        if revised is not None:
            if not _foundation_runtime_matches(revised, scientific_architecture, case_runtime):
                _revalidate_cached_foundation_runtime(revised, scientific_architecture, case_runtime)
            return revised
        cached = _load_cached_foundation(
            manifest_path=manifest_path,
            snapshot_dir=snapshot_dir,
            expected_input_hash=input_hash,
            expected_required_modules=required_modules,
        )
        if cached is not None and _foundation_runtime_matches(cached, scientific_architecture, case_runtime):
            write_json(audit_dir / "03b_foundation_writer_resume.json", {"ok": True, "source": "content_addressed_snapshot"})
            return cached
        writer_delivery = load_foundation_writer_delivery(
            delivery_dir=writer_delivery_dir,
            expected_input_hash=input_hash,
            expected_required_modules=required_modules,
        )
        validation_record = _load_foundation_validation_record(
            validation_path=audit_dir / "03b_foundation_validation.json",
            expected_input_hash=input_hash,
        )
        reuse_writer_delivery = (
            writer_delivery is not None
            and _validation_allows_writer_delivery_reuse(validation_record)
        )
        host_validation_policy_hash = _host_validation_policy_hash()
        if (
            reuse_writer_delivery
            and isinstance(validation_record, dict)
            and validation_record.get("ok") is False
            and _host_revalidation_already_attempted(
                resume_path=audit_dir / "03b_foundation_writer_resume.json",
                expected_input_hash=input_hash,
                expected_policy_hash=host_validation_policy_hash,
            )
        ):
            raise RuntimeError(
                "Foundation host validation still fails after one pristine delivery "
                "revalidation; the completed Writer is preserved and will not be rerun"
            )
        if reuse_writer_delivery and writer_delivery is not None:
            try:
                restore_foundation_writer_delivery(
                    delivery_dir=writer_delivery_dir,
                    receipt=writer_delivery,
                    sandbox=sandbox,
                )
            except (OSError, RuntimeError, ValueError) as exc:
                raise RuntimeError(
                    f"cached Foundation Writer delivery could not be restored safely: {exc}"
                ) from exc
            previous_issues = (
                validation_record.get("issues")
                if isinstance(validation_record, dict)
                and isinstance(validation_record.get("issues"), list)
                else []
            )
            source = (
                "cached_writer_delivery_freeze_retry"
                if isinstance(validation_record, dict) and validation_record.get("ok") is True
                else "cached_writer_delivery_host_revalidation"
            )
            write_json(
                audit_dir / "03b_foundation_writer_resume.json",
                {
                    "ok": None,
                    "source": source,
                    "input_hash": input_hash,
                    "previous_issues": previous_issues,
                    "writer_rerun": False,
                    "host_validation_policy_hash": host_validation_policy_hash,
                },
            )
            return _finalize_foundation_delivery(
                sandbox=sandbox,
                snapshot_dir=snapshot_dir,
                manifest_path=manifest_path,
                audit_dir=audit_dir,
                scientific_architecture=scientific_architecture,
                required_modules=required_modules,
                analysis_hash=analysis_hash,
                environment_hash=environment_hash,
                input_hash=input_hash,
                trusted_changed=list(writer_delivery.get("trusted_changed") or []),
                case_runtime=case_runtime,
            )
        if writer_delivery is not None:
            write_json(
                audit_dir / "03b_foundation_writer_resume.json",
                {
                    "ok": None,
                    "source": "cached_writer_delivery_requires_writer_repair",
                    "input_hash": input_hash,
                    "previous_issues": (
                        validation_record.get("issues")
                        if isinstance(validation_record, dict)
                        and isinstance(validation_record.get("issues"), list)
                        else []
                    ),
                    "writer_rerun": True,
                },
            )

    for path in (sandbox, snapshot_dir):
        if path.exists():
            shutil.rmtree(path)
    sandbox.mkdir(parents=True, exist_ok=True)
    if previous_foundation is not None and revision_request is not None:
        install_foundation_snapshot(sandbox, previous_foundation)
    write_text(
        sandbox / "requirements.txt",
        _initial_foundation_requirements(scientific_architecture, case_runtime=case_runtime),
    )
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
    prompt = _foundation_brief(scientific_architecture, case_runtime=case_runtime)
    if revision_request is not None:
        prompt += (
            "\n## Serialized scientific revision\n"
            "Continue the installed prior implementation. Change only the source modules "
            "listed in module_paths and their tests. Preserve every other source file byte "
            "for byte. Implement the paper-grounded correction, add a regression test for "
            "the observed failure, and rerun the shared contract tests. Do not train task "
            "checkpoints or run logical experiments. The host publishes a new immutable "
            "generation only after validation.\n"
            + pretty_json(revision_request)
        )
    write_text(audit_dir / f"{FOUNDATION_LABEL}_brief.md", prompt)
    selected_python = (
        case_runtime.python_executable if case_runtime is not None else Path(sys.executable).absolute()
    )
    python_dir = selected_python.parent
    runtime_env = {
        "GENG_PYTHON": str(selected_python),
        "GENG_PYTHON_EXECUTABLE": str(selected_python),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    if case_runtime is not None:
        runtime_env["VIRTUAL_ENV"] = str(case_runtime.venv_dir)
    status = run_codex_subprocess(
        role="foundation_writer",
        work_dir=sandbox,
        prompt=prompt,
        audit_dir=audit_dir,
        label=FOUNDATION_LABEL,
        sandbox="workspace-write",
        command_override=get_config_value("GENG_CODEX_FOUNDATION_WRITER_CMD"),
        image_paths=sorted(
            path.resolve()
            for path in (sandbox / PAPER_EVIDENCE_DIR / "full_paper_pages").glob("paper_page_*.png")
            if path.is_file()
        ),
        extra_env=runtime_env,
        path_prepend=[python_dir],
    )

    try:
        # This must be the first inspection after the agent returns. Nothing
        # below may read or replace an agent-controlled path until the complete
        # sandbox has been walked without following links or reparse points.
        _assert_foundation_sandbox_layout_safe(sandbox)
        pending_requests = list(
            read_environment_request(sandbox=sandbox, source="foundation_writer")
        )
        if case_runtime is not None:
            pending_requests.extend(
                requirements_missing_from_lock(
                    sandbox / "requirements.txt",
                    case_runtime.lock,
                    source="foundation_writer:requirements.txt",
                )
            )
        if pending_requests:
            raise EnvironmentRequestRequired(
                pending_requests,
                source="foundation_writer",
            )
        trusted_after = _trusted_hashes(sandbox)
        trusted_changed = sorted(path for path, digest in trusted_before.items() if trusted_after.get(path) != digest)
        _restore_trusted_runtime_atomically(sandbox)
    except EnvironmentRequestRequired:
        raise
    except (OSError, RuntimeError, EnvironmentPolicyError) as exc:
        raise RuntimeError(f"foundation writer produced an unsafe filesystem layout: {exc}") from exc
    if not status.get("ok"):
        raise RuntimeError(f"foundation writer failed: {status.get('error') or status.get('blocked_reason') or 'unknown error'}")
    if revision_request is not None and previous_foundation is not None:
        _validate_scoped_foundation_revision(
            sandbox=sandbox,
            previous_foundation=previous_foundation,
            revision_request=revision_request,
        )
    writer_delivery = persist_foundation_writer_delivery(
        sandbox=sandbox,
        delivery_dir=writer_delivery_dir,
        input_hash=input_hash,
        analysis_hash=analysis_hash,
        environment_hash=environment_hash,
        required_modules=required_modules,
        trusted_changed=trusted_changed,
    )
    restore_foundation_writer_delivery(
        delivery_dir=writer_delivery_dir,
        receipt=writer_delivery,
        sandbox=sandbox,
    )
    finalized = _finalize_foundation_delivery(
        sandbox=sandbox,
        snapshot_dir=snapshot_dir,
        manifest_path=manifest_path,
        audit_dir=audit_dir,
        scientific_architecture=scientific_architecture,
        required_modules=required_modules,
        analysis_hash=analysis_hash,
        environment_hash=environment_hash,
        input_hash=input_hash,
        trusted_changed=trusted_changed,
        case_runtime=case_runtime,
    )
    if revision_request is not None:
        write_json(
            audit_dir / "03b_foundation_current_revision.json",
            {
                "base_input_hash": base_input_hash,
                "revision_input_hash": input_hash,
                "snapshot_path": snapshot_dir.relative_to(audit_dir).as_posix(),
                "request_id": revision_request["request_id"],
            },
        )
    return finalized


def _foundation_runtime_matches(
    foundation: dict[str, Any], architecture: dict[str, Any], case_runtime: CaseRuntime | None,
) -> bool:
    """Revalidate only versions actually consumed by cached shared source."""
    if not isinstance(architecture.get("_foundation_scope"), dict):
        return True
    return foundation.get("manifest", {}).get("consumed_runtime") == foundation_consumed_runtime(
        architecture=architecture, case_runtime=case_runtime,
        source_root=Path(foundation["snapshot_dir"]),
    )


def _revalidate_cached_foundation_runtime(
    foundation: dict[str, Any], architecture: dict[str, Any], case_runtime: CaseRuntime | None,
) -> None:
    # A runtime update must not resurrect the pre-repair scientific source.
    issues, tests = _validate_foundation_delivery(
        sandbox=Path(foundation["snapshot_dir"]), architecture=architecture,
        trusted_changed=[], case_runtime=case_runtime,
    )
    if issues:
        raise RuntimeError(f"revised Foundation failed validation in the changed runtime: {issues[:5]}")
    manifest = foundation["manifest"]
    manifest["consumed_runtime"] = foundation_consumed_runtime(
        architecture=architecture, case_runtime=case_runtime,
        source_root=Path(foundation["snapshot_dir"]),
    )
    manifest["validation"] = {"tests_passed": bool(tests.get("passed")), "local_imports_resolve": True}
    manifest["environment_lock_hash"] = case_runtime.environment_hash if case_runtime is not None else "host-runtime"
    _write_foundation_manifest(Path(foundation["manifest_path"]), manifest)


def _load_current_foundation_revision(
    *,
    audit_dir: Path,
    manifest_path: Path,
    expected_base_input_hash: str,
    expected_required_modules: set[str],
) -> dict[str, Any] | None:
    """Resume the latest validated repair instead of resurrecting its old source."""

    pointer = audit_dir / "03b_foundation_current_revision.json"
    try:
        if path_is_foundation_link(pointer) or not pointer.is_file():
            return None
        record = json.loads(pointer.read_text(encoding="utf-8-sig"))
        if not isinstance(record, dict) or record.get("base_input_hash") != expected_base_input_hash:
            return None
        parts = str(record.get("snapshot_path") or "").split("/")
        if not parts or parts[0] != "03b_foundation_revisions" or any(
            part in {"", ".", ".."} or ":" in part or "\\" in part for part in parts
        ):
            return None
        snapshot = audit_dir
        for part in parts:
            snapshot /= part
            if path_is_foundation_link(snapshot):
                return None
        snapshot.resolve(strict=True).relative_to(audit_dir.resolve(strict=True))
        return _load_cached_foundation(
            manifest_path=manifest_path,
            snapshot_dir=snapshot,
            expected_input_hash=str(record.get("revision_input_hash") or ""),
            expected_required_modules=expected_required_modules,
        )
    except (OSError, ValueError, UnicodeError):
        return None


def _validate_scoped_foundation_revision(
    *,
    sandbox: Path,
    previous_foundation: dict[str, Any],
    revision_request: dict[str, Any],
) -> None:
    """Keep a requested repair from changing unrelated shared science."""

    allowed = set(revision_request["module_paths"])
    previous_files = {
        str(item["path"]): item
        for item in previous_foundation["manifest"]["files"]
    }
    changed: list[str] = []
    for relative, item in previous_files.items():
        if not relative.startswith(("src/", "configs/foundation")) or relative in allowed:
            continue
        candidate = resolve_foundation_path(sandbox, relative)
        if not candidate.is_file() or file_sha256(candidate) != item["sha256"]:
            raise RuntimeError(f"Foundation revision changed unrelated source: {relative}")
    for candidate in (sandbox / "src").rglob("*.py"):
        relative = candidate.relative_to(sandbox).as_posix()
        if relative in {"src/_io.py", "src/_backend.py"}:
            continue
        if relative not in previous_files and relative not in allowed:
            raise RuntimeError(f"Foundation revision introduced an undeclared source module: {relative}")
    for relative in sorted(allowed):
        candidate = resolve_foundation_path(sandbox, relative)
        previous = previous_files.get(relative)
        if not candidate.is_file():
            raise RuntimeError(f"Foundation revision removed required source: {relative}")
        if previous is None or file_sha256(candidate) != previous["sha256"]:
            changed.append(relative)
    if not changed:
        raise RuntimeError("Foundation revision made no source change; preserve the existing scientific result")


def _load_foundation_validation_record(
    *,
    validation_path: Path,
    expected_input_hash: str,
) -> dict[str, Any] | None:
    """Load a host-owned validation record for the exact Writer input."""

    try:
        if (
            path_is_foundation_link(validation_path)
            or path_is_foundation_link(validation_path.parent)
            or not validation_path.is_file()
        ):
            return None
        record = json.loads(validation_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if (
        not isinstance(record, dict)
        or str(record.get("input_hash") or "") != expected_input_hash
        or record.get("ok") not in (True, False)
    ):
        return None
    return record


def _validation_allows_writer_delivery_reuse(
    record: dict[str, Any] | None,
) -> bool:
    """Reuse only for an unfinished finalize or evidence of host-side failure."""

    if record is None or record.get("ok") is True:
        return True
    issues = record.get("issues")
    tests = record.get("tests")
    if not isinstance(issues, list) or not isinstance(tests, dict):
        return False
    if not issues or any(
        not isinstance(item, dict) or str(item.get("file") or "") != "tests"
        for item in issues
    ):
        return False
    if tests.get("skipped") is True:
        return False
    diagnostics = "\n".join(
        str(tests.get(key) or "") for key in ("stdout", "stderr", "error")
    ).casefold()
    if tests.get("timed_out") is True:
        # A timeout is an indeterminate host-validation result, not evidence
        # that regenerating the completed scientific implementation will help.
        # Reuse only the pristine immutable delivery; the existing policy-hash
        # marker permits one host revalidation and then stops without freezing
        # or accepting a delivery that still times out.
        explicit_test_failure = any(
            marker in diagnostics
            for marker in (
                "assertionerror",
                "failed (failures=",
                "failed (errors=",
            )
        ) or any(
            line.lstrip().startswith(("fail:", "error:"))
            for line in diagnostics.splitlines()
        )
        return (
            tests.get("passed") is False
            and tests.get("delivery_immutable") is True
            and tests.get("returncode") is None
            and not tests.get("spawn_error")
            and not explicit_test_failure
        )
    if (
        tests.get("runtime_cleanup_error")
        or tests.get("spawn_error")
        or (
            isinstance(tests.get("returncode"), int)
            and tests["returncode"] < 0
        )
    ):
        return True
    if "foundation runtime guard:" in diagnostics and "site-packages" in diagnostics:
        return True
    host_stack_signatures = (
        ("site-packages", "nameerror"),
        ("getpass.py", "no module named 'pwd'"),
        ("site-packages", "torch", "already has an"),
        ("site-packages", "dll load failed"),
    )
    return any(all(marker in diagnostics for marker in signature) for signature in host_stack_signatures)


def _host_revalidation_already_attempted(
    *,
    resume_path: Path,
    expected_input_hash: str,
    expected_policy_hash: str,
) -> bool:
    try:
        if (
            path_is_foundation_link(resume_path)
            or path_is_foundation_link(resume_path.parent)
            or not resume_path.is_file()
        ):
            return False
        record = json.loads(resume_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return bool(
        isinstance(record, dict)
        and record.get("source") == "cached_writer_delivery_host_revalidation"
        and record.get("input_hash") == expected_input_hash
        and record.get("host_validation_policy_hash") == expected_policy_hash
        and record.get("writer_rerun") is False
    )


def _host_validation_policy_hash() -> str:
    """Fingerprint the host validator and guarded unittest runner implementation."""

    digest = hashlib.sha256()
    module_paths = (
        Path(__file__),
        Path(str(getattr(sys.modules.get(run_python_unittest_subprocess.__module__), "__file__", ""))),
    )
    for path in module_paths:
        try:
            digest.update(path.resolve().read_bytes())
        except (OSError, RuntimeError):
            digest.update(str(path).encode("utf-8", errors="replace"))
    return digest.hexdigest()


def _finalize_foundation_delivery(
    *,
    sandbox: Path,
    snapshot_dir: Path,
    manifest_path: Path,
    audit_dir: Path,
    scientific_architecture: dict[str, Any],
    required_modules: set[str],
    analysis_hash: str,
    environment_hash: str,
    input_hash: str,
    trusted_changed: list[str],
    case_runtime: CaseRuntime | None,
) -> dict[str, Any]:
    """Host-validate and freeze one completed Writer delivery without rerunning it."""

    issues, test_result = _validate_foundation_delivery(
        sandbox=sandbox,
        architecture=scientific_architecture,
        trusted_changed=trusted_changed,
        case_runtime=case_runtime,
    )
    write_json(
        audit_dir / "03b_foundation_validation.json",
        {"ok": not issues, "issues": issues, "tests": test_result, "input_hash": input_hash},
    )
    if issues:
        raise RuntimeError(
            "foundation validation failed: "
            + "; ".join(str(item.get("message")) for item in issues[:8])
        )

    files = _publish_foundation_snapshot(sandbox=sandbox, snapshot_dir=snapshot_dir)
    frozen_files = [
        item for item in files if is_foundation_frozen_path(str(item.get("path") or ""))
    ]
    snapshot_hash = foundation_snapshot_hash(files)
    manifest = {
        "schema_version": FOUNDATION_SCHEMA_VERSION,
        "workflow_version": FOUNDATION_WORKFLOW_VERSION,
        "contract_version": FOUNDATION_CONTRACT_VERSION,
        "input_hash": input_hash,
        "analysis_snapshot_hash": analysis_hash,
        "environment_lock_hash": environment_hash,
        "snapshot_hash": snapshot_hash,
        "files": files,
        "frozen_files": frozen_files,
        "required_modules": sorted(required_modules),
        "scope": scientific_architecture.get("_foundation_scope"),
        "revision": scientific_architecture.get("_foundation_revision"),
        "consumed_runtime": foundation_consumed_runtime(
            architecture=scientific_architecture, case_runtime=case_runtime,
            source_root=snapshot_dir,
        ) if isinstance(scientific_architecture.get("_foundation_scope"), dict) else None,
        "validation": {
            "tests_passed": bool(test_result.get("passed")),
            "local_imports_resolve": True,
        },
    }
    manifest_issues = validate_foundation_manifest(
        manifest,
        expected_input_hash=input_hash,
        expected_required_modules=required_modules,
    )
    if manifest_issues:
        raise RuntimeError(
            f"internal Foundation manifest validation failed: {manifest_issues[:5]}"
        )
    _write_foundation_manifest(manifest_path, manifest)
    return {
        "manifest": manifest,
        "manifest_path": str(manifest_path),
        "snapshot_dir": str(snapshot_dir),
        "snapshot_hash": snapshot_hash,
    }

def _validate_foundation_delivery(
    *,
    sandbox: Path,
    architecture: dict[str, Any],
    trusted_changed: list[str],
    case_runtime: CaseRuntime | None = None,
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
    # failures. Preserve them in the audit, but let the concrete case capability
    # probes and host unittests decide whether the Foundation is usable.
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
    scope = architecture.get("_foundation_scope")
    if isinstance(scope, dict):
        for relative in scope.get("private_module_paths", []):
            if (sandbox / str(relative)).is_file():
                issues.append({
                    "file": str(relative),
                    "message": "task-private scientific module is outside the shared Foundation scope",
                })
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
    requirement_issues = validate_requirements(
        sandbox,
        runtime_policy=case_runtime.manifest if case_runtime is not None else None,
        runtime_lock=case_runtime.lock if case_runtime is not None else None,
    )
    if _architecture_requires_execution_contracts(architecture):
        blocking_requirements, requirement_warnings = split_requirement_issues(
            requirement_issues,
            runtime_policy=case_runtime.manifest if case_runtime is not None else None,
            runtime_lock=case_runtime.lock if case_runtime is not None else None,
        )
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
    delivery_hashes_before = _foundation_delivery_hashes(sandbox)
    test_result = dict(
        _run_foundation_tests(sandbox)
        if case_runtime is None
        else _run_foundation_tests(sandbox, case_runtime=case_runtime)
    )
    test_result["warnings"] = warnings
    changed_delivery_files: list[str] = []
    try:
        delivery_hashes_after = _foundation_delivery_hashes(sandbox)
        changed_delivery_files = sorted(
            relative
            for relative in set(delivery_hashes_before) | set(delivery_hashes_after)
            if delivery_hashes_before.get(relative) != delivery_hashes_after.get(relative)
        )
    except (OSError, RuntimeError, ValueError) as exc:
        changed_delivery_files = [f"layout inspection failed: {type(exc).__name__}: {exc}"]
    test_result["delivery_immutable"] = not changed_delivery_files
    if changed_delivery_files:
        test_result["passed"] = False
        test_result["changed_delivery_files"] = changed_delivery_files
        issues.append(
            {
                "file": "tests",
                "message": (
                    "Foundation contract tests changed files that are eligible for freezing: "
                    + ", ".join(changed_delivery_files[:8])
                ),
            }
        )
    if not test_result.get("passed"):
        issues.append({"file": "tests", "message": "Foundation contract tests failed or timed out"})
    return issues, test_result


def _foundation_delivery_hashes(sandbox: Path) -> dict[str, str]:
    """Hash exactly the Writer-owned files eligible for the frozen snapshot."""

    return {
        path.relative_to(sandbox).as_posix(): file_sha256(path)
        for path in _foundation_project_files(sandbox)
    }


def _run_foundation_tests(
    sandbox: Path,
    *,
    case_runtime: CaseRuntime | None = None,
) -> dict[str, Any]:
    return run_python_unittest_subprocess(
        work_dir=sandbox,
        start_dir="tests",
        timeout=120.0,
        python_executable=(case_runtime.python_executable if case_runtime is not None else None),
        venv_dir=(case_runtime.venv_dir if case_runtime is not None else None),
        trusted_runtime_roots=(
            case_runtime.trusted_read_roots if case_runtime is not None else None
        ),
    )
