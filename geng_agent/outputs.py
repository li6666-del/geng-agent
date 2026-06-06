from __future__ import annotations

import py_compile
import base64
import csv
import json
from pathlib import Path
from typing import Any

from .json_utils import pretty_json


REQUIRED_REPRO_FILES = {
    "README.md",
    "requirements.txt",
    "config.json",
    "config_smoke.json",
    "run_experiment.py",
    "src/channel.py",
    "src/modulation.py",
    "src/metrics.py",
    "src/simulation.py",
}


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(pretty_json(data) + "\n", encoding="utf-8")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def write_file_manifest(manifest: dict[str, Any], target_dir: Path) -> list[Path]:
    files = manifest.get("files")
    if not isinstance(files, list):
        raise ValueError("Generated manifest must contain a files array.")

    target_dir.mkdir(parents=True, exist_ok=True)
    prepared: list[tuple[Path, str]] = []
    seen_paths: set[str] = set()
    for item in files:
        if not isinstance(item, dict):
            raise ValueError("Each files item must be an object.")
        rel_path = item.get("path")
        if not isinstance(rel_path, str) or not rel_path.strip():
            raise ValueError("files[].path must be a non-empty string.")

        output_path = resolve_inside(target_dir, rel_path)
        normalized = output_path.relative_to(target_dir.resolve()).as_posix()
        if normalized in seen_paths:
            raise ValueError(f"Duplicate manifest path: {rel_path}")
        seen_paths.add(normalized)
        prepared.append((output_path, _extract_manifest_content(item, rel_path)))

    written: list[Path] = []
    for output_path, content in prepared:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(content, encoding="utf-8", newline="\n")
        written.append(output_path)

    (target_dir / "outputs").mkdir(parents=True, exist_ok=True)
    return written


def _extract_manifest_content(item: dict[str, Any], rel_path: str) -> str:
    present = [key for key in ("content", "content_lines", "content_b64") if key in item]
    if len(present) != 1:
        raise ValueError(f"{rel_path} must contain exactly one of content, content_lines, content_b64.")

    if "content" in item:
        content = item["content"]
        if not isinstance(content, str):
            raise ValueError(f"{rel_path} content must be a string.")
        return content

    if "content_lines" in item:
        lines = item["content_lines"]
        if not isinstance(lines, list) or not all(isinstance(line, str) for line in lines):
            raise ValueError(f"{rel_path} content_lines must be a list of strings.")
        return "\n".join(lines) + ("\n" if lines else "")

    content_b64 = item["content_b64"]
    if not isinstance(content_b64, str):
        raise ValueError(f"{rel_path} content_b64 must be a string.")
    try:
        return base64.b64decode(content_b64, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError(f"{rel_path} content_b64 must be valid base64-encoded UTF-8.") from exc


def resolve_inside(root: Path, rel_path: str) -> Path:
    normalized = rel_path.replace("\\", "/").strip()
    if normalized.startswith("repro_project/"):
        normalized = normalized[len("repro_project/") :]
    candidate = Path(normalized)
    if candidate.is_absolute():
        raise ValueError(f"Refusing absolute path: {rel_path}")
    if any(part == ".." for part in candidate.parts):
        raise ValueError(f"Refusing path traversal: {rel_path}")

    root_resolved = root.resolve()
    output_path = (root_resolved / candidate).resolve()
    if output_path != root_resolved and root_resolved not in output_path.parents:
        raise ValueError(f"Path escapes target directory: {rel_path}")
    return output_path


def validate_repro_project(root: Path) -> dict[str, Any]:
    existing = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and not _is_auxiliary_generated_path(path, root)
    }
    missing = sorted(REQUIRED_REPRO_FILES - existing)

    compile_errors = []
    for py_file in root.rglob("*.py"):
        if _is_auxiliary_generated_path(py_file, root):
            continue
        try:
            py_compile.compile(str(py_file), doraise=True)
        except py_compile.PyCompileError as exc:
            compile_errors.append(
                {
                    "file": py_file.relative_to(root).as_posix(),
                    "error": str(exc),
                }
            )

    return {
        "required_files_present": not missing,
        "missing_files": missing,
        "python_compiles": not compile_errors,
        "compile_errors": compile_errors,
    }


def _is_auxiliary_generated_path(path: Path, root: Path) -> bool:
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        parts = path.parts
    return "__pycache__" in parts or "repair_logs" in parts


def inspect_output_artifacts(root: Path, *, since: float | None = None) -> dict[str, Any]:
    outputs = root / "outputs"
    if not outputs.exists():
        return {
            "outputs_dir_exists": False,
            "csv_files": [],
            "png_files": [],
            "summary_json_files": [],
            "invalid_files": [],
            "has_csv": False,
            "has_png": False,
            "has_summary_json": False,
        }

    invalid_files: list[dict[str, str]] = []
    csv_files = []
    for path in sorted(outputs.glob("*.csv")):
        if _is_fresh(path, since) and _valid_csv(path):
            csv_files.append(path.name)
        else:
            invalid_files.append({"file": path.name, "reason": "csv is stale, empty, or invalid"})

    png_files = []
    for path in sorted(outputs.glob("*.png")):
        if _is_fresh(path, since) and _valid_png(path):
            png_files.append(path.name)
        else:
            invalid_files.append({"file": path.name, "reason": "png is stale or invalid"})

    summary_json_files = []
    for path in sorted(outputs.glob("summary*.json")):
        if _is_fresh(path, since) and _valid_summary_json(path):
            summary_json_files.append(path.name)
        else:
            invalid_files.append({"file": path.name, "reason": "summary json is stale or invalid"})
    return {
        "outputs_dir_exists": True,
        "csv_files": csv_files,
        "png_files": png_files,
        "summary_json_files": summary_json_files,
        "invalid_files": invalid_files,
        "has_csv": bool(csv_files),
        "has_png": bool(png_files),
        "has_summary_json": bool(summary_json_files),
    }


def _is_fresh(path: Path, since: float | None) -> bool:
    return since is None or path.stat().st_mtime >= since


def _valid_csv(path: Path) -> bool:
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            rows = list(csv.reader(handle))
    except (OSError, UnicodeDecodeError, csv.Error):
        return False
    return len(rows) >= 2 and bool(rows[0]) and any(any(cell.strip() for cell in row) for row in rows[1:])


def _valid_png(path: Path) -> bool:
    try:
        data = path.read_bytes()
    except OSError:
        return False
    return (
        data.startswith(b"\x89PNG\r\n\x1a\n")
        and len(data) >= 33
        and data[12:16] == b"IHDR"
    )


def _valid_summary_json(path: Path) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict) or not data:
        return False
    if "assumptions" in data and not isinstance(data["assumptions"], list):
        return False
    if "metrics" in data and not isinstance(data["metrics"], (dict, list)):
        return False
    return any(key in data for key in ("task_id", "tasks", "metrics", "results", "assumptions"))
