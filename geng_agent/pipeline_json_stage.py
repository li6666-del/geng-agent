from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .agentic_analysis import CODEX_ANALYSIS_BACKEND
from .json_utils import parse_json_object, pretty_json
from .outputs import write_json, write_text
from .pipeline_helpers import (
    _is_non_retryable_llm_error,
    _read_json_file,
    _temporary_client_timeout,
    build_json_retry_prompt,
    summarize_bad_output,
)
from .runtime_status import _load_valid_stage_cache, build_stage_cache_metadata
from .schema_models import response_format_for_stage
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
        inputs=cache_inputs if cache_inputs is not None else {},
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
) -> str:
    client = client or pipeline.client
    if client is None:
        raise RuntimeError("LLM client is required for analysis_backend='llm'")
    response_format = response_format_for_stage(schema_stage)
    if images and hasattr(client, "complete_multimodal"):
        try:
            return client.complete_multimodal(
                prompt,
                images=images,
                system=system_message,
                response_format=response_format,
            )
        except Exception:
            pass
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
    truncation_recovery: Callable[[str], dict[str, Any] | None] | None = None,
    images: list[Any] | None = None,
    client: Any = None,
) -> dict[str, Any]:
    client = client or pipeline.client
    current_prompt = prompt
    last_errors = ""
    for attempt in range(1, max_attempts + 1):
        try:
            with _temporary_client_timeout(client, request_timeout):
                raw = pipeline._complete_maybe_multimodal(
                    current_prompt,
                    schema_stage=schema_stage,
                    images=images,
                    client=client,
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
            current_prompt = prompt
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
                current_prompt = build_json_retry_prompt(
                    prompt, summarize_bad_output(raw), last_errors
                )
                continue
            parsed = recovered

        if candidate_normalizer is not None:
            parsed = candidate_normalizer(parsed)

        normalization_issues = (
            pre_validation(parsed) if pre_validation is not None else []
        )
        issues = normalization_issues or validate_stage(schema_stage, parsed)
        if not issues and extra_validation is not None:
            issues.extend(extra_validation(parsed))
        if not issues:
            write_json(
                audit_dir / f"validation_{stage_label}_attempt_{attempt}.json",
                {"ok": True, "errors": []},
            )
            return parsed

        last_errors = format_issues(issues)
        write_json(
            audit_dir / f"validation_{stage_label}_attempt_{attempt}.json",
            {"ok": False, "errors": [issue.as_dict() for issue in issues]},
        )
        if normalization_issues:
            raise RuntimeError(
                f"{stage_label} deterministic normalization conflict: {last_errors}"
            )
        current_prompt = build_json_retry_prompt(
            prompt, summarize_bad_output(pretty_json(parsed)), last_errors
        )

    raise RuntimeError(
        f"{stage_label} did not pass JSON validation after {max_attempts} "
        f"attempts: {last_errors}"
    )
