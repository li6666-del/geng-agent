"""Filesystem and embedded project-reference portability scans."""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path, PurePosixPath
import re
from typing import Any, Iterable, Mapping

from .portability_contracts import (
    _PARENT_SEGMENT,
    _URL,
    _as_warning,
    _is_absolute_cross_platform,
    _issue,
)
from .portability_inventory import (
    _is_ignored_directory,
    _is_ignored_file,
    _iter_regular_files,
)

_TEXT_SUFFIXES = {".cfg", ".ini", ".json", ".ps1", ".py", ".sh", ".toml", ".yaml", ".yml"}
_TEXT_FILENAMES = {"requirements.txt"}
_ROOT_MANIFEST_NAMES = {
    "artifact_lineage.json",
    "environment.lock.json",
    "execution_plan.json",
    "foundation_manifest.json",
    "package_manifest.json",
    "project_manifest.json",
    "project_portability_manifest.json",
    "repro_project_manifest.json",
    "reproducibility_manifest.json",
    "tasks_manifest.json",
}
_SINGLE_FILE_REFERENCE_KEYS = {
    "artifact_lineage",
    "checkpoint",
    "checkpoint_path",
    "config",
    "config_full",
    "config_path",
    "config_smoke",
    "data_path",
    "dataset_path",
    "entrypoint",
    "environment_lock",
    "execution_plan",
    "file",
    "manifest_path",
    "model_path",
    "path",
    "relative_path",
    "requirements_file",
    "script",
    "source_path",
    "source_inventory",
    "tasks_manifest",
    "vocab_path",
}
_MULTI_FILE_REFERENCE_KEYS = {
    "evidence_files",
    "files",
    "frozen_files",
    "generated_paths",
    "local_assets",
    "paper_assets",
    "required_modules",
    "source_files",
}
_HOST_ABSOLUTE_PATH = re.compile(
    r"(?<![A-Za-z0-9_])(?:"
    r"[A-Za-z]:[\\/][^\s\"'`<>|]+"
    r"|\\\\[^\\/\s]+[\\/][^\s\"'`<>|]+"
    r"|/(?:root|home|Users|mnt|media|workspace|workspaces|tmp|data)(?:/[^\s\"'`<>|]+)+"
    r"|/(?:[^/\s\"'`<>|]+/)*(?:audit|cases?|case[-_][^/\s\"'`<>|]+)(?:/[^\s\"'`<>|]+)+"
    r")"
)

_MAX_SCANNED_TEXT_BYTES = 2_000_000

_PYTHON_PATH_NAME_HINTS = frozenset(
    {
        "artifact",
        "cache",
        "checkpoint",
        "config",
        "data",
        "dataset",
        "dir",
        "directory",
        "file",
        "folder",
        "home",
        "manifest",
        "model",
        "output",
        "path",
        "root",
        "source",
        "vocab",
        "weight",
    }
)
_PYTHON_PATH_CALL_HINTS = frozenset(
    {
        "chdir",
        "exists",
        "isdir",
        "isfile",
        "listdir",
        "load",
        "makedirs",
        "mkdir",
        "open",
        "path",
        "read_bytes",
        "read_csv",
        "read_json",
        "read_text",
        "save",
        "stat",
        "touch",
        "write_bytes",
        "write_csv",
        "write_json",
        "write_text",
    }
)

def _filesystem_issues(root: Path) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    casefolded: dict[str, str] = {}
    for directory, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        directory_path = Path(directory)
        kept: list[str] = []
        for name in sorted(dirnames):
            path = directory_path / name
            if _is_ignored_directory(name):
                continue
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                issues.append(_issue("filesystem_link", relative, "directory link is not a self-contained project entry"))
                continue
            kept.append(name)
        dirnames[:] = kept
        for name in sorted(filenames):
            path = directory_path / name
            if _is_ignored_file(path):
                continue
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                issues.append(_issue("filesystem_link", relative, "file link is not a self-contained project entry"))
                continue
            folded = relative.casefold()
            previous = casefolded.get(folded)
            if previous is not None and previous != relative:
                issues.append(
                    _issue(
                        "case_colliding_paths",
                        relative,
                        f"path collides case-insensitively with {previous}",
                    )
                )
            else:
                casefolded[folded] = relative
    return issues

