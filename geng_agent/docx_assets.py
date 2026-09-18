from __future__ import annotations

import math
import re
import unicodedata
from pathlib import Path

from docx.document import Document as DocumentObject
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.image.image import Image
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor
from docx.table import _Cell

from .docx_styles import (
    BODY_FONT,
    HEADER_FILL,
    _add_markdown_inline_runs,
    _set_cell_margins,
    _repeat_table_header,
    _set_run_font,
    _set_table_column_widths,
    _set_table_width,
    _shade_cell,
)


# Retained for callers importing the old defaults. Rendering uses the current
# section's available space, including a height limit for portrait pages.
RESULT_REVIEW_IMAGE_WIDTH_IN = 6.2
RESULT_REVIEW_COMPARISON_IMAGE_WIDTH_IN = 3.0
_CELL_PADDING_IN = 1 / 12


def _page_content_size(document: DocumentObject) -> tuple[float, float]:
    section = document.sections[-1]
    width = section.page_width - section.left_margin - section.right_margin
    height = section.page_height - section.top_margin - section.bottom_margin
    return width / 914400, height / 914400


def _image_pixel_size(path: Path) -> tuple[int, int] | None:
    try:
        image = Image.from_file(str(path))
        return image.px_width, image.px_height
    except Exception:
        # Bad/unsupported images still reach the visible delivery warning.
        return None


def _can_compare_side_by_side(row: list[str], base_dir: Path | None) -> bool:
    """Use columns only for two similarly shaped, single landscape/square images.

    Geometry cannot identify a square multi-panel chart. The editor can request
    full-width presentation by emitting separate Markdown image blocks for it.
    Unknown, portrait, wide, or multiple images use a conservative stacked layout.
    """
    if len(row) != 2:
        return False
    ratios: list[float] = []
    for value in row:
        items = _parse_markdown_image_cell(value)
        if not items or len(items) != 1:
            return False
        path = _resolve_markdown_image_path(items[0][1], base_dir)
        size = _image_pixel_size(path)
        if size is None:
            return False
        width, height = size
        ratio = width / height
        if not 0.9 <= ratio <= 1.6:
            return False
        ratios.append(ratio)
    return max(ratios) / min(ratios) <= 1.25


def _add_image_comparison_table(
    document: DocumentObject,
    headers: list[str],
    rows: list[list[str]],
    *,
    base_dir: Path | None = None,
) -> None:
    if not headers:
        return
    page_width, page_height = _page_content_size(document)
    column_count = len(headers)
    table = document.add_table(rows=0, cols=column_count)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = False
    width_dxa = round(page_width * 1440)
    _set_table_width(table, width_dxa)
    base_width, remainder = divmod(width_dxa, column_count)
    column_widths = [base_width] * column_count
    column_widths[-1] += remainder
    _set_table_column_widths(table, column_widths)
    # Figures are grouped by source labels, without a heavy grid around them.
    borders = OxmlElement("w:tblBorders")
    for side in ("top", "left", "bottom", "right", "insideH", "insideV"):
        border = OxmlElement(f"w:{side}")
        border.set(qn("w:val"), "nil")
        borders.append(border)
    table._tbl.tblPr.append(borders)

    repeat_parallel_header = all(
        _can_compare_side_by_side((row + [""] * column_count)[:column_count], base_dir)
        for row in rows
    )

    for row in rows:
        values = (row + [""] * column_count)[:column_count]
        if _can_compare_side_by_side(values, base_dir):
            heading_row = table.add_row()
            for cell, header in zip(heading_row.cells, headers):
                _add_image_source_label(cell, header)
            if len(table.rows) == 1 and repeat_parallel_header:
                _repeat_table_header(heading_row)
            cells = table.add_row().cells
            image_width = page_width / column_count - 2 * _CELL_PADDING_IN
            for cell, value in zip(cells, values):
                _prepare_image_cell(cell)
                _add_markdown_image_to_cell(
                    cell, value, base_dir=base_dir,
                    image_width_in=image_width, page_height_in=page_height,
                )
        else:
            # Preserve the source label, complete caption and every image.
            # Separate rows allow large multi-image cells to span pages.
            for header, value in zip(headers, values):
                label_cells = table.add_row().cells
                label = label_cells[0].merge(label_cells[-1])
                _add_image_source_label(label, header)
                items = _parse_markdown_image_cell(value)
                chunks = [f"![{caption}]({path})" for caption, path in items] if items else [value]
                for chunk in chunks:
                    cells = table.add_row().cells
                    cell = cells[0].merge(cells[-1])
                    _prepare_image_cell(cell)
                    _add_markdown_image_to_cell(
                        cell, chunk, base_dir=base_dir,
                        image_width_in=page_width - 2 * _CELL_PADDING_IN,
                        page_height_in=page_height,
                    )
    spacer = document.add_paragraph()
    spacer.paragraph_format.space_after = Pt(2)
    spacer.paragraph_format.line_spacing = 1
    spacer.paragraph_format.space_before = Pt(0)
    _set_run_font(spacer.add_run(), size=Pt(1))


def _prepare_image_cell(cell: _Cell) -> None:
    _clear_cell(cell)
    _set_cell_margins(cell, top=60, start=120, bottom=60, end=120)
    cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.TOP


def _add_image_source_label(cell: _Cell, header: str) -> None:
    _prepare_image_cell(cell)
    paragraph = cell.paragraphs[0]
    paragraph.paragraph_format.space_after = Pt(0)
    paragraph.paragraph_format.keep_with_next = True
    _add_markdown_inline_runs(paragraph, header, bold=True, size=Pt(9.5))
    _shade_cell(cell, HEADER_FILL)


