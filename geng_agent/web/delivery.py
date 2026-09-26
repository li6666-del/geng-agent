"""Export the two reports and a clean, runnable directory for each task."""
from __future__ import annotations

import json
import os
import re
import uuid
import zipfile
from pathlib import Path

from geng_agent.artifact_paths import path_is_link
from geng_agent.final_delivery import collect_task_delivery, render_task_readme

DELIVERY_ROOT = "复现交付包"
REPORTS = {
    "result_review.docx": f"{DELIVERY_ROOT}/论文复现结果对比报告.docx",
    "reproduction_report.docx": f"{DELIVERY_ROOT}/本地复现报告.docx",
}


def local_file(root: Path, relative: str) -> Path:
    path = root / relative
    if Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise ValueError("交付路径必须位于案例目录内")
    path.resolve().relative_to(root.resolve())
    for part in (path, *path.parents):
        if part == root.parent:
            break
        if path_is_link(part):
            raise ValueError("交付文件不能通过链接引用其他目录")
    if not path.is_file():
        raise FileNotFoundError(relative)
    return path


def bundle_path(case_dir: Path, job_id: str) -> Path:
    return case_dir / "exports" / f"{uuid.UUID(job_id)}.zip"


def _task_directories(project: Path) -> list[tuple[str, str | None, Path]]:
    """Use existing task directories; an optional index only supplies task names."""
    labels: dict[str, str] = {}
    try:
        index_path = local_file(project, "package_index.json")
        index = json.loads(index_path.read_text(encoding="utf-8-sig"))
        entries = index.get("tasks", []) if isinstance(index, dict) else []
        for item in entries if isinstance(entries, list) else []:
            if isinstance(item, dict) and isinstance(item.get("directory"), str) and item.get("task_id"):
                labels[item["directory"].replace("\\", "/")] = str(item["task_id"])
    except (OSError, ValueError):
        pass
    packages = project / "task_packages"
    if path_is_link(packages):
        raise ValueError("复现任务目录不能是外部链接")
    tasks = []
    if packages.is_dir():
        for task_root in sorted(packages.iterdir()):
            if task_root.name.startswith("."):
                continue
            if path_is_link(task_root):
                raise ValueError("复现任务目录不能是外部链接")
            if task_root.is_dir():
                task_id = labels.get(task_root.relative_to(project).as_posix())
                task_id = task_id or re.sub(r"^t\d+_", "", task_root.name)
                tasks.append((task_root.name, task_id, task_root))
    # Older cases used a single project directory instead of task_packages.
    return tasks or [("t01_task", None, project)]


def build_delivery(case_dir: Path, job_id: str) -> Path:
    files = [(local_file(case_dir, source), target) for source, target in REPORTS.items()]
    project = case_dir / "repro_project"
    if not project.is_dir() or path_is_link(project):
        raise FileNotFoundError("repro_project")
    guides: list[tuple[str, str]] = []
    directories: list[str] = []
    for directory, task_id, task_root in _task_directories(project):
        prefix = f"{DELIVERY_ROOT}/复现任务/{directory}"
        selected = collect_task_delivery(task_root, case_root=case_dir, task_id=task_id)
        for item in selected:
            path = local_file(case_dir, item["source"].relative_to(case_dir).as_posix())
            folder = "复现结果" if item["role"] == "result" else "代码"
            files.append((path, f"{prefix}/{folder}/{item['relative_path']}"))
        directories.extend((f"{prefix}/代码/", f"{prefix}/复现结果/"))
        guides.append((f"{prefix}/readme.md", render_task_readme(task_id or "task", selected, task_root=task_root)))
    if len(files) == len(REPORTS):
        raise FileNotFoundError("复现项目中尚无可交付文件")
    destination = bundle_path(case_dir, job_id)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if path_is_link(destination.parent) or path_is_link(destination):
        raise ValueError("交付目录不能是外部链接")
    temp = destination.with_name(f"{destination.stem}-{uuid.uuid4().hex[:8]}.tmp")
    try:
        with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for name in directories:
                archive.writestr(name, "")
            for path, name in files:
                archive.write(path, arcname=name)
            for name, guide in guides:
                archive.writestr(name, guide.encode("utf-8"))
        os.replace(temp, destination)
    finally:
        temp.unlink(missing_ok=True)
    return destination
