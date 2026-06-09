from __future__ import annotations

import ast
import base64
import json
from pathlib import Path
from typing import Any

"""Repro-project manifest / per-file normalization and validation: path safety, required-
file checks, content-type (Python/JSON) checks, size limits, and loose-recovery helpers."""

from .json_utils import parse_json_object, pretty_json
from .outputs import REQUIRED_REPRO_FILES, resolve_inside
from .schemas import ValidationIssue, validate_stage


REPRO_PROJECT_FILE_ORDER = [
    "requirements.txt",
    "config.json",
    "config_smoke.json",
    "src/channel.py",
    "src/modulation.py",
    "src/metrics.py",
    "src/simulation.py",
    "run_experiment.py",
    "README.md",
]


# Size limits apply to NON-code files only. Generated CODE (.py) files are intentionally
# left UNCAPPED (no line or char limit): a too-tight size cap on code was the single biggest
# template-fallback trigger, and a long-but-real reproduction beats discarding it for length.
# Code conciseness is steered by the generation prompt and code review, not a hard cap; .py
# files are still required to compile (ast check in _content_type_issues).
REPRO_PROJECT_FILE_LIMITS = {
    "README.md": {"lines": 200, "chars": 20000},
    "requirements.txt": {"lines": 200, "chars": 4000},
    "config.json": {"lines": 200, "chars": 20000},
    "config_smoke.json": {"lines": 200, "chars": 20000},
}


# Per-task layout: the model generates the shared science + configs + docs, plus ONE
# tasks/<module>.py per repro_task. It does NOT generate run_experiment.py (the harness
# injects a deterministic dispatcher), tasks_manifest.json, or src/_io.py (both injected).
SHARED_GENERATED_FILES = REQUIRED_REPRO_FILES - {"run_experiment.py"}
# Generate shared science modules before the rest so per-task scripts (appended after) and
# the dispatcher see the real src/ interfaces.
SHARED_GENERATION_ORDER = [path for path in REPRO_PROJECT_FILE_ORDER if path != "run_experiment.py"]


def expected_generated_paths(task_scripts: list[str] | None) -> set[str]:
    """Full set of MODEL-generated paths for a per-task project: shared files + one
    tasks/<module>.py per repro_task. run_experiment.py / tasks_manifest.json / src/_io.py
    are harness-injected (not generated), so they are intentionally absent here."""
    return set(SHARED_GENERATED_FILES) | set(task_scripts or [])


def _manifest_paths(manifest: dict[str, Any], repro_project_dir: Path) -> list[Path]:
    files = manifest.get("files", [])
    if not isinstance(files, list):
        return []
    paths = []
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            continue
        try:
            paths.append(resolve_inside(repro_project_dir, item["path"]))
        except ValueError:
            continue
    return paths


def _ordered_project_paths(plan: dict[str, Any], task_scripts: list[str] | None = None) -> list[str]:
    planned = {
        _normalize_manifest_path_for_pipeline(item.get("path"))
        for item in plan.get("files", [])
        if isinstance(item, dict)
    }
    if task_scripts is None:
        # Legacy single-script layout.
        return [path for path in REPRO_PROJECT_FILE_ORDER if path in planned]
    # Per-task layout: shared science/configs/docs first, then each tasks/<module>.py.
    ordered = [path for path in SHARED_GENERATION_ORDER if path in planned]
    ordered += [script for script in task_scripts if script in planned]
    return ordered


def _validate_project_plan_paths(
    plan: dict[str, Any], expected_paths: set[str] | None = None
) -> list[ValidationIssue]:
    expected = set(expected_paths) if expected_paths is not None else REQUIRED_REPRO_FILES
    issues: list[ValidationIssue] = []
    files = plan.get("files")
    if not isinstance(files, list):
        return issues
    seen: set[str] = set()
    for index, item in enumerate(files):
        if not isinstance(item, dict):
            continue
        normalized = _normalize_manifest_path_for_pipeline(item.get("path"))
        if normalized is None:
            issues.append(ValidationIssue(f"$.files[{index}].path", "must be a safe relative path"))
            continue
        if normalized in seen:
            issues.append(ValidationIssue(f"$.files[{index}].path", "duplicate path"))
        seen.add(normalized)
    missing = sorted(expected - seen)
    extra = sorted(seen - expected)
    for path in missing:
        issues.append(ValidationIssue("$.files", f"missing required file: {path}"))
    for path in extra:
        issues.append(ValidationIssue("$.files", f"unexpected file: {path}"))
    return issues


def _validate_project_file(file_data: dict[str, Any], expected_path: str) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    normalized = _normalize_manifest_path_for_pipeline(file_data.get("path"))
    if normalized is None:
        issues.append(ValidationIssue("$.path", "must be a safe relative path"))
    elif normalized != expected_path:
        issues.append(ValidationIssue("$.path", f"must equal {expected_path}"))
    lines = file_data.get("content_lines")
    if isinstance(lines, list):
        if expected_path.endswith(".py") and not any(str(line).strip() for line in lines):
            issues.append(ValidationIssue("$.content_lines", "Python file cannot be empty"))
        limit = REPRO_PROJECT_FILE_LIMITS.get(expected_path)
        if limit is not None:
            char_count = sum(len(str(line)) + 1 for line in lines)
            if len(lines) > limit["lines"]:
                issues.append(ValidationIssue("$.content_lines", f"must be at most {limit['lines']} lines for {expected_path}"))
            if char_count > limit["chars"]:
                issues.append(ValidationIssue("$.content_lines", f"must be at most {limit['chars']} characters for {expected_path}"))
        issues.extend(_content_type_issues(expected_path, lines))
    return issues


