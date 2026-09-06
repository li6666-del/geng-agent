"""Task-scoped scientific inputs and consumed runtime dependencies for resume."""

from __future__ import annotations

import ast
import hashlib
import importlib.metadata
import json
from pathlib import Path
from typing import Any

from packaging.requirements import Requirement, InvalidRequirement
from packaging.utils import canonicalize_name

from .case_runtime import CaseRuntime
from .foundation_scope import derive_foundation_scope
from .foundation_snapshot import file_sha256, path_is_foundation_link, scan_foundation_tree
from .task_writer_support import WRITER_HANDOFF_POLICY_VERSION, WRITER_ANALYSIS_SCHEMA_VERSION
from .task_writer_units import _execution_unit_sandbox, _execution_unit_work_items, _public_execution_unit
from .paper_evidence import safe_label, thesis_comparisons_for_task


WRITER_LINEAGE_VERSION = "unit-scientific-inputs-v1"


def writer_policy_content_hashes() -> dict[str, str]:
    """Changes to the actual execution contract must enter the cache key."""
    root = Path(__file__).parent
    return {name: file_sha256(root / name) for name in (
        "task_writer_prompts.py", "task_writer_execution_binding.py",
        "task_writer_contracts.py", "task_writer_support.py",
        "scientific_materiality.py", "execution_receipts.py", "execution_client.py", "execution_sandbox.py",
        "writer_lineage.py",
    )}


def _objects(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _json(path: Path) -> dict[str, Any]:
    try:
        if path_is_foundation_link(path):
            return {}
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _module_name(relative: str) -> str:
    return (relative[:-12] if relative.endswith("/__init__.py") else relative[:-3]).replace("/", ".")


def _python_sources(root: Path) -> list[Path]:
    if not root.is_dir() or path_is_foundation_link(root):
        return []
    return [path for path in scan_foundation_tree(root)[0] if path.suffix == ".py"]


def _source_closure(root: Path, initial: set[str]) -> tuple[dict[str, str], set[str]]:
    modules: dict[str, str] = {}
    for path in _python_sources(root / "src"):
        if not path_is_foundation_link(path) and path.is_file():
            relative = path.relative_to(root).as_posix()
            modules[_module_name(relative)] = relative
    pending = list(initial)
    hashes: dict[str, str] = {}
    external: set[str] = set()
    while pending:
        relative = pending.pop()
        if relative in hashes:
            continue
        path = root / relative
        try:
            if any(path_is_foundation_link(part) for part in [path, *path.parents] if part != root.parent and part.is_relative_to(root)) or not path.is_file():
                continue
            path.resolve().relative_to(root.resolve())
            hashes[relative] = file_sha256(path)
            tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, SyntaxError):
            continue
        for parent in Path(relative).parents:
            initializer = (parent / "__init__.py").as_posix()
            if str(parent) != "." and initializer not in hashes and (root / initializer).is_file():
                pending.append(initializer)
        module = _module_name(relative)
        package = module if relative.endswith("/__init__.py") else module.rpartition(".")[0]
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    prefix = package.split(".")[:len(package.split(".")) - node.level + 1]
                    base = ".".join([*prefix, base]).strip(".")
                names = [base, *(f"{base}.{alias.name}" for alias in node.names)]
            for name in names:
                if name in modules:
                    pending.append(modules[name])
                elif name and name.split(".")[0] not in {"src", "tasks"}:
                    external.add(name.split(".")[0])
    return hashes, external


def _runtime_dependency_graph(
    runtime: CaseRuntime | None, *, import_distributions: dict[str, set[str]] | None = None,
    installed_versions: dict[str, str] | None = None,
) -> dict[str, set[str]]:
    if runtime is None:
        return {}
    graph, names, versions = runtime_distribution_metadata(
        runtime.venv_dir,
        marker_environment=runtime.lock.get("interpreter", {}).get("marker_environment") or {},
    )
    if import_distributions is not None:
        import_distributions.update(names)
    if installed_versions is not None:
        installed_versions.update(versions)
    return graph


