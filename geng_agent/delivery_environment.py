"""Export a small installable environment and instructions for a final package."""
from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
from typing import Any
from urllib.parse import urlsplit

from packaging.requirements import Requirement, InvalidRequirement
from packaging.utils import canonicalize_name
from packaging.markers import default_environment


def _read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _observed_version_mismatches(project: Path, versions: dict[str, str]) -> list[dict[str, Any]]:
    """Compare exported dependencies with existing host observations, without probes."""
    from .execution_receipts import _inside
    evidence_path = project / "execution_evidence.json"
    evidence = _read(evidence_path)
    mismatches = []
    for task in evidence.get("tasks", []):
        try:
            receipt = _read(_inside(project, str(task.get("receipt") or "")))
        except (OSError, ValueError):
            continue
        inventory = receipt.get("environment_observation", {}).get("before", {}).get("inventory", {})
        observed = {canonicalize_name(str(item[0])): str(item[1])
                    for item in inventory.get("packages", []) if isinstance(item, (list, tuple)) and len(item) == 2}
        differences = [{"distribution": name, "observed_version": observed[name], "exported_version": version}
                       for name, version in sorted(versions.items()) if name in observed and observed[name] != version]
        if differences:
            mismatches.append({"task_id": task.get("task_id"), "run_id": task.get("run_id"), "dependencies": differences})
    if evidence:
        evidence["installation_version_mismatches"] = mismatches
        evidence_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return mismatches