def _caption_reserve_inches(caption: str, width_in: float) -> float:
    # Estimate wrapping at the caption's 9 pt font without discarding text.
    characters_per_line = max(1, width_in * 72 / 4.5)
    lines = 0
    for line in caption.splitlines():
        units = sum(2 if unicodedata.east_asian_width(char) in "WF" else 1 for char in line)
        lines += max(1, math.ceil(units / characters_per_line))
    return (lines * 10.5 + 8) / 72 if caption else 0


def _fit_image_size(
    path: Path,
    *,
    width_in: float,
    page_height_in: float,
    caption: str,
) -> tuple[float, float]:
    image = Image.from_file(str(path))
    ratio = image.px_width / image.px_height
    caption_height = min(_caption_reserve_inches(caption, width_in), page_height_in / 2)
    # Leave room for source labels, cell padding and paragraph baselines. Very
    # long captions remain breakable instead of forcing an oversized table row.
    max_height = max(0.25, page_height_in - caption_height - 0.65)
    width = min(width_in, max_height * ratio)
    return width, width / ratio


def _insert_picture(
    paragraph,
    image_path: Path,
    caption: str,
    raw_path: str,
    *,
    width_in: float,
    page_height_in: float,
) -> None:
    width, height = _fit_image_size(
        image_path, width_in=width_in, page_height_in=page_height_in, caption=caption,
    )
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    paragraph.paragraph_format.space_before = Pt(0)
    paragraph.paragraph_format.space_after = Pt(3 if caption else 6)
    paragraph.paragraph_format.line_spacing = 1
    paragraph.paragraph_format.keep_with_next = bool(caption)
    shape = paragraph.add_run().add_picture(str(image_path), width=Inches(width), height=Inches(height))
    shape._inline.docPr.set("descr", caption)
    shape._inline.docPr.set("title", raw_path)


def _add_caption(paragraph, caption: str) -> None:
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    paragraph.paragraph_format.space_before = Pt(0)
    paragraph.paragraph_format.space_after = Pt(6)
    paragraph.paragraph_format.line_spacing = 1.1
    paragraph.paragraph_format.keep_together = False
    paragraph.paragraph_format.keep_with_next = False
    run = paragraph.add_run(caption)
    run.font.color.rgb = RGBColor(95, 95, 95)
    _set_run_font(run, BODY_FONT, Pt(9))


def _add_markdown_image_to_cell(
    cell: _Cell,
    value: str,
    *,
    base_dir: Path | None = None,
    image_width_in: float = RESULT_REVIEW_COMPARISON_IMAGE_WIDTH_IN,
    page_height_in: float = 9.4,
) -> None:
    image_items = _parse_markdown_image_cell(value)
    if image_items is None:
        _add_cell_paragraph(cell, value or "无可用图片", align=WD_ALIGN_PARAGRAPH.LEFT)
        return
    for caption, raw_path in image_items:
        image_path = _resolve_markdown_image_path(raw_path, base_dir)
        if not image_path.exists():
            _add_cell_paragraph(cell, f"{caption}\n图片缺失：{raw_path}", italic=True)
            continue
        try:
            paragraph = _available_cell_paragraph(cell)
            _insert_picture(paragraph, image_path, caption, raw_path, width_in=image_width_in, page_height_in=page_height_in)
            if caption:
                _add_caption(cell.add_paragraph(), caption)
        except Exception as exc:
            _add_cell_paragraph(cell, f"{caption}\n图片插入失败：{raw_path}（{type(exc).__name__}: {exc}）", italic=True)


def _parse_markdown_image_cell(value: str) -> list[tuple[str, str]] | None:
    parts = [part.strip() for part in re.split(r"<br\s*/?>", value.strip(), flags=re.IGNORECASE)]
    if not parts or any(not part for part in parts):
        return None
    images: list[tuple[str, str]] = []
    for part in parts:
        image_match = re.fullmatch(r"!\[([^\]]*)\]\((.*)\)", part)
        if not image_match:
            return None
        images.append((image_match.group(1).strip(), image_match.group(2).strip()))
    return images


def _available_cell_paragraph(cell: _Cell):
    first = cell.paragraphs[0]
    # Picture-only paragraphs have empty .text; they are not empty paragraphs.
    return first if not first.text and not first._p.xpath(".//w:drawing") else cell.add_paragraph()


def _add_cell_paragraph(
    cell: _Cell,
    text: str,
    *,
    align: int | None = None,
    italic: bool = False,
    muted: bool = False,
) -> None:
    paragraph = _available_cell_paragraph(cell)
    paragraph.paragraph_format.space_after = Pt(2)
    if align is not None:
        paragraph.alignment = align
    _add_markdown_inline_runs(paragraph, text, italic=italic, color=RGBColor(95, 95, 95) if muted else None, size=Pt(9))


def _clear_cell(cell: _Cell) -> None:
    cell.text = ""


def _add_markdown_image(
    document: DocumentObject,
    caption: str,
    raw_path: str,
    *,
    base_dir: Path | None = None,
) -> None:
    image_path = _resolve_markdown_image_path(raw_path, base_dir)
    if not image_path.exists():
        _add_caption(document.add_paragraph(), f"{caption}\n图片缺失：{raw_path}")
        return
    try:
        width, height = _page_content_size(document)
        _insert_picture(document.add_paragraph(), image_path, caption, raw_path, width_in=width, page_height_in=height)
        if caption:
            _add_caption(document.add_paragraph(), caption)
    except Exception as exc:
        _add_caption(document.add_paragraph(), f"{caption}\n图片插入失败：{raw_path}（{type(exc).__name__}: {exc}）")


def _resolve_markdown_image_path(raw_path: str, base_dir: Path | None) -> Path:
    path = Path(raw_path.strip().strip("<>"))
    if path.is_absolute() or base_dir is None:
        return path
    return base_dir / path
