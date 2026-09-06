"""Static scanner for dangerous behavior in generated reproduction code."""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

from .dependency_import_policy import iter_project_python_files
from .security_env import SENSITIVE_ENV_KEYS
from .security_static_policy import (
    DANGEROUS_DYNAMIC_IMPORT_ROOTS,
    DANGEROUS_REFLECTION_ATTRIBUTES,
    FORBIDDEN_BUILTINS,
    FORBIDDEN_CALLS,
    FORBIDDEN_DUNDER_ATTRS,
    FORBIDDEN_IMPORTS,
    ORDINARY_REFLECTION_BUILTINS,
    _call_name,
)


def classify_static_security_issue(issue: dict[str, Any]) -> str:
    """Return a stable policy category for one static scanner finding.

    Classification is intentionally based on the scanner message rather than a
    caller-provided severity/category. This keeps the global scanner strict while
    allowing a narrow consumer, such as the clean Foundation case sandbox, to
    downgrade only explicitly approved categories.
    """

    message = str(issue.get("message") or "")
    import_prefix = "forbidden import: "
    if message.startswith(import_prefix):
        imported = message[len(import_prefix):].strip()
        if imported == "importlib" or imported.startswith("importlib."):
            return "importlib_usage"
    environment_prefix = "forbidden environment access: "
    if message.startswith(environment_prefix):
        target = message[len(environment_prefix):].strip()
        if target in {"os.environ", "os.getenv"}:
            return "environment_access"
    builtin_prefix = "forbidden dynamic builtin: "
    if message.startswith(builtin_prefix):
        builtin = message[len(builtin_prefix):].strip()
        if builtin in ORDINARY_REFLECTION_BUILTINS:
            return "ordinary_reflection"
        if builtin in {"eval", "exec", "compile", "__import__"}:
            return "dynamic_execution"
    if message.startswith("dangerous dynamic import: "):
        return "dangerous_dynamic_import"
    if message.startswith("dangerous reflection: "):
        return "dangerous_reflection"
    if message.startswith("dangerous environment access: "):
        # Foundation executes under a host-owned, scrubbed environment. Reads,
        # dynamic keys, and even bulk/mutating access through ``os.environ`` are
        # therefore advisory there; the same findings remain errors for every
        # strict scanner consumer because severity is assigned by
        # ``split_static_security_issues``. Keep the separate dangerous category
        # for environment APIs the user did not authorize (for example
        # ``os.putenv``/``os.unsetenv``).
        if "os.environ" in message or "os.getenv" in message:
            return "environment_access"
        return "dangerous_environment_access"
    return "security_violation"


