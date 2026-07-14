from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .paper_evidence import safe_label


PAPER_TARGET_METADATA_FILE = "paper_target_metadata.json"
FIGURE_TARGET_RE = re.compile(r"(?i)\bfig(?:\.|ure)?\s*([0-9]+)")
PAREN_SUBFIGURE_RE = re.compile(r"(?i)^\s*\(([a-z])\)")
ADJACENT_SUBFIGURE_RE = re.compile(r"(?i)^([a-z])\b")


def finalize_paper_target(
    *,
    paper_path: Path,
    workspace: Path,
    task: dict[str, Any],
    task_id: str,
    candidates: list[dict[str, Any]],
    verification: dict[str, Any],
) -> dict[str, Any]:
    """Replace model-made paper crops with deterministic high-resolution PDF crops when possible."""
    asset_dir = workspace / "report_assets" / safe_label(task_id)
    asset_dir.mkdir(parents=True, exist_ok=True)
    target_path = asset_dir / "paper_target.png"
    metadata_path = workspace / PAPER_TARGET_METADATA_FILE
    metadata = _read_json_object(metadata_path)
    target_figure, target_subfigure = _task_figure_target(task)
    selected, selection_reason, rejected_candidate_id = _select_candidate(candidates, metadata, target_figure)
    if selected and target_subfigure is None and not _whole_figure_visual_check_passes(metadata):
        rejected_candidate_id = str(selected.get("candidate_id") or "").strip() or rejected_candidate_id
        selected = None
        selection_reason = "candidate_boundary_not_verified"
    result: dict[str, Any] = {
        "status": "unresolved",
        "target_figure": target_figure,
        "target_subfigure": target_subfigure,
        "source_page": None,
        "parent_bbox_norm": None,
        "crop_bbox_norm": None,
        "candidate_id": selected.get("candidate_id") if selected else None,
        "rejected_candidate_id": rejected_candidate_id,
        "candidate_status": metadata.get("candidate_status"),
        "selection_reason": selection_reason,
        "source_mode": None,
        "metadata_path": str(metadata_path) if metadata_path.is_file() else None,
        "output_path": None,
        "output_sha256": None,
        "issues": [],
    }

    if selected and paper_path.suffix.lower() == ".pdf":
        parent_bbox = _valid_bbox(selected.get("bbox_norm"))
        page = _page_number(selected.get("page"))
        result["source_page"] = page
        result["parent_bbox_norm"] = parent_bbox
        crop_bbox: list[float] | None = None
        status = "complete_figure"
        if target_subfigure:
            crop_bbox = _valid_bbox(metadata.get("bbox_norm") or metadata.get("crop_bbox_norm"))
            if crop_bbox is None:
                relative = _valid_bbox(metadata.get("child_bbox_relative") or metadata.get("bbox_relative"))
                crop_bbox = _relative_to_page(parent_bbox, relative)
            if (
                crop_bbox is not None
                and parent_bbox is not None
                and _mostly_inside(crop_bbox, parent_bbox)
                and _subfigure_visual_check_passes(metadata)
            ):
                status = "exact_subfigure"
            else:
                crop_bbox = parent_bbox
                status = "fallback_parent_figure"
                result["issues"].append(
                    "No visually verified subfigure bbox was supplied; used the complete parent figure."
                )
        else:
            crop_bbox = parent_bbox
        if page and crop_bbox and _crop_pdf_region_atomic(
            paper_path=paper_path,
            page=page,
            bbox_norm=crop_bbox,
            target=target_path,
        ):
            result["status"] = status
            result["source_mode"] = "verified_mineru_candidate"
            result["crop_bbox_norm"] = crop_bbox
            result["output_path"] = str(target_path)
            result["output_sha256"] = _file_sha256(target_path)
            verification["paper_assets"] = [
                f"report_assets/{safe_label(task_id)}/{target_path.name}"
            ]
            _record_crop_uncertainty(verification, result)
            return result
        result["issues"].append("Deterministic PDF crop failed.")

    manual_crop = _manual_crop_spec(metadata, workspace)
    if manual_crop and paper_path.suffix.lower() == ".pdf":
        manual_page, manual_bbox = manual_crop
        if _crop_pdf_region_atomic(
            paper_path=paper_path,
            page=manual_page,
            bbox_norm=manual_bbox,
            target=target_path,
            add_margin=False,
        ):
            result["status"] = "verified_manual_page_crop"
            result["source_mode"] = "reporter_manual_page_crop"
            result["source_page"] = manual_page
            result["crop_bbox_norm"] = manual_bbox
            result["output_path"] = str(target_path)
            result["output_sha256"] = _file_sha256(target_path)
            verification["paper_assets"] = [f"report_assets/{safe_label(task_id)}/{target_path.name}"]
            return result
        result["issues"].append("Reporter manual PDF crop could not be regenerated.")

    if _valid_existing_image(target_path):
        result["status"] = (
            "reporter_provided_crop"
            if rejected_candidate_id or selection_reason.startswith("reporter_")
            else "legacy_reporter_crop"
        )
        result["source_mode"] = "reporter_provided_crop"
        result["output_path"] = str(target_path)
        result["output_sha256"] = _file_sha256(target_path)
        result["issues"].append("Used the reporter-provided crop because no deterministic MinerU crop was available.")
        verification["paper_assets"] = [f"report_assets/{safe_label(task_id)}/{target_path.name}"]
        _record_crop_uncertainty(verification, result)
        return result

    result["issues"].append("No usable paper target image is available.")
    verification["paper_assets"] = []
    return result


