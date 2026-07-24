from __future__ import annotations

import ast
import py_compile
import base64
import csv
import json
import os
from pathlib import Path
import tempfile
from typing import Any

from .json_utils import pretty_json


# The generated project has one harness-owned human entry point. Everything
# else is task/architecture specific and is declared by tasks_manifest.json.
# Communication-shaped placeholder modules are not universal requirements.
REQUIRED_REPRO_FILES = {"run_experiment.py"}


def write_json(path: Path, data: Any) -> None:
    _atomic_write_text(path, pretty_json(data) + "\n")


def write_text(path: Path, text: str) -> None:
    _atomic_write_text(path, text)


def _atomic_write_text(path: Path, text: str) -> None:
    """Commit UTF-8 text without exposing a partially written destination."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


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
    declared_required = _declared_required_project_files(root)
    missing = sorted(declared_required - existing)

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

    missing_local_imports = _missing_local_imports(root)

    return {
        "required_files_present": not missing,
        "required_files": sorted(declared_required),
        "missing_files": missing,
        "python_compiles": not compile_errors,
        "compile_errors": compile_errors,
        # Static import discovery cannot model callbacks, package exports,
        # generated modules, or dynamic registries. Keep it as audit evidence;
        # successful real execution is stronger evidence.
        "local_imports_resolve": True,
        "static_local_imports_resolve": not missing_local_imports,
        "missing_local_imports": missing_local_imports,
        "local_import_validation": "advisory",
    }



def _declared_required_project_files(root: Path) -> set[str]:
    required = set(REQUIRED_REPRO_FILES)
    manifest_path = root / "tasks_manifest.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        return required
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return required | {"tasks_manifest.json"}
    tasks = manifest.get("tasks") if isinstance(manifest, dict) else None
    if not isinstance(tasks, list):
        return required | {"tasks_manifest.json"}
    required.add("tasks_manifest.json")
    for item in tasks:
        if not isinstance(item, dict):
            continue
        for key in ("script", "entrypoint", "config_full", "config_smoke"):
            raw = str(item.get(key) or "").strip()
            if not raw:
                continue
            try:
                path = resolve_inside(root, raw)
            except ValueError:
                # Leave an escaping declaration missing so a manifest cannot
                # weaken path containment.
                required.add(raw.replace("\\", "/"))
                continue
            required.add(path.relative_to(root.resolve()).as_posix())
    return required


def _missing_local_imports(root: Path) -> list[dict[str, str]]:
    """Find imports that target this project but have no local module.

    ``py_compile`` accepts ``from tasks import missing_helper`` because it
    never resolves imports. Generated projects are assembled from isolated
    sandboxes, so this static gate catches omitted dependency files without
    importing or executing untrusted scientific code.
    """

    local_roots = {
        path.name
        for path in root.iterdir()
        if path.is_dir() and ((path / "__init__.py").is_file() or any(path.glob("*.py")))
    }
    local_roots.update(path.stem for path in root.glob("*.py"))
    issues: list[dict[str, str]] = []
    seen: set[tuple[str, str, int]] = set()
    for path in root.rglob("*.py"):
        if _is_auxiliary_generated_path(path, root):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        except (OSError, SyntaxError, UnicodeError):
            continue
        package = list(path.relative_to(root).with_suffix("").parts[:-1])
        for node in ast.walk(tree):
            candidates: list[str] = []
            if isinstance(node, ast.Import):
                candidates.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level and node.level > len(package):
                    key = (
                        path.relative_to(root).as_posix(),
                        "<relative-import-beyond-top-level>",
                        int(getattr(node, "lineno", 0)),
                    )
                    if key not in seen:
                        seen.add(key)
                        issues.append({"file": key[0], "module": key[1], "line": str(key[2])})
                    continue
                base_parts = _resolved_import_parts(package, node.module, node.level)
                if not base_parts:
                    continue
                base = ".".join(base_parts)
                candidates.append(base)
                if _module_is_package(root, base):
                    for alias in node.names:
                        if alias.name == "*" or _package_defines_name(root, base, alias.name):
                            continue
                        candidates.append(f"{base}.{alias.name}")
            for module in candidates:
                top = module.split(".", 1)[0]
                if top not in local_roots or _local_module_exists(root, module):
                    continue
                key = (path.relative_to(root).as_posix(), module, int(getattr(node, "lineno", 0)))
                if key in seen:
                    continue
                seen.add(key)
                issues.append({"file": key[0], "module": module, "line": str(key[2])})
    return sorted(issues, key=lambda item: (item["file"], int(item["line"]), item["module"]))


def _resolved_import_parts(package: list[str], module: str | None, level: int) -> list[str]:
    if level:
        keep = max(0, len(package) - level + 1)
        parts = package[:keep]
        if module:
            parts.extend(part for part in module.split(".") if part)
        return parts
    return [part for part in str(module or "").split(".") if part]


def _local_module_exists(root: Path, module: str) -> bool:
    path = root.joinpath(*module.split("."))
    return path.with_suffix(".py").is_file() or (path / "__init__.py").is_file()


def _module_is_package(root: Path, module: str) -> bool:
    return (root.joinpath(*module.split(".")) / "__init__.py").is_file()


def _package_defines_name(root: Path, module: str, name: str) -> bool:
    init_path = root.joinpath(*module.split(".")) / "__init__.py"
    try:
        tree = ast.parse(init_path.read_text(encoding="utf-8-sig"), filename=str(init_path))
    except (OSError, SyntaxError, UnicodeError):
        return False
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == name:
            return True
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(target, ast.Name) and target.id == name for target in targets):
                return True
        if isinstance(node, ast.Import):
            if any((alias.asname or alias.name.split(".", 1)[0]) == name for alias in node.names):
                return True
        if isinstance(node, ast.ImportFrom):
            if any((alias.asname or alias.name) == name for alias in node.names):
                return True
    return False


def _is_auxiliary_generated_path(path: Path, root: Path) -> bool:
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        parts = path.parts
    return "__pycache__" in parts or "repair_logs" in parts


def inspect_output_artifacts(
    root: Path,
    *,
    since: float | None = None,
    subdir: str | None = None,
    declared_artifacts: Any = None,
) -> dict[str, Any]:
    outputs = root / "outputs"
    if subdir:
        outputs = outputs / subdir
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
            "artifact_files": [],
            "declared_artifact_files": [],
            "missing_declared_artifacts": _declared_artifact_names(
                declared_artifacts,
                subdir=subdir,
            ),
            "has_artifacts": False,
        }


    def _rel(path: Path) -> str:
        try:
            return path.relative_to(outputs).as_posix()
        except ValueError:
            return path.name

    invalid_files: list[dict[str, str]] = []
    csv_files: list[str] = []
    png_files: list[str] = []
    summary_json_files: list[str] = []
    artifact_files: list[str] = []
    excluded_names = {
        "task_agent_result.json",
        "task_agent_result.md",
        "paper_target_crop.png",
        "paper_target_locator.png",
    }
    for path in sorted(outputs.rglob("*")):
        if (
            not path.is_file()
            or path.is_symlink()
            or path.name.lower() in excluded_names
        ):
            continue
        relative = _rel(path)
        valid, reason = _valid_scientific_artifact(path, since=since)
        if not valid:
            invalid_files.append({"file": relative, "reason": reason})
            continue
        artifact_files.append(relative)
        suffix = path.suffix.lower()
        if suffix == ".csv":
            csv_files.append(relative)
        elif suffix == ".png":
            png_files.append(relative)
        if suffix == ".json" and path.name.lower().startswith("summary"):
            summary_json_files.append(relative)

    declared = _declared_artifact_names(declared_artifacts, subdir=subdir)
    present_by_name = set(artifact_files)
    declared_present = [
        name
        for name in declared
        if name in present_by_name
        or any(item.endswith("/" + name) for item in present_by_name)
    ]
    missing_declared = [name for name in declared if name not in declared_present]
    return {
        "outputs_dir_exists": True,
        "csv_files": csv_files,
        "png_files": png_files,
        "summary_json_files": summary_json_files,
        "invalid_files": invalid_files,
        "has_csv": bool(csv_files),
        "has_png": bool(png_files),
        "has_summary_json": bool(summary_json_files),
        "artifact_files": artifact_files,
        "declared_artifact_files": declared_present,
        "missing_declared_artifacts": missing_declared,
        "has_artifacts": bool(artifact_files),
    }


def _declared_artifact_names(value: Any, *, subdir: str | None) -> list[str]:
    values = value if isinstance(value, list) else []
    normalized: list[str] = []
    prefix = f"{str(subdir).strip('/')}/" if subdir else ""
    for item in values:
        raw = str(item).replace("\\", "/").strip().lstrip("./")
        if not raw:
            continue
        if raw.startswith("outputs/"):
            raw = raw[len("outputs/") :]
        if prefix and raw.startswith(prefix):
            raw = raw[len(prefix) :]
        if raw and ".." not in Path(raw).parts and raw not in normalized:
            normalized.append(raw)
    return normalized


def _valid_scientific_artifact(
    path: Path,
    *,
    since: float | None,
) -> tuple[bool, str]:
    try:
        if not _is_fresh(path, since) or path.stat().st_size <= 0:
            return False, "artifact is stale or empty"
    except OSError:
        return False, "artifact could not be inspected"
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return (
            (True, "")
            if _valid_csv(path)
            else (False, "csv is empty or invalid")
        )
    if suffix == ".png":
        return (
            (True, "")
            if _valid_png(path)
            else (False, "png is invalid")
        )
    if suffix == ".json":
        if path.name.lower().startswith("summary"):
            return (
                (True, "")
                if _valid_summary_json(path)
                else (False, "summary json is empty or invalid")
            )
        try:
            json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return False, "json is invalid"
    # Non-empty domain-native outputs (.npy/.npz/.pt/.h5/.mat/.pdf/.svg,
    # etc.) remain evidence even when the harness has no format-specific parser.
    return True, ""


def _is_fresh(path: Path, since: float | None) -> bool:
    return since is None or path.stat().st_mtime >= since


def _valid_csv(path: Path) -> bool:
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            rows = list(csv.reader(handle))
    except (OSError, UnicodeDecodeError, csv.Error):
        return False
    return bool(rows) and any(any(cell.strip() for cell in row) for row in rows)


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
