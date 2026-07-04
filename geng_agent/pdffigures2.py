from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import threading
import hashlib
from pathlib import Path
from typing import Any

from .config import get_config_value
from .outputs import write_json
from .security import redact_text


PDFFIGURES2_DIR = "pdffigures2"
PDFFIGURES2_INDEX = "paper_figures.json"
_PDFFIGURES2_CACHE_LOCK = threading.Lock()
_PDFFIGURES2_RUN_CACHE: dict[str, dict[str, Any]] = {}


def build_pdffigures2_evidence(*, paper_path: Path, evidence_root: Path) -> dict[str, Any]:
    """Run PDFFigures2 when configured and normalize its figure metadata.

    PDFFigures2 is intentionally optional: when it is missing, callers keep the
    page-level evidence path and reports must label the lower confidence.
    """
    root = evidence_root / PDFFIGURES2_DIR
    root.mkdir(parents=True, exist_ok=True)
    index_path = root / PDFFIGURES2_INDEX
    command = get_config_value("GENG_PDFFIGURES2_CMD")
    if not command:
        doc = {
            "version": 1,
            "tool": "pdffigures2",
            "enabled": False,
            "ok": False,
            "reason": "GENG_PDFFIGURES2_CMD is not set",
            "figures": [],
        }
        write_json(index_path, doc)
        return doc

    if paper_path.suffix.lower() != ".pdf" or not paper_path.exists():
        doc = {
            "version": 1,
            "tool": "pdffigures2",
            "enabled": True,
            "ok": False,
            "reason": "paper is not an existing PDF",
            "figures": [],
        }
        write_json(index_path, doc)
        return doc

    input_dir = root / "input"
    data_dir = root / "data"
    image_dir = root / "images"
    crop_dir = root / "crops"
    for directory in (input_dir, data_dir, image_dir, crop_dir):
        directory.mkdir(parents=True, exist_ok=True)
    input_pdf = input_dir / paper_path.name
    try:
        if paper_path.resolve() != input_pdf.resolve():
            shutil.copy2(paper_path, input_pdf)
    except Exception:
        input_pdf = paper_path

    stats_path = root / "stats.json"
    cache_key = _pdffigures2_cache_key(paper_path=paper_path, command=command)
    with _PDFFIGURES2_CACHE_LOCK:
        cached = _PDFFIGURES2_RUN_CACHE.get(cache_key)
        if cached is None:
            status = _run_pdffigures2_command(
                command=command,
                paper_path=input_pdf,
                input_dir=input_dir,
                data_dir=data_dir,
                image_dir=image_dir,
                stats_path=stats_path,
            )
            parsed_docs = _load_pdffigures2_json_documents(data_dir=data_dir, stdout=status.get("stdout", ""))
            _PDFFIGURES2_RUN_CACHE[cache_key] = {
                "status": dict(status),
                "parsed_docs": json.loads(json.dumps(parsed_docs)),
            }
            cache_hit = False
        else:
            status = dict(cached.get("status") or {})
            parsed_docs = json.loads(json.dumps(cached.get("parsed_docs") or []))
            cache_hit = True
    figures = _normalize_pdffigures2_figures(parsed_docs, pdf_path=input_pdf, crop_dir=crop_dir, evidence_root=evidence_root)
    doc = {
        "version": 1,
        "tool": "pdffigures2",
        "enabled": True,
        "ok": bool(figures) and status.get("returncode") == 0,
        "returncode": status.get("returncode"),
        "reason": (
            None
            if figures and status.get("returncode") == 0
            else status.get("error") or ("PDFFigures2 exited non-zero" if figures else "PDFFigures2 produced no usable figures")
        ),
        "command": redact_text(command),
        "cache_hit": cache_hit,
        "figures": figures,
        "stderr_tail": str(status.get("stderr", ""))[-2000:],
    }
    write_json(index_path, doc)
    return doc