def runtime_distribution_metadata(
    prefix: Path, *, marker_environment: dict[str, str] | None = None,
) -> tuple[dict[str, set[str]], dict[str, set[str]], dict[str, str]]:
    """Read selected-prefix dependency/import/version metadata without execution."""
    candidates = [prefix / "Lib" / "site-packages", *prefix.glob("lib/python*/site-packages")]
    paths = [str(path) for path in candidates if path.is_dir()]
    graph: dict[str, set[str]] = {}
    import_distributions: dict[str, set[str]] = {}
    installed_versions: dict[str, str] = {}
    for distribution in importlib.metadata.distributions(path=paths):
        name = canonicalize_name(distribution.metadata.get("Name") or "")
        dependencies: set[str] = set()
        for raw in distribution.requires or []:
            try:
                dependency = Requirement(raw)
                if dependency.marker is not None and not dependency.marker.evaluate({**(marker_environment or {}), "extra": ""}):
                    continue
                dependencies.add(canonicalize_name(dependency.name))
            except (InvalidRequirement, KeyError):
                continue
        if name:
            graph[name] = dependencies
            installed_versions[name] = distribution.version
            roots = set((distribution.read_text("top_level.txt") or "").splitlines())
            # Some wheels omit top_level.txt. Their RECORD still identifies
            # importable package/module roots without importing the code.
            if not roots:
                for file in distribution.files or []:
                    first = str(file).replace("\\", "/").split("/")[0]
                    if first.endswith((".dist-info", ".egg-info", ".data")):
                        continue
                    roots.add(first.split(".")[0])
            for root in roots:
                root = root.strip()
                if root.isidentifier():
                    import_distributions.setdefault(root, set()).add(name)
    return graph, import_distributions, installed_versions


def _runtime_projection(
    runtime: CaseRuntime | None,
    libraries: set[str],
    imports: set[str],
    dependency_graph: dict[str, set[str]],
    import_distributions: dict[str, set[str]] | None = None,
    installed_versions: dict[str, str] | None = None,
) -> dict[str, Any]:
    if runtime is None:
        return {}
    lock = runtime.lock
    requirements = _objects(lock.get("requirements"))
    aliases = {"pytorch": "torch", "sklearn": "scikit-learn", "yaml": "pyyaml", "pil": "pillow"}
    requested: set[str] = set()
    for raw in libraries | imports:
        try:
            name = canonicalize_name(Requirement(raw).name)
        except InvalidRequirement:
            continue
        requested.add(aliases.get(name, name))
    for item in requirements:
        if imports.intersection(str(value).split(".")[0] for value in item.get("import_names", [])):
            requested.add(canonicalize_name(str(item.get("distribution") or "")))
    for root in imports:
        requested.update((import_distributions or {}).get(root.split(".")[0], set()))
    pending = list(requested)
    while pending:
        for dependency in dependency_graph.get(pending.pop(), set()) - requested:
            requested.add(dependency)
            pending.append(dependency)
    installed = {
        canonicalize_name(str(item.get("distribution") or "")): str(item.get("version") or "")
        for item in _objects(lock.get("installed_distributions"))
    }
    for item in requirements:
        try:
            name = str(item.get("distribution") or Requirement(str(item.get("requirement") or "")).name)
        except InvalidRequirement:
            continue
        if item.get("installed_version"):
            installed[canonicalize_name(name)] = str(item["installed_version"])
    installed.update(installed_versions or {})
    interpreter = lock.get("interpreter") or {}
    return {
        "python_full_version": interpreter.get("python_full_version"),
        "implementation": interpreter.get("implementation"),
        "marker_environment": interpreter.get("marker_environment"),
        # Standard-library imports do not acquire artificial package identities.
        "distributions": {name: installed[name] for name in sorted(requested & installed.keys())},
    }


def _observed_imports_by_task(audit_dir: Path) -> dict[str, set[str]]:
    """Read only host-owned observations; Writer result notes are not evidence."""
    run_root = audit_dir / "execution_runs"
    if not run_root.is_dir() or path_is_foundation_link(run_root):
        return {}
    latest: dict[str, tuple[float, set[str]]] = {}
    for directory in sorted(run_root.iterdir()):
        if not directory.is_dir() or path_is_foundation_link(directory):
            continue
        receipt = _json(directory / "execution_receipt.json")
        if receipt.get("observer") != "orchestration_host":
            continue
        task_id = str(receipt.get("task_id") or "")
        timestamp = receipt.get("finished_at")
        roots = receipt.get("observed_import_roots")
        if not task_id or not isinstance(timestamp, (int, float)):
            continue
        if task_id not in latest or timestamp >= latest[task_id][0]:
            latest[task_id] = (timestamp, {str(root).split(".")[0] for root in roots if isinstance(root, str)}
                               if isinstance(roots, list) else set())
    return {task_id: observed[1] for task_id, observed in latest.items()}