def export_installation(project: Path, *, python_executable: Path | None = None) -> set[str]:
    lock = _read(project / "environment.lock.json")
    installed = {canonicalize_name(str(item.get("distribution", ""))): item
                 for item in lock.get("installed_distributions", []) if isinstance(item, dict)}
    # Read dependency metadata from the selected execution interpreter, never from
    # this orchestrator's unrelated environment and never run project code here.
    metadata_available = False
    if python_executable is not None:
        script = "import importlib.metadata as m,json;print(json.dumps([{\"distribution\":d.metadata[\"Name\"],\"version\":d.version,\"requires\":d.requires or []} for d in m.distributions()]))"
        try:
            probe = subprocess.run([str(python_executable), "-I", "-c", script], capture_output=True,
                                   text=True, timeout=30, check=True)
            installed = {canonicalize_name(item["distribution"]): item for item in json.loads(probe.stdout)}
            metadata_available = True
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    requirements_path = project / "requirements.txt"
    raw_lines = requirements_path.read_text(encoding="utf-8-sig").splitlines() if requirements_path.is_file() else []
    declared: dict[str, Requirement] = {}
    warnings: list[str] = []
    if not requirements_path.is_file():
        warnings.append("requirements.txt is missing; dependencies have not been declared.")
    if not metadata_available:
        warnings.append("Dependency metadata unavailable: only declared packages have recorded pins; transitive versions require clean-install verification.")
    for raw in raw_lines:
        raw = raw.strip()
        if not raw or raw.startswith("#"):
            continue
        try:
            requirement = Requirement(raw)
            if requirement.url:
                raise ValueError("direct URL")
            declared[canonicalize_name(requirement.name)] = requirement
        except (ValueError, InvalidRequirement):
            warnings.append(f"Unexportable dependency declaration: {raw}")
    marker_environment = default_environment()
    recorded_markers = lock.get("interpreter", {}).get("marker_environment", {})
    if isinstance(recorded_markers, dict):
        marker_environment.update({key: str(value) for key, value in recorded_markers.items() if value is not None})
    selected = set(declared)
    extras_by_name = {name: set(req.extras) for name, req in declared.items()}
    pending = list(selected)
    visited: set[tuple[str, tuple[str, ...]]] = set()
    while pending:
        name = pending.pop()
        extras = extras_by_name.get(name, set())
        identity = (name, tuple(sorted(extras)))
        if identity in visited:
            continue
        visited.add(identity)
        for raw in installed.get(name, {}).get("requires", []):
            try:
                req = Requirement(raw)
                if req.marker and not any(req.marker.evaluate({**marker_environment, "extra": extra}) for extra in (extras | {""})):
                    continue
                dep = canonicalize_name(req.name)
                previous_extras = extras_by_name.get(dep, set())
                if dep not in selected or not set(req.extras) <= previous_extras:
                    extras_by_name[dep] = previous_extras | set(req.extras)
                    pending.append(dep)
                selected.add(dep)
            except (ValueError, InvalidRequirement):
                continue
    constraints = []
    for name in sorted(selected):
        version = installed.get(name, {}).get("version")
        if version:
            constraints.append(f"{name}=={version}")
        elif name in declared:
            warnings.append(f"No installed version recorded for {name}; install uses the declared requirement.")
    trusted = lock.get("trusted_sources", {})
    primary = trusted.get("url") if isinstance(trusted, dict) else None
    indexes = [primary or "https://pypi.org/simple"]
    build_sources = []
    # Preserve accelerator build identity. This is a reconstruction source derived
    # from an official PyTorch build tag, not a claim about its original download.
    for name in ("torch", "torchvision", "torchaudio"):
        version = str(installed.get(name, {}).get("version") or "")
        match = re.search(r"\+(cu\d+|cpu|rocm[\d.]+)$", version)
        if name in selected and match:
            url = f"https://download.pytorch.org/whl/{match.group(1)}"
            if url not in indexes:
                indexes.append(url)
            build_sources.append({"distribution": name, "version": version, "url": url,
                                  "provenance": "reconstruction_index_from_build_tag"})
    safe_indexes = []
    for url in indexes:
        parsed = urlsplit(str(url))
        if parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password:
            safe_indexes.append(str(url))
        else:
            warnings.append("Recorded package index is not an unauthenticated HTTPS source.")
    (project / "constraints.repro.txt").write_text("\n".join(constraints) + "\n", encoding="utf-8")
    install_lines = ["# Reconstruct on the recorded platform; preserve accelerator build versions."]
    if safe_indexes:
        install_lines.append(f"--index-url {safe_indexes[0]}")
        install_lines.extend(f"--extra-index-url {url}" for url in safe_indexes[1:])
    install_lines += ["--only-binary=:all:", "-c constraints.repro.txt", "-r requirements.txt"]
    (project / "requirements.repro.txt").write_text("\n".join(install_lines) + "\n", encoding="utf-8")
    mismatches = _observed_version_mismatches(project, {
        name: str(installed[name]["version"]) for name in selected if installed.get(name, {}).get("version")})
    for mismatch in mismatches:
        details = "; ".join(f"{item['distribution']} {item['observed_version']} -> {item['exported_version']}"
                            for item in mismatch["dependencies"])
        warnings.append(f"Exported dependency versions differ from observed execution for task {mismatch['task_id']}: {details}.")
    document = {"schema_version": "1.0", "requirements": "requirements.txt",
                "constraints": "constraints.repro.txt", "install_file": "requirements.repro.txt",
                "indexes": safe_indexes, "accelerator_sources": build_sources,
                "metadata_dependency_closure": metadata_available,
                "observed_execution_version_mismatches": mismatches,
                "selected_distributions": sorted(selected), "warnings": warnings,
                "python": lock.get("interpreter", {})}
    (project / "installation.json").write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest = _read(project / "tasks_manifest.json")
    lines = ["# Reproduction project", "", "## Install and run", "",
             "Use Python " + str(lock.get("interpreter", {}).get("python_full_version", "3.11+")) + ".",
             "Create a virtual environment without system site packages:", "", "```sh",
             "python -m venv .venv", "# Linux/macOS", ".venv/bin/python -m pip install -r requirements.repro.txt",
             ".venv/bin/python run_experiment.py config_smoke.json",
             ".venv/bin/python run_experiment.py config.json", "```", "",
             "On Windows use `.venv\\Scripts\\python.exe` in place of `.venv/bin/python`.", "",
             "`requirements.txt` declares the project dependencies; `constraints.repro.txt` pins only their recorded dependency closure. "
             "`installation.json` records wheel sources and unresolved export details. A different OS or accelerator may require "
             "a compatible wheel build; disclose that change instead of silently replacing a CUDA build with CPU.", "",
             "## Experiments and evidence", "", "| Task | Full configuration | Output directory |", "| --- | --- | --- |"]
    for task in manifest.get("tasks", []):
        if isinstance(task, dict):
            lines.append(f"| {task.get('task_id', '')} | `{task.get('config_full', 'config.json')}` | `outputs/{task.get('output_subdir', task.get('task_id', ''))}` |")
    lines += ["", "Run smoke first. Full may train models and overwrite generated outputs; keep an untouched copy of this delivered package. "
              "Task configurations specify seeds and sample sizes. Per-task notes are under `task_notes/`; scientific verdicts are in the case reports.", "",
              "## Checkpoints and verification", "",
              "`artifact_lineage.json` lists persisted shared checkpoints and their producing/consuming tasks. "
              "Keep those files at their relative paths. Select checkpoint reuse or retraining only through the supplied task configuration; "
              "the existence of a checkpoint alone does not prove which run used it.", "",
              "`source_inventory.json` hashes the delivered files. `reproducibility_manifest.json` contains machine-readable smoke/full commands. "
              "Execution receipts, where present, bind code/configuration and outputs to a completed run. "
              "`execution_evidence.json` maps their original paths and hashes to delivered files; "
              "`execution_records/` preserves any executed input whose bytes changed during assembly. "
              "These mappings preserve the original run and do not claim that assembly itself repeated the experiment. "
              "Directory relocation and clean-environment installation are distinct checks; their status is recorded in the case portability audit.", ""]
    if warnings:
        lines += ["## Installation limitations", "", *[f"- {warning}" for warning in warnings], ""]
    (project / "README.md").write_text("\n".join(lines), encoding="utf-8")
    return {"README.md", "installation.json", "requirements.repro.txt", "constraints.repro.txt"}
