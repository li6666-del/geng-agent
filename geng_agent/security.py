from __future__ import annotations

import ast
import importlib.util
import os
import re
import sys
from pathlib import Path
from typing import Any


ALLOWED_REQUIREMENTS = {
    "numpy",
    "scipy",
    "matplotlib",
    "scikit-learn",
    "sklearn",
    "reedsolo",
    "pillow",
    # Broadened scientific / communications stack so task writers are not forced into
    # crude simplifications. All pure-computation, no
    # network/system access. Kept in sync with pyproject [repro]; actual local
    # availability is reported by preflight / dependency policy prompts.
    "pandas",
    "sympy",
    "numba",
    "torch",
    "brotli",
    "pesq",
    "scikit-commpy",
    "galois",
    "networkx",
    "h5py",
    "tqdm",
}

REQUIREMENT_IMPORT_NAMES = {
    "scikit-learn": {"sklearn"},
    "sklearn": {"sklearn"},
    "pillow": {"PIL"},
    "scikit-commpy": {"commpy"},
}

IMPORT_REQUIREMENT_NAMES = {
    "sklearn": "scikit-learn",
    "PIL": "pillow",
    "commpy": "scikit-commpy",
    # Matplotlib exposes several namespace packages that are imported directly
    # by normal plotting code but are not installable PyPI distributions.
    "mpl_toolkits": "matplotlib",
}

_UNDECLARED_IMPORT_RE = re.compile(
    r"third-party import is not declared in requirements\.txt: .+ \(expected package ([^)]+)\)"
)

FORBIDDEN_IMPORTS = {
    "socket",
    "requests",
    "urllib",
    "http",
    "ftplib",
    "paramiko",
    "subprocess",
    "multiprocessing",
    "webbrowser",
    "ctypes",
    "importlib",
    "asyncio",   # asyncio.create_subprocess_* spawns processes -> shell bypass
    "pty",       # pty.spawn() launches an interactive shell
    "winreg",    # Windows registry write = persistence / RCE pivot
}

# Dynamic-execution / reflection builtins. Clean numerical reproduction code never
# needs these, but they are the standard way to bypass the static name-based scan
# (e.g. getattr(os, "sys"+"tem"), __import__("sock"+"et"), eval/exec of a string).
# Blocking the call sites closes the obvious obfuscation paths.
FORBIDDEN_BUILTINS = {
    "eval",
    "exec",
    "compile",
    "__import__",
    "getattr",
    "setattr",
    "delattr",
    "globals",
    "vars",
}

ORDINARY_REFLECTION_BUILTINS = frozenset(
    {
        "getattr",
        "setattr",
        "delattr",
        "globals",
        "vars",
    }
)

FOUNDATION_STATIC_SECURITY_ADVISORY_CATEGORIES = frozenset(
    {
        "environment_access",
        "importlib_usage",
        "ordinary_reflection",
    }
)


# Reflection/introspection dunders that defeat the name-based call scan. getattr is a
# blocked builtin, but `os.__getattribute__("system")` fetches os.system unscathed; the
# `().__class__.__bases__[0].__subclasses__()` chain reaches arbitrary C-level callables;
# __globals__/__builtins__ recover eval/exec; __reduce__ is the pickle RCE hook. Clean
# numerical code never touches these (__class__/__dict__ are deliberately NOT here -- they
# see legitimate use and blocking __bases__/__subclasses__/__mro__ already breaks the chain).
FORBIDDEN_DUNDER_ATTRS = {
    "__getattribute__",
    "__subclasses__",
    "__bases__",
    "__mro__",
    "__globals__",
    "__builtins__",
    "__reduce__",
    "__reduce_ex__",
    "__code__",
}

