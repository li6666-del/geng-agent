from __future__ import annotations

from copy import deepcopy
from contextlib import nullcontext
import hashlib
import json
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from .agentic_analysis import CODEX_ANALYSIS_BACKEND
from .json_utils import parse_json_object, pretty_json
from .outputs import write_json, write_text
from .pipeline_helpers import (
    _is_non_retryable_llm_error,
    _read_json_file,
    _temporary_client_timeout,
    build_json_inline_retry_prompt,
    build_json_scientific_retry_prompt,
    build_json_preservation_retry_prompt,
    build_text_only_evidence_prompt,
)
from .analysis_repair import preserved_science_issues, scientific_correction_paths
from .prompt_identity import analysis_contract_identity, scientific_cache_value
from .runtime_status import _load_valid_stage_cache, build_stage_cache_metadata
from .schema_models import model_for_stage, response_format_for_stage
from .schemas import ValidationIssue, format_issues, validate_stage
from .scientific_materiality import SCIENTIFIC_POLICY_ID
from .stage_cleanup import _clear_stage_outputs


def load_or_create_stage_json(
    pipeline: Any,
    *,
    output_path: Path,
    output_dir: Path,
    audit_dir: Path,
    prompt: str,
    stage_label: str,
    cleanup_stage: str,
    schema_stage: str,
    max_attempts: int,
    resume: bool,
    pre_validation: Callable[[dict[str, Any]], list[ValidationIssue]] | None = None,
    extra_validation: Callable[[dict[str, Any]], list[ValidationIssue]] | None = None,
    request_timeout: float | None = None,
    fallback_factory: Callable[[Exception], dict[str, Any] | None] | None = None,
    candidate_normalizer: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    repair_preservation_validator: (
        Callable[[dict[str, Any], dict[str, Any]], list[ValidationIssue]] | None
    ) = None,
    salvage_failed_candidates: bool = False,
    truncation_recovery: Callable[[str], dict[str, Any] | None] | None = None,
    images: list[Any] | None = None,
    client: Any = None,
    backend: str = "llm",
    cache_inputs: Any = None,
    codex_stage_runner: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    cache_reuse_enabled = cache_inputs is not None
    cache_metadata = build_stage_cache_metadata(
        stage_label=stage_label,
        schema_stage=schema_stage,
        prompt=prompt,
        policy_version=SCIENTIFIC_POLICY_ID,
        inputs={
            "scientific_inputs": scientific_cache_value(cache_inputs if cache_inputs is not None else {}),
            "contract": analysis_contract_identity(
                stage_label=stage_label, schema_stage=schema_stage,
                backend=backend, prompt=prompt, client=client or getattr(pipeline, "client", None),
            ),
            "images": [{"label": image.label, "mime_type": image.mime_type,
                        "sha256": hashlib.sha256(image.data_b64.encode("ascii")).hexdigest()}
                       for image in images or []],
        },
    )

    def _normalize_and_bind_cache(candidate: dict[str, Any]) -> dict[str, Any]:
        normalized = (
            candidate_normalizer(candidate)
            if candidate_normalizer is not None
            else candidate
        )
        meta = (
            dict(normalized.get("_meta", {}))
            if isinstance(normalized.get("_meta"), dict)
            else {}
        )
        meta["cache"] = cache_metadata
        normalized["_meta"] = meta
        return normalized

    cache_validation: Callable[[dict[str, Any]], list[ValidationIssue]] | None = None
    if pre_validation is not None or extra_validation is not None:

        def _combined_validation(parsed: dict[str, Any]) -> list[ValidationIssue]:
            issues = pre_validation(parsed) if pre_validation is not None else []
            if extra_validation is not None:
                issues.extend(extra_validation(parsed))
            return issues

        cache_validation = _combined_validation

    if resume and cache_reuse_enabled and output_path.exists():
        cached = _load_valid_stage_cache(
            path=output_path,
            audit_dir=audit_dir,
            stage_label=stage_label,
            schema_stage=schema_stage,
            extra_validation=cache_validation,
            expected_cache_metadata=cache_metadata,
        )
        if cached is not None:
            return cached

    if (
        resume
        and cache_reuse_enabled
        and salvage_failed_candidates
        and candidate_normalizer is not None
    ):
        candidates = sorted(
            audit_dir.glob(f"normalized_{stage_label}_attempt_*.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for candidate_path in candidates:
            try:
                raw_candidate = _read_json_file(candidate_path)
            except Exception:
                continue
            raw_meta = (
                raw_candidate.get("_meta")
                if isinstance(raw_candidate.get("_meta"), dict)
                else {}
            )
            raw_cache = (
                raw_meta.get("cache")
                if isinstance(raw_meta.get("cache"), dict)
                else {}
            )
            if raw_cache.get("fingerprint") != cache_metadata.get("fingerprint"):
                write_json(
                    audit_dir
                    / f"resume_rejected_{stage_label}_{candidate_path.stem}.json",
                    {
                        "ok": False,
                        "source": candidate_path.name,
                        "reason": (
                            "candidate cache scientific inputs or policy changed"
                        ),
                        "expected_cache": cache_metadata,
                        "actual_cache": raw_cache,
                    },
                )
                continue
            try:
                candidate = _normalize_and_bind_cache(raw_candidate)
            except Exception:
                continue
            candidate_issues = (
                pre_validation(candidate) if pre_validation is not None else []
            )
            if not candidate_issues:
                candidate_issues.extend(validate_stage(schema_stage, candidate))
            if not candidate_issues and extra_validation is not None:
                candidate_issues.extend(extra_validation(candidate))
            if candidate_issues:
                continue
            meta = (
                dict(candidate.get("_meta", {}))
                if isinstance(candidate.get("_meta"), dict)
                else {}
            )
            meta.update(
                {
                    "analysis_backend": backend,
                    "analysis_stage_label": stage_label,
                    "analysis_resume_source": candidate_path.name,
                }
            )
            meta["cache"] = cache_metadata
            candidate["_meta"] = meta
            write_json(output_path, candidate)
            try:
                preserved_output = output_path.relative_to(output_dir).as_posix()
            except ValueError:
                preserved_output = ""
            _clear_stage_outputs(
                output_dir,
                cleanup_stage,
                preserve_audit=True,
                preserve_paths={preserved_output} if preserved_output else set(),
            )
            write_json(
                audit_dir / f"resume_{stage_label}.json",
                {
                    "ok": True,
                    "source": candidate_path.name,
                    "mode": "deterministic_normalization",
                },
            )
            return candidate

    write_text(audit_dir / f"{stage_label}.md", prompt)
    try:
        if backend == CODEX_ANALYSIS_BACKEND:
            parsed = codex_stage_runner(
                prompt=prompt,
                stage_label=stage_label,
                schema_stage=schema_stage,
                output_dir=output_dir,
                audit_dir=audit_dir,
                max_attempts=max_attempts,
                pre_validation=pre_validation,
                extra_validation=extra_validation,
                candidate_normalizer=_normalize_and_bind_cache,
                repair_preservation_validator=repair_preservation_validator,
                truncation_recovery=truncation_recovery,
                images=images,
            )
        elif backend == "llm":
            parsed = pipeline._call_validated_json(
                prompt=prompt,
                stage_label=stage_label,
                schema_stage=schema_stage,
                audit_dir=audit_dir,
                max_attempts=max_attempts,
                pre_validation=pre_validation,
                extra_validation=extra_validation,
                request_timeout=request_timeout,
                candidate_normalizer=_normalize_and_bind_cache,
                repair_preservation_validator=repair_preservation_validator,
                truncation_recovery=truncation_recovery,
                images=images,
                client=client,
            )
        else:
            raise ValueError(f"unknown analysis backend: {backend}")
    except Exception as exc:
        if fallback_factory is None:
            raise
        parsed = fallback_factory(exc)
        if parsed is None:
            raise
        issues = pre_validation(parsed) if pre_validation is not None else []
        issues.extend(validate_stage(schema_stage, parsed))
        if extra_validation is not None:
            issues.extend(extra_validation(parsed))
        if issues:
            raise RuntimeError(
                f"{stage_label} local fallback did not pass validation: "
                f"{format_issues(issues)}"
            ) from exc
        write_json(
            audit_dir / f"local_fallback_{stage_label}.json",
            {
                "ok": True,
                "reason": parsed.get("_meta", {}).get("fallback_reason"),
                "fallback": parsed.get("_meta", {}),
            },
        )
    meta = (
        dict(parsed.get("_meta", {}))
        if isinstance(parsed.get("_meta"), dict)
        else {}
    )
    meta["cache"] = cache_metadata
    parsed["_meta"] = meta
    write_json(output_path, parsed)
    try:
        preserved_output = output_path.relative_to(output_dir).as_posix()
    except ValueError:
        preserved_output = ""
    _clear_stage_outputs(
        output_dir,
        cleanup_stage,
        preserve_audit=True,
        preserve_paths={preserved_output} if preserved_output else set(),
    )
    return parsed


def complete_maybe_multimodal(
    pipeline: Any,
    prompt: str,
    *,
    schema_stage: str,
    images: list[Any] | None,
    client: Any = None,
    system_message: str,
    input_observer: Callable[[str, list[Any], dict[str, Any]], None] | None = None,
) -> str:
    client = client or pipeline.client
    if client is None:
        raise RuntimeError("LLM client is required for analysis_backend='llm'")
    response_format = response_format_for_stage(schema_stage)
    def observe(actual_prompt: str, actual_images: list[Any], **visibility: Any) -> None:
        if input_observer is not None:
            input_observer(actual_prompt, actual_images, {
                "system_message": system_message, "response_format": response_format,
                **visibility,
            })

    if images:
        if hasattr(client, "complete_multimodal"):
            observe(prompt, images, mode="multimodal")
            try:
                return client.complete_multimodal(
                    prompt, images=images, system=system_message,
                    response_format=response_format,
                )
            except Exception as exc:
                detail = str(exc).lower()
                unsupported = any(signature in detail for signature in (
                    "unsupported image", "does not support image", "image input is not supported",
                    "vision is not supported", "image_url is not supported",
                ))
                if not unsupported or _is_non_retryable_llm_error(detail):
                    raise
                reason = "The provider explicitly rejected image input."
        else:
            reason = "The configured client has no multimodal completion capability."
        image_labels = [str(getattr(image, "label", "")) for image in images]
        prompt = build_text_only_evidence_prompt(prompt, image_labels, reason)
        observe(prompt, [], mode="text_only_downgrade", omitted_image_labels=image_labels, reason=reason)
    else:
        observe(prompt, [], mode="text")
    return client.complete(
        prompt,
        system=system_message,
        response_format=response_format,
    )


def call_validated_json(
    pipeline: Any,
    prompt: str,
    stage_label: str,
    schema_stage: str,
    audit_dir: Path,
    max_attempts: int,
    pre_validation: Callable[[dict[str, Any]], list[ValidationIssue]] | None = None,
    extra_validation: Callable[[dict[str, Any]], list[ValidationIssue]] | None = None,
    request_timeout: float | None = None,
    candidate_normalizer: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    repair_preservation_validator: Callable[[dict[str, Any], dict[str, Any]], list[ValidationIssue]] | None = None,
    truncation_recovery: Callable[[str], dict[str, Any] | None] | None = None,
    images: list[Any] | None = None,
    client: Any = None,
) -> dict[str, Any]:
    client = client or pipeline.client
    current_prompt = prompt
    last_errors = ""
    schema_text = pretty_json(model_for_stage(schema_stage).model_json_schema())
    repair_baseline = None
    authorized_paths: list[str] = []
    format_repair = False
    attempts = max(1, int(max_attempts or 1))
    delivered_visibility: dict[str, Any] = {}
    for attempt in range(1, attempts + 1):
        active_images = [] if format_repair else images
        def record_input(actual_prompt: str, actual_images: list[Any], visibility: dict[str, Any]) -> None:
            if not format_repair:
                # Formatting/restoration does not re-extract science, so its
                # lack of attachments must not erase the reasoning call's
                # actual visual coverage from the resulting document.
                delivered_visibility.clear()
                delivered_visibility.update({key: value for key, value in visibility.items() if key not in {"system_message", "response_format"}})
                delivered_visibility["supplied_image_labels"] = [image.label for image in actual_images]
            snapshot = audit_dir / "api_prompt_inputs" / uuid4().hex
            snapshot.mkdir(parents=True, exist_ok=True)
            write_text(snapshot / "brief.md", actual_prompt)
            write_text(audit_dir / f"{stage_label}_llm_attempt_{attempt}_brief.md", actual_prompt)
            manifest = {
                "backend": "llm", "prompt_sha256": hashlib.sha256(actual_prompt.encode("utf-8")).hexdigest(),
                "mode": "format_repair" if format_repair else "evidence_reasoning",
                "visibility": visibility,
                "prompt_path": str(snapshot / "brief.md"),
                "images": [{"label": image.label, "mime_type": image.mime_type,
                            "sha256": hashlib.sha256(image.data_b64.encode("ascii")).hexdigest()}
                           for image in actual_images or []],
            }
            write_json(snapshot / "input.json", manifest)
            write_json(audit_dir / f"{stage_label}_llm_attempt_{attempt}_input.json", manifest)
        try:
            request_audit = (
                client.audit_requests(audit_dir / "api_requests", f"{stage_label}_attempt_{attempt}")
                if hasattr(client, "audit_requests") else nullcontext()
            )
            with _temporary_client_timeout(client, request_timeout), request_audit:
                raw = pipeline._complete_maybe_multimodal(
                    current_prompt,
                    schema_stage=schema_stage,
                    images=active_images,
                    client=client,
                    input_observer=record_input,
                )
        except Exception as exc:
            last_errors = f"LLM request error: {type(exc).__name__}: {exc}"
            write_json(
                audit_dir / f"validation_{stage_label}_attempt_{attempt}.json",
                {"ok": False, "errors": [{"path": "$", "message": last_errors}]},
            )
            write_json(
                audit_dir / f"llm_error_{stage_label}_attempt_{attempt}.json",
                {"stage": stage_label, "attempt": attempt, "error": last_errors},
            )
            if _is_non_retryable_llm_error(last_errors):
                raise RuntimeError(
                    f"{stage_label} LLM request failed: {last_errors}"
                ) from exc
            continue
        write_text(audit_dir / f"raw_{stage_label}_attempt_{attempt}.txt", raw)
        write_text(audit_dir / f"raw_{stage_label}.txt", raw)

        try:
            parsed = parse_json_object(raw)
        except Exception as exc:
            recovered = (
                truncation_recovery(raw) if truncation_recovery is not None else None
            )
            if recovered is None:
                last_errors = f"JSON parse error: {exc}"
                write_json(
                    audit_dir / f"validation_{stage_label}_attempt_{attempt}.json",
                    {
                        "ok": False,
                        "errors": [{"path": "$", "message": last_errors}],
                    },
                )
                current_prompt = build_json_inline_retry_prompt(
                    candidate_text=raw, schema_text=schema_text,
                    issues=[ValidationIssue("$", last_errors)],
                )
                format_repair = True
                continue
            parsed = recovered

        if candidate_normalizer is not None:
            parsed = candidate_normalizer(parsed)
        if repair_baseline is None:
            repair_baseline = deepcopy(parsed)

        normalization_issues = (
            pre_validation(parsed) if pre_validation is not None else []
        )
        schema_issues = validate_stage(schema_stage, parsed)
        preservation_issues = [] if schema_issues else preserved_science_issues(
            repair_preservation_validator, repair_baseline, parsed, authorized_paths,
        )
        scientific_issues = extra_validation(parsed) if not schema_issues and extra_validation is not None else []
        issues = [*normalization_issues, *schema_issues, *preservation_issues, *scientific_issues]
        if not issues:
            meta = dict(parsed.get("_meta", {})) if isinstance(parsed.get("_meta"), dict) else {}
            meta["evidence_visibility"] = dict(delivered_visibility)
            parsed["_meta"] = meta
            write_json(
                audit_dir / f"validation_{stage_label}_attempt_{attempt}.json",
                {"ok": True, "errors": []},
            )
            return parsed

        last_errors = format_issues(issues)
        write_json(
            audit_dir / f"validation_{stage_label}_attempt_{attempt}.json",
            {"ok": False, "errors": [issue.as_dict() for issue in issues],
             "categories": {
                 "normalization": [issue.as_dict() for issue in normalization_issues],
                 "schema": [issue.as_dict() for issue in schema_issues],
                 "preservation": [issue.as_dict() for issue in preservation_issues],
                 "scientific": [issue.as_dict() for issue in scientific_issues],
             }},
        )
        write_json(audit_dir / f"normalized_{stage_label}_attempt_{attempt}.json", parsed)
        if normalization_issues or scientific_issues:
            authorized_paths = list(dict.fromkeys([
                *authorized_paths,
                *scientific_correction_paths([*scientific_issues, *normalization_issues], parsed, repair_baseline),
            ]))
            current_prompt = build_json_scientific_retry_prompt(
                candidate_text=pretty_json(parsed), schema_text=schema_text,
                issues=issues, original_task=prompt, authorized_paths=authorized_paths,
                preservation_baseline_text=pretty_json(repair_baseline) if preservation_issues else None,
            )
            format_repair = False
        elif preservation_issues:
            current_prompt = build_json_preservation_retry_prompt(
                candidate_text=pretty_json(parsed), baseline_text=pretty_json(repair_baseline),
                schema_text=schema_text, issues=issues, authorized_paths=authorized_paths,
            )
            format_repair = True
        else:
            current_prompt = build_json_inline_retry_prompt(
                candidate_text=pretty_json(parsed), schema_text=schema_text, issues=issues,
            )
            format_repair = True

    raise RuntimeError(
        f"{stage_label} did not pass JSON validation after {max_attempts} "
        f"attempts: {last_errors}"
    )
