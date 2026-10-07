"""Export the two reports and a clean, runnable directory for each task."""
from __future__ import annotations

import os
import uuid
import zipfile
from pathlib import Path

from geng_agent.artifact_paths import path_is_link
from geng_agent.final_delivery import (
    DELIVERY_ROOT, REPORTS, collect_task_delivery, render_task_readme,
    task_delivery_directories as _task_directories, task_download_paths,
)


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


def build_delivery(case_dir: Path, job_id: str) -> Path:
    files = [(local_file(case_dir, source), target) for source, target in REPORTS.items()]
    project = case_dir / "repro_project"
    if not project.is_dir() or path_is_link(project):
        raise FileNotFoundError("repro_project")
    guides: list[tuple[str, str]] = []
    directories: list[str] = []
    for directory, task_id, task_root in _task_directories(project):
        paths = task_download_paths(directory)
        selected = collect_task_delivery(task_root, case_root=case_dir, task_id=task_id)
        for item in selected:
            path = local_file(case_dir, item["source"].relative_to(case_dir).as_posix())
            folder = paths["results_directory"] if item["role"] == "result" else paths["code_directory"]
            files.append((path, f"{folder}/{item['relative_path']}"))
        directories.extend((f"{paths['code_directory']}/", f"{paths['results_directory']}/"))
        guides.append((paths["readme_path"], render_task_readme(task_id or "task", selected, task_root=task_root)))
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
