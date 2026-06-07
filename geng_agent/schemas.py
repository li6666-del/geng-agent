from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .outputs import REQUIRED_REPRO_FILES, resolve_inside
from .schema_models import model_for_stage


SCHEMA_ROOT = Path("__schema_root__")


@dataclass(frozen=True)
class ValidationIssue:
    path: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"path": self.path, "message": self.message}


def validate_stage(stage: str, data: dict[str, Any]) -> list[ValidationIssue]:
    model = model_for_stage(stage)
    issues: list[ValidationIssue] = []
    try:
        # Loose JSON recovery annotates manifests with _meta. That metadata is useful
        # for risk reporting, but it is not part of the public schema contract.
        model.model_validate({key: value for key, value in data.items() if key != "_meta"})
    except ValidationError as exc:
        issues.extend(_issues_from_pydantic(exc))

    if stage == "repro_project_manifest":
        issues.extend(validate_manifest_business_rules(data, require_all=True, require_repair_meta=False))
    elif stage == "repair_manifest":
        issues.extend(validate_manifest_business_rules(data, require_all=False, require_repair_meta=True))

    return _dedupe_issues(issues)


def format_issues(issues: list[ValidationIssue]) -> str:
    return "\n".join(f"- {issue.path}: {issue.message}" for issue in issues)


def validate_fact_sources(
    data: dict[str, Any], valid_chunk_ids: set[str], valid_pages: set[int] | None = None
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    facts = data.get("engineering_facts")
    if not isinstance(facts, list):
        return issues
    for index, fact in enumerate(facts):
        if not isinstance(fact, dict):
            continue
        source = fact.get("source")
        if not isinstance(source, dict):
            continue
        base = f"$.engineering_facts[{index}].source"
        if source.get("source_kind") == "figure":
            # figure-sourced fact must cite a page the model actually saw (a rendered page image)
            page = source.get("page")
            if valid_pages is not None and (not isinstance(page, int) or page not in valid_pages):
                issues.append(ValidationIssue(f"{base}.page", "figure source must cite a rendered paper page"))
        else:
            chunk_id = source.get("chunk_id")
            if isinstance(chunk_id, str) and chunk_id not in valid_chunk_ids:
                issues.append(ValidationIssue(f"{base}.chunk_id", "must refer to paper_chunks.json"))
    return issues


def validate_task_fact_refs(tasks_data: dict[str, Any], facts_data: dict[str, Any]) -> list[ValidationIssue]:
    fact_keys = {
        (fact.get("type"), fact.get("name"))
        for fact in facts_data.get("engineering_facts", [])
        if isinstance(fact, dict)
    }
    issues: list[ValidationIssue] = []
    tasks = tasks_data.get("repro_tasks")
    if not isinstance(tasks, list):
        return issues
    for task_index, task in enumerate(tasks):
        if not isinstance(task, dict) or not isinstance(task.get("required_facts"), list):
            continue
        for ref_index, ref in enumerate(task["required_facts"]):
            if not isinstance(ref, dict):
                continue
            key = (ref.get("type"), ref.get("name"))
            if key not in fact_keys:
                issues.append(
                    ValidationIssue(
                        f"$.repro_tasks[{task_index}].required_facts[{ref_index}]",
                        "must refer to an extracted engineering fact",
                    )
                )
    return issues


def validate_manifest_business_rules(
    data: dict[str, Any],
    require_all: bool,
    require_repair_meta: bool,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    files = data.get("files")
    if not isinstance(files, list):
        return issues

    seen_paths: set[str] = set()
    manifest_paths: list[str] = []
    for index, item in enumerate(files):
        base = f"$.files[{index}]"
        if not isinstance(item, dict):
            continue
        present = [key for key in ("content", "content_lines", "content_b64") if key in item]
        if len(present) != 1:
            issues.append(ValidationIssue(base, "must contain exactly one of content, content_lines, content_b64"))

        path = item.get("path")
        if not isinstance(path, str) or not path.strip():
            continue
        normalized = _normalize_manifest_path(path, f"{base}.path", issues)
        if normalized is None:
            continue
        if normalized in seen_paths:
            issues.append(ValidationIssue(f"{base}.path", "duplicate path"))
        seen_paths.add(normalized)
        manifest_paths.append(normalized)

    if require_all:
        missing = sorted(REQUIRED_REPRO_FILES - seen_paths)
        for path in missing:
            issues.append(ValidationIssue("$.files", f"missing required file: {path}"))

    if require_repair_meta:
        touched = data.get("touched_files")
        if isinstance(touched, list):
            for index, path in enumerate(touched):
                if not isinstance(path, str) or not path.strip():
                    continue
                normalized = _normalize_manifest_path(path, f"$.touched_files[{index}]", issues)
                if normalized is not None and normalized not in manifest_paths:
                    issues.append(ValidationIssue(f"$.touched_files[{index}]", "must also appear in files[].path"))

    return issues


def _issues_from_pydantic(exc: ValidationError) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for error in exc.errors():
        loc = error.get("loc", ())
        path = _loc_to_json_path(loc)
        message = str(error.get("msg", "invalid value"))
        issues.append(ValidationIssue(path, message))
    return issues


def _loc_to_json_path(loc: tuple[Any, ...]) -> str:
    path = "$"
    for part in loc:
        if isinstance(part, int):
            path += f"[{part}]"
        elif isinstance(part, str):
            # Pydantic unions include branch model names in loc. They are helpful
            # for debugging but noisy for LLM retry prompts, so keep them as fields.
            if part.isidentifier():
                path += f".{part}"
            else:
                path += f"[{part!r}]"
    return path


def _normalize_manifest_path(path: str, issue_path: str, issues: list[ValidationIssue]) -> str | None:
    try:
        resolved = resolve_inside(SCHEMA_ROOT, path)
    except ValueError as exc:
        issues.append(ValidationIssue(issue_path, str(exc)))
        return None
    return resolved.relative_to(SCHEMA_ROOT.resolve()).as_posix()


def _dedupe_issues(issues: list[ValidationIssue]) -> list[ValidationIssue]:
    seen: set[tuple[str, str]] = set()
    deduped: list[ValidationIssue] = []
    for issue in issues:
        key = (issue.path, issue.message)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(issue)
    return deduped