def select_pdffigures2_crop_for_task(
    *,
    source_page_image: Path,
    figure_ref: dict[str, str],
    target_path: Path,
) -> dict[str, Any] | None:
    """Return a crop entry for the task figure, or None when PDFFigures2 has no match."""
    number = str(figure_ref.get("number") or "").strip()
    if not number:
        return None
    index_path = _find_pdffigures2_index(source_page_image)
    if index_path is None:
        return None
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if index.get("ok") is not True:
        return None
    figures = index.get("figures")
    if not isinstance(figures, list):
        return None
    page_no = _paper_page_number_from_image_path(source_page_image)
    candidates = [item for item in figures if _figure_number_matches(item, number)]
    if page_no:
        same_page = [item for item in candidates if str(item.get("page") or "") == page_no]
        if same_page:
            candidates = same_page
    if not candidates:
        return None
    figure = candidates[0]
    subfigure = str(figure_ref.get("subfigure") or "").lower()
    if subfigure:
        sub_crop = _write_pdffigures2_subfigure_crop(figure=figure, subfigure=subfigure, target_path=target_path)
        if sub_crop is not None:
            return sub_crop
        whole = _write_pdffigures2_figure_crop(figure=figure, target_path=target_path)
        if whole is None:
            return None
        whole.update(
            {
                "crop_reason": "pdffigures2_figure_subfigure_low_confidence",
                "confidence": "low",
                "subfigure": subfigure,
                "warning": "subfigure label/panel could not be isolated; using full PDFFigures2 figure",
            }
        )
        return whole
    whole = _write_pdffigures2_figure_crop(figure=figure, target_path=target_path)
    if whole is not None:
        whole.setdefault("confidence", "high")
    return whole