FORBIDDEN_CALLS = {
    "os.system",
    "os.popen",
    "os.startfile",
    # process-launch family: spawn* replaces/forks, exec* replaces the image,
    # posix_spawn* forks -- all are os.system-equivalent command execution.
    "os.spawnl",
    "os.spawnle",
    "os.spawnlp",
    "os.spawnlpe",
    "os.spawnv",
    "os.spawnve",
    "os.spawnvp",
    "os.spawnvpe",
    "os.execl",
    "os.execle",
    "os.execlp",
    "os.execlpe",
    "os.execv",
    "os.execve",
    "os.execvp",
    "os.execvpe",
    "os.posix_spawn",
    "os.posix_spawnp",
    "os.remove",
    "os.unlink",
    "os.rmdir",
    "shutil.rmtree",
    "shutil.move",
    "subprocess.run",
    "subprocess.call",
    "subprocess.Popen",
    "asyncio.create_subprocess_exec",
    "asyncio.create_subprocess_shell",
    "Path.home",
    "Path.expanduser",
}

DANGEROUS_DYNAMIC_IMPORT_ROOTS = frozenset(
    {
        *FORBIDDEN_IMPORTS,
        "builtins",
        "os",
        "pathlib",
        "shutil",
    }
)

DANGEROUS_REFLECTION_ATTRIBUTES = {
    "asyncio": frozenset(
        {
            "create_subprocess_exec",
            "create_subprocess_shell",
        }
    ),
    "builtins": frozenset(
        {
            "__import__",
            "compile",
            "eval",
            "exec",
            "open",
        }
    ),
    "importlib": frozenset(
        {
            "import_module",
            "reload",
        }
    ),
    "importlib.util": frozenset({"spec_from_file_location"}),
    "os": frozenset(
        {
            "chdir",
            "chmod",
            "chown",
            "execl",
            "execle",
            "execlp",
            "execlpe",
            "execv",
            "execve",
            "execvp",
            "execvpe",
            "open",
            "popen",
            "posix_spawn",
            "posix_spawnp",
            "remove",
            "rmdir",
            "spawnl",
            "spawnle",
            "spawnlp",
            "spawnlpe",
            "spawnv",
            "spawnve",
            "spawnvp",
            "spawnvpe",
            "startfile",
            "system",
            "unlink",
        }
    ),
    "shutil": frozenset({"copy", "copy2", "copyfile", "copytree", "move", "rmtree"}),
    "subprocess": frozenset({"Popen", "call", "check_call", "check_output", "run"}),
}

SENSITIVE_ENV_KEYS = {
    "GENG_LLM_API_KEY",
    "OPENAI_API_KEY",
    "DEEPSEEK_API_KEY",
    "ANTHROPIC_API_KEY",
    "HF_TOKEN",
    "HUGGINGFACE_HUB_TOKEN",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AZURE_OPENAI_API_KEY",
    "GOOGLE_API_KEY",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
}

SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{12,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9_\-\.]{12,}", re.IGNORECASE),
    re.compile(r"(?i)(api[_-]?key|token|secret|password)\s*[:=]\s*['\"]?[^'\"\s]+"),
]

TRUSTED_RUNTIME_FILES = {
    "src/_io.py",
    "src/_backend.py",
}


def build_safe_env() -> dict[str, str]:
    keep = {
        "PATH",
        "SystemRoot",
        "WINDIR",
        "windir",
        "TEMP",
        "TMP",
        "PYTHONIOENCODING",
        "MPLBACKEND",
    }
    keep_lower = {key.lower() for key in keep}
    safe_env = {key: value for key, value in os.environ.items() if key.lower() in keep_lower}
    windows_root = os.environ.get("SystemRoot") or os.environ.get("WINDIR") or os.environ.get("windir")
    if windows_root:
        safe_env.setdefault("SystemRoot", windows_root)
        safe_env.setdefault("WINDIR", windows_root)
        safe_env.setdefault("windir", windows_root)
    safe_env["PYTHONIOENCODING"] = "utf-8"
    safe_env["MPLBACKEND"] = "Agg"
    safe_env["MPLCONFIGDIR"] = safe_env.get("TEMP") or safe_env.get("TMP") or "."
    for key in SENSITIVE_ENV_KEYS:
        safe_env.pop(key, None)
    return safe_env


