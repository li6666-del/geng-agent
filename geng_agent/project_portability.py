"""Public façade and orchestration for reproduction-project portability checks."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from .portability_contracts import (
    ProjectPortabilityError,
    _PARENT_SEGMENT,
    _URL,
    _as_warning,
    _dedupe_issues,
    _is_absolute_cross_platform,
    _issue,
    _result,
    _warning,
)
from .portability_inventory import (
    _IGNORED_DIRECTORIES,
    _IGNORED_DIRECTORY_NAMES,
    _IGNORED_SUFFIXES,
    _SOURCE_INVENTORY_NAME,
    _inventory_digest,
    _is_ignored_directory,
    _is_ignored_file,
    _iter_regular_files,
    _sha256_file,
    _source_inventory_issues,
    build_source_inventory,
)
from .portability_reference_scan import (
    _HOST_ABSOLUTE_PATH,
    _MAX_SCANNED_TEXT_BYTES,
    _MULTI_FILE_REFERENCE_KEYS,
    _PYTHON_PATH_CALL_HINTS,
    _PYTHON_PATH_NAME_HINTS,
    _ROOT_MANIFEST_NAMES,
    _SINGLE_FILE_REFERENCE_KEYS,
    _TEXT_FILENAMES,
    _TEXT_SUFFIXES,
    _case_path_reference_findings,
    _dotted_ast_name,
    _filesystem_issues,
    _is_python_test_source,
    _json_strings,
    _literal_path_issues,
    _looks_path_like,
    _manifest_reference_issues,
    _manifest_references,
    _name_has_path_hint,
    _python_docstring_nodes,
    _python_literal_is_execution_path,
    _python_parent_map,
    _python_parent_reference_is_direct,
    _should_scan_text_path,
    _target_has_path_hint,
    _validate_manifest_reference,
)
from .portability_smoke import (
    _LARGE_RELOCATION_COPY_BYTES,
    _MAX_SMOKE_TIMEOUT_SECONDS,
    _SMOKE_GUARD_SOURCE,
    _copy_ignore,
    _discover_smoke_command,
    _display_command,
    _guarded_smoke_command,
    _nested_value,
    _offline_smoke_environment,
    _run_relocated_smoke,
    _safe_python_command,
)


def validate_repro_project_portability(
    project_root: str | Path,
    *,
    run_smoke: bool = False,
    python_executable: str | Path | None = None,
    smoke_command: Sequence[str] | None = None,
    smoke_timeout_s: float = 60.0,
    raise_on_error: bool = True,
) -> dict[str, Any]:
    """Validate that a reproduction project can be moved and run independently.

    With ``run_smoke=True`` the validated tree is copied to a new temporary
    directory before a Python-only, explicitly lightweight smoke command runs in
    an isolated home and minimal environment.  The command may be supplied
    directly or declared by a root reproducibility manifest.  The timeout is
    capped at two minutes; timeout and relocation-copy infrastructure failures
    are reported as inconclusive audit warnings, not project defects.
    """

    root = Path(project_root)
    issues: list[dict[str, Any]] = []
    if not root.is_dir():
        issues.append(_issue("project_root_missing", ".", "project root is missing or is not a directory"))
        result = _result(
            inventory=None,
            issues=issues,
            warnings=[],
            smoke={"requested": bool(run_smoke), "ran": False},
        )
        if raise_on_error:
            raise ProjectPortabilityError(result)
        return result

    try:
        inventory = build_source_inventory(root)
    except OSError as exc:
        issues.append(_issue("inventory_failed", ".", f"project files could not be inventoried: {exc}"))
        result = _result(
            inventory=None,
            issues=issues,
            warnings=[],
            smoke={"requested": bool(run_smoke), "ran": False},
        )
        if raise_on_error:
            raise ProjectPortabilityError(result) from exc
        return result
    warnings: list[dict[str, Any]] = []
    issues.extend(_source_inventory_issues(root, inventory))
    issues.extend(_filesystem_issues(root))
    path_issues, path_warnings = _case_path_reference_findings(root)
    issues.extend(path_issues)
    warnings.extend(path_warnings)
    issues.extend(_manifest_reference_issues(root))
    issues = _dedupe_issues(issues)
    warnings = _dedupe_issues(warnings)

    smoke_result: dict[str, Any] = {"requested": bool(run_smoke), "ran": False}
    if run_smoke and not issues:
        smoke_result, smoke_issues, smoke_warnings = _run_relocated_smoke(
            root,
            inventory=inventory,
            python_executable=python_executable,
            smoke_command=smoke_command,
            timeout_s=smoke_timeout_s,
        )
        issues.extend(smoke_issues)
        warnings.extend(smoke_warnings)
        issues = _dedupe_issues(issues)
        warnings = _dedupe_issues(warnings)

    result = _result(inventory=inventory, issues=issues, warnings=warnings, smoke=smoke_result)
    if issues and raise_on_error:
        raise ProjectPortabilityError(result)
    return result
