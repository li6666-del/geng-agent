"""Round 3.5: static code-faithfulness review of the generated reproduction project.

Local structural checks (schema, paths, syntax) cannot see *content* errors -- e.g. a
constellation built from the wrong levels, a flipped sign, or Eb/Es confusion. This
module adds a general, paper-agnostic content check: an LLM critic verifies that the
generated code faithfully implements the already-extracted spec (engineering_facts +
repro_tasks). Generality comes from anchoring to those per-paper artifacts rather than
to any hard-coded domain assertions.

Guardrails that keep the (probabilistic) critic from destabilizing good runs:
- dual-evidence grounding: every finding must quote BOTH a spec excerpt that really
  appears in facts/tasks AND a code excerpt that really appears in the project; findings
  failing either are dropped locally (kills hallucinated findings).
- severity gate: only ``blocking`` findings trigger a revise; ``minor`` are recorded.
- bounded revise loop reusing the RepairManifest contract; on exhaustion it does NOT
  hard-block or fall back to a template -- it keeps the code and records residual
  findings so the risk report stays honest.

It is a faithfulness gate, not a correctness proof: it cannot verify numeric agreement
with the paper (that is the runtime/multimodal layer's job), and it is bounded by the
quality of the extracted spec.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from .json_utils import parse_json_object, pretty_json
from .llm import LLMClient
from .outputs import validate_repro_project, write_file_manifest, write_json, write_text
from .prompts import PromptBook
from .schema_models import response_format_for_stage
from .schemas import format_issues, validate_stage

_CODE_SUFFIXES = {".py", ".json", ".txt", ".md"}
_SKIP_DIRS = {"outputs", "repair_logs", "__pycache__"}
_SPEC_WINDOW = 8   # spec excerpts may be paraphrased -> shorter anchor
_CODE_WINDOW = 12  # code excerpts should be quoted near-verbatim


def collect_project_code(repro_project_dir: Path, max_chars: int = 60000) -> str:
    parts: list[str] = []
    for path in sorted(repro_project_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(repro_project_dir)
        if any(part in _SKIP_DIRS for part in rel.parts):
            continue
        if path.suffix.lower() not in _CODE_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        parts.append(f"### FILE: {rel.as_posix()}\n{text}")
    return "\n\n".join(parts)[:max_chars]


def _norm(text: str) -> str:
    return "".join(ch.lower() for ch in text if not ch.isspace())


def _grounded(evidence: Any, haystack_norm: str, window: int) -> bool:
    if not isinstance(evidence, str):
        return False
    needle = _norm(evidence)
    if not needle:
        return False
    if len(needle) <= window:
        return needle in haystack_norm
    return any(needle[i : i + window] in haystack_norm for i in range(0, len(needle) - window + 1))


def filter_grounded_findings(
    findings: list[dict[str, Any]], facts: Any, tasks: Any, repro_project_dir: Path
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Keep only findings whose spec AND code excerpts actually appear in the inputs."""
    spec_norm = _norm(pretty_json(facts) + pretty_json(tasks))
    code_norm = _norm(collect_project_code(repro_project_dir))
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for finding in findings:
        if not isinstance(finding, dict):
            dropped.append({"reason": "not an object"})
            continue
        spec_ok = _grounded(finding.get("evidence_spec"), spec_norm, _SPEC_WINDOW)
        code_ok = _grounded(finding.get("evidence_code"), code_norm, _CODE_WINDOW)
        if spec_ok and code_ok:
            kept.append(finding)
        else:
            dropped.append({"spec_ref": finding.get("spec_ref"), "spec_ok": spec_ok, "code_ok": code_ok})
    return kept, dropped


def _call_validated(
    client: LLMClient, prompt: str, *, stage: str, system: str, audit_dir: Path, label: str, max_attempts: int = 3
) -> dict[str, Any]:
    current = prompt
    last = ""
    for attempt in range(1, max_attempts + 1):
        raw = client.complete(current, system=system, response_format=response_format_for_stage(stage))
        write_text(audit_dir / f"raw_{label}_attempt_{attempt}.txt", raw)
        try:
            parsed = parse_json_object(raw)
        except Exception as exc:  # noqa: BLE001 - any parse failure -> retry
            last = f"JSON parse error: {exc}"
            current = f"{prompt}\n\n# 上次失败\n{last}\n只输出合法 JSON object。"
            continue
        issues = validate_stage(stage, parsed)
        if not issues:
            return parsed
        last = format_issues(issues)
        current = f"{prompt}\n\n# 上次校验失败\n{last}\n请修正并只输出合法 JSON object。"
    raise RuntimeError(f"{stage} did not pass validation after {max_attempts} attempts: {last}")