def _content_type_issues(expected_path: str, lines: list[Any]) -> list[ValidationIssue]:
    """Catch a generated file whose body is not actually the declared type -- e.g. an LLM
    that returns prose/markdown instead of Python, or malformed JSON. Without this such a
    file passes the per-file structural checks but later fails the project-level
    python_compiles check, which discards the whole generated project for a generic
    template fallback. Flagging it here makes the per-file generation loop regenerate just
    this one file instead of sinking the entire project."""
    content = "\n".join(str(line) for line in lines)
    if not content.strip():
        return []
    if expected_path.endswith(".py"):
        try:
            ast.parse(content)
        except SyntaxError as exc:
            return [
                ValidationIssue(
                    "$.content_lines",
                    f"{expected_path} is not valid Python (looks like prose or has a syntax error, "
                    f"not runnable code): {exc.msg} at line {exc.lineno}",
                )
            ]
    elif expected_path.endswith(".json"):
        try:
            json.loads(content)
        except ValueError as exc:
            return [ValidationIssue("$.content_lines", f"{expected_path} is not valid JSON: {exc}")]
    return []


def _normalize_manifest_path_for_pipeline(path: Any) -> str | None:
    if not isinstance(path, str) or not path.strip():
        return None
    schema_root = Path("__schema_root__")
    try:
        resolved = resolve_inside(schema_root, path)
    except ValueError:
        return None
    return resolved.relative_to(schema_root.resolve()).as_posix()


def _manifest_path_slug(path: str) -> str:
    return "".join(char if char.isalnum() else "_" for char in path).strip("_") or "file"


def _generated_files_context(files: list[dict[str, Any]], max_chars: int = 120000) -> str:
    """Full content of already-generated files, so the next file (e.g. src/simulation.py)
    sees the REAL interfaces of channel/modulation/metrics it must import and wire, not a
    truncated preview. The generous overall cap only guards against pathological size."""
    context: list[dict[str, Any]] = []
    remaining = max_chars
    for item in files:
        path = str(item.get("path", ""))
        lines = item.get("content_lines", [])
        if not isinstance(lines, list):
            continue
        content = "\n".join(str(line) for line in lines)
        if remaining <= 0:
            context.append({"path": path, "content": "[omitted: context budget exhausted]"})
            continue
        if len(content) > remaining:
            content = content[:remaining] + "\n# [truncated: context budget exhausted]"
        remaining -= len(content)
        context.append({"path": path, "content": content})
    return pretty_json(context)


def _recover_manifest_from_audit(audit_dir: Path) -> dict[str, Any] | None:
    if not audit_dir.exists():
        return None
    candidates = sorted(
        audit_dir.glob("raw_03_generate_repro_project_attempt_*.txt"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for path in candidates:
        try:
            parsed = parse_json_object(path.read_text(encoding="utf-8"), allow_loose_manifest=True)
            normalized = normalize_repro_project_manifest_candidate(parsed)
            issues = validate_stage("repro_project_manifest", normalized)
            if not issues:
                return normalized
        except Exception:
            continue
    return None


def normalize_repro_project_manifest_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    files = candidate.get("files")
    if not isinstance(files, list):
        return candidate

    normalized_files: list[dict[str, Any]] = []
    for item in files:
        if not isinstance(item, dict):
            normalized_files.append(item)
            continue
        normalized: dict[str, Any] = {}
        path = item.get("path")
        if isinstance(path, str):
            normalized["path"] = path
        content_keys = [key for key in ("content", "content_lines", "content_b64") if key in item]
        if content_keys:
            key = content_keys[0]
            normalized[key] = item[key]
        normalized_files.append(normalized)

    normalized_manifest = {"files": normalized_files}
    meta = candidate.get("_meta")
    if isinstance(meta, dict):
        normalized_manifest["_meta"] = meta
    if set(candidate.keys()) != {"files"}:
        normalized_manifest["_meta"] = {
            **(normalized_manifest.get("_meta", {}) if isinstance(normalized_manifest.get("_meta"), dict) else {}),
            "manifest_normalized": True,
        }
    return normalized_manifest


def normalize_repro_project_file_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    """Coerce one generated-file payload to the strict per-file shape {path, content_lines}.

    The whole-manifest schema accepts the file body as `content` (string), `content_lines`
    (list) or `content_b64`, but the per-file schema only accepts `content_lines`. Models
    sometimes return `content`/`content_b64` for a single file; without this, that one file
    fails JSON validation on the key name and the whole project gets nuked to a template
    (observed on 2603.29359: a correct src/metrics.py was discarded purely because it came
    back under `content`). Normalising to content_lines here absorbs that variance locally."""
    if not isinstance(candidate, dict):
        return candidate
    normalized: dict[str, Any] = {}
    path = candidate.get("path")
    if isinstance(path, str):
        normalized["path"] = path
    if isinstance(candidate.get("content_lines"), list):
        normalized["content_lines"] = candidate["content_lines"]
    elif isinstance(candidate.get("content"), str):
        normalized["content_lines"] = candidate["content"].splitlines()
    elif isinstance(candidate.get("content_b64"), str):
        try:
            decoded = base64.b64decode(candidate["content_b64"], validate=True).decode("utf-8")
            normalized["content_lines"] = decoded.splitlines()
        except Exception:
            pass  # leave content_lines unset -> strict validation reports it and we retry
    return normalized
