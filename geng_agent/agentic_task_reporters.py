from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from .codex_runner import run_codex_subprocess
from .execution_receipts import trusted_input_snapshot
from .config import get_config_value
from .mineru_adapter import task_figure_candidates
from .outputs import write_json, write_text
from .paper_crop import finalize_paper_target
from .paper_evidence import safe_label
from .schemas import validate_stage
from .security import redact_text
from .task_reporter_context import (
    REPORTER_CONVERGENCE_POLICY,
    TASK_VERIFICATION_FILE,
    _build_task_reporter_brief,
    _canonicalize_reporter_paper_evidence,
    _reporter_attachment_visibility,
    _copy_task_figure_candidates,
    _experiment_for_task,
    _load_task_reporter_cache,
    _page_number,
    _prepare_task_reporter_input,
    _read_json_object,
    _source_pages_for_task,
    _task_only_facts,
    _task_reporter_image_paths,
    _task_reporter_input_hash,
)
from .task_reporter_snapshot import (
    REPORT_ASSETS_DIR,
    WRITER_SOURCE_DIR,
    _WRITER_OUTPUT_MAX_FILE_BYTES,
    _WRITER_OUTPUT_MAX_TOTAL_BYTES,
    _WRITER_SOURCE_EXCLUDED_DIRS,
    _WRITER_SOURCE_EXCLUDED_FILES,
    _WRITER_SOURCE_MAX_BYTES,
    _copy_regular_file_without_links,
    _copy_writer_output_snapshot,
    _copy_writer_source_snapshot,
    _file_inventory,
    _looks_like_text_source,
    _manifest_declared_source_paths,
    _path_is_link_like,
    _safe_writer_tree_files,
    _sha256_file,
    _writer_source_inventory,
    _writer_source_paths,
)
from .task_reporter_validation import (
    _accepted_asset_issues,
    _copy_task_assets,
    _evidence_path_issues,
    normalize_reporter_observation_evidence,
    _materialize_task_assets,
    _normalize_verification_paths,
    _task_asset_manifest,
    _task_record_run_valid_hint,
    _task_reporter_failure,
    _task_reporter_reason,
)
from .task_writer_support import _write_paper_evidence_bundle
from .verification_result import (
    aggregate_task_verifications,
    normalize_task_verification,
    partition_task_verification_issues,
    rerun_evidence_path_issues,
)


def _next_available_task_reporter_round(
    task_audit_dir: Path,
    requested_round: int,
) -> int:
    """Choose a new audit generation without overwriting prior Reporter evidence."""

    candidate = max(1, int(requested_round))
    while any(task_audit_dir.glob(f"round_{candidate:03d}*")):
        candidate += 1
    return candidate


