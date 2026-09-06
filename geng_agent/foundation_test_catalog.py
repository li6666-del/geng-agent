"""Catalog and normalize substantive Foundation contract tests."""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

from .foundation_bindings import _ast_dotted_name


_DeliveredTest = tuple[
    ast.FunctionDef | ast.AsyncFunctionDef,
    ast.Module,
    ast.ClassDef,
]


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
        if any(
            tokens[index:index + width] == alias_tokens
            for index in range(len(tokens) - width + 1)
        ):
            return True
    return False


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


def _test_is_skipped(
    node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
) -> bool:
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


def _test_has_substantive_body(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> bool:
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
                        references[_normalized_test_reference(reference)] = (
                            method,
                            tree,
                            node,
                        )
    return references


def _capability_status_passed(item: dict[str, Any]) -> bool:
    status = item.get("status")
    if status is True:
        return True
    return isinstance(status, str) and status.strip().casefold() in {
        "ok",
        "passed",
        "success",
        "verified",
    }


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
    return f"{module}.{segments[0]}", len(segments) > 1
