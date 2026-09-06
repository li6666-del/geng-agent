"""Python import discovery, declaration validation, and reconciliation."""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Any

from .dependency_policy import (
    RuntimeDocument,
    _canonical_distribution_name,
    _matching_lock_record,
    _parse_safe_requirement,
    _runtime_dependency_state,
    _runtime_lock_binding_issues,
    _runtime_requirement_records,
    requirement_name_for_import,
)
from .security_static_policy import (
    FORBIDDEN_IMPORTS,
    TRUSTED_RUNTIME_FILES,
    _call_name,
)


def validate_requirements(
    root: Path,
    *,
    runtime_policy: RuntimeDocument = None,
    runtime_lock: RuntimeDocument = None,
) -> list[dict[str, str]]:
    requirements = root / "requirements.txt"
    if not requirements.exists():
        return [{
            "file": "requirements.txt",
            "category": "requirements_file_missing",
            "message": "requirements.txt is missing",
        }]

    issues = _runtime_lock_binding_issues(runtime_policy, runtime_lock)
    declared_packages: set[str] = set()
    for line_no, raw_line in enumerate(requirements.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parsed, syntax_issue = _parse_safe_requirement(line)
        if syntax_issue or parsed is None:
            issues.append({
                "file": "requirements.txt",
                "line": str(line_no),
                "category": syntax_issue or "invalid_requirement",
                "message": f"disallowed requirement syntax: {line}",
            })
            continue
        package = _canonical_distribution_name(parsed.name)
        declared_packages.add(package)
        if parsed.marker is not None and runtime_lock is None and not parsed.marker.evaluate():
            continue
        if runtime_lock is not None and _matching_lock_record(parsed, runtime_lock) is None:
            issues.append({
                "file": "requirements.txt",
                "line": str(line_no),
                "category": "dependency_lock_constraint_mismatch",
                "package": package,
                "message": f"declared requirement is not exactly represented by the case lock: {line}",
            })
            continue
        category, available = _runtime_dependency_state(
            package, runtime_policy=runtime_policy, runtime_lock=runtime_lock
        )
        if not available:
            issues.append({
                "file": "requirements.txt",
                "line": str(line_no),
                "category": category,
                "package": package,
                "message": (
                    f"declared package is unresolved in the case runtime lock: {package}"
                    if category == "dependency_lock_unresolved"
                    else f"declared package is not available in the selected runtime: {package}"
                ),
            })
    issues.extend(validate_import_requirements(
        root,
        declared_packages,
        runtime_policy=runtime_policy,
        runtime_lock=runtime_lock,
    ))
    return issues


def reconcile_runtime_requirements(
    root: Path,
    *,
    runtime_policy: RuntimeDocument = None,
    runtime_lock: RuntimeDocument = None,
) -> list[str]:
    """Declare imports already proven available by the active runtime.

    In case-lock mode, only satisfied lock entries are added. The compatibility
    mode without a lock uses the active interpreter. Package names are never
    admitted or rejected by a global static list.
    """
    req_path = root / "requirements.txt"
    declared: set[str] = set()
    existing: list[str] = []
    if req_path.exists():
        for raw in req_path.read_text(encoding="utf-8", errors="replace").splitlines():
            existing.append(raw)
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parsed, _ = _parse_safe_requirement(line)
            if parsed is not None:
                declared.add(_canonical_distribution_name(parsed.name))

    local_modules = collect_local_module_roots(root)
    to_add: dict[str, str] = {}
    locked_records = _runtime_requirement_records(runtime_lock)
    for py_file in iter_project_python_files(root):
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for import_ref in collect_third_party_imports(tree, local_modules):
            package = requirement_name_for_import(
                import_ref["root"], runtime_policy=runtime_policy, runtime_lock=runtime_lock
            )
            _, available = _runtime_dependency_state(
                package,
                runtime_policy=runtime_policy,
                runtime_lock=runtime_lock,
                for_import=True,
            )
            if package not in declared and available:
                locked = locked_records.get(package, {})
                to_add[package] = str(locked.get("requirement") or package)
        if (
            uses_trusted_torch_backend(tree)
            and "torch" not in declared
        ):
            _, torch_available = _runtime_dependency_state(
                "torch",
                runtime_policy=runtime_policy,
                runtime_lock=runtime_lock,
                for_import=True,
            )
            if torch_available:
                locked = locked_records.get("torch", {})
                to_add["torch"] = str(locked.get("requirement") or "torch")

    if not to_add:
        return []
    added = sorted(to_add)
    lines = [line for line in existing if line.strip()]
    lines.extend(to_add[package] for package in added)
    req_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return added


def validate_import_requirements(
    root: Path,
    declared_packages: set[str],
    *,
    runtime_policy: RuntimeDocument = None,
    runtime_lock: RuntimeDocument = None,
) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    local_modules = collect_local_module_roots(root)
    for py_file in iter_project_python_files(root):
        rel = py_file.relative_to(root).as_posix()
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8", errors="replace"), filename=rel)
        except SyntaxError:
            continue
        third_party_imports = collect_third_party_imports(tree, local_modules)
        for import_ref in third_party_imports:
            package = requirement_name_for_import(
                import_ref["root"], runtime_policy=runtime_policy, runtime_lock=runtime_lock
            )
            if package not in declared_packages:
                issues.append(
                    {
                        "file": rel,
                        "line": str(import_ref["line"]),
                        "category": "dependency_declaration_missing",
                        "package": package,
                        "message": f"third-party import is not declared in requirements.txt: {import_ref['name']} (expected package {package})",
                    }
                )
            else:
                category, available = _runtime_dependency_state(
                    package,
                    runtime_policy=runtime_policy,
                    runtime_lock=runtime_lock,
                    for_import=True,
                )
                if not available:
                    issues.append(
                        {
                            "file": rel,
                            "line": str(import_ref["line"]),
                            "category": category,
                            "package": package,
                            "message": f"third-party import is declared but unavailable in the selected runtime: {import_ref['name']}",
                        }
                    )

        if uses_trusted_torch_backend(tree):
            if "torch" not in declared_packages:
                issues.append(
                    {
                        "file": rel,
                        "line": "0",
                        "category": "dependency_declaration_missing",
                        "package": "torch",
                        "message": "trusted torch backend is used but requirements.txt does not declare torch",
                    }
                )
            else:
                category, available = _runtime_dependency_state(
                    "torch",
                    runtime_policy=runtime_policy,
                    runtime_lock=runtime_lock,
                    for_import=True,
                )
                if not available:
                    issues.append(
                        {
                            "file": rel,
                            "line": "0",
                            "category": category,
                            "package": "torch",
                            "message": "trusted torch backend is requested but torch is unavailable in the selected runtime",
                        }
                    )

        for node in ast.walk(tree):
            if isinstance(node, ast.Try) and catches_broad_exception(node):
                guarded_imports = []
                for child in node.body:
                    if isinstance(child, (ast.Import, ast.ImportFrom)):
                        guarded_imports.extend(collect_imports_from_node(child, local_modules))
                for import_ref in guarded_imports:
                    issues.append(
                        {
                            "file": rel,
                            "line": str(import_ref["line"]),
                            "category": "dependency_import_fallback",
                            "package": requirement_name_for_import(
                                import_ref["root"],
                                runtime_policy=runtime_policy,
                                runtime_lock=runtime_lock,
                            ),
                            "message": f"third-party import is guarded by broad try/except and may silently degrade scientific behavior: {import_ref['name']}",
                        }
                    )
    return issues


def collect_local_module_roots(root: Path) -> set[str]:
    modules = set()
    for path in root.iterdir():
        if path.name.startswith("."):
            continue
        if path.is_dir() and (path / "__init__.py").exists():
            modules.add(path.name)
        elif path.is_file() and path.suffix == ".py":
            modules.add(path.stem)
    if (root / "src").is_dir():
        modules.add("src")
    return modules


def iter_project_python_files(root: Path):
    for py_file in root.rglob("*.py"):
        rel_parts = py_file.relative_to(root).parts
        if "__pycache__" in rel_parts or "repair_logs" in rel_parts or "outputs" in rel_parts:
            continue
        if py_file.relative_to(root).as_posix() in TRUSTED_RUNTIME_FILES:
            continue
        yield py_file


def collect_third_party_imports(tree: ast.AST, local_modules: set[str]) -> list[dict[str, Any]]:
    imports: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            imports.extend(collect_imports_from_node(node, local_modules))
    return imports


def collect_imports_from_node(node: ast.Import | ast.ImportFrom, local_modules: set[str]) -> list[dict[str, Any]]:
    imports: list[dict[str, Any]] = []
    if isinstance(node, ast.Import):
        names = [alias.name for alias in node.names]
    elif node.level:
        names = []
    else:
        names = [node.module or ""]
    for name in names:
        root_name = name.split(".", 1)[0]
        if is_third_party_import(root_name, local_modules):
            imports.append({"name": name, "root": root_name, "line": getattr(node, "lineno", 0)})
    return imports


def is_third_party_import(root_name: str, local_modules: set[str]) -> bool:
    if not root_name or root_name in local_modules:
        return False
    if root_name in FORBIDDEN_IMPORTS:
        return False
    stdlib_names = getattr(sys, "stdlib_module_names", set())
    if root_name in stdlib_names or root_name in sys.builtin_module_names:
        return False
    return True


def catches_broad_exception(node: ast.Try) -> bool:
    for handler in node.handlers:
        if handler.type is None:
            return True
        if isinstance(handler.type, ast.Name) and handler.type.id in {"Exception", "BaseException"}:
            return True
        if isinstance(handler.type, ast.Tuple):
            for item in handler.type.elts:
                if isinstance(item, ast.Name) and item.id in {"Exception", "BaseException"}:
                    return True
    return False


def uses_trusted_torch_backend(tree: ast.AST) -> bool:
    backend_aliases = {"_backend"}
    torch_func_aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "src":
            for alias in node.names:
                if alias.name == "_backend":
                    backend_aliases.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module == "src._backend":
            for alias in node.names:
                if alias.name == "torch":
                    torch_func_aliases.add(alias.asname or alias.name)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node.func)
        if name in torch_func_aliases:
            return True
        if any(name == f"{alias}.torch" for alias in backend_aliases):
            return True
    return False