def split_static_security_issues(
    issues: list[dict[str, Any]],
    *,
    advisory_categories: frozenset[str] | set[str] = frozenset(),
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Partition findings without changing the scanner's strict default policy."""

    blocking: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    allowed = frozenset(advisory_categories)
    for raw in issues:
        item = {str(key): str(value) for key, value in raw.items()}
        category = classify_static_security_issue(raw)
        item["category"] = category
        if category in allowed:
            item["severity"] = "warning"
            warnings.append(item)
        else:
            item["severity"] = "error"
            blocking.append(item)
    return blocking, warnings


def _security_import_aliases(tree: ast.AST) -> tuple[dict[str, str], dict[str, str]]:
    module_aliases: dict[str, str] = {}
    symbol_aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root_name = alias.name.split(".", 1)[0]
                local_name = alias.asname or root_name
                module_aliases[local_name] = alias.name if alias.asname else root_name
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            for alias in node.names:
                if alias.name == "*":
                    continue
                symbol_aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return module_aliases, symbol_aliases


def _resolve_security_name(
    name: str,
    *,
    module_aliases: dict[str, str],
    symbol_aliases: dict[str, str],
) -> str:
    if not name:
        return ""
    if name in symbol_aliases:
        return symbol_aliases[name]
    head, separator, tail = name.partition(".")
    if head == "__builtins__":
        prefix = "builtins"
        return f"{prefix}.{tail}" if separator else prefix
    if head in symbol_aliases:
        prefix = symbol_aliases[head]
        return f"{prefix}.{tail}" if separator else prefix
    if head in module_aliases:
        prefix = module_aliases[head]
        return f"{prefix}.{tail}" if separator else prefix
    return name


def _is_sensitive_callable_name(name: str) -> bool:
    root_name = name.split(".", 1)[0]
    if (
        root_name in DANGEROUS_DYNAMIC_IMPORT_ROOTS
        or root_name in DANGEROUS_REFLECTION_ATTRIBUTES
    ):
        return True
    if name in FORBIDDEN_CALLS or name in FORBIDDEN_BUILTINS:
        return True
    if name.startswith("builtins.") and name.split(".", 1)[1] in FORBIDDEN_BUILTINS:
        return True
    if name in {
        "importlib.import_module",
        "importlib.reload",
        "importlib.util.spec_from_file_location",
    }:
        return True
    if name.startswith("importlib.machinery.") and name.rsplit(".", 1)[-1].endswith("Loader"):
        return True
    return name.endswith(".exec_module") or name.endswith(".load_module")


def _add_one_level_callable_aliases(
    tree: ast.AST,
    *,
    module_aliases: dict[str, str],
    symbol_aliases: dict[str, str],
) -> None:
    assignments = sorted(
        (node for node in ast.walk(tree) if isinstance(node, (ast.Assign, ast.AnnAssign))),
        key=lambda node: getattr(node, "lineno", 0),
    )
    for node in assignments:
        value = node.value
        if not isinstance(value, (ast.Name, ast.Attribute)):
            continue
        resolved = _resolve_security_name(
            _call_name(value),
            module_aliases=module_aliases,
            symbol_aliases=symbol_aliases,
        )
        if not _is_sensitive_callable_name(resolved):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if isinstance(target, ast.Name):
                symbol_aliases[target.id] = resolved


def _call_argument(node: ast.Call, position: int, keyword: str) -> ast.AST | None:
    if len(node.args) > position:
        return node.args[position]
    for item in node.keywords:
        if item.arg == keyword:
            return item.value
    return None


def _static_string_value(node: ast.AST | None) -> str | None:
    """Fold only syntax that is unambiguously a side-effect-free string literal."""

    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _static_string_value(node.left)
        right = _static_string_value(node.right)
        if left is not None and right is not None:
            return left + right
        return None
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
                return None
            parts.append(value.value)
        return "".join(parts)
    return None


def _importlib_package_root(package: str) -> str:
    return package.lstrip(".").split(".", 1)[0]


def _dangerous_dynamic_import_message(node: ast.Call, *, call_name: str) -> str | None:
    if call_name == "importlib.reload":
        return "dangerous dynamic import: importlib.reload executes module code"
    if call_name == "importlib.util.spec_from_file_location":
        return "dangerous dynamic import: spec_from_file_location loads an arbitrary path"
    if (
        call_name.startswith("importlib.machinery.")
        and call_name.rsplit(".", 1)[-1].endswith("Loader")
    ):
        return f"dangerous dynamic import: loader construction via {call_name}"
    if call_name.endswith(".exec_module") or call_name.endswith(".load_module"):
        return f"dangerous dynamic import: loader execution via {call_name}"
    if call_name != "importlib.import_module":
        return None
    target_node = _call_argument(node, 0, "name")
    if target_node is None:
        return "dangerous dynamic import: missing module target"
    target = _static_string_value(target_node)
    if target is None:
        return "dangerous dynamic import: module target is not a string literal"
    target = target.strip()
    if target.startswith("."):
        package_node = _call_argument(node, 1, "package")
        if package_node is None:
            return "dangerous dynamic import: relative module target is missing package"
        package = _static_string_value(package_node)
        if package is None:
            return "dangerous dynamic import: relative module package is not a string literal"
        package = package.strip()
        if not package:
            return "dangerous dynamic import: relative module package is empty"
        root_name = _importlib_package_root(package)
        if root_name in DANGEROUS_DYNAMIC_IMPORT_ROOTS:
            return (
                "dangerous dynamic import: forbidden relative package "
                f"{package!r} for target {target!r}"
            )
        return None
    root_name = target.split(".", 1)[0]
    if root_name in DANGEROUS_DYNAMIC_IMPORT_ROOTS:
        return f"dangerous dynamic import: forbidden module target {target!r}"
    return None


def _is_dangerous_loader_reflection(target_name: str, attribute: str) -> bool:
    if target_name == "importlib.machinery" and attribute.endswith("Loader"):
        return True
    return (
        target_name.endswith(".loader")
        and attribute in {"exec_module", "load_module"}
    )


def _dangerous_reflection_message(
    node: ast.Call,
    *,
    call_name: str,
    module_aliases: dict[str, str],
    symbol_aliases: dict[str, str],
) -> str | None:
    if call_name not in {"getattr", "builtins.getattr"} or not node.args:
        return None
    target_name = _resolve_security_name(
        _call_name(node.args[0]),
        module_aliases=module_aliases,
        symbol_aliases=symbol_aliases,
    )
    root_name = target_name.split(".", 1)[0]
    if len(node.args) < 2:
        return None
    attribute = _static_string_value(node.args[1])
    if attribute is None:
        return None
    if _is_dangerous_loader_reflection(target_name, attribute):
        return (
            "dangerous reflection: dynamic loader attribute "
            f"{attribute!r} on {target_name}"
        )
    dangerous_attributes = (
        DANGEROUS_REFLECTION_ATTRIBUTES.get(target_name, frozenset())
        | DANGEROUS_REFLECTION_ATTRIBUTES.get(root_name, frozenset())
    )
    if attribute in dangerous_attributes:
        return (
            "dangerous reflection: "
            f"sensitive module {root_name} attribute {attribute!r}"
        )
    return None


def _dangerous_reflection_subscript_message(
    node: ast.Subscript,
    *,
    module_aliases: dict[str, str],
    symbol_aliases: dict[str, str],
) -> str | None:
    """Flag only literal mapping lookups that resolve to a blocked capability."""

    key = _static_string_value(node.slice)
    if key is None:
        return None

    container = node.value
    if isinstance(container, ast.Name) and container.id == "__builtins__" and key in {
        "compile",
        "eval",
        "exec",
        "__import__",
    }:
        return f"dangerous reflection: direct __builtins__ capability {key!r}"

    if isinstance(container, ast.Call):
        accessor = _resolve_security_name(
            _call_name(container.func),
            module_aliases=module_aliases,
            symbol_aliases=symbol_aliases,
        )
        if accessor in {"vars", "builtins.vars"} and container.args:
            target_name = _resolve_security_name(
                _call_name(container.args[0]),
                module_aliases=module_aliases,
                symbol_aliases=symbol_aliases,
            )
            if _is_dangerous_loader_reflection(target_name, key):
                return (
                    "dangerous reflection: dynamic loader attribute "
                    f"{key!r} on {target_name} via vars"
                )
            root_name = target_name.split(".", 1)[0]
            dangerous_attributes = (
                DANGEROUS_REFLECTION_ATTRIBUTES.get(target_name, frozenset())
                | DANGEROUS_REFLECTION_ATTRIBUTES.get(root_name, frozenset())
            )
            if key in dangerous_attributes:
                return (
                    "dangerous reflection: "
                    f"sensitive module {target_name} attribute {key!r} via vars"
                )
        if accessor in {"globals", "builtins.globals"} and key in {
            "compile",
            "eval",
            "exec",
            "__import__",
        }:
            return f"dangerous reflection: global builtin capability {key!r}"

    if isinstance(container, ast.Attribute) and container.attr == "__dict__":
        target_name = _resolve_security_name(
            _call_name(container.value),
            module_aliases=module_aliases,
            symbol_aliases=symbol_aliases,
        )
        if _is_dangerous_loader_reflection(target_name, key):
            return (
                "dangerous reflection: dynamic loader __dict__ attribute "
                f"{key!r} on {target_name}"
            )
        root_name = target_name.split(".", 1)[0]
        dangerous_attributes = (
            DANGEROUS_REFLECTION_ATTRIBUTES.get(target_name, frozenset())
            | DANGEROUS_REFLECTION_ATTRIBUTES.get(root_name, frozenset())
        )
        if key in dangerous_attributes:
            return (
                "dangerous reflection: "
                f"sensitive module {target_name} __dict__ attribute {key!r}"
            )

    if isinstance(container, ast.Subscript) and key in {
        "compile",
        "eval",
        "exec",
        "__import__",
    }:
        inner_key = _static_string_value(container.slice)
        inner_container = container.value
        if inner_key == "__builtins__" and isinstance(inner_container, ast.Call):
            accessor = _resolve_security_name(
                _call_name(inner_container.func),
                module_aliases=module_aliases,
                symbol_aliases=symbol_aliases,
            )
            if accessor in {"globals", "builtins.globals"}:
                return f"dangerous reflection: globals __builtins__ capability {key!r}"

    return None


def _environment_key_issue(key_node: ast.AST | None, *, access: str) -> str | None:
    if key_node is None:
        return f"dangerous environment access: missing key for {access}"
    if not isinstance(key_node, ast.Constant) or not isinstance(key_node.value, str):
        return f"dangerous environment access: dynamic key for {access}"
    key = key_node.value.upper()
    if key in SENSITIVE_ENV_KEYS:
        return f"dangerous environment access: sensitive key {key!r} via {access}"
    return None


def _dangerous_environment_call_message(
    node: ast.Call,
    *,
    call_name: str,
    module_aliases: dict[str, str],
    symbol_aliases: dict[str, str],
) -> str | None:
    if call_name in {"os.getenv", "os.environ.get"}:
        key_node = node.args[0] if node.args else None
        return _environment_key_issue(key_node, access=call_name)
    if call_name in {"os.putenv", "os.unsetenv"}:
        return f"dangerous environment access: environment mutation via {call_name}"
    if call_name.startswith("os.environ."):
        method = call_name.rsplit(".", 1)[-1]
        if method in {
            "clear",
            "copy",
            "items",
            "keys",
            "pop",
            "popitem",
            "setdefault",
            "update",
            "values",
        }:
            return f"dangerous environment access: bulk or mutating operation {call_name}"
    if call_name in {"dict", "list", "set", "tuple"} and node.args:
        target_name = _resolve_security_name(
            _call_name(node.args[0]),
            module_aliases=module_aliases,
            symbol_aliases=symbol_aliases,
        )
        if target_name == "os.environ":
            return f"dangerous environment access: bulk copy via {call_name}(os.environ)"
    return None


def _dangerous_environment_subscript_message(
    node: ast.Subscript,
    *,
    module_aliases: dict[str, str],
    symbol_aliases: dict[str, str],
) -> str | None:
    target_name = _resolve_security_name(
        _call_name(node.value),
        module_aliases=module_aliases,
        symbol_aliases=symbol_aliases,
    )
    if target_name != "os.environ":
        return None
    return _environment_key_issue(node.slice, access="os.environ[...]")


def static_scan_repro_project(root: Path) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    for py_file in iter_project_python_files(root):
        rel = py_file.relative_to(root).as_posix()
        if rel == "run_task.py" and py_file.read_text(encoding="utf-8") == Path(__file__).with_name("execution_client.py").read_text(encoding="utf-8"):
            continue
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8", errors="replace"), filename=rel)
        except SyntaxError as exc:
            issues.append({"file": rel, "line": str(exc.lineno or 0), "message": f"syntax error: {exc.msg}"})
            continue
        module_aliases, symbol_aliases = _security_import_aliases(tree)
        _add_one_level_callable_aliases(
            tree,
            module_aliases=module_aliases,
            symbol_aliases=symbol_aliases,
        )

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root_name = alias.name.split(".", 1)[0]
                    if root_name in FORBIDDEN_IMPORTS:
                        issues.append({"file": rel, "line": str(node.lineno), "message": f"forbidden import: {alias.name}"})
            elif isinstance(node, ast.ImportFrom):
                root_name = (node.module or "").split(".", 1)[0]
                if root_name in FORBIDDEN_IMPORTS:
                    issues.append({"file": rel, "line": str(node.lineno), "message": f"forbidden import: {node.module}"})
            elif isinstance(node, ast.Call):
                name = _call_name(node.func)
                raw_name = name
                name = _resolve_security_name(
                    name,
                    module_aliases=module_aliases,
                    symbol_aliases=symbol_aliases,
                )
                if name in FORBIDDEN_CALLS:
                    issues.append({"file": rel, "line": str(getattr(node, "lineno", 0)), "message": f"forbidden call: {name}"})
                if name in FORBIDDEN_BUILTINS:
                    issues.append({"file": rel, "line": str(getattr(node, "lineno", 0)), "message": f"forbidden dynamic builtin: {name}"})
                if name in {"open", "Path", "PurePath"}:
                    _check_absolute_path_literal(node, rel, issues)
                if name.startswith("builtins."):
                    builtin_name = name.split(".", 1)[1]
                    if builtin_name in FORBIDDEN_BUILTINS:
                        issues.append(
                            {
                                "file": rel,
                                "line": str(getattr(node, "lineno", 0)),
                                "message": f"forbidden dynamic builtin: {builtin_name}",
                            }
                        )
                dynamic_import_message = _dangerous_dynamic_import_message(
                    node,
                    call_name=name,
                )
                if dynamic_import_message:
                    issues.append(
                        {
                            "file": rel,
                            "line": str(getattr(node, "lineno", 0)),
                            "message": dynamic_import_message,
                        }
                    )
                reflection_message = _dangerous_reflection_message(
                    node,
                    call_name=name,
                    module_aliases=module_aliases,
                    symbol_aliases=symbol_aliases,
                )
                if reflection_message:
                    issues.append(
                        {
                            "file": rel,
                            "line": str(getattr(node, "lineno", 0)),
                            "message": reflection_message,
                        }
                    )
                environment_message = _dangerous_environment_call_message(
                    node,
                    call_name=name,
                    module_aliases=module_aliases,
                    symbol_aliases=symbol_aliases,
                )
                if environment_message:
                    issues.append(
                        {
                            "file": rel,
                            "line": str(getattr(node, "lineno", 0)),
                            "message": environment_message,
                        }
                    )
                if name in {"os.environ", "os.getenv"} and raw_name != name:
                    issues.append(
                        {
                            "file": rel,
                            "line": str(getattr(node, "lineno", 0)),
                            "message": f"forbidden environment access: {name}",
                        }
                    )
                if name in {"builtins.open", "pathlib.Path", "pathlib.PurePath"}:
                    _check_absolute_path_literal(node, rel, issues)
            elif isinstance(node, ast.Attribute):
                if node.attr in FORBIDDEN_DUNDER_ATTRS:
                    issues.append({"file": rel, "line": str(getattr(node, "lineno", 0)), "message": f"forbidden reflection attribute: {node.attr}"})
                name = _call_name(node)
                name = _resolve_security_name(
                    name,
                    module_aliases=module_aliases,
                    symbol_aliases=symbol_aliases,
                )
                if name in {"os.environ", "os.getenv"}:
                    issues.append({"file": rel, "line": str(getattr(node, "lineno", 0)), "message": f"forbidden environment access: {name}"})
            elif isinstance(node, ast.Subscript):
                reflection_message = _dangerous_reflection_subscript_message(
                    node,
                    module_aliases=module_aliases,
                    symbol_aliases=symbol_aliases,
                )
                if reflection_message:
                    issues.append(
                        {
                            "file": rel,
                            "line": str(getattr(node, "lineno", 0)),
                            "message": reflection_message,
                        }
                    )
                environment_message = _dangerous_environment_subscript_message(
                    node,
                    module_aliases=module_aliases,
                    symbol_aliases=symbol_aliases,
                )
                if environment_message:
                    issues.append(
                        {
                            "file": rel,
                            "line": str(getattr(node, "lineno", 0)),
                            "message": environment_message,
                        }
                    )

    return issues


def _check_absolute_path_literal(node: ast.Call, rel: str, issues: list[dict[str, str]]) -> None:
    if not node.args:
        return
    first = node.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        value = first.value
        if re.match(r"^[A-Za-z]:[\\/]", value) or value.startswith(("/", "\\")):
            issues.append({"file": rel, "line": str(getattr(node, "lineno", 0)), "message": f"absolute path literal is forbidden: {value}"})
