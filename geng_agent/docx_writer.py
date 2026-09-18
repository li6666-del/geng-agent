from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from docx import Document
from docx.document import Document as DocumentObject
from docx.table import _Cell

from .docx_assets import (
    RESULT_REVIEW_COMPARISON_IMAGE_WIDTH_IN,
    RESULT_REVIEW_IMAGE_WIDTH_IN,
    _add_cell_paragraph,
    _add_image_comparison_table,
    _add_markdown_image,
    _add_markdown_image_to_cell,
    _clear_cell,
    _parse_markdown_image_cell,
    _resolve_markdown_image_path,
)
from .docx_markdown import (
    _add_markdown_body,
    _parse_markdown_image_table,
    _split_markdown_table_row,
)
from .docx_styles import (
    ACCENT,
    BODY_FONT,
    DISCLAIMER,
    HEADER_FILL,
    LIGHT_FILL,
    SERIF_FONT,
    _add_appendix_note,
    _add_bullets,
    _add_disclaimer,
    _add_heading,
    _add_kv_table,
    _add_labelled_bullets,
    _add_note,
    _add_table,
    _add_title,
    _clean_markdown_inline,
    _items_or_default,
    _safe_text,
    _save,
    _set_cell_margins,
    _set_cell_text,
    _set_cell_width,
    _set_run_font,
    _set_style_font,
    _set_table_column_widths,
    _set_table_width,
    _setup_document,
    _shade_cell,
)


def write_result_review_markdown_docx(
    path: Path,
    *,
    markdown_text: str,
    status: dict[str, Any] | None = None,
) -> Path:
    """Create the human-readable result review Word report from Markdown."""

    return write_markdown_report_docx(
        path,
        markdown_text=markdown_text,
        title="复现结果二次审查报告",
        subtitle="本地复现结果与论文证据的人工阅读版对比报告",
        base_dir=path.parent,
    )


def write_markdown_report_docx(
    path: Path,
    *,
    markdown_text: str,
    title: str,
    subtitle: str,
    base_dir: Path | None = None,
) -> Path:
    """Render a Codex-authored Markdown report without rewriting its content."""

    document = Document()
    _setup_document(document)
    # A Markdown title belongs to the editor's report.  The supplied title and
    # subtitle are legacy fallbacks, not an additional cover or authored body.
    lines = markdown_text.splitlines()
    first = next((index for index, line in enumerate(lines) if line.strip()), None)
    match = re.fullmatch(r"#\s+(.+?)\s*", lines[first].strip()) if first is not None else None
    if match:
        _add_title(document, re.sub(r"\s+#+$", "", match.group(1)), "")
        del lines[first]
        markdown_text = "\n".join(lines)
    else:
        _add_title(document, title, subtitle)
    _add_markdown_body(document, markdown_text, base_dir=base_dir, heading_offset=-1 if match else 0)
    return _save(document, path)
