from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable


FIGURE_REF_RE = re.compile(r"(?i)\bfig(?:\.|ure)?\s*([0-9]+)")
PAREN_SUBFIGURE_RE = re.compile(r"(?i)^\s*\(([a-z])\)")
ADJACENT_SUBFIGURE_RE = re.compile(r"(?i)^([a-z])\b")
SUBFIGURE_RE = re.compile(r"(?<![A-Za-z0-9])\(([a-z])\)", re.IGNORECASE)
CAPTION_TEXT_KEYS = frozenset({"caption", "content", "text", "value", "text_content"})
CAPTION_METADATA_KEYS = frozenset({"type", "bbox", "page_idx", "page_index", "image_path", "index"})


def build_figure_index(
    *,
    raw_dir: Path,
    paper_path: Path,
    case_root: Path,
    candidate_dir: Path,
    paper_sha256: str,
    backend: str | None,
) -> dict[str, Any]:
    """Normalize one MinerU output tree into portable, page-oriented figure candidates."""
    source_files, source_format = _select_source_files(raw_dir)
    records: list[dict[str, Any]] = []
    for source in source_files:
        payload = _read_json(source)
        if source_format == "content_list_v2":
            records.extend(_records_from_content_list_v2(payload, source))
        elif source_format == "content_list":
            records.extend(_records_from_content_list(payload, source))
        elif source_format == "middle":
            records.extend(_records_from_middle(payload, source))
        elif source_format == "model":
            records.extend(_records_from_model(payload, source))

    candidate_dir.mkdir(parents=True, exist_ok=True)
    figures: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []
    seen: set[tuple[int, tuple[int, int, int, int], str]] = set()
    for record in records:
        page = _positive_int(record.get("page"))
        bbox = _valid_norm_bbox(record.get("bbox_norm"))
        if page is None or bbox is None:
            continue
        caption = _clean_text(record.get("caption"))
        figure_number, caption_subfigure, possible_figure_numbers, identity_status = _figure_identity(caption)
        fingerprint_bbox = tuple(round(value * 1000) for value in bbox)
        dedupe_key = (page, fingerprint_bbox, caption.lower()[:120])
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        candidate_id = f"page_{page:04d}_visual_{len(figures) + len(unmatched) + 1:03d}"
        asset = _crop_pdf_candidate(
            paper_path=paper_path,
            page=page,
            bbox_norm=bbox,
            target=candidate_dir / f"{candidate_id}.png",
        )
        item = {
            "candidate_id": candidate_id,
            "figure_ref": f"Fig. {figure_number}" if figure_number else None,
            "figure_number": figure_number,
            "possible_figure_numbers": possible_figure_numbers,
            "identity_status": identity_status,
            "caption_subfigure": caption_subfigure,
            "subfigure_labels": sorted(set(SUBFIGURE_RE.findall(caption.lower()))),
            "page": page,
            "bbox_norm": bbox,
            "caption_bbox_norm": _valid_norm_bbox(record.get("caption_bbox_norm")),
            "caption": caption,
            "visual_type": str(record.get("visual_type") or "image"),
            "asset_path": _relative_path(asset, case_root) if asset else None,
            "source_format": source_format,
            "source_file": _relative_path(Path(str(record.get("source_file") or "")), case_root),
        }
        if figure_number:
            figures.append(item)
        else:
            unmatched.append(item)

    figures.sort(key=lambda item: (int(item["figure_number"]), int(item["page"]), item["candidate_id"]))
    unmatched.sort(key=lambda item: (int(item["page"]), item["candidate_id"]))
    return {
        "schema_version": "1.0",
        "paper_sha256": paper_sha256,
        "backend": backend,
        "source_format": source_format,
        "figures": figures,
        "unmatched_visuals": unmatched,
        "_meta": {
            "source_files": [_relative_path(path, case_root) for path in source_files],
            "figure_count": len(figures),
            "unmatched_visual_count": len(unmatched),
            "coordinate_system": "normalized_page_xyxy_0_1",
        },
    }