def _case_path_reference_findings(
    root: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    issues: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    for path in _iter_regular_files(root):
        if path.stat().st_size > _MAX_SCANNED_TEXT_BYTES:
            continue
        if not _should_scan_text_path(path, root):
            continue
        relative = path.relative_to(root).as_posix()
        try:
            text = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError):
            continue

        if path.suffix.lower() == ".json":
            try:
                document = json.loads(text)
            except json.JSONDecodeError as exc:
                issues.append(
                    _issue(
                        "invalid_json",
                        relative,
                        f"JSON used as configuration or manifest is invalid: {exc.msg}",
                        line=exc.lineno,
                    )
                )
                continue
            for location, value in _json_strings(document):
                issues.extend(_literal_path_issues(relative, value, location=location))
            continue

        if path.suffix.lower() == ".py":
            try:
                tree = ast.parse(text, filename=relative)
            except SyntaxError as exc:
                issues.append(
                    _issue(
                        "source_parse_error",
                        relative,
                        f"Python source cannot be imported: {exc.msg}",
                        line=exc.lineno,
                    )
                )
                continue
            docstrings = _python_docstring_nodes(tree)
            parents = _python_parent_map(tree)
            is_test_source = _is_python_test_source(path, root)
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and id(node) not in docstrings
                ):
                    literal_findings = _literal_path_issues(
                        relative,
                        node.value,
                        location=f"line {getattr(node, 'lineno', 0)}",
                        line=getattr(node, "lineno", None),
                    )
                    if not literal_findings:
                        continue
                    execution_path = (
                        not is_test_source
                        and _python_literal_is_execution_path(node, parents)
                    )
                    direct_parent_io = _python_parent_reference_is_direct(node, parents)
                    for item in literal_findings:
                        is_parent = item.get("code") == "parent_path_reference"
                        if execution_path and (not is_parent or direct_parent_io):
                            issues.append(item)
                        else:
                            warnings.append(
                                _as_warning(
                                    item,
                                    code="python_path_literal_warning",
                                    message=(
                                        "path literal is not a demonstrated project escape; "
                                        "review it if runtime resolution can leave the package"
                                    ),
                                )
                            )
            continue

        for line_no, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith(("#", ";")):
                continue
            line_findings = _literal_path_issues(
                relative,
                stripped,
                location=f"line {line_no}",
                line=line_no,
            )
            if path.suffix.lower() in {".ps1", ".sh"}:
                for item in line_findings:
                    if item.get("code") == "parent_path_reference":
                        warnings.append(
                            _as_warning(
                                item,
                                code="script_parent_path_warning",
                                message=(
                                    "script parent navigation is not a demonstrated project escape"
                                ),
                            )
                        )
                    else:
                        issues.append(item)
            else:
                issues.extend(line_findings)
    return issues, warnings