def run_code_faithfulness_review(
    *,
    client: LLMClient,
    prompt_book: PromptBook,
    repro_project_dir: Path,
    audit_dir: Path,
    facts: dict[str, Any],
    tasks: dict[str, Any],
    paper_context: str,
    system_message: str,
    max_revise_attempts: int = 1,
) -> dict[str, Any]:
    facts_json = pretty_json(facts)
    tasks_json = pretty_json(tasks)
    reviews: list[dict[str, Any]] = []
    revised = False
    # Best-of: track the fewest-blocking COMPILABLE state across revise rounds so that on
    # exhaustion we restore the best-reviewed version (not whatever the last revise produced)
    # and never fall back to a template.
    best_snapshot: Path | None = None
    best_blocking: int | None = None
    best_findings: list[dict[str, Any]] = []

    review_no = 0
    while True:
        review_no += 1
        label = f"code_review_{review_no:02d}"
        prompt = prompt_book.render(
            "review_repro_project_code.md",
            engineering_facts_json=facts_json,
            repro_tasks_json=tasks_json,
            project_files=collect_project_code(repro_project_dir),
            paper_context_json=paper_context,
        )
        try:
            review = _call_validated(client, prompt, stage="code_faithfulness_review", system=system_message, audit_dir=audit_dir, label=label)
        except Exception as exc:  # noqa: BLE001 - review could not run -> do NOT block
            return {
                "enabled": True,
                "passed": None,
                "revised": revised,
                "review_attempts": review_no - 1,
                "reviews": reviews,
                "unresolved_findings": [],
                "error": str(exc),
            }

        kept, dropped = filter_grounded_findings(review.get("findings", []), facts, tasks, repro_project_dir)
        blocking = [f for f in kept if f.get("severity") == "blocking"]
        minor = [f for f in kept if f.get("severity") != "blocking"]
        summary = {
            "attempt": review_no,
            "verdict": review.get("verdict"),
            "blocking": len(blocking),
            "minor": len(minor),
            "dropped_ungrounded": len(dropped),
            "findings": kept,
            "note": review.get("note"),
        }
        reviews.append(summary)
        write_json(audit_dir / f"code_review_{review_no:02d}.json", summary)

        if bool(validate_repro_project(repro_project_dir).get("python_compiles")) and (
            best_blocking is None or len(blocking) < best_blocking
        ):
            best_snapshot = _snapshot_project(repro_project_dir, audit_dir / "code_review_best_snapshot")
            best_blocking = len(blocking)
            best_findings = blocking

        if not blocking:
            return {
                "enabled": True,
                "passed": True,
                "revised": revised,
                "review_attempts": review_no,
                "reviews": reviews,
                "unresolved_findings": [],
            }
        if review_no > max_revise_attempts:
            kept_best = False
            unresolved = blocking
            if best_snapshot is not None and best_blocking is not None and best_blocking < len(blocking):
                # The last revise regressed vs an earlier version: restore the best-reviewed
                # compilable state instead of keeping the regression (never fall back here).
                _restore_project(best_snapshot, repro_project_dir)
                kept_best = True
                unresolved = best_findings
            return {
                "enabled": True,
                "passed": False,
                "revised": revised,
                "review_attempts": review_no,
                "reviews": reviews,
                "unresolved_findings": unresolved,
                "kept_best_of_compilable": kept_best,
                "kept_blocking_findings": best_blocking,
            }

        # Revise: turn blocking findings into a RepairManifest and apply it in place.
        repair_prompt = prompt_book.render(
            "repair_repro_project_for_review.md",
            review_findings_json=pretty_json(blocking),
            engineering_facts_json=facts_json,
            repro_tasks_json=tasks_json,
            project_files=collect_project_code(repro_project_dir),
        )
        try:
            manifest = _call_validated(client, repair_prompt, stage="repair_manifest", system=system_message, audit_dir=audit_dir, label=f"code_review_revise_{review_no:02d}")
            write_file_manifest(manifest, repro_project_dir)
            revised = True
        except Exception as exc:  # noqa: BLE001 - revise failed -> keep best-of, report
            if best_snapshot is not None and best_blocking is not None and best_blocking < len(blocking):
                _restore_project(best_snapshot, repro_project_dir)
            return {
                "enabled": True,
                "passed": False,
                "revised": revised,
                "review_attempts": review_no,
                "reviews": reviews,
                "unresolved_findings": best_findings
                if (best_snapshot is not None and best_blocking is not None and best_blocking < len(blocking))
                else blocking,
                "revise_error": str(exc),
            }