def _record_crop_uncertainty(verification: dict[str, Any], result: dict[str, Any]) -> None:
    status = str(result.get("status") or "")
    if status not in {"fallback_parent_figure", "legacy_reporter_crop", "reporter_provided_crop"}:
        return
    uncertainties = verification.setdefault("remaining_uncertainties", [])
    if not isinstance(uncertainties, list):
        uncertainties = []
        verification["remaining_uncertainties"] = uncertainties
    if status == "fallback_parent_figure":
        message = "The exact subfigure boundary could not be verified; the complete parent figure is shown."
    elif status == "reporter_provided_crop":
        message = "The MinerU candidate was rejected or unverified; the reporter-provided paper crop is shown."
    else:
        message = "MinerU did not provide a deterministic candidate; the reporter-provided paper crop is shown."
    if message not in uncertainties:
        uncertainties.append(message)


def _subfigure_visual_check_passes(metadata: dict[str, Any]) -> bool:
    visual_check = metadata.get("visual_check")
    if not isinstance(visual_check, dict):
        return False
    required = (
        "target_identity_confirmed",
        "panel_boundary_complete",
        "axes_and_labels_complete",
        "legend_and_annotations_complete",
        "compared_against_parent",
    )
    return all(visual_check.get(key) is True for key in required)


def _whole_figure_visual_check_passes(metadata: dict[str, Any]) -> bool:
    if str(metadata.get("candidate_status") or "").strip().lower() != "accepted":
        return False
    visual_check = metadata.get("visual_check")
    if not isinstance(visual_check, dict):
        return False
    required = (
        "target_identity_confirmed",
        "figure_content_complete",
        "panel_boundary_complete",
        "axes_and_labels_complete",
        "legend_and_annotations_complete",
        "caption_complete",
        "no_adjacent_content",
        "compared_against_parent",
    )
    return all(visual_check.get(key) is True for key in required)


def _select_candidate(
    candidates: list[dict[str, Any]],
    metadata: dict[str, Any],
    target_figure: str | None,
) -> tuple[dict[str, Any] | None, str, str | None]:
    candidate_id = str(metadata.get("candidate_id") or "").strip()
    rejected_candidate_id = str(metadata.get("rejected_candidate_id") or candidate_id).strip() or None
    rejection_reason = _metadata_candidate_rejection_reason(metadata)
    if rejection_reason:
        return None, rejection_reason, rejected_candidate_id
    if candidate_id:
        for candidate in candidates:
            if str(candidate.get("candidate_id") or "") == candidate_id:
                if _candidate_is_ambiguous(candidate) and not _metadata_confirms_candidate(metadata):
                    return None, "ambiguous_candidate_requires_reporter_confirmation", candidate_id
                return candidate, "reporter_selected_candidate", None
    source_page = _page_number(metadata.get("source_page"))
    if source_page:
        page_matches = [candidate for candidate in candidates if _page_number(candidate.get("page")) == source_page]
        if len(page_matches) == 1:
            if _candidate_is_ambiguous(page_matches[0]):
                return None, "ambiguous_candidate_requires_reporter_confirmation", str(page_matches[0].get("candidate_id") or "") or None
            return page_matches[0], "unique_candidate_on_reporter_page", None
    if target_figure:
        figure_matches = [candidate for candidate in candidates if str(candidate.get("figure_number") or "") == target_figure]
        if len(figure_matches) == 1:
            if _candidate_is_ambiguous(figure_matches[0]):
                return None, "ambiguous_candidate_requires_reporter_confirmation", str(figure_matches[0].get("candidate_id") or "") or None
            return figure_matches[0], "unique_candidate_for_target_figure", None
    if len(candidates) == 1:
        candidate = candidates[0]
        candidate_id = str(candidate.get("candidate_id") or "") or None
        if _candidate_is_ambiguous(candidate):
            return None, "ambiguous_candidate_requires_reporter_confirmation", candidate_id
        return candidate, "single_unambiguous_candidate", None
    return None, "no_unique_verified_candidate", None