def _python_parent_map(tree: ast.AST) -> dict[int, ast.AST]:
    parents: dict[int, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[id(child)] = parent
    return parents

def _is_python_test_source(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    return path.name.casefold().startswith("test_") or any(
        part.casefold() in {"test", "tests"} for part in relative.parts[:-1]
    )

def _python_literal_is_execution_path(node: ast.Constant, parents: Mapping[int, ast.AST]) -> bool:
    """Return true only when a literal is directly wired into runtime path I/O.

    Free-standing messages, examples, assertions, and comparison strings remain
    visible as warnings instead of becoming static portability blockers.
    """

    child: ast.AST = node
    for _ in range(5):
        parent = parents.get(id(child))
        if parent is None:
            break
        if isinstance(parent, ast.keyword) and _name_has_path_hint(parent.arg or ""):
            return True
        if isinstance(parent, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
            targets: list[ast.AST] = []
            if isinstance(parent, ast.Assign):
                targets.extend(parent.targets)
            else:
                targets.append(parent.target)
            if any(_target_has_path_hint(target) for target in targets):
                return True
        if isinstance(parent, ast.Call):
            call_name = _dotted_ast_name(parent.func).casefold()
            leaf = call_name.rsplit(".", 1)[-1]
            if (
                leaf in _PYTHON_PATH_CALL_HINTS
                or any(_name_has_path_hint(part) for part in call_name.split("."))
            ):
                return True
        if isinstance(parent, (ast.Assert, ast.Compare, ast.Expr)):
            return False
        child = parent
    return False

def _python_parent_reference_is_direct(
    node: ast.Constant,
    parents: Mapping[int, ast.AST],
) -> bool:
    parent = parents.get(id(node))
    if isinstance(parent, ast.Call):
        return True
    if isinstance(parent, ast.keyword):
        return isinstance(parents.get(id(parent)), ast.Call)
    return False

def _target_has_path_hint(node: ast.AST) -> bool:
    if isinstance(node, ast.Name):
        return _name_has_path_hint(node.id)
    if isinstance(node, ast.Attribute):
        return _name_has_path_hint(node.attr)
    if isinstance(node, (ast.Tuple, ast.List)):
        return any(_target_has_path_hint(item) for item in node.elts)
    return False

def _name_has_path_hint(name: str) -> bool:
    tokens = {token for token in re.split(r"[^A-Za-z0-9]+|_+", name.casefold()) if token}
    return bool(tokens & _PYTHON_PATH_NAME_HINTS)

def _dotted_ast_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted_ast_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""

def _json_strings(value: Any, location: str = "$") -> Iterable[tuple[str, str]]:
    if isinstance(value, str):
        yield location, value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            yield from _json_strings(item, f"{location}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _json_strings(item, f"{location}[{index}]")

def _should_scan_text_path(path: Path, root: Path) -> bool:
    suffix = path.suffix.lower()
    if suffix not in _TEXT_SUFFIXES and path.name.lower() not in _TEXT_FILENAMES:
        return False
    if suffix in {".py", ".ps1", ".sh"} or path.name.lower() in _TEXT_FILENAMES:
        return True
    relative = path.relative_to(root)
    name = path.name.casefold()
    config_parent = "configs" in {part.casefold() for part in relative.parts[:-1]}
    if suffix == ".json":
        return config_parent or "config" in name or "manifest" in name
    return (
        len(relative.parts) == 1
        or config_parent
        or "config" in name
        or "manifest" in name
    )

def _python_docstring_nodes(tree: ast.AST) -> set[int]:
    result: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", [])
        if not body or not isinstance(body[0], ast.Expr):
            continue
        value = body[0].value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            result.add(id(value))
    return result

def _literal_path_issues(
    file: str,
    value: str,
    *,
    location: str,
    line: int | None = None,
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    if _URL.match(value.strip()):
        return issues
    matched_paths: set[str] = set()
    for match in _HOST_ABSOLUTE_PATH.finditer(value):
        path = match.group(0).rstrip(".,);]")
        matched_paths.add(path)
        issues.append(
            _issue(
                "absolute_case_path",
                file,
                f"host-specific absolute path prevents relocation: {path}",
                location=location,
                line=line,
                reference=path,
            )
        )
    candidate = value.strip().strip("\"'")
    if (
        not matched_paths
        and _looks_path_like(candidate)
        and _is_absolute_cross_platform(candidate)
    ):
        issues.append(
            _issue(
                "absolute_case_path",
                file,
                f"host-specific absolute path prevents relocation: {candidate}",
                location=location,
                line=line,
                reference=candidate,
            )
        )
    if _PARENT_SEGMENT.search(value.replace("\\", "/")):
        if _looks_path_like(candidate):
            issues.append(
                _issue(
                    "parent_path_reference",
                    file,
                    f"parent-directory reference can escape the relocated project: {candidate}",
                    location=location,
                    line=line,
                    reference=candidate,
                )
            )
    return issues

def _manifest_reference_issues(root: Path) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for name in sorted(_ROOT_MANIFEST_NAMES):
        manifest_path = root / name
        if not manifest_path.is_file() or manifest_path.is_symlink():
            continue
        try:
            document = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            # The focused text scan already reports invalid JSON.
            continue
        for location, reference in _manifest_references(document):
            if not _looks_path_like(reference) or _URL.match(reference):
                continue
            issue = _validate_manifest_reference(root, name, location, reference)
            if issue is not None:
                issues.append(issue)
    return issues

def _manifest_references(value: Any, location: str = "$") -> Iterable[tuple[str, str]]:
    if isinstance(value, Mapping):
        for raw_key, item in value.items():
            key = str(raw_key)
            child_location = f"{location}.{key}"
            if key in _SINGLE_FILE_REFERENCE_KEYS and isinstance(item, str):
                yield child_location, item
            elif key in _MULTI_FILE_REFERENCE_KEYS and isinstance(item, list):
                for index, member in enumerate(item):
                    if isinstance(member, str):
                        yield f"{child_location}[{index}]", member
            yield from _manifest_references(item, child_location)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _manifest_references(item, f"{location}[{index}]")

def _validate_manifest_reference(
    root: Path,
    manifest_name: str,
    location: str,
    reference: str,
) -> dict[str, Any] | None:
    raw = reference.strip().replace("\\", "/")
    if not raw or "{" in raw or "}" in raw or any(character in raw for character in "*?["):
        return None
    if _is_absolute_cross_platform(raw) or _PARENT_SEGMENT.search(raw):
        return _issue(
            "manifest_path_escape",
            manifest_name,
            f"manifest reference must stay inside the project: {reference}",
            location=location,
            reference=reference,
        )
    candidate = (root / PurePosixPath(raw)).resolve(strict=False)
    root_resolved = root.resolve()
    try:
        candidate.relative_to(root_resolved)
    except ValueError:
        return _issue(
            "manifest_path_escape",
            manifest_name,
            f"manifest reference resolves outside the project: {reference}",
            location=location,
            reference=reference,
        )
    if not candidate.exists() or candidate.is_symlink():
        return _issue(
            "manifest_reference_missing",
            manifest_name,
            f"manifest references a missing project file: {reference}",
            location=location,
            reference=reference,
        )
    return None

def _looks_path_like(value: str) -> bool:
    raw = value.strip().strip("\"'")
    if not raw or "\n" in raw or _URL.match(raw):
        return False
    if _is_absolute_cross_platform(raw) or _PARENT_SEGMENT.search(raw):
        return True
    path = PurePosixPath(raw.replace("\\", "/"))
    return "/" in raw.replace("\\", "/") or bool(path.suffix)
