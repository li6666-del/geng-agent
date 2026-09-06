"""Static source-tree and callable binding analysis for Foundation contracts."""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path
from typing import Any

from .foundation_architecture import library_keys as _library_keys
from .foundation_execution_policy import (
    FRAMEWORK_EXEMPTIONS as _FRAMEWORK_EXEMPTIONS,
    LIBRARY_CANONICAL_NAMES as _LIBRARY_CANONICAL_NAMES,
    TRUSTED_PROJECT_FILES as _TRUSTED_PROJECT_FILES,
)


_Binding = tuple[str, ast.AST | None, str]


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
            trees[relative] = ast.parse(
                path.read_text(encoding="utf-8-sig"),
                filename=str(path),
            )
        except (OSError, SyntaxError, UnicodeError) as exc:
            issues.append(
                {
                    "file": relative,
                    "message": f"cannot statically inspect Python source: {exc}",
                }
            )
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
                return _top_level_binding(
                    trees=trees,
                    module=imported,
                    name=alias.name,
                    seen=seen,
                )
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
            return _expression_binding(
                trees=trees,
                module=module,
                expression=value,
                seen=seen,
            )
    return ("missing", None, module)


def _expression_binding(
    *,
    trees: dict[str, ast.Module],
    module: str,
    expression: ast.AST,
    seen: set[tuple[str, str]],
) -> _Binding:
    if isinstance(expression, ast.Name):
        return _top_level_binding(
            trees=trees,
            module=module,
            name=expression.id,
            seen=seen,
        )
    if isinstance(expression, ast.Subscript):
        return _expression_binding(
            trees=trees,
            module=module,
            expression=expression.value,
            seen=seen,
        )
    if isinstance(expression, ast.Attribute):
        segments: list[str] = []
        current: ast.AST = expression
        while isinstance(current, ast.Attribute):
            segments.append(current.attr)
            current = current.value
        if not isinstance(current, ast.Name):
            return ("missing", None, module)
        binding = _top_level_binding(
            trees=trees,
            module=module,
            name=current.id,
            seen=seen,
        )
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
        binding = _expression_binding(
            trees=trees,
            module=module,
            expression=base,
            seen=seen_bindings,
        )
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
        return _top_level_binding(
            trees=trees,
            module=module,
            name=segment,
            seen=seen_bindings,
        )
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
    binding = _top_level_binding(
        trees=trees,
        module=module,
        name=segments[0],
        seen=set(),
    )
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
            [
                {
                    "file": module or "scientific_architecture.json",
                    "message": f"component {component_id} has no declared callable",
                }
            ],
            [],
        )
    if re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", callable_name) is None:
        return (
            [
                {
                    "file": module or "scientific_architecture.json",
                    "message": (
                        f"component {component_id} callable is not a simple dotted Python name: "
                        f"{callable_name}"
                    ),
                }
            ],
            [],
        )
    if module not in trees:
        return (
            [
                {
                    "file": module or "scientific_architecture.json",
                    "message": (
                        f"component {component_id} declared module is missing or cannot be "
                        "statically inspected"
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


def _ast_dotted_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _ast_dotted_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    if isinstance(node, ast.Call):
        return _ast_dotted_name(node.func)
    return ""
