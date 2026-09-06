"""Data-only policy and shared AST naming for static security checks."""

from __future__ import annotations

import ast


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
TRUSTED_RUNTIME_FILES = {
    "src/_io.py",
    "src/_backend.py",
}


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _call_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""