def codex_safe_env() -> dict[str, str]:
    """Environment for a Codex subprocess: the inherited parent env MINUS geng's own LLM
    secrets, which codex never needs (it authenticates with its own credentials). Unlike
    :func:`build_safe_env` -- which strips the env to a minimal allowlist for running
    UNTRUSTED generated code -- codex is a trusted external tool needing a normal env (PATH,
    HOME, its own auth), so keep everything except the GENG_* keys it has no reason to read."""
    env = dict(os.environ)
    for key in ("GENG_LLM_API_KEY", "GENG_LLM2_API_KEY"):
        env.pop(key, None)
    _prefer_geng_python_for_codex(env)
    return env


def _prefer_geng_python_for_codex(env: dict[str, str]) -> None:
    raw_python = _select_geng_python(env.get("GENG_PYTHON"))
    if not raw_python:
        return
    python_path = Path(raw_python)
    python_dir = python_path.parent
    prefix = [
        python_dir,
        python_dir / "Scripts",
        python_dir / "Library" / "bin",
    ]
    existing = env.get("PATH") or env.get("Path") or ""
    seen: set[str] = set()
    path_parts: list[str] = []
    for item in [str(path) for path in prefix if path] + existing.split(os.pathsep):
        if not item:
            continue
        key = item.lower() if sys.platform == "win32" else item
        if key in seen:
            continue
        seen.add(key)
        path_parts.append(item)
    env["PATH"] = os.pathsep.join(path_parts)
    if sys.platform == "win32":
        env["Path"] = env["PATH"]
    env["PYTHON"] = str(python_path)
    env["GENG_PYTHON"] = str(python_path)
    if python_dir.parent.name == "envs":
        env.setdefault("CONDA_PREFIX", str(python_dir))


def _select_geng_python(explicit_python: str | None) -> str:
    for raw_python in (explicit_python, _default_geng_python()):
        python_path = _valid_python_path(raw_python)
        if python_path is not None:
            return str(python_path)
    return ""


def _valid_python_path(raw_python: str | None) -> Path | None:
    raw = (raw_python or "").strip().strip('"')
    if not raw:
        return None
    python_path = Path(raw).expanduser()
    if python_path.name.lower() not in {"python.exe", "python"}:
        return None
    if not python_path.exists() or not python_path.is_file():
        return None
    return python_path


def _default_geng_python() -> str:
    homes = [os.environ.get("USERPROFILE"), os.environ.get("HOME")]
    for home in homes:
        if not home:
            continue
        candidate = Path(home) / "miniconda3" / "envs" / "torch" / ("python.exe" if sys.platform == "win32" else "bin/python")
        if candidate.exists():
            return str(candidate)
    return ""