def _snapshot_project(src: Path, dst: Path) -> Path:
    """Copy the current repro project (source files only) to dst, overwriting any prior
    snapshot. Used to remember the best-reviewed compilable state during code review."""
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("outputs", "repair_logs", "__pycache__", "*.pyc"))
    return dst


def _restore_project(snapshot: Path, dst: Path) -> None:
    """Copy a snapshot's files back over the project dir, restoring the best-reviewed state."""
    for path in snapshot.rglob("*"):
        if path.is_file():
            target = dst / path.relative_to(snapshot)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)


def _ground_findings_against(
    findings: list[dict[str, Any]], facts: Any, tasks: Any, code_text: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Like filter_grounded_findings but grounds the code excerpt against a single in-memory
    code string -- used for per-file review during generation, before files are on disk."""
    spec_norm = _norm(pretty_json(facts) + pretty_json(tasks))
    code_norm = _norm(code_text)
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for finding in findings:
        if not isinstance(finding, dict):
            dropped.append({"reason": "not an object"})
            continue
        spec_ok = _grounded(finding.get("evidence_spec"), spec_norm, _SPEC_WINDOW)
        code_ok = _grounded(finding.get("evidence_code"), code_norm, _CODE_WINDOW)
        if spec_ok and code_ok:
            kept.append(finding)
        else:
            dropped.append({"spec_ref": finding.get("spec_ref"), "spec_ok": spec_ok, "code_ok": code_ok})
    return kept, dropped


def review_single_generated_file(
    *,
    client: LLMClient,
    prompt_book: PromptBook,
    target_path: str,
    content: str,
    facts: dict[str, Any],
    tasks: dict[str, Any],
    paper_context: str,
    prior_files_context: str,
    system_message: str,
    audit_dir: Path,
    label: str,
) -> dict[str, Any]:
    """Faithfulness-review ONE just-generated file (in memory, before it is written to disk)
    against the extracted spec. Returns {verdict, blocking, minor, dropped, error?}. A finding
    only counts when its spec excerpt is grounded in facts/tasks AND its code excerpt is
    grounded in THIS file -- so it reviews the target file, not the prior files shown only as
    reference. On any review error it returns no blocking findings (never blocks generation)."""
    if prior_files_context:
        project_files = (
            f"== 已生成文件（仅供参考，不要评审这些） ==\n{prior_files_context}\n\n"
            f"== 待评审目标文件: {target_path} ==\n{content}"
        )
    else:
        project_files = f"== 待评审目标文件: {target_path} ==\n{content}"
    prompt = prompt_book.render(
        "review_repro_project_code.md",
        engineering_facts_json=pretty_json(facts),
        repro_tasks_json=pretty_json(tasks),
        project_files=project_files,
        paper_context_json=paper_context,
    )
    try:
        review = _call_validated(
            client, prompt, stage="code_faithfulness_review", system=system_message, audit_dir=audit_dir, label=label
        )
    except Exception as exc:  # noqa: BLE001 - review could not run -> do NOT block generation
        return {"verdict": None, "blocking": [], "minor": [], "dropped": 0, "error": str(exc)}
    kept, dropped = _ground_findings_against(review.get("findings", []), facts, tasks, content)
    blocking = [finding for finding in kept if finding.get("severity") == "blocking"]
    minor = [finding for finding in kept if finding.get("severity") != "blocking"]
    return {"verdict": review.get("verdict"), "blocking": blocking, "minor": minor, "dropped": len(dropped)}