def _scoped_architecture_metadata(
    architecture: dict[str, Any], components: list[dict[str, Any]],
    bindings: list[dict[str, Any]], task_ids: set[str],
) -> dict[str, list[dict[str, Any]]]:
    quantity_ids = {str(key) for item in components for field in ("inputs", "outputs", "parameters") for key in item.get(field, [])}
    quantity_ids.update(str(key) for binding in bindings for key in [*binding.get("outputs", []), *binding.get("overrides", {})])
    groups = [item for item in _objects(architecture.get("consistency_groups"))
              if task_ids.intersection(map(str, item.get("task_ids", [])))]
    quantity_ids.update(str(key) for item in groups for key in item.get("shared_quantity_ids", []))
    subjects = quantity_ids | {str(item.get("id")) for item in components} | {str(item.get("id")) for item in groups}
    invariants = [item for item in _objects(architecture.get("invariants"))
                  if (not item.get("subjects") and not item.get("task_ids"))
                  or task_ids.intersection(map(str, item.get("task_ids", [])))
                  or subjects.intersection(map(str, item.get("subjects", [])))]
    return {"quantities": [item for item in _objects(architecture.get("quantities")) if str(item.get("id")) in quantity_ids],
            "consistency_groups": groups, "invariants": invariants}


def foundation_cache_projection(
    *, architecture: dict[str, Any], facts: dict[str, Any], paper_path: Path,
    case_runtime: CaseRuntime | None,
) -> tuple[str, dict[str, Any], str]:
    """Exclude private task edits and unused package additions from Foundation."""
    scope = architecture.get("_foundation_scope") or {}
    components = _objects(architecture.get("components"))
    component_ids = {str(item.get("id")) for item in components}
    task_ids = {str(key) for component in component_ids for key in scope.get("component_task_ids", {}).get(component, [])}
    quantity_ids = {str(key) for item in components for field in ("inputs", "outputs", "parameters") for key in item.get(field, [])}
    bindings = [{"task_id": item.get("task_id"), "experiment_id": item.get("experiment_id"),
                 "components": sorted(component_ids.intersection(map(str, item.get("components", [])))),
                 "overrides": {key: value for key, value in (item.get("overrides") or {}).items() if key in quantity_ids}}
                for item in _objects(architecture.get("bindings")) if str(item.get("task_id")) in task_ids]
    metadata = _scoped_architecture_metadata(architecture, components, bindings, task_ids)
    refs = {(str(ref.get("type")), str(ref.get("name")).casefold())
            for item in [*components, *metadata["quantities"], *metadata["invariants"]]
            for ref in _objects((item.get("basis") or {}).get("evidence_facts"))}
    selected_facts = [item for item in _objects(facts.get("engineering_facts"))
                      if not refs or (str(item.get("type")), str(item.get("name")).casefold()) in refs]
    module_root = Path(__file__).parent
    policy = {name: file_sha256(module_root / name) for name in (
        "foundation_prompt_cache.py", "foundation_scope.py", "foundation_revision.py", "writer_lineage.py",
    )}
    analysis_hash = _digest({"paper_sha256": file_sha256(paper_path) if paper_path.is_file() else None,
                             "facts": selected_facts, "policy_content_hashes": policy})
    projected = {"schema_version": architecture.get("schema_version"), "components": components,
                 "bindings": bindings, **metadata,
                 "scope_policy": scope.get("policy_version")}
    if architecture.get("_foundation_revision"):
        projected["_foundation_revision"] = architecture["_foundation_revision"]
    runtime = foundation_consumed_runtime(architecture=architecture, case_runtime=case_runtime)
    return analysis_hash, projected, _digest(runtime)


def foundation_consumed_runtime(
    *, architecture: dict[str, Any], case_runtime: CaseRuntime | None,
    source_root: Path | None = None,
) -> dict[str, Any]:
    libraries: set[str] = set()
    modules: set[str] = set()
    for component in _objects(architecture.get("components")):
        execution = component.get("execution") or {}
        libraries.add(str(execution.get("primary_framework") or ""))
        libraries.update(map(str, execution.get("supporting_libraries") or []))
        modules.add(str(component.get("module") or ""))
    imports = _source_closure(source_root, modules)[1] if source_root is not None else set()
    distribution_names: dict[str, set[str]] = {}
    versions: dict[str, str] = {}
    graph = _runtime_dependency_graph(case_runtime, import_distributions=distribution_names, installed_versions=versions)
    return _runtime_projection(case_runtime, libraries, imports, graph, distribution_names, versions)