def _metadata_candidate_rejection_reason(metadata: dict[str, Any]) -> str | None:
    status = str(metadata.get("candidate_status") or "").strip().lower()
    if status in {
        "rejected",
        "rejected_wrong_identity",
        "rejected_incomplete_boundary",
        "unverified",
        "ambiguous",
    }:
        return f"reporter_{status}"
    visual_check = metadata.get("visual_check")
    if not isinstance(visual_check, dict):
        return None
    if visual_check.get("target_identity_confirmed") is False:
        return "reporter_rejected_target_identity"
    if visual_check.get("mineru_candidate_identity_confirmed") is False:
        return "reporter_rejected_mineru_candidate_identity"
    return None


def _metadata_confirms_candidate(metadata: dict[str, Any]) -> bool:
    if str(metadata.get("candidate_status") or "").strip().lower() == "accepted":
        return True
    visual_check = metadata.get("visual_check")
    return isinstance(visual_check, dict) and visual_check.get("target_identity_confirmed") is True


def _candidate_is_ambiguous(candidate: dict[str, Any]) -> bool:
    return str(candidate.get("identity_status") or "") == "ambiguous_multi_figure_caption"


def _task_figure_target(task: dict[str, Any]) -> tuple[str | None, str | None]:
    text = " ".join(str(task.get(key) or "") for key in ("figure_or_claim", "target", "task_id"))
    match = FIGURE_TARGET_RE.search(text)
    if not match:
        return None, None
    tail = text[match.end():]
    subfigure = PAREN_SUBFIGURE_RE.match(tail) or ADJACENT_SUBFIGURE_RE.match(tail)
    return match.group(1), subfigure.group(1).lower() if subfigure else None


def _relative_to_page(parent: list[float] | None, child: list[float] | None) -> list[float] | None:
    if parent is None or child is None:
        return None
    width, height = parent[2] - parent[0], parent[3] - parent[1]
    return _valid_bbox(
        [
            parent[0] + child[0] * width,
            parent[1] + child[1] * height,
            parent[0] + child[2] * width,
            parent[1] + child[3] * height,
        ]
    )


def _mostly_inside(child: list[float], parent: list[float]) -> bool:
    tolerance = 0.015
    return (
        child[0] >= parent[0] - tolerance
        and child[1] >= parent[1] - tolerance
        and child[2] <= parent[2] + tolerance
        and child[3] <= parent[3] + tolerance
    )


def _manual_crop_spec(metadata: dict[str, Any], workspace: Path) -> tuple[int, list[float]] | None:
    nested = metadata.get("manual_crop")
    manual = nested if isinstance(nested, dict) else {}
    page = _page_number(manual.get("source_page") or metadata.get("source_page"))
    if page is None:
        return None

    normalized = _valid_bbox(
        manual.get("bbox_norm")
        or manual.get("crop_bbox_norm")
        or metadata.get("manual_crop_bbox_norm")
    )
    if normalized is not None:
        return page, normalized

    pixels = _pixel_bbox(
        manual.get("bbox_pixels")
        or manual.get("pixel_bbox")
        or metadata.get("manual_crop_pixel_bbox")
    )
    if pixels is None:
        return None
    source_raw = str(
        manual.get("source_image")
        or manual.get("source")
        or metadata.get("manual_crop_source")
        or f"paper_evidence/full_paper_pages/paper_page_{page:03d}.png"
    ).strip()
    source = _workspace_file(workspace, source_raw)
    if source is None:
        return None
    try:
        from PIL import Image

        with Image.open(source) as image:
            width, height = image.size
    except Exception:
        return None
    if width <= 0 or height <= 0:
        return None
    normalized = _valid_bbox(
        [pixels[0] / width, pixels[1] / height, pixels[2] / width, pixels[3] / height]
    )
    return (page, normalized) if normalized is not None else None