def _run_pdffigures2_command(
    *,
    command: str,
    paper_path: Path,
    input_dir: Path,
    data_dir: Path,
    image_dir: Path,
    stats_path: Path,
) -> dict[str, Any]:
    image_prefix = image_dir / "figure"
    substitutions = {
        "pdf": str(paper_path),
        "pdf_dir": str(input_dir),
        "input_dir": str(input_dir),
        "out_dir": str(data_dir.parent),
        "json_dir": str(data_dir),
        "data_dir": str(data_dir),
        "image_dir": str(image_dir),
        "image_prefix": str(image_prefix),
        "stats": str(stats_path),
    }
    timeout = _pdffigures2_timeout()
    try:
        if "{" in command and "}" in command:
            args = _template_pdffigures2_args(command, substitutions)
            completed = subprocess.run(
                args,
                cwd=str(data_dir.parent),
                shell=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        else:
            args = _default_pdffigures2_args(command, input_dir=input_dir, data_dir=data_dir, image_prefix=image_prefix, stats_path=stats_path)
            completed = subprocess.run(
                args,
                cwd=str(data_dir.parent),
                shell=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        return {
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }
    except FileNotFoundError as exc:
        return {"returncode": None, "stdout": "", "stderr": "", "error": f"not found: {exc}"}
    except subprocess.TimeoutExpired as exc:
        return {
            "returncode": None,
            "stdout": exc.stdout or "",
            "stderr": exc.stderr or "",
            "error": f"timeout after {timeout}s",
        }
    except Exception as exc:
        return {"returncode": None, "stdout": "", "stderr": "", "error": f"{type(exc).__name__}: {exc}"}


def _default_pdffigures2_args(
    command: str,
    *,
    input_dir: Path,
    data_dir: Path,
    image_prefix: Path,
    stats_path: Path,
) -> list[str]:
    raw = command.strip().strip('"')
    if raw.lower().endswith(".jar"):
        java_cmd = get_config_value("GENG_PDFFIGURES2_JAVA_CMD") or "java"
        return [*_split_command_args(java_cmd), "-jar", raw, str(input_dir), "-s", str(stats_path), "-m", str(image_prefix), "-d", str(data_dir / "figure")]
    parts = _split_command_args(command)
    return [*parts, str(input_dir), "-s", str(stats_path), "-m", str(image_prefix), "-d", str(data_dir / "figure")]


def _template_pdffigures2_args(command: str, substitutions: dict[str, str]) -> list[str]:
    parts = _split_command_args(command)
    if not parts:
        raise ValueError("GENG_PDFFIGURES2_CMD produced no executable arguments")
    return [part.format(**substitutions) for part in parts]


def _split_command_args(command: str) -> list[str]:
    parts = shlex.split(command, posix=os.name != "nt")
    return [_strip_outer_quotes(part) for part in parts if _strip_outer_quotes(part)]


def _strip_outer_quotes(value: str) -> str:
    text = str(value).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        return text[1:-1]
    return text


def _pdffigures2_cache_key(*, paper_path: Path, command: str) -> str:
    try:
        stat = paper_path.stat()
        source = f"{paper_path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}|{command}|{_pdffigures2_timeout()}"
    except OSError:
        source = f"{paper_path}|missing|{command}|{_pdffigures2_timeout()}"
    return hashlib.sha256(source.encode("utf-8", errors="replace")).hexdigest()


def _pdffigures2_timeout() -> float:
    raw = get_config_value("GENG_PDFFIGURES2_TIMEOUT")
    try:
        value = float(raw) if raw else 120.0
    except ValueError:
        value = 120.0
    return max(5.0, value)


def _load_pdffigures2_json_documents(*, data_dir: Path, stdout: str) -> list[Any]:
    docs: list[Any] = []
    for path in sorted(data_dir.rglob("*.json")):
        try:
            docs.append(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            continue
    text = stdout.strip()
    if text:
        try:
            docs.append(json.loads(text))
        except Exception:
            pass
    return docs


def _normalize_pdffigures2_figures(
    docs: list[Any],
    *,
    pdf_path: Path,
    crop_dir: Path,
    evidence_root: Path,
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for raw in _iter_figure_objects(docs):
        figure_number = _extract_figure_number(raw)
        page_index = _extract_page_index(raw)
        box = _extract_boundary(raw.get("regionBoundary") or raw.get("region") or raw.get("figureBoundary") or raw.get("boundary"))
        if not figure_number or page_index is None or box is None:
            continue
        fig_type = str(raw.get("figType") or raw.get("type") or "Figure")
        key = (fig_type.lower(), figure_number, str(page_index))
        if key in seen:
            continue
        seen.add(key)
        page_number = page_index + 1
        crop_path = crop_dir / f"{_safe_name(fig_type)}_{_safe_name(figure_number)}_p{page_number}.png"
        crop_result = _render_pdf_crop(pdf_path=pdf_path, page_index=page_index, box=box, target_path=crop_path)
        caption_box = _extract_boundary(raw.get("captionBoundary") or raw.get("captionRegion") or raw.get("caption_box"))
        item = {
            "type": fig_type,
            "name": str(raw.get("name") or figure_number),
            "figure_number": figure_number,
            "page": page_number,
            "page_index": page_index,
            "source_pdf": str(pdf_path),
            "caption": str(raw.get("caption") or ""),
            "figure_box": box,
            "caption_box": caption_box,
            "image_text": raw.get("imageText") if isinstance(raw.get("imageText"), list) else [],
            "image_path": _rel_or_abs(crop_path, evidence_root) if crop_result else None,
            "crop_source": "pdffigures2",
        }
        normalized.append(item)
    normalized.sort(key=lambda item: (int(item.get("page") or 0), str(item.get("figure_number") or "")))
    return normalized


def _iter_figure_objects(values: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    stack = list(values if isinstance(values, list) else [values])
    while stack:
        item = stack.pop()
        if isinstance(item, list):
            stack.extend(item)
            continue
        if not isinstance(item, dict):
            continue
        if _looks_like_pdffigures2_figure(item):
            found.append(item)
            continue
        for key in ("figures", "tables", "entries", "figureList"):
            child = item.get(key)
            if isinstance(child, list):
                stack.extend(child)
    return found


def _looks_like_pdffigures2_figure(item: dict[str, Any]) -> bool:
    return bool(
        item.get("regionBoundary") is not None
        and item.get("page") is not None
        and (item.get("caption") is not None or item.get("name") is not None or item.get("figType") is not None)
    )


def _extract_figure_number(item: dict[str, Any]) -> str:
    for key in ("name", "figure_number", "label"):
        value = str(item.get(key) or "").strip()
        match = re.search(r"([0-9]+[a-zA-Z]?)", value)
        if match:
            return match.group(1)
    caption = str(item.get("caption") or "")
    match = re.search(r"\b(?:fig\.?|figure|table)\s*([0-9]+[a-zA-Z]?)", caption, re.I)
    return match.group(1) if match else ""


def _extract_page_index(item: dict[str, Any]) -> int | None:
    raw = item.get("page")
    try:
        page = int(raw)
    except (TypeError, ValueError):
        return None
    # PDFFigures2 reports pages as 0-based locations.
    return page if page >= 0 else None


def _extract_boundary(value: Any) -> list[float] | None:
    if not isinstance(value, dict):
        if isinstance(value, (list, tuple)) and len(value) >= 4:
            try:
                nums = [float(value[i]) for i in range(4)]
                return _sorted_box(nums)
            except Exception:
                return None
        return None
    keys = (("x1", "y1", "x2", "y2"), ("left", "top", "right", "bottom"), ("x0", "y0", "x1", "y1"))
    for group in keys:
        if all(key in value for key in group):
            try:
                nums = [float(value[key]) for key in group]
                return _sorted_box(nums)
            except Exception:
                return None
    return None


def _sorted_box(nums: list[float]) -> list[float]:
    x0, y0, x1, y1 = nums[:4]
    left, right = sorted((x0, x1))
    top, bottom = sorted((y0, y1))
    return [round(left, 2), round(top, 2), round(right, 2), round(bottom, 2)]


def _write_pdffigures2_subfigure_crop(*, figure: dict[str, Any], subfigure: str, target_path: Path) -> dict[str, Any] | None:
    pdf_path = _figure_pdf_path(figure)
    page_index = _figure_page_index(figure)
    figure_box = figure.get("figure_box")
    if pdf_path is None or page_index is None or not isinstance(figure_box, list):
        return None
    try:
        import fitz
    except Exception:
        return None
    try:
        document = fitz.open(str(pdf_path))
        try:
            if page_index < 0 or page_index >= document.page_count:
                return None
            page = document.load_page(page_index)
            figure_rect = page.rect.__class__(figure_box)
            labels = _find_subfigure_labels(page=page, figure_rect=figure_rect)
            clip = _subfigure_clip_from_labels(page=page, figure_rect=figure_rect, labels=labels, subfigure=subfigure)
            if clip is None:
                return None
            target_path.parent.mkdir(parents=True, exist_ok=True)
            pixmap = page.get_pixmap(matrix=fitz.Matrix(2.0, 2.0), clip=clip, alpha=False)
            pixmap.save(str(target_path))
            return {
                "crop_box": _rect_box(clip),
                "crop_reason": "pdffigures2_subfigure",
                "confidence": "medium",
                "subfigure": subfigure,
                "source_figure_box": figure_box,
            }
        finally:
            document.close()
    except Exception:
        return None


def _write_pdffigures2_figure_crop(*, figure: dict[str, Any], target_path: Path) -> dict[str, Any] | None:
    image_path = _resolve_figure_image_path(figure)
    if image_path and image_path.exists():
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(image_path, target_path)
        return {
            "crop_box": figure.get("figure_box"),
            "crop_reason": "pdffigures2_figure",
            "confidence": "high",
            "source_image_path": str(image_path),
        }
    pdf_path = _figure_pdf_path(figure)
    page_index = _figure_page_index(figure)
    figure_box = figure.get("figure_box")
    if pdf_path is None or page_index is None or not isinstance(figure_box, list):
        return None
    if not _render_pdf_crop(pdf_path=pdf_path, page_index=page_index, box=figure_box, target_path=target_path):
        return None
    return {
        "crop_box": figure_box,
        "crop_reason": "pdffigures2_figure",
        "confidence": "high",
    }


def _find_subfigure_labels(*, page: Any, figure_rect: Any) -> list[dict[str, Any]]:
    labels: list[dict[str, Any]] = []
    pattern = re.compile(r"^\(([a-z])\)$", re.I)
    try:
        words = page.get_text("words")
    except Exception:
        words = []
    for word in words:
        if len(word) < 5:
            continue
        text = str(word[4] or "").strip()
        match = pattern.match(text)
        if not match:
            continue
        rect = page.rect.__class__(word[:4])
        if _rect_inside(rect, figure_rect):
            _append_unique_label(labels, {"letter": match.group(1).lower(), "rect": rect})
    if labels:
        labels.sort(key=lambda item: (_rect_center_y(item["rect"]), _rect_center_x(item["rect"])))
        return labels
    try:
        blocks = page.get_text("blocks")
    except Exception:
        blocks = []
    block_pattern = re.compile(r"^\s*\(([a-z])\)(?:\s|$|[:.])", re.I)
    for block in blocks:
        if len(block) < 5:
            continue
        text = str(block[4] or "").strip()
        match = block_pattern.search(text)
        if not match:
            continue
        rect = page.rect.__class__(block[:4])
        if _rect_inside(rect, figure_rect) and rect.width <= figure_rect.width * 0.75:
            _append_unique_label(labels, {"letter": match.group(1).lower(), "rect": rect})
    labels.sort(key=lambda item: (_rect_center_y(item["rect"]), _rect_center_x(item["rect"])))
    return labels


def _subfigure_clip_from_labels(*, page: Any, figure_rect: Any, labels: list[dict[str, Any]], subfigure: str) -> Any | None:
    if not labels:
        return None
    target = subfigure.strip().lower()
    rows = _group_labels_by_row(labels, figure_height=float(figure_rect.height))
    row_index = col_index = -1
    for r_index, row in enumerate(rows):
        for c_index, label in enumerate(row):
            if label["letter"] == target:
                row_index, col_index = r_index, c_index
                break
        if row_index >= 0:
            break
    if row_index < 0:
        return None
    row = rows[row_index]
    x_centers = [_rect_center_x(label["rect"]) for label in row]
    x_center = x_centers[col_index]
    left = float(figure_rect.x0) if col_index == 0 else (x_centers[col_index - 1] + x_center) / 2
    right = float(figure_rect.x1) if col_index == len(row) - 1 else (x_center + x_centers[col_index + 1]) / 2

    row_top = min(float(label["rect"].y0) for label in row)
    row_bottom = max(float(label["rect"].y1) for label in row)
    if row_index == 0:
        top = float(figure_rect.y0)
    else:
        previous_bottom = max(float(label["rect"].y1) for label in rows[row_index - 1])
        top = previous_bottom
    if row_index == len(rows) - 1:
        bottom = float(figure_rect.y1)
    else:
        bottom = row_bottom

    margin_x = float(figure_rect.width) * 0.015
    margin_y = float(figure_rect.height) * 0.015
    left = max(float(figure_rect.x0), left - margin_x)
    right = min(float(figure_rect.x1), right + margin_x)
    top = max(float(figure_rect.y0), top - margin_y)
    bottom = min(float(figure_rect.y1), bottom + margin_y)
    if right <= left or bottom <= top:
        return None
    rect = page.rect.__class__(left, top, right, bottom)
    area_ratio = (rect.width * rect.height) / max(1.0, float(figure_rect.width) * float(figure_rect.height))
    if area_ratio < 0.04 or area_ratio > 0.85:
        return None
    return rect


def _group_labels_by_row(labels: list[dict[str, Any]], *, figure_height: float) -> list[list[dict[str, Any]]]:
    threshold = max(8.0, figure_height * 0.07)
    rows: list[list[dict[str, Any]]] = []
    row_centers: list[float] = []
    for label in labels:
        center = _rect_center_y(label["rect"])
        if rows and abs(center - row_centers[-1]) <= threshold:
            rows[-1].append(label)
            row_centers[-1] = sum(_rect_center_y(item["rect"]) for item in rows[-1]) / len(rows[-1])
        else:
            rows.append([label])
            row_centers.append(center)
    for row in rows:
        row.sort(key=lambda item: _rect_center_x(item["rect"]))
    return rows


def _append_unique_label(labels: list[dict[str, Any]], candidate: dict[str, Any]) -> None:
    for existing in labels:
        if existing["letter"] == candidate["letter"] and abs(_rect_center_x(existing["rect"]) - _rect_center_x(candidate["rect"])) < 8 and abs(
            _rect_center_y(existing["rect"]) - _rect_center_y(candidate["rect"])
        ) < 8:
            return
    labels.append(candidate)


def _render_pdf_crop(*, pdf_path: Path, page_index: int, box: list[float], target_path: Path) -> bool:
    try:
        import fitz
    except Exception:
        return False
    try:
        document = fitz.open(str(pdf_path))
        try:
            if page_index < 0 or page_index >= document.page_count:
                return False
            page = document.load_page(page_index)
            rect = page.rect.__class__(box)
            rect = rect & page.rect
            if rect.is_empty or rect.width <= 0 or rect.height <= 0:
                return False
            target_path.parent.mkdir(parents=True, exist_ok=True)
            pixmap = page.get_pixmap(matrix=fitz.Matrix(2.0, 2.0), clip=rect, alpha=False)
            pixmap.save(str(target_path))
            return True
        finally:
            document.close()
    except Exception:
        return False


def _find_pdffigures2_index(path: Path) -> Path | None:
    for parent in [path.parent, *path.parents]:
        candidate = parent / PDFFIGURES2_DIR / PDFFIGURES2_INDEX
        if candidate.exists():
            return candidate
    return None


def _paper_page_number_from_image_path(path: Path) -> str:
    match = re.search(r"paper_page_(\d+)\.png$", path.name)
    return match.group(1) if match else ""


def _figure_number_matches(item: dict[str, Any], number: str) -> bool:
    return str(item.get("figure_number") or "").lower() == number.lower()


def _resolve_figure_image_path(figure: dict[str, Any]) -> Path | None:
    raw = figure.get("image_path")
    if not raw:
        return None
    path = Path(str(raw))
    if path.is_absolute():
        return path
    source_pdf = _figure_pdf_path(figure)
    if source_pdf is None:
        return None
    for parent in [source_pdf.parent, *source_pdf.parents]:
        evidence_root = parent.parent if parent.name == "source" else parent
        candidate = evidence_root / path
        if candidate.exists():
            return candidate
    return None


def _figure_pdf_path(figure: dict[str, Any]) -> Path | None:
    raw = figure.get("source_pdf") or figure.get("pdf_path")
    if raw:
        path = Path(str(raw))
        return path if path.exists() else None
    image_path = _resolve_raw_image_path(figure.get("image_path"))
    if image_path:
        for parent in [image_path.parent, *image_path.parents]:
            source_dir = parent / "source"
            if source_dir.exists():
                pdfs = sorted(source_dir.glob("*.pdf"))
                if pdfs:
                    return pdfs[0]
    return None


def _resolve_raw_image_path(value: Any) -> Path | None:
    if not value:
        return None
    path = Path(str(value))
    return path if path.is_absolute() and path.exists() else None


def _figure_page_index(figure: dict[str, Any]) -> int | None:
    try:
        return int(figure.get("page_index"))
    except (TypeError, ValueError):
        page = figure.get("page")
        try:
            return int(page) - 1
        except (TypeError, ValueError):
            return None


def _rect_inside(rect: Any, container: Any) -> bool:
    center_x = _rect_center_x(rect)
    center_y = _rect_center_y(rect)
    return float(container.x0) <= center_x <= float(container.x1) and float(container.y0) <= center_y <= float(container.y1)


def _rect_center_x(rect: Any) -> float:
    return (float(rect.x0) + float(rect.x1)) / 2


def _rect_center_y(rect: Any) -> float:
    return (float(rect.y0) + float(rect.y1)) / 2


def _rect_box(rect: Any) -> list[float]:
    return [round(float(rect.x0), 2), round(float(rect.y0), 2), round(float(rect.x1), 2), round(float(rect.y1), 2)]


def _rel_or_abs(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _safe_name(text: str) -> str:
    safe = re.sub(r"[^0-9A-Za-z._-]+", "_", str(text)).strip("._-")
    return safe or "item"