def task_figure_candidates(figure_index: dict[str, Any] | None, task: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(figure_index, dict):
        return []
    target_text = " ".join(
        str(task.get(key) or "")
        for key in ("figure_or_claim", "target", "task_id")
    )
    targets = _target_figure_refs(target_text)
    if not targets:
        return []
    target_numbers = {number for number, _ in targets}
    target_subfigures = {subfigure for _, subfigure in targets if subfigure}
    candidates: list[dict[str, Any]] = []
    source_items = list(figure_index.get("figures", [])) + list(figure_index.get("unmatched_visuals", []))
    for item in source_items:
        if not isinstance(item, dict):
            continue
        possible_numbers = {
            str(value)
            for value in item.get("possible_figure_numbers", [])
            if str(value).strip()
        }
        figure_number = str(item.get("figure_number") or "")
        if figure_number not in target_numbers and not (possible_numbers & target_numbers):
            continue
        copied = json.loads(json.dumps(item, ensure_ascii=False))
        copied["target_subfigure"] = sorted(target_subfigures)[0] if len(target_subfigures) == 1 else None
        candidates.append(copied)
    return sorted(
        candidates,
        key=lambda item: (
            0 if item.get("caption") else 1,
            int(item.get("page") or 0),
            str(item.get("candidate_id") or ""),
        ),
    )


def figure_index_prompt_summary(figure_index: dict[str, Any] | None, limit: int = 80) -> dict[str, Any]:
    figures = figure_index.get("figures", []) if isinstance(figure_index, dict) else []
    unmatched_visuals = figure_index.get("unmatched_visuals", []) if isinstance(figure_index, dict) else []
    ambiguous = [
        item
        for item in unmatched_visuals
        if isinstance(item, dict) and item.get("identity_status") == "ambiguous_multi_figure_caption"
    ]
    return {
        "source": "mineru_layout_candidates",
        "figures": [
            {
                "figure_ref": item.get("figure_ref"),
                "page": item.get("page"),
                "caption": str(item.get("caption") or "")[:600],
                "subfigure_labels": item.get("subfigure_labels", []),
                "identity_status": item.get("identity_status"),
                "possible_figure_numbers": item.get("possible_figure_numbers", []),
            }
            for item in (list(figures) + ambiguous)[:limit]
            if isinstance(item, dict)
        ],
    }


def resolve_candidate_asset(candidate: dict[str, Any], case_root: Path) -> Path | None:
    raw = str(candidate.get("asset_path") or "").strip()
    if not raw:
        return None
    path = (case_root / raw).resolve()
    try:
        inside = path.is_relative_to(case_root.resolve())
    except (OSError, ValueError):
        inside = False
    return path if inside and path.is_file() and not path.is_symlink() else None


def _select_source_files(raw_dir: Path) -> tuple[list[Path], str]:
    priorities = (
        ("content_list_v2", "*_content_list_v2.json"),
        ("content_list", "*_content_list.json"),
        ("middle", "*_middle.json"),
        ("model", "*_model.json"),
    )
    for source_format, pattern in priorities:
        files = sorted(path for path in raw_dir.rglob(pattern) if path.is_file() and not path.is_symlink())
        if files:
            return files, source_format
    return [], "none"


def _records_from_content_list_v2(payload: Any, source: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    pages = payload if isinstance(payload, list) else []
    for page_index, page_items in enumerate(pages):
        if not isinstance(page_items, list):
            continue
        for item in page_items:
            if not isinstance(item, dict) or str(item.get("type") or "").lower() not in {"image", "chart"}:
                continue
            records.append(
                {
                    "page": page_index + 1,
                    "bbox_norm": _normalize_bbox(item.get("bbox"), mode="thousand"),
                    "caption_bbox_norm": None,
                    "caption": _caption_text(item),
                    "visual_type": item.get("type"),
                    "source_file": source,
                }
            )
    return records


def _records_from_content_list(payload: Any, source: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for item in payload if isinstance(payload, list) else []:
        if not isinstance(item, dict) or str(item.get("type") or "").lower() not in {"image", "chart"}:
            continue
        page_index = item.get("page_idx", item.get("page_index", 0))
        page = int(page_index) + 1 if isinstance(page_index, int) else 1
        records.append(
            {
                "page": page,
                "bbox_norm": _normalize_bbox(item.get("bbox"), mode="thousand"),
                "caption_bbox_norm": None,
                "caption": _caption_text(item),
                "visual_type": item.get("type"),
                "source_file": source,
            }
        )
    return records


def _records_from_middle(payload: Any, source: Path) -> list[dict[str, Any]]:
    root = payload if isinstance(payload, dict) else {}
    pages = root.get("pdf_info") if isinstance(root.get("pdf_info"), list) else []
    records: list[dict[str, Any]] = []
    for page_offset, page_info in enumerate(pages):
        if not isinstance(page_info, dict):
            continue
        page_idx = page_info.get("page_idx")
        page = int(page_idx) + 1 if isinstance(page_idx, int) else page_offset + 1
        page_size = page_info.get("page_size")
        blocks = page_info.get("para_blocks") if isinstance(page_info.get("para_blocks"), list) else []
        for block in blocks:
            if not isinstance(block, dict) or str(block.get("type") or "").lower() not in {"image", "chart"}:
                continue
            child_blocks = block.get("blocks") if isinstance(block.get("blocks"), list) else []
            body = next(
                (child for child in child_blocks if isinstance(child, dict) and str(child.get("type") or "").endswith("_body")),
                None,
            )
            captions = [
                child
                for child in child_blocks
                if isinstance(child, dict) and str(child.get("type") or "").endswith("_caption")
            ]
            records.append(
                {
                    "page": page,
                    "bbox_norm": _normalize_bbox((body or block).get("bbox"), page_size=page_size),
                    "caption_bbox_norm": _union_bboxes(
                        [_normalize_bbox(child.get("bbox"), page_size=page_size) for child in captions]
                    ),
                    "caption": " ".join(filter(None, (_caption_text(child) for child in captions))),
                    "visual_type": block.get("type"),
                    "source_file": source,
                }
            )
    return records


def _records_from_model(payload: Any, source: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for page_index, blocks in enumerate(payload if isinstance(payload, list) else []):
        if not isinstance(blocks, list):
            continue
        captions = [item for item in blocks if isinstance(item, dict) and str(item.get("type") or "") == "image_caption"]
        for item in blocks:
            if not isinstance(item, dict) or str(item.get("type") or "") != "image":
                continue
            caption = min(captions, key=lambda value: _vertical_distance(item.get("bbox"), value.get("bbox")), default={})
            records.append(
                {
                    "page": page_index + 1,
                    "bbox_norm": _normalize_bbox(item.get("bbox"), mode="unit"),
                    "caption_bbox_norm": _normalize_bbox(caption.get("bbox"), mode="unit"),
                    "caption": _caption_text(caption),
                    "visual_type": "image",
                    "source_file": source,
                }
            )
    return records


def _normalize_bbox(raw: Any, *, page_size: Any = None, mode: str | None = None) -> list[float] | None:
    values = _bbox_values(raw)
    if values is None:
        return None
    x0, y0, x1, y1 = values
    if mode == "unit":
        normalized = values
    elif mode == "thousand":
        normalized = [value / 1000.0 for value in values]
    elif isinstance(page_size, (list, tuple)) and len(page_size) >= 2:
        width, height = float(page_size[0]), float(page_size[1])
        normalized = [x0 / width, y0 / height, x1 / width, y1 / height] if width > 0 and height > 0 else values
    elif max(abs(value) for value in values) <= 1.5:
        normalized = values
    else:
        normalized = [value / 1000.0 for value in values]
    return _valid_norm_bbox(normalized)


def _bbox_values(raw: Any) -> list[float] | None:
    if isinstance(raw, dict):
        raw = [raw.get("x0"), raw.get("y0"), raw.get("x1"), raw.get("y1")]
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        return None
    try:
        return [float(value) for value in raw]
    except (TypeError, ValueError):
        return None


def _valid_norm_bbox(raw: Any) -> list[float] | None:
    values = _bbox_values(raw)
    if values is None:
        return None
    x0, y0, x1, y1 = [min(1.0, max(0.0, value)) for value in values]
    if x1 - x0 < 0.01 or y1 - y0 < 0.01:
        return None
    return [round(x0, 6), round(y0, 6), round(x1, 6), round(y1, 6)]


def _union_bboxes(values: Iterable[list[float] | None]) -> list[float] | None:
    boxes = [value for value in values if value]
    if not boxes:
        return None
    return [min(box[0] for box in boxes), min(box[1] for box in boxes), max(box[2] for box in boxes), max(box[3] for box in boxes)]


def _caption_text(value: Any) -> str:
    parts: list[str] = []

    def visit(item: Any, *, caption_context: bool = False) -> None:
        if isinstance(item, str):
            if caption_context:
                parts.append(item)
            return
        if isinstance(item, list):
            for child in item:
                visit(child, caption_context=caption_context)
            return
        if not isinstance(item, dict):
            return
        item_type = str(item.get("type") or "").lower()
        is_caption = caption_context or "caption" in item_type
        for key, child in item.items():
            key_lower = str(key).lower()
            if key_lower in CAPTION_METADATA_KEYS:
                continue
            child_is_caption = is_caption or "caption" in key_lower
            if isinstance(child, str):
                if child_is_caption and (key_lower in CAPTION_TEXT_KEYS or "caption" in key_lower):
                    parts.append(child)
                continue
            visit(child, caption_context=child_is_caption)

    visit(value)
    return _clean_text(" ".join(parts))


def _figure_identity(caption: str) -> tuple[str | None, str | None, list[str], str]:
    matches = list(FIGURE_REF_RE.finditer(caption))
    possible_numbers = sorted({match.group(1) for match in matches}, key=int)
    if not matches:
        return None, None, [], "unlabeled"
    if len(possible_numbers) != 1:
        return None, None, possible_numbers, "ambiguous_multi_figure_caption"
    first = matches[0]
    return possible_numbers[0], _subfigure_after_match(caption, first), possible_numbers, "caption_single_figure"


def _target_figure_refs(text: str) -> set[tuple[str, str | None]]:
    return {(match.group(1), _subfigure_after_match(text, match)) for match in FIGURE_REF_RE.finditer(text)}


def _subfigure_after_match(text: str, match: re.Match[str]) -> str | None:
    tail = text[match.end():]
    subfigure = PAREN_SUBFIGURE_RE.match(tail) or ADJACENT_SUBFIGURE_RE.match(tail)
    return subfigure.group(1).lower() if subfigure else None


def _crop_pdf_candidate(*, paper_path: Path, page: int, bbox_norm: list[float], target: Path) -> Path | None:
    if paper_path.suffix.lower() != ".pdf":
        return None
    try:
        import fitz
    except ImportError:
        return None
    document = fitz.open(str(paper_path))
    try:
        if page < 1 or page > document.page_count:
            return None
        pdf_page = document.load_page(page - 1)
        rect = pdf_page.rect
        margin_x, margin_y = rect.width * 0.006, rect.height * 0.006
        clip = fitz.Rect(
            max(rect.x0, bbox_norm[0] * rect.width - margin_x),
            max(rect.y0, bbox_norm[1] * rect.height - margin_y),
            min(rect.x1, bbox_norm[2] * rect.width + margin_x),
            min(rect.y1, bbox_norm[3] * rect.height + margin_y),
        )
        pixmap = pdf_page.get_pixmap(matrix=fitz.Matrix(2.5, 2.5), clip=clip, alpha=False)
        target.parent.mkdir(parents=True, exist_ok=True)
        pixmap.save(str(target))
        return target if target.is_file() else None
    except Exception:
        return None
    finally:
        document.close()


def _vertical_distance(first: Any, second: Any) -> float:
    a = _bbox_values(first)
    b = _bbox_values(second)
    if a is None or b is None:
        return float("inf")
    return abs(b[1] - a[3])


def _positive_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def _relative_path(path: Path | None, root: Path) -> str | None:
    if path is None or not str(path):
        return None
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        return str(path)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