def _workspace_file(workspace: Path, raw_path: str) -> Path | None:
    if not raw_path:
        return None
    try:
        root = workspace.resolve()
        candidate = Path(raw_path)
        path = candidate.resolve() if candidate.is_absolute() else (workspace / candidate).resolve()
        inside = path.is_relative_to(root)
    except (OSError, ValueError):
        return None
    return path if inside and path.is_file() and not path.is_symlink() else None


def _pixel_bbox(value: Any) -> list[float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        x0, y0, x1, y1 = [float(item) for item in value]
    except (TypeError, ValueError):
        return None
    if x0 < 0 or y0 < 0 or x1 - x0 < 2 or y1 - y0 < 2:
        return None
    return [x0, y0, x1, y1]


def _crop_pdf_region_atomic(
    *,
    paper_path: Path,
    page: int,
    bbox_norm: list[float],
    target: Path,
    add_margin: bool = True,
) -> bool:
    temporary = target.with_name(f".{target.stem}.generated{target.suffix}")
    try:
        temporary.unlink(missing_ok=True)
        if not _crop_pdf_region(
            paper_path=paper_path,
            page=page,
            bbox_norm=bbox_norm,
            target=temporary,
            add_margin=add_margin,
        ):
            return False
        temporary.replace(target)
        return _valid_existing_image(target, min_width=160, min_height=100)
    except OSError:
        return False
    finally:
        temporary.unlink(missing_ok=True)


def _crop_pdf_region(
    *,
    paper_path: Path,
    page: int,
    bbox_norm: list[float],
    target: Path,
    add_margin: bool = True,
) -> bool:
    try:
        import fitz
    except ImportError:
        return False
    document = fitz.open(str(paper_path))
    try:
        if page < 1 or page > document.page_count:
            return False
        pdf_page = document.load_page(page - 1)
        rect = pdf_page.rect
        width = bbox_norm[2] - bbox_norm[0]
        height = bbox_norm[3] - bbox_norm[1]
        margin_x = max(0.003, min(0.012, width * 0.025)) if add_margin else 0.0
        margin_y = max(0.003, min(0.012, height * 0.025)) if add_margin else 0.0
        clip = fitz.Rect(
            max(rect.x0, (bbox_norm[0] - margin_x) * rect.width),
            max(rect.y0, (bbox_norm[1] - margin_y) * rect.height),
            min(rect.x1, (bbox_norm[2] + margin_x) * rect.width),
            min(rect.y1, (bbox_norm[3] + margin_y) * rect.height),
        )
        pixmap = pdf_page.get_pixmap(matrix=fitz.Matrix(3.0, 3.0), clip=clip, alpha=False)
        target.parent.mkdir(parents=True, exist_ok=True)
        pixmap.save(str(target))
    except Exception:
        return False
    finally:
        document.close()
    return _valid_existing_image(target, min_width=160, min_height=100)


def _file_sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def _valid_existing_image(path: Path, *, min_width: int = 1, min_height: int = 1) -> bool:
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 20_000_000:
        return False
    try:
        from PIL import Image

        with Image.open(path) as image:
            image.load()
            width, height = image.size
        return width >= min_width and height >= min_height
    except Exception:
        return False


def _valid_bbox(value: Any) -> list[float] | None:
    if isinstance(value, dict):
        value = [value.get("x0"), value.get("y0"), value.get("x1"), value.get("y1")]
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        values = [float(item) for item in value]
    except (TypeError, ValueError):
        return None
    x0, y0, x1, y1 = [min(1.0, max(0.0, item)) for item in values]
    if x1 - x0 < 0.01 or y1 - y0 < 0.01:
        return None
    return [round(x0, 6), round(y0, 6), round(x1, 6), round(y1, 6)]


def _page_number(value: Any) -> int | None:
    if isinstance(value, list) and len(value) == 1:
        value = value[0]
    if isinstance(value, dict):
        value = value.get("page") or value.get("source_page")
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _read_json_object(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 2_000_000:
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}
