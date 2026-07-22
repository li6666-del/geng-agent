from __future__ import annotations

import csv
import hashlib
import json
import mimetypes
import os
import uuid
import zipfile
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from geng_agent.progress import phase_for_step

from .models import ArtifactRecord, CaseRecord


STEP_FILES: dict[str, tuple[str, ...]] = {
    "paper": ("paper_chunks.json", "paper/", "paper_figure_index.json"),
    "facts_initial": ("engineering_facts_initial.json",),
    "facts": (
        "engineering_facts_backfill.json",
        "engineering_facts.json",
        "analysis_warnings.json",
    ),
    "repro_tasks": ("repro_tasks_preliminary.json", "repro_tasks.json"),
    "experiment_index": (
        "experiment_index.json",
        "paper_thesis.json",
        "fact_conflicts.json",
        "task_conflicts.json",
    ),
    "scientific_architecture": ("scientific_architecture.json",),
    "foundation_manifest": ("foundation_manifest.json", "audit/03b_foundation_snapshot/"),
    "repro_project_manifest": ("repro_project_manifest.json",),
    "repro_project": (
        "repro_project/",
        "audit/03c_task_writer_sandboxes/",
        "audit/04a_task_reporters/",
    ),
    "runtime": ("runtime_result.json",),
    "verification_result": ("verification_result.json",),
    "result_review": ("report_assets/", "reproduction_report.md", "result_review.md", "review.md"),
    "reports": (
        "reproduction_report.docx",
        "result_review.docx",
        "review.docx",
        "risk_report.json",
        "generated_files.json",
        "run_cost.json",
        "automation_provenance.json",
    ),
}

LIVE_AUDIT_GLOBS: tuple[str, ...] = (
    "audit/03c_task_writer_sandboxes/*/outputs/**/*",
    "audit/03c_task_writer_sandboxes/*/task_agent_result.json",
    "audit/03c_task_writer_sandboxes/*/task_agent_result.md",
    "audit/04a_task_reporters/*/round_*/report_assets/**/*",
    "audit/04a_task_reporters/*/round_*/task_verification_result.json",
)


class UnsafeArtifactPath(ValueError):
    pass


class LocalArtifactStore:
    def __init__(self, case_dir: Path) -> None:
        self.case_dir = case_dir.resolve()

    def resolve(self, relative_path: str, *, must_exist: bool = True) -> Path:
        if not relative_path or Path(relative_path).is_absolute():
            raise UnsafeArtifactPath("产物路径必须是案例内的相对路径")
        normalized = Path(relative_path.replace("\\", "/"))
        if ".." in normalized.parts:
            raise UnsafeArtifactPath("产物路径不能包含上级目录")
        target = (self.case_dir / normalized).resolve(strict=False)
        try:
            target.relative_to(self.case_dir)
        except ValueError as exc:
            raise UnsafeArtifactPath("产物路径越过案例目录") from exc
        if target.is_symlink() or any(part.is_symlink() for part in target.parents if part != self.case_dir.parent):
            raise UnsafeArtifactPath("不允许通过符号链接访问产物")
        if must_exist and (not target.exists() or not target.is_file()):
            raise FileNotFoundError(relative_path)
        return target

    def iter_files(self) -> Iterable[Path]:
        if not self.case_dir.exists():
            return
        yielded: set[Path] = set()
        for path in self.case_dir.rglob("*"):
            if not path.is_file() or path.is_symlink():
                continue
            rel = path.relative_to(self.case_dir)
            if any(part.startswith(".") for part in rel.parts) or rel.parts[0] in {"audit", "exports"}:
                continue
            yielded.add(path)
            yield path
        for pattern in LIVE_AUDIT_GLOBS:
            for path in self.case_dir.glob(pattern):
                if path in yielded or not path.is_file() or path.is_symlink():
                    continue
                yielded.add(path)
                yield path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def artifact_kind(path: Path) -> str:
    suffix = path.suffix.lower()
    return {
        ".png": "image",
        ".jpg": "image",
        ".jpeg": "image",
        ".csv": "csv",
        ".json": "json",
        ".md": "markdown",
        ".py": "code",
        ".toml": "code",
        ".txt": "text",
        ".docx": "document",
        ".pdf": "document",
        ".zip": "archive",
    }.get(suffix, "file")


def phase_for_path(relative_path: str) -> str:
    normalized = relative_path.replace("\\", "/")
    best: tuple[int, str] | None = None
    for step, prefixes in STEP_FILES.items():
        for prefix in prefixes:
            if normalized == prefix.rstrip("/") or normalized.startswith(prefix):
                candidate = (len(prefix), phase_for_step(step))
                if best is None or candidate[0] > best[0]:
                    best = candidate
    return best[1] if best else "task_reproduction"


def catalog_case_artifacts(session: Session, case: CaseRecord) -> list[ArtifactRecord]:
    store = LocalArtifactStore(Path(case.directory))
    existing = {
        item.relative_path: item
        for item in session.scalars(
            select(ArtifactRecord).where(ArtifactRecord.case_id == case.id)
        ).all()
    }
    seen: set[str] = set()
    records: list[ArtifactRecord] = []
    for path in store.iter_files():
        rel = path.relative_to(store.case_dir).as_posix()
        seen.add(rel)
        try:
            before = path.stat()
            sha256 = _sha256(path)
            after = path.stat()
        except OSError:
            # A writer may atomically replace an output while a live catalog pass is
            # walking the case. Keep any previous record and retry at the next boundary.
            continue
        if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
            continue
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        record = existing.get(rel)
        if record is None:
            record = ArtifactRecord(
                id=str(uuid.uuid4()),
                case_id=case.id,
                relative_path=rel,
            )
            session.add(record)
        record.phase = phase_for_path(rel)
        record.kind = artifact_kind(path)
        record.mime_type = mime
        record.size_bytes = after.st_size
        record.sha256 = sha256
        records.append(record)
    for rel, record in existing.items():
        if rel not in seen:
            session.delete(record)
    session.commit()
    return records


def preview_artifact(path: Path, kind: str) -> dict[str, Any] | None:
    if path.stat().st_size > 2 * 1024 * 1024:
        return None
    if kind == "csv":
        with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
            rows = []
            reader = csv.reader(handle)
            for index, row in enumerate(reader):
                rows.append(row[:20])
                if index >= 20:
                    break
        return {"rows": rows}
    if kind == "json":
        try:
            return {"json": json.loads(path.read_text(encoding="utf-8"))}
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
    if kind in {"markdown", "code", "text"}:
        return {"text": path.read_text(encoding="utf-8", errors="replace")[:100_000]}
    return None


def build_zip(store: LocalArtifactStore, files: Iterable[str], destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_suffix(".tmp")
    with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for relative_path in sorted(set(files)):
            source = store.resolve(relative_path)
            archive.write(source, arcname=relative_path)
    os.replace(temp, destination)


def manifest_hash(artifacts: Iterable[ArtifactRecord]) -> str:
    digest = hashlib.sha256()
    for item in sorted(artifacts, key=lambda value: value.relative_path):
        digest.update(f"{item.relative_path}\0{item.sha256}\n".encode())
    return digest.hexdigest()
