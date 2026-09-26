"""Select the human-facing task delivery without changing the scientific project."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import quote, urlsplit

from .artifact_paths import path_is_link


_CACHE_DIRS = {
    "__pycache__", "node_modules", "venv", "env", "envs", "site-packages",
    "cache", "caches", "tmp", "temp", "scratch", "debug", "repair_logs",
    "writer_progress",
}
_AUDIT_DIRS = {
    "audit", "execution_records", "task_notes", "task_requirements",
    "logs",
}
_MATERIAL_DIRS = {"paper_evidence", "full_paper_pages", "report_assets", "report_materials", "report_artwork"}
_HOST_FILES = {
    "package_index.json", "source_inventory.json", "execution_evidence.json",
    "execution_receipt.json", "artifact_lineage.json", "execution_plan.json",
    "execution_unit.json", "execution_unit_result.json", "task_agent_result.json",
    "task_agent_result.md", "reproducibility_manifest.json", "project_manifest.json",
    "package_manifest.json", "repro_project_manifest.json",
    "project_portability_manifest.json", "environment.lock.json",
    "writer_environment.lock.json", "environment_request.json", "installation.json",
    "reproduction_report.docx", "reproduction_report.md", "reproduction_report.json",
    "result_review.docx", "result_review.md", "result_review.json",
    "delivery_readme.md",
}
_RESULT_ROOTS = {"outputs", "results", "figures", "plots"}
_INPUT_ROOTS = {
    "data", "datasets", "inputs", "assets", "resources", "checkpoints",
    "models", "weights", "execution_units",
}
_CONFIG_SUFFIXES = {".json", ".yaml", ".yml", ".toml", ".ini", ".cfg"}
_SECRET_STEMS = {
    "credentials", "secrets", "secret", "token", "tokens", "passwords",
    "private_key", "api_key", "service_account", "id_rsa", "id_ed25519",
}
_MISSING_DESCRIPTION = "该旧交付未提供此文件的用途说明。"


def _relative(raw: Any) -> str | None:
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        return None
    value = raw.replace("\\", "/")
    while value.startswith("./"):
        value = value[2:]
    posix, windows = PurePosixPath(value), PureWindowsPath(value)
    if (not value or posix.is_absolute() or windows.drive or windows.root
            or ":" in value or any(part in {"", ".", ".."} for part in value.split("/"))):
        return None
    return posix.as_posix()


def _private_path(relative: str) -> bool:
    parts = PurePosixPath(relative).parts
    return any(
        part.startswith(".") or Path(part).stem.casefold() in _SECRET_STEMS
        or Path(part).suffix.casefold() in {".pem", ".key", ".p12", ".pfx"}
        for part in parts
    )


def _safe_file(root: Path, relative: str) -> Path | None:
    relative = _relative(relative)
    if relative is None or _private_path(relative):
        return None
    path = root
    try:
        for part in PurePosixPath(relative).parts:
            path = path / part
            if path_is_link(path):
                return None
        path.resolve().relative_to(root.resolve())
        return path if path.is_file() else None
    except (OSError, ValueError):
        return None


def _files(root: Path) -> dict[str, Path]:
    files = {}
    for directory, dirs, names in os.walk(root, followlinks=False):
        parent = Path(directory)
        dirs[:] = sorted(name for name in dirs
                         if not name.startswith(".") and name.casefold() not in _CACHE_DIRS
                         and not path_is_link(parent / name))
        for name in sorted(names):
            relative = (parent / name).relative_to(root).as_posix()
            path = _safe_file(root, relative)
            if path is not None:
                files[relative] = path
    return files


def _json(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, UnicodeError):
        return {}


def _excluded(relative: str, *, observed_input: bool = False) -> bool:
    path = PurePosixPath(relative)
    parts = [part.casefold() for part in path.parts]
    name = parts[-1]
    if (_private_path(relative) or name in _HOST_FILES
            or path.suffix.casefold() in {".pyc", ".pyo", ".log", ".tmp", ".bak", ".old", ".part"}
            or any(part in _CACHE_DIRS for part in parts[:-1])
            or any(part in _AUDIT_DIRS for part in parts[:-1])
            or (path.suffix.casefold() == ".py" and re.match(r"(?:debug|scratch|tmp|temp)[_-]", name))):
        return True
    # Original paper pages and report illustrations are runtime inputs only if
    # an execution receipt actually identifies them as consumed inputs.
    return not observed_input and (any(part in _MATERIAL_DIRS for part in parts[:-1])
                                   or name in {"paper_target_crop.png", "paper_target_locator.png"})


def _legacy_code(relative: str, *, include_inputs: bool) -> bool:
    path = PurePosixPath(relative)
    root_name = path.parts[0].casefold()
    if root_name in {"src", "tasks", "configs"} or (include_inputs and root_name in _INPUT_ROOTS):
        return True
    if len(path.parts) != 1:
        return False
    name = path.name.casefold()
    return (path.suffix.casefold() == ".py"
            or name == "tasks_manifest.json"
            or (name.startswith(("requirements", "constraints")) and name.endswith(".txt"))
            or (path.suffix.casefold() in _CONFIG_SUFFIXES
                and ("config" in path.stem.casefold()
                     or path.stem.casefold() in {"params", "parameters", "settings", "hyperparameters"})))


def _smoke_result(relative: str) -> bool:
    path = PurePosixPath(relative)
    return any(re.search(r"(?:^|[_-])smoke(?:$|[_-])", part.casefold())
               for part in (*path.parts[:-1], path.stem))


def _observed_inputs(root: Path, files: dict[str, Path], task_id: str | None) -> dict[str, Path]:
    """Map original consumed paths to available bytes, including archived inputs."""
    inputs: dict[str, Path] = {}
    evidence = _json(files.get("execution_evidence.json"))
    tasks = evidence.get("tasks")
    receipts = [path for relative, path in files.items()
                if PurePosixPath(relative).name == "execution_receipt.json"]
    for task in tasks if isinstance(tasks, list) else []:
        if not isinstance(task, dict) or (task_id is not None and str(task.get("task_id") or "") != task_id):
            continue
        receipt = _safe_file(root, task.get("receipt"))
        if receipt is not None:
            receipts.append(receipt)
        mappings = task.get("files")
        for item in mappings if isinstance(mappings, list) else []:
            if not isinstance(item, dict) or item.get("kind") != "input_hashes":
                continue
            original = _relative(item.get("original_path"))
            if original is None or _excluded(original, observed_input=True):
                continue
            source = (_safe_file(root, item.get("packaged_path"))
                      or _safe_file(root, original))
            if source is not None:
                inputs[original] = source
    for path in sorted(set(receipts)):
        receipt = _json(path)
        if (receipt.get("observer") != "orchestration_host"
                or (task_id is not None and str(receipt.get("task_id") or "") != task_id)):
            continue
        hashes = receipt.get("input_hashes")
        for raw in hashes if isinstance(hashes, dict) else []:
            relative = _relative(raw)
            if relative is None or _excluded(relative, observed_input=True):
                continue
            source = _safe_file(root, relative)
            if source is not None:
                inputs.setdefault(relative, source)
    return inputs


def _readme_descriptions(files: dict[str, Path]) -> dict[str, str]:
    descriptions: dict[str, str] = {}
    for relative, path in sorted(files.items()):
        if not (relative == "README.md" or relative.startswith("task_notes/")) or path.suffix.lower() != ".md":
            continue
        try:
            with path.open(encoding="utf-8-sig", errors="replace") as handle:
                text = handle.read(64000)
        except OSError:
            continue
        for line in text.splitlines():
            match = re.search(r"`([^`]+)`\s*(?:\||[:：]|[—–-])\s*(.+)", line)
            if not match:
                match = re.search(r"\[([^]]+)\]\(([^)]+)\)\s*(?:\||[:：]|[—–-])\s*(.+)", line)
                if match:
                    candidate, description = match.group(2), match.group(3)
                else:
                    match = re.match(r"\s*\|\s*([^|]+?)\s*\|\s*([^|]+)", line)
                    if not match:
                        match = re.match(r"\s*[-*]?\s*([^\s]+\.[A-Za-z0-9]+)\s*[:：]\s*(.+)", line)
                    if not match:
                        continue
                    candidate, description = match.group(1), match.group(2)
            else:
                candidate, description = match.group(1), match.group(2)
            candidate = _relative(candidate)
            description = description.strip(" |")
            # Generated task indexes contain config/output columns, not file
            # descriptions. Keep only actual existing prose descriptions.
            bare = description.strip("`")
            if (re.fullmatch(r"\[[^]]+\]\([^)]+\)", bare)
                    or (not re.search(r"\s", bare) and "/" in bare
                        and _relative(bare.rstrip("/")) is not None)):
                continue
            if candidate and description:
                descriptions.setdefault(candidate, description)
    return descriptions


def _description(relative: str, notes: dict[str, str]) -> str:
    if relative in notes:
        return notes[relative]
    fixed = {
        "run_experiment.py": "运行本任务完整实验的入口，读取所选配置并调用任务实现。",
        "run_task.py": "按任务标识和配置启动实验的入口。",
        "tasks_manifest.json": "任务运行配置，记录任务模块及配置文件位置。",
        "requirements.txt": "Python 依赖包列表。",
        "requirements.repro.txt": "用于重建运行环境的依赖安装入口。",
        "constraints.repro.txt": "已记录的依赖包版本约束。",
        "config.json": "完整实验的入口配置。",
        "config_smoke.json": "小规模运行的入口配置。",
    }
    return fixed.get(relative, _MISSING_DESCRIPTION)


def collect_task_delivery(task_root: Path, *, case_root: Path, task_id: str | None) -> list[dict]:
    """Return delivery files; None includes all tasks in a legacy single project."""
    root, case = Path(task_root).absolute(), Path(case_root).absolute()
    try:
        root.relative_to(case)
        root.resolve().relative_to(case.resolve())
    except ValueError as exc:
        raise ValueError("任务交付目录必须位于案例目录内") from exc
    cursor = root
    while True:
        if path_is_link(cursor):
            raise ValueError("任务交付目录不能通过链接引用其他目录")
        if cursor == case:
            break
        cursor = cursor.parent
    if not root.is_dir():
        return []
    files = _files(root)
    notes = _readme_descriptions(files)
    observed_inputs = _observed_inputs(root, files, task_id)
    selected: dict[tuple[str, str], dict] = {}

    def add(relative: str, source: Path, role: str, description: Any = None) -> None:
        existing = selected.get((role, relative))
        supplied = description.strip() if isinstance(description, str) else ""
        selected[(role, relative)] = {
            "source": source, "relative_path": relative, "role": role,
            "description": supplied or (existing or {}).get("description")
            or _description(relative, notes),
        }

    declared = 0
    incomplete = False
    for relative, path in sorted(files.items()):
        if not (relative.startswith("outputs/") and path.name == "task_agent_result.json"):
            continue
        document = _json(path)
        if task_id is not None and str(document.get("task_id") or "") != task_id:
            continue
        entries = document.get("delivery_files")
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict):
                incomplete = True
                continue
            original = _relative(entry.get("path"))
            role = entry.get("role")
            source = _safe_file(root, original) if original else None
            if (original is None or source is None or not isinstance(role, str)
                    or role not in {"code", "input", "result"}):
                incomplete = True
                continue
            if _excluded(original, observed_input=original in observed_inputs or role == "input"):
                continue
            add(original, source, "code" if role == "input" else role, entry.get("description"))
            declared += 1

    # Keep the runnable scaffold/dependency tree even when a Writer lists only
    # its figures. An incomplete optional note must not trigger another run.
    for relative, path in sorted(files.items()):
        if _excluded(relative):
            continue
        fallback = not declared or incomplete
        if (_legacy_code(relative, include_inputs=False)
                or (_legacy_code(relative, include_inputs=fallback) and ("result", relative) not in selected)):
            add(relative, path, "code")
        elif fallback and PurePosixPath(relative).parts[0].casefold() in _RESULT_ROOTS:
            if path.name.casefold() != "manifest.json" and not _smoke_result(relative):
                add(relative, path, "result")
    for relative, source in observed_inputs.items():
        add(relative, source, "code")
    return [selected[key] for key in sorted(selected)]


def _installation_guidance(task_root: Path | None) -> tuple[dict, list[str], list[str]]:
    if task_root is None or path_is_link(Path(task_root)):
        return {}, [], []
    document = _json(_safe_file(Path(task_root), "installation.json"))
    indexes: list[str] = []
    raw_indexes = document.get("indexes")
    candidates = list(raw_indexes) if isinstance(raw_indexes, list) else []
    builds = document.get("accelerator_sources")
    for build in builds if isinstance(builds, list) else []:
        if isinstance(build, dict):
            candidates.append(build.get("url"))
    for raw in candidates:
        if not isinstance(raw, str):
            continue
        try:
            parsed = urlsplit(raw)
            if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                    or parsed.query or parsed.fragment):
                continue
            # Never reproduce credentials, local paths or shell syntax from an
            # environment record. Public HTTPS indexes remain usable by pip.
            url = quote(raw, safe="/:@.%+-_~")
            if url not in indexes:
                indexes.append(url)
        except ValueError:
            continue
    notes = []
    interpreter = document.get("python")
    version = interpreter.get("python_full_version") if isinstance(interpreter, dict) else None
    if isinstance(version, str) and re.fullmatch(r"\d+\.\d+(?:\.\d+)?(?:[a-z]+\d+)?", version):
        notes.append(f"原运行环境记录的 Python 版本为 `{version}`。")
    if indexes:
        notes.append("依赖安装使用下列已记录的公共软件源：" + "、".join(f"`{url}`" for url in indexes) + "。")
    if isinstance(builds, list) and builds:
        notes.append("加速计算依赖应保持约束文件记录的 CUDA、ROCm 或 CPU 构建版本，并使用对应软件源；不同平台需选择兼容构建。")
    if document.get("metadata_dependency_closure") is False:
        notes.append("环境记录未完整解析传递依赖；现有约束仅覆盖已记录的依赖版本。")
    if document.get("observed_execution_version_mismatches"):
        notes.append("导出的部分依赖版本与原执行记录不同；环境重建时需留意该差异。")
    if document.get("warnings"):
        notes.append("环境导出记录含安装限制，部分依赖或软件源可能需要按所用平台补充。")
    return document, indexes, notes


def render_task_readme(task_id: str, files: list[dict], *, task_root: Path | None = None) -> str:
    """Pass through the Writer's guide, or retain existing descriptions for old packages."""
    if task_root is not None and not path_is_link(Path(task_root)):
        guide = _safe_file(Path(task_root), "delivery_readme.md")
        if guide is not None:
            try:
                text = guide.read_bytes().decode("utf-8")
                if text:
                    return text
            except (OSError, UnicodeError):
                pass
    code = {item["relative_path"] for item in files if item.get("role") == "code"}
    lines = [f"# 任务 {task_id}", "", "本任务未提供完整中文导读。以下沿用已有文件说明；未记录的用途明确标注。", ""]
    for role, heading, prefix in (("code", "代码说明", "代码"), ("result", "结果说明", "复现结果")):
        lines += [f"## {heading}", ""]
        entries = [item for item in files if item.get("role") == role]
        entries.sort(key=lambda item: (not item.get("description") or item.get("description") == _MISSING_DESCRIPTION,
                                       item["relative_path"]))
        if not entries:
            lines += ["暂无可交付文件。", ""]
            continue
        lines += ["| 文件 | 内容与用途 |", "| --- | --- |"]
        for item in entries:
            relative = prefix + "/" + item["relative_path"]
            label = relative.replace("|", "\\|").replace("[", "\\[").replace("]", "\\]")
            description = " ".join(str(item.get("description") or _MISSING_DESCRIPTION).split()).replace("|", "\\|")
            lines.append(f"| [{label}]({quote(relative, safe='/')}) | {description} |")
        lines.append("")

    lines += ["## 安装与运行", ""]
    installation, indexes, install_notes = _installation_guidance(task_root)
    if install_notes:
        lines += [*install_notes, ""]
    commands = ['cd "代码"']
    install_file = _relative(installation.get("install_file"))
    requirements = next((name for name in (install_file, "requirements.repro.txt", "requirements.txt")
                         if name is not None and name in code), None)
    if requirements is not None:
        command = f'python -m pip install -r "{requirements}"'
        for index, url in enumerate(indexes):
            command += (' --index-url ' if index == 0 else ' --extra-index-url ') + f'"{url}"'
        commands.append(command)
    if "run_experiment.py" in code:
        commands.append("python run_experiment.py" + (" config.json" if "config.json" in code else ""))
    elif "run_task.py" in code and "tasks_manifest.json" in code:
        commands.append("python run_task.py --task " + json.dumps(str(task_id), ensure_ascii=False) + " --mode full")
    if len(commands) > 1:
        lines += ["在任务目录打开终端，进入代码目录后安装依赖并运行：", "", "```sh", *commands, "```", ""]
    lines += ["新运行会按原程序约定写入 `代码/outputs/` 或其他原定目录；"
              "`复现结果/` 是已有结果的交付副本。若某个结果同时是计算所需输入，代码目录也保留其原路径副本。", "",
              "各文件保留原项目内部相对路径，便于代码读取，也避免同名文件相互覆盖。", ""]
    return "\n".join(lines) + "\n"