def run_codex_task_reporter_workflow(
    *,
    index: int,
    task: dict[str, Any],
    task_record: dict[str, Any],
    paper: dict[str, Any],
    paper_path: Path,
    facts: dict[str, Any],
    experiment_index: dict[str, Any],
    paper_thesis: dict[str, Any] | None,
    paper_images: list[Any] | None,
    output_dir: Path,
    audit_dir: Path,
    resume: bool,
    figure_index: dict[str, Any] | None = None,
    round_no: int = 1,
    include_all_paper_pages: bool = False,
    repair_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Verify one task in a workspace that contains no other writer output."""

    task_id = str(
        task.get("task_id")
        or task_record.get("task_id")
        or f"task_{index}"
    )
    label = f"{index:02d}_{safe_label(task_id)}"
    task_audit_dir = audit_dir / "04a_task_reporters" / label
    task_audit_dir.mkdir(parents=True, exist_ok=True)
    figure_candidates = task_figure_candidates(figure_index, task)
    input_hash = _task_reporter_input_hash(
        task=task,
        task_record=task_record,
        paper_path=paper_path,
        facts=facts,
        experiment_index=experiment_index,
        paper_thesis=paper_thesis,
        figure_candidates=figure_candidates,
        writer_output_max_file_bytes=_WRITER_OUTPUT_MAX_FILE_BYTES,
        writer_output_max_total_bytes=_WRITER_OUTPUT_MAX_TOTAL_BYTES,
        paper_images=paper_images,
        paper=paper,
        output_dir=output_dir,
    )
    status_path = task_audit_dir / "status.json"
    if resume:
        cached = _load_task_reporter_cache(
            status_path=status_path,
            output_dir=output_dir,
            task_id=task_id,
            input_hash=input_hash,
        )
        if cached is not None:
            cached["cached"] = True
            return cached

    requested_round_no = max(1, int(round_no))
    reporter_round_no = _next_available_task_reporter_round(
        task_audit_dir,
        requested_round_no,
    )
    workspace = task_audit_dir / f"round_{reporter_round_no:03d}"
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True)
    inputs_dir = workspace / "inputs"
    inputs_dir.mkdir()
    try:
        isolated_facts = _task_only_facts(facts, task)
        _write_paper_evidence_bundle(
            repro_project_dir=workspace,
            paper_path=paper_path,
            paper=paper,
            facts=isolated_facts,
            tasks={"repro_tasks": [task]},
            paper_thesis=None,
            full_paper_images=paper_images,
        )
        _canonicalize_reporter_paper_evidence(workspace)
        copied_figure_candidates = _copy_task_figure_candidates(
            workspace=workspace,
            output_dir=output_dir,
            candidates=figure_candidates,
        )
        report_input = _prepare_task_reporter_input(
            inputs_dir=inputs_dir,
            task=task,
            task_record=task_record,
            facts=isolated_facts,
            experiment_index=experiment_index,
            paper_thesis=paper_thesis,
            figure_candidates=copied_figure_candidates,
            writer_output_max_file_bytes=_WRITER_OUTPUT_MAX_FILE_BYTES,
            writer_output_max_total_bytes=_WRITER_OUTPUT_MAX_TOTAL_BYTES,
        )
        write_json(inputs_dir / "task_report_input.json", report_input)
        if repair_context:
            previous = Path(str(repair_context.get("workspace") or "")) / TASK_VERIFICATION_FILE
            previous_note = ""
            if previous.is_file() and not _path_is_link_like(previous) and previous.stat().st_size <= 1024 * 1024:
                previous_note = previous.read_text(encoding="utf-8-sig", errors="replace")
            write_text(inputs_dir / "previous_reporter_note.txt", previous_note)
            write_json(inputs_dir / "reporter_repair.json", {
                "kind": "structure_recovery",
                "issues": repair_context.get("validation_issues") or [repair_context.get("error") or "No readable evidence note was produced"],
                "previous_note": "inputs/previous_reporter_note.txt",
                "evidence": "inputs/task_report_input.json",
                "instruction": "Preserve existing scientific observations; repair only the stated delivery problem.",
            })
        prompt = _build_task_reporter_brief(
            task_id=task_id,
            report_asset_dir=report_input["report_asset_dir"],
            include_all_paper_pages=include_all_paper_pages,
            repair=bool(repair_context),
        )
        write_text(
            task_audit_dir / f"round_{reporter_round_no:03d}_brief.md",
            prompt,
        )
    except Exception as exc:
        return _task_reporter_failure(
            task_id=task_id,
            task_audit_dir=task_audit_dir,
            status_path=status_path,
            input_hash=input_hash,
            workspace=workspace,
            error=exc,
            error_kind="preparation_failed",
        )

    image_paths = _task_reporter_image_paths(
        workspace=workspace,
        task=task,
        experiment_index=experiment_index,
        local_images=report_input.get("local_image_paths", []),
        figure_candidates=report_input.get("figure_candidates", []),
        include_all_paper_pages=include_all_paper_pages,
    )
    attachment_manifest, visibility_note = _reporter_attachment_visibility(workspace, image_paths)
    write_json(inputs_dir / "attachment_manifest.json", attachment_manifest)
    prompt += visibility_note
    write_text(task_audit_dir / f"round_{reporter_round_no:03d}_brief.md", prompt)
    immutable_inputs = trusted_input_snapshot(workspace, ("inputs", "paper_evidence"))
    codex_status = run_codex_subprocess(
        role="task_reporter",
        work_dir=workspace,
        prompt=prompt,
        audit_dir=task_audit_dir,
        label=f"round_{reporter_round_no:03d}",
        sandbox="workspace-write",
        command_override=get_config_value("GENG_CODEX_TASK_REPORTER_CMD"),
        image_paths=image_paths,
    )
    verification_path = workspace / TASK_VERIFICATION_FILE
    try:
        intact = immutable_inputs == trusted_input_snapshot(workspace, ("inputs", "paper_evidence"))
    except (OSError, ValueError):
        intact = False
    if not intact:
        return _task_reporter_failure(task_id=task_id, task_audit_dir=task_audit_dir,
            status_path=status_path, input_hash=input_hash, workspace=workspace,
            error=RuntimeError("Reporter changed its evidence snapshot; scientific decision discarded"),
            error_kind="evidence_snapshot_modified")
    run_valid_hint = _task_record_run_valid_hint(task_record)
    raw_verification = _read_json_object(verification_path)
    raw_note_available = bool(raw_verification)
    raw_verification, observation_evidence_warnings = normalize_reporter_observation_evidence(
        raw_verification, workspace, host_execution=task_record.get("host_execution")
    )
    rerun_path_issues = rerun_evidence_path_issues(
        raw_verification,
        workspace,
    )
    if rerun_path_issues:
        raw_verification.setdefault("_engineering_issues", []).extend(rerun_path_issues)
    verification = normalize_task_verification(
        raw_verification,
        task_id,
        task=task,
        run_valid_hint=run_valid_hint,
        evidence_workspace=workspace,
    )
    schema_warnings = [
        f"{issue.path}: {issue.message}"
        for issue in validate_stage("task_verification_result", verification)
    ]
    validation_issues, contract_warnings = partition_task_verification_issues(
        verification,
        task_id,
    )
    validation_warnings = (
        schema_warnings
        + contract_warnings
        + rerun_path_issues
        + observation_evidence_warnings
        + _evidence_path_issues(verification, workspace)
    )
    process_usable = raw_note_available
    scientific_terminal = (
        process_usable
        and not validation_issues
        and verification.get("host_action") == "complete"
    )
    from .verification_result import verification_scientifically_successful
    scientific_successful = verification_scientifically_successful(verification)
    crop_result: dict[str, Any] = {"status": "not_applicable", "issues": []}
    asset_issues: list[str] = []
    copied_assets: list[str] = []
    published_assets: dict[str, list[str]] = {
        "local_assets": [],
        "paper_assets": [],
    }
    asset_manifest: list[dict[str, Any]] = []
    asset_candidates = {
        key: list(verification.get(key, []))
        if isinstance(verification.get(key), list)
        else []
        for key in ("local_assets", "paper_assets")
    }
    if scientific_terminal and not asset_candidates["local_assets"]:
        # Do not silently lose generated figures. These are presentation
        # attachments, explicitly not an additional scientific assessment.
        asset_candidates["local_assets"] = list(report_input.get("local_image_paths") or [])
        if asset_candidates["local_assets"]:
            verification.setdefault("asset_notes", []).append(
                "已有本地图作为展示附件交付；Reporter 未主动选图，这些附件不增加科学支持证据。")
    if scientific_terminal:
        try:
            crop_result = finalize_paper_target(
                paper_path=paper_path,
                workspace=workspace,
                task=task,
                task_id=task_id,
                candidates=report_input.get("figure_candidates", []),
                verification=verification,
            )
        except Exception as exc:
            crop_result = {
                "status": "unresolved",
                "issues": [
                    "optional paper crop failed: "
                    + redact_text(f"{type(exc).__name__}: {exc}")[:500]
                ],
            }
        write_json(
            task_audit_dir / f"round_{reporter_round_no:03d}_crop.json",
            crop_result,
        )
        finalized_paper_assets = verification.get("paper_assets")
        if isinstance(finalized_paper_assets, list) and finalized_paper_assets:
            asset_candidates["paper_assets"] = list(finalized_paper_assets)
        try:
            published_assets, materialization_warnings = _materialize_task_assets(
                asset_candidates=asset_candidates,
                workspace=workspace,
                task_id=task_id,
            )
        except Exception as exc:
            published_assets = {"local_assets": [], "paper_assets": []}
            materialization_warnings = [
                "optional report asset materialization failed: "
                + redact_text(f"{type(exc).__name__}: {exc}")[:500]
            ]
        verification["local_assets"] = published_assets["local_assets"]
        verification["paper_assets"] = published_assets["paper_assets"]
        canonical_asset_issues = _accepted_asset_issues(
            verification,
            workspace,
            task_id,
            crop_result=crop_result,
            require_verified_pdf_crop=paper_path.suffix.lower() == ".pdf",
        )
        asset_issues.extend(materialization_warnings)
        asset_issues.extend(canonical_asset_issues)
        if not canonical_asset_issues and any(published_assets.values()):
            try:
                copied_assets = _copy_task_assets(
                    source=workspace / REPORT_ASSETS_DIR / safe_label(task_id),
                    target=output_dir / REPORT_ASSETS_DIR / safe_label(task_id),
                )
            except (OSError, ValueError) as exc:
                asset_issues.append(
                    f"asset copy failed: {type(exc).__name__}: {exc}"
                )
                published_assets = {"local_assets": [], "paper_assets": []}
        asset_manifest = _task_asset_manifest(
            output_dir,
            task_id,
            published_assets,
        )
        if any(published_assets.values()) and len(asset_manifest) != sum(
            len(values) for values in published_assets.values()
        ):
            asset_issues.append("published asset manifest could not verify every declared image")
            published_assets = {"local_assets": [], "paper_assets": []}
            asset_manifest = []
    validation_warnings.extend(asset_issues)
    if verification and not validation_issues:
        verification = _normalize_verification_paths(
            verification=verification,
            workspace=workspace,
            output_dir=output_dir,
            published_assets=published_assets,
        )
    ok = process_usable and not validation_issues
    if verification:
        write_json(
            task_audit_dir
            / f"round_{reporter_round_no:03d}_verification.json",
            verification,
        )
    status: dict[str, Any] = {
        "ok": ok,
        "backend": "codex",
        "mode": "isolated_task_reporter",
        "task_id": task_id,
        "input_hash": input_hash,
        "cached": False,
        "round_no": reporter_round_no,
        "requested_round_no": requested_round_no,
        "workspace": str(workspace),
        "codex_status": codex_status,
        "process_warning": (
            None
            if codex_status.get("ok")
            else (
                codex_status.get("error")
                or codex_status.get("blocked_reason")
                or "reporter process ended after producing a usable verification"
            )
        ),
        "task_verification": verification,
        "validation_issues": validation_issues,
        "recovery_kind": "structure" if not raw_note_available or validation_issues else None,
        "validation_warnings": validation_warnings,
        "asset_issues": asset_issues,
        "asset_paths": copied_assets,
        "asset_manifest": asset_manifest,
        "scientific_successful": scientific_successful,
        "scientific_terminal": scientific_terminal,
        "scientific_outcome": verification.get("outcome"),
        "crop_status": crop_result.get("status"),
        "crop_result": crop_result,
        "terminal": scientific_terminal,
        "paper_asset_verified": bool(published_assets["paper_assets"]),
        "error": (
            None
            if ok
            else "Reporter did not produce a readable evidence note" if not raw_note_available
            else _task_reporter_reason(codex_status, validation_issues, [])
        ),
    }
    write_json(status_path, status)
    write_json(
        task_audit_dir / f"round_{reporter_round_no:03d}_status.json",
        status,
    )
    return status


def task_verifications_document(
    results: list[dict[str, Any]],
) -> dict[str, Any]:
    return aggregate_task_verifications(
        [
            result.get("task_verification")
            for result in results
            if isinstance(result, dict)
            and isinstance(result.get("task_verification"), dict)
        ]
    )