def build_writer_unit_lineage(
    *,
    task_pairs: list[tuple[dict[str, Any], dict[str, Any]]],
    execution_plan: dict[str, Any],
    facts: dict[str, Any],
    experiment_index: dict[str, Any],
    paper_path: Path,
    analysis_artifacts: dict[str, Path],
    foundation: dict[str, Any] | None,
    case_runtime: CaseRuntime | None,
    task_root: Path,
    paper_thesis: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    architecture = _json(analysis_artifacts["scientific_architecture.json"]) if "scientific_architecture.json" in analysis_artifacts else {}
    scope = derive_foundation_scope(architecture, execution_plan)
    components = {str(item.get("id")): item for item in _objects(architecture.get("components"))}
    paper_hash = file_sha256(paper_path) if paper_path.is_file() else None
    distribution_names: dict[str, set[str]] = {}
    versions: dict[str, str] = {}
    graph = _runtime_dependency_graph(case_runtime, import_distributions=distribution_names, installed_versions=versions)
    observed_imports = _observed_imports_by_task(task_root.parent)
    policy_hashes = writer_policy_content_hashes()
    if paper_thesis is None and "paper_thesis.json" in analysis_artifacts:
        paper_thesis = _json(analysis_artifacts["paper_thesis.json"])
    result: dict[str, dict[str, Any]] = {}
    for unit in _execution_unit_work_items(task_pairs, execution_plan):
        task_ids = {str(task.get("task_id") or entry.get("task_id")) for _, task, entry in unit["members"]}
        unit_tasks = [task for _, task, _ in unit["members"]]
        component_ids = {key for task_id in task_ids for key in scope["task_component_ids"].get(task_id, [])}
        unit_components = [components[key] for key in sorted(component_ids) if key in components]
        bindings = [item for item in _objects(architecture.get("bindings")) if str(item.get("task_id")) in task_ids]
        metadata = _scoped_architecture_metadata(architecture, unit_components, bindings, task_ids)
        unit_quantities = metadata["quantities"]
        refs = {
            (str(ref.get("type")), str(ref.get("name")).casefold())
            for task in unit_tasks for ref in _objects(task.get("required_facts"))
        }
        for item in [*unit_components, *unit_quantities, *metadata["invariants"]]:
            basis = item.get("basis") or {}
            refs.update((str(ref.get("type")), str(ref.get("name")).casefold()) for ref in _objects(basis.get("evidence_facts")))
        # Without explicit task references there is no evidence that an edited
        # fact is unrelated. Keep that legacy/incomplete handoff conservative.
        facts_are_scoped = all(_objects(task.get("required_facts")) for task in unit_tasks)
        unit_facts = [item for item in _objects(facts.get("engineering_facts"))
                      if not facts_are_scoped or (str(item.get("type")), str(item.get("name")).casefold()) in refs]
        shared_paths = {str(item.get("module")) for item in unit_components if str(item.get("id")) in scope["component_ids"]}
        shared_hashes: dict[str, str] = {}
        imports: set[str] = set()
        if foundation is not None:
            shared_hashes, imports = _source_closure(Path(foundation["snapshot_dir"]), shared_paths)
        for task_id in task_ids:
            imports.update(observed_imports.get(task_id, set()))
        members = unit["members"]
        sandbox = (
            task_root / f"{members[0][0]:02d}_{safe_label(str(members[0][1].get('task_id') or members[0][2].get('task_id') or 'task'))}"
            if len(members) == 1 else _execution_unit_sandbox(task_root, str(unit["unit_id"]))
        )
        if sandbox.is_dir() and not path_is_foundation_link(sandbox):
            owned = {path.relative_to(sandbox).as_posix() for path in _python_sources(sandbox / "tasks")}
            _, actual_imports = _source_closure(sandbox, owned)
            imports.update(actual_imports)
        libraries: set[str] = set()
        for component in unit_components:
            execution = component.get("execution") or {}
            libraries.add(str(execution.get("primary_framework") or ""))
            libraries.update(map(str, execution.get("supporting_libraries") or []))
        payload = {
            "policy": [WRITER_LINEAGE_VERSION, WRITER_HANDOFF_POLICY_VERSION, WRITER_ANALYSIS_SCHEMA_VERSION],
            "policy_content_hashes": policy_hashes,
            "paper_sha256": paper_hash,
            "tasks": unit_tasks,
            "execution_unit": _public_execution_unit(unit),
            "facts": unit_facts,
            "facts_scope": "explicit_references" if facts_are_scoped else "all_facts_without_task_references",
            "paper_comparisons": {str(task.get("task_id")): thesis_comparisons_for_task(paper_thesis, task) for task in unit_tasks},
            "bindings": bindings,
            "components": unit_components,
            **metadata,
            "experiments": [item for item in _objects(experiment_index.get("experiments")) if str(item.get("task_id")) in task_ids],
            "shared_source_hashes": shared_hashes,
            "runtime": _runtime_projection(case_runtime, libraries, imports, graph, distribution_names, versions),
        }
        result[str(unit["unit_id"])] = {"snapshot_hash": _digest(payload), "inputs": payload}
    return result