def redact_text(text: str) -> str:
    redacted = text
    for pattern in SECRET_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def redact_data(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [redact_data(item) for item in value]
    if isinstance(value, dict):
        return {key: redact_data(item) for key, item in value.items()}
    return value


def validate_requirements(root: Path) -> list[dict[str, str]]:
    requirements = root / "requirements.txt"
    if not requirements.exists():
        return [{"file": "requirements.txt", "message": "requirements.txt is missing"}]

    issues: list[dict[str, str]] = []
    declared_packages: set[str] = set()
    for line_no, raw_line in enumerate(requirements.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-") or "://" in line or "@" in line or "\\" in line or "/" in line:
            issues.append({"file": "requirements.txt", "line": str(line_no), "message": f"disallowed requirement syntax: {line}"})
            continue
        package = re.split(r"[<>=!~\[]", line, maxsplit=1)[0].strip().lower().replace("_", "-")
        declared_packages.add(package)
        if package not in ALLOWED_REQUIREMENTS:
            issues.append({"file": "requirements.txt", "line": str(line_no), "message": f"package is not whitelisted: {package}"})
            continue
        for import_name in import_names_for_requirement(package):
            if importlib.util.find_spec(import_name) is None:
                issues.append(
                    {
                        "file": "requirements.txt",
                        "line": str(line_no),
                        "message": f"declared package is not installed in current environment: {package} (import {import_name})",
                    }
                )
    issues.extend(validate_import_requirements(root, declared_packages))
    return issues


def split_requirement_issues(issues: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split dependency findings into runner-blocking issues and softer warnings.

    In the task-writer workflow, a writer may already have produced trusted full-run
    artifacts. A missing declaration for a whitelisted package that is installed in the
    active environment is reproducibility metadata debt, not evidence that the run failed.
    Unknown packages, missing installations, unsafe requirement syntax, and broad import
    fallbacks remain blocking.
    """
    blocking: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    for issue in issues:
        target = warnings if is_nonblocking_requirement_issue(issue) else blocking
        item = dict(issue)
        item.setdefault("severity", "warning" if target is warnings else "error")
        target.append(item)
    return blocking, warnings


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


def is_nonblocking_requirement_issue(issue: dict[str, Any]) -> bool:
    message = str(issue.get("message") or "")
    match = _UNDECLARED_IMPORT_RE.fullmatch(message)
    if match:
        package = match.group(1).strip().lower().replace("_", "-")
        return package in ALLOWED_REQUIREMENTS and _requirement_imports_available(package)
    if message == "trusted torch backend is used but requirements.txt does not declare torch":
        return "torch" in ALLOWED_REQUIREMENTS and importlib.util.find_spec("torch") is not None
    return False


def _requirement_imports_available(package: str) -> bool:
    try:
        return all(importlib.util.find_spec(name) is not None for name in import_names_for_requirement(package))
    except (ImportError, ValueError):
        return False


def reconcile_whitelisted_requirements(root: Path) -> list[str]:
    """Auto-add to requirements.txt any third-party import that maps to a WHITELISTED and
    INSTALLED package but was left undeclared.

    Rationale (Bug A): the generator/repair sometimes imports e.g. ``scipy.linalg`` while
    forgetting to list ``scipy`` in requirements.txt, and the consistency gate then refuses
    to run it. For a whitelisted+installed library that omission is harmless friction, not a
    security risk, so we normalise it by declaring it. Imports that are NOT whitelisted, or
    whitelisted but NOT installed, are intentionally left undeclared so the gate still blocks
    them. Returns the package names that were added (empty if nothing changed)."""
    req_path = root / "requirements.txt"
    declared: set[str] = set()
    existing: list[str] = []
    if req_path.exists():
        for raw in req_path.read_text(encoding="utf-8", errors="replace").splitlines():
            existing.append(raw)
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            pkg = re.split(r"[<>=!~\[]", line, maxsplit=1)[0].strip().lower().replace("_", "-")
            declared.add(pkg)

    local_modules = collect_local_module_roots(root)
    to_add: set[str] = set()
    for py_file in iter_project_python_files(root):
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for import_ref in collect_third_party_imports(tree, local_modules):
            package = requirement_name_for_import(import_ref["root"])
            if package in ALLOWED_REQUIREMENTS and package not in declared and all(
                importlib.util.find_spec(name) is not None
                for name in import_names_for_requirement(package)
            ):
                to_add.add(package)
        if (
            uses_trusted_torch_backend(tree)
            and "torch" not in declared
            and "torch" in ALLOWED_REQUIREMENTS
            and importlib.util.find_spec("torch") is not None
        ):
            to_add.add("torch")

    if not to_add:
        return []
    added = sorted(to_add)
    lines = [line for line in existing if line.strip()]
    lines.extend(added)
    req_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return added


def dependency_policy_prompt_text() -> str:
    available = []
    unavailable = []
    for package in sorted(ALLOWED_REQUIREMENTS):
        import_names = sorted(import_names_for_requirement(package))
        item = f"{package} (import: {', '.join(import_names)})"
        if all(importlib.util.find_spec(import_name) is not None for import_name in import_names):
            available.append(item)
        else:
            unavailable.append(item)

    lines = [
        "依赖与 import 规则：",
        "1. Python 标准库和本项目本地模块不需要写入 requirements.txt。",
        "2. 第三方库只能从“当前环境已安装且允许使用”的清单中选择；清单外的库不要 import，改用标准库/本地实现/更简单的近似模型。",
        "3. 只要 Python 代码里出现第三方 import，就必须在 requirements.txt 里写对应包名，一行一个包名。",
        "4. requirements.txt 只写纯包名，不写版本号、URL、本地路径、VCS 地址、安装参数或解释性文字。",
        "5. 不要用 broad try/except 包住第三方 import 来静默降级；缺库时应避免使用该库，而不是隐藏错误。",
        "6. 标准通信原语优先调库、不要手搓：调制/解调、标准信道（AWGN/瑞利/莱斯）、信道编码与译码（卷积/Turbo/LDPC/RS/BCH）、滤波/重采样等成熟原语，优先调用下面“已安装且允许使用”清单中的库，不要从零手写——手搓 QAM 星座、LDPC 译码、信道实现极易出微妙的数学/物理错（星座不对称、功率未归一、概率越界等）。典型映射（仅在对应库可用时）：调制·标准信道·卷积/Turbo/LDPC → commpy；有限域·BCH·RS·LDPC 校验矩阵 → galois，纯 RS → reedsolo；滤波·FFT·窗 → scipy.signal / numpy.fft；预编码·SVD·特征值·pinv → numpy.linalg / scipy.linalg。仅当（a）论文方法与库实现确有差异、或（b）清单中无对应库时才手写，并在 assumptions 注明；绝不要为了“用上库”而把论文真正的自定义算法替换成库的标准版本。",
        "当前环境已安装且允许使用：",
    ]
    lines.extend(f"- {item}" for item in available)
    if unavailable:
        lines.append("白名单中但当前环境未安装，默认不要使用：")
        lines.extend(f"- {item}" for item in unavailable)
    lines.append(
        "Architecture execution contract precedence: if scientific_architecture/1.1 "
        "requires a framework that is unavailable or disallowed, report an explicit "
        "environment/architecture capability gap. Never silently replace it with a "
        "standard-library placeholder or a scientifically weaker approximation."
    )
    return "\n".join(lines)


def validate_import_requirements(root: Path, declared_packages: set[str]) -> list[dict[str, str]]:
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
            package = requirement_name_for_import(import_ref["root"])
            if package not in declared_packages:
                issues.append(
                    {
                        "file": rel,
                        "line": str(import_ref["line"]),
                        "message": f"third-party import is not declared in requirements.txt: {import_ref['name']} (expected package {package})",
                    }
                )
            elif importlib.util.find_spec(import_ref["root"]) is None:
                issues.append(
                    {
                        "file": rel,
                        "line": str(import_ref["line"]),
                        "message": f"third-party import is declared but not installed in current environment: {import_ref['name']}",
                    }
                )

        if uses_trusted_torch_backend(tree):
            if "torch" not in declared_packages:
                issues.append(
                    {
                        "file": rel,
                        "line": "0",
                        "message": "trusted torch backend is used but requirements.txt does not declare torch",
                    }
                )
            elif importlib.util.find_spec("torch") is None:
                issues.append(
                    {
                        "file": rel,
                        "line": "0",
                        "message": "trusted torch backend is requested but torch is not installed in current environment",
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
                            "message": f"third-party import is guarded by broad try/except and may silently degrade scientific behavior: {import_ref['name']}",
                        }
                    )
    return issues


def import_names_for_requirement(package: str) -> set[str]:
    return REQUIREMENT_IMPORT_NAMES.get(package, {package.replace("-", "_")})


def requirement_name_for_import(import_root: str) -> str:
    return IMPORT_REQUIREMENT_NAMES.get(import_root, import_root.lower().replace("_", "-"))


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

def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _call_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _check_absolute_path_literal(node: ast.Call, rel: str, issues: list[dict[str, str]]) -> None:
    if not node.args:
        return
    first = node.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        value = first.value
        if re.match(r"^[A-Za-z]:[\\/]", value) or value.startswith(("/", "\\")):
            issues.append({"file": rel, "line": str(getattr(node, "lineno", 0)), "message": f"absolute path literal is forbidden: {value}"})
