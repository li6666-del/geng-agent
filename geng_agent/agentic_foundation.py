from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from dataclasses import asdict
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
from .case_environment import EnvironmentPolicyError, RequirementRequest, normalize_requirement
from .case_runtime_requests import _lock_satisfies_requirement
from .json_utils import pretty_json
from .outputs import write_json, write_text
from .scientific_architecture import foundation_module_paths
from .task_writer_support import (
    PAPER_EVIDENCE_DIR, _analysis_snapshot_hash, _collect_writer_analysis_artifacts,
    _missing_required_analysis_artifacts, _write_paper_evidence_bundle,
)
from .foundation_architecture import initial_foundation_requirements as _initial_foundation_requirements
from .foundation_prompt_cache import (
    FOUNDATION_LABEL, FOUNDATION_RESULT_STATUS,
    _foundation_brief, _foundation_input_hash, _load_cached_foundation,
    _required_foundation_modules,
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
    recovery_instructions: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Build and freeze the shared scientific layer before parallel task writers.

    The Foundation Writer owns shared ``src`` modules and their contract tests,
    but never task modules or outputs.  Its content-addressed snapshot is the
    sole shared layer installed into every writer sandbox and the final project.
    """

    allow_environment_resume = resume
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
    if recovery_instructions is not None:
        resume = True

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
    # This key identifies the completed implementation, not a successful test
    # result. Changing only the environment must revalidate its original bytes.
    environment_resume_key = _foundation_input_hash(
        analysis_hash, {"architecture": scientific_architecture,
                        "recovery_instructions": recovery_instructions},
        environment_hash="foundation_environment_handoff_v1",
    )
    environment_resume_path = (
        audit_dir / "03b_foundation_environment_resume" / f"{environment_resume_key}.json"
    )
    if allow_environment_resume:
        pending_delivery = _load_environment_pending_foundation(
            path=environment_resume_path, audit_dir=audit_dir,
            expected_key=environment_resume_key, required_modules=required_modules,
        )
        if pending_delivery is not None:
            saved_dir, receipt, record = pending_delivery
            restore_foundation_writer_delivery(
                delivery_dir=saved_dir, receipt=receipt, sandbox=sandbox,
            )
            pending = _foundation_pending_requirements(
                sandbox=sandbox, case_runtime=case_runtime,
                explicit_requests=[RequirementRequest(**item) for item in record["requests"]],
            )
            if pending:
                raise EnvironmentRequestRequired(pending, source="foundation_writer")
            write_json(audit_dir / "03b_foundation_writer_resume.json", {
                "ok": None, "source": "environment_pending_writer_delivery",
                "input_hash": input_hash, "writer_input_hash": receipt["input_hash"],
                "writer_snapshot_hash": receipt["snapshot_hash"], "writer_rerun": False,
            })
            finalized = _finalize_foundation_delivery(
                sandbox=sandbox, snapshot_dir=snapshot_dir, manifest_path=manifest_path,
                audit_dir=audit_dir, scientific_architecture=scientific_architecture,
                required_modules=required_modules, analysis_hash=analysis_hash,
                environment_hash=environment_hash, input_hash=input_hash,
                trusted_changed=list(receipt["trusted_changed"]), case_runtime=case_runtime,
            )
            if revision_request is not None:
                write_json(audit_dir / "03b_foundation_current_revision.json", {
                    "base_input_hash": base_input_hash, "revision_input_hash": input_hash,
                    "snapshot_path": snapshot_dir.relative_to(audit_dir).as_posix(),
                    "request_id": revision_request["request_id"],
                })
            write_json(environment_resume_path, {
                **record, "state": "validated", "validated_input_hash": input_hash,
                "validated_environment_hash": environment_hash,
                "validation": finalized["manifest"]["validation"],
            })
            return finalized

    repair_delivery = None
    repair_validation = None
    if resume:
        revised = _load_current_foundation_revision(
            audit_dir=audit_dir,
            manifest_path=manifest_path,
            expected_base_input_hash=base_input_hash,
            expected_required_modules=required_modules,
        )
        if revised is not None and recovery_instructions is None:
            if not _foundation_runtime_matches(revised, scientific_architecture, case_runtime):
                _revalidate_cached_foundation_runtime(revised, scientific_architecture, case_runtime)
            return revised
        cached = _load_cached_foundation(
            manifest_path=manifest_path,
            snapshot_dir=snapshot_dir,
            expected_input_hash=input_hash,
            expected_required_modules=required_modules,
        )
        if cached is not None and recovery_instructions is None and _foundation_runtime_matches(cached, scientific_architecture, case_runtime):
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
        # Reconcile the immutable delivery first. Only the supervisor may ask
        # the Writer to change it; failure-message heuristics do not choose an owner.
        reuse_writer_delivery = writer_delivery is not None and recovery_instructions is None
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
            repair_delivery = writer_delivery
            repair_validation = validation_record
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

    if repair_delivery is not None:
        restore_foundation_writer_delivery(
            delivery_dir=writer_delivery_dir, receipt=repair_delivery, sandbox=sandbox,
        )
    else:
        for path in (sandbox, snapshot_dir):
            if path.exists():
                shutil.rmtree(path)
        sandbox.mkdir(parents=True, exist_ok=True)
    if previous_foundation is not None and revision_request is not None:
        install_foundation_snapshot(sandbox, previous_foundation)
    if repair_delivery is None:
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
    write_json(sandbox / PAPER_EVIDENCE_DIR / "analysis_artifacts" / "foundation_context.json", cache_architecture)
    trusted_before = _trusted_hashes(sandbox)
    prompt = _foundation_brief(scientific_architecture, case_runtime=case_runtime)
    if recovery_instructions:
        write_json(sandbox / "supervisor_recovery.json", recovery_instructions)
        prompt += (
            "\n## Project supervisor repair assignment\n"
            "Repair the preserved Foundation within its original scientific scope. "
            "Use the diagnosis below and verify the expected change with the existing contract tests. "
            "Do not weaken tests, alter paper goals, or claim success without validation.\n"
            + pretty_json(recovery_instructions)
        )
    if repair_delivery is not None:
        validation = repair_validation or {}
        write_json(sandbox / "foundation_validation_feedback.json", validation)
        tests = validation.get("tests") or {}
        prompt = (
            "# Repair the installed Foundation delivery\n"
            "The host restored the completed implementation. Repair the failures below; "
            "do not regenerate unchanged modules. Full diagnostics are in "
            "`foundation_validation_feedback.json`. Re-run the shared contract tests "
            "after the smallest causal correction.\n"
            + pretty_json({"issues": validation.get("issues", []),
                           "moderator_instructions": recovery_instructions,
                           "returncode": tests.get("returncode"),
                           "stderr_tail": str(tests.get("stderr") or tests.get("error") or "")[-4000:]})
            + "\n\n" + prompt
        )
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
        trusted_after = _trusted_hashes(sandbox)
        trusted_changed = sorted(path for path, digest in trusted_before.items() if trusted_after.get(path) != digest)
        _restore_trusted_runtime_atomically(sandbox)
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
    pending_requests = _foundation_pending_requirements(
        sandbox=sandbox, case_runtime=case_runtime,
        explicit_requests=list(read_environment_request(sandbox=sandbox, source="foundation_writer")),
    )
    if pending_requests:
        write_json(environment_resume_path, {
            "schema_version": 1, "state": "awaiting_environment",
            "recovery_input_hash": environment_resume_key,
            "writer_input_hash": input_hash,
            "writer_snapshot_hash": writer_delivery["snapshot_hash"],
            "requests": [asdict(request) for request in pending_requests],
            "validation": None,
        })
        raise EnvironmentRequestRequired(pending_requests, source="foundation_writer")
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


def _load_environment_pending_foundation(
    *, path: Path, audit_dir: Path, expected_key: str, required_modules: set[str],
) -> tuple[Path, dict[str, Any], dict[str, Any]] | None:
    """Read a host checkpoint only when its completed delivery is still intact."""
    if path_is_foundation_link(path) or path_is_foundation_link(path.parent):
        raise RuntimeError("unsafe Foundation environment-resume checkpoint")
    if not path.is_file():
        return None
    record = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(record, dict) or record.get("recovery_input_hash") != expected_key:
        raise RuntimeError("Foundation environment-resume checkpoint does not match its inputs")
    if record.get("state") == "validated":
        return None
    writer_hash = record.get("writer_input_hash")
    if (record.get("state") != "awaiting_environment" or not isinstance(writer_hash, str)
            or len(writer_hash) != 64 or any(char not in "0123456789abcdef" for char in writer_hash)
            or not isinstance(record.get("requests"), list)):
        raise RuntimeError("invalid Foundation environment-resume checkpoint")
    delivery_dir = audit_dir / "03b_foundation_writer_deliveries" / writer_hash
    receipt = load_foundation_writer_delivery(
        delivery_dir=delivery_dir, expected_input_hash=writer_hash,
        expected_required_modules=required_modules,
    )
    if receipt is None or receipt["snapshot_hash"] != record.get("writer_snapshot_hash"):
        raise RuntimeError("completed Foundation delivery changed before environment recovery; preserved for inspection")
    return delivery_dir, receipt, record


def _foundation_pending_requirements(
    *, sandbox: Path, case_runtime: CaseRuntime | None,
    explicit_requests: list[RequirementRequest],
) -> list[RequirementRequest]:
    if case_runtime is None:
        return explicit_requests
    pending = [request for request in explicit_requests
               if not _lock_satisfies_requirement(normalize_requirement(request), case_runtime.lock)]
    pending.extend(requirements_missing_from_lock(
        sandbox / "requirements.txt", case_runtime.lock,
        source="foundation_writer:requirements.txt",
    ))
    return pending


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
    manifest["validation"] = {"tests_passed": tests.get("passed"), "local_imports_resolve": None,
                              "observations": tests.get("observations", []), "tests": tests}
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
            "tests_passed": test_result.get("passed"),
            "local_imports_resolve": None,
            "observations": test_result.get("observations", []),
            "tests": test_result,
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
    """Collect actual delivery/test observations; usability belongs to the supervisor.

    Only unsafe access or changes to the files being certified prevent freezing.
    An optional test failure, import spelling or architecture description is not
    a Python verdict about whether this Foundation can support the task.
    """
    issues = [{"file": path, "message": "Foundation Writer modified a host-trusted runtime file"}
              for path in trusted_changed]
    observations: list[dict[str, Any]] = []
    try:
        files = _foundation_project_files(sandbox)
    except (OSError, RuntimeError, ValueError) as exc:
        issues.append({"file": str(getattr(exc, "filename", None) or sandbox),
                       "message": f"{type(exc).__name__}: {exc}"})
        return issues, {"passed": False, "skipped": True,
                        "reason": "unsafe Foundation filesystem layout", "observations": observations}
    if issues:
        return issues, {"passed": False, "skipped": True,
                        "reason": "host runtime integrity changed", "observations": observations}

    result: dict[str, Any] = {}
    try:
        value = json.loads((sandbox / "foundation_result.json").read_text(encoding="utf-8-sig"))
        if isinstance(value, dict):
            result = value
        else:
            observations.append({"file": "foundation_result.json", "kind": "handoff_not_object",
                                 "message": "Writer handoff is not a JSON object; original content is retained."})
    except (OSError, UnicodeError, ValueError) as exc:
        observations.append({"file": "foundation_result.json", "kind": "handoff_unavailable",
                             "message": f"{type(exc).__name__}: {exc}"})

    observed_paths = {path.relative_to(sandbox).as_posix() for path in files}
    expected_modules = sorted(_required_foundation_modules(architecture))
    missing_modules = [path for path in expected_modules if path not in observed_paths]
    if missing_modules:
        observations.append({"kind": "declared_modules_missing", "paths": missing_modules,
                             "message": "Declared modules are absent; the supervisor decides whether the actual implementation fulfills the handoff."})
    tests = sorted(path for path in observed_paths if path.startswith("tests/") and Path(path).name.startswith("test") and path.endswith(".py"))
    if not tests:
        observations.append({"kind": "no_delivered_tests", "message": "No Foundation test files were delivered."})

    before = _foundation_delivery_hashes(sandbox)
    test_result = dict(_run_foundation_tests(sandbox) if case_runtime is None
                       else _run_foundation_tests(sandbox, case_runtime=case_runtime))
    test_result.update(observations=observations, writer_handoff=result,
                       source_files=sorted(observed_paths), declared_modules=expected_modules,
                       test_files=tests, decision_authority="supervisor")
    try:
        after = _foundation_delivery_hashes(sandbox)
        changed = sorted(path for path in set(before) | set(after) if before.get(path) != after.get(path))
    except (OSError, RuntimeError, ValueError) as exc:
        changed = [f"layout inspection failed: {type(exc).__name__}: {exc}"]
    test_result["delivery_immutable"] = not changed
    if changed:
        test_result["changed_delivery_files"] = changed
        issues.append({"file": "tests", "message": "Foundation tests changed files eligible for freezing: " + ", ".join(changed[:8])})
    # Keep the subprocess result exactly as observed. The node supervisor can
    # distinguish an irrelevant test from a broken shared implementation.
    if test_result.get("passed") is not True:
        observations.append({"kind": "test_execution_not_passed",
                             "returncode": test_result.get("returncode"),
                             "timed_out": test_result.get("timed_out", False),
                             "message": "Delivered tests did not pass; this is an observation, not a scientific rejection."})
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
