"""Read-only checks for Word files authored in the Report Editor workspace."""

from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree
from zipfile import BadZipFile, LargeZipFile, ZipFile


REPORT_WORD_FILES = ("review.docx", "reproduction_report.docx", "result_review.docx")
REQUIRED_REPORT_WORD_FILES = ("reproduction_report.docx", "result_review.docx")
REPORT_LAYOUT_SCRIPT = "report_layout.py"
REPORT_WORD_MAX_BYTES = 128 * 1024 * 1024
REPORT_WORD_MAX_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
REPORT_LAYOUT_SCRIPT_MAX_BYTES = 1024 * 1024


def report_file_limit(name: str, *, markdown_max_bytes: int) -> int:
    if name.endswith(".docx"):
        return REPORT_WORD_MAX_BYTES
    if name == REPORT_LAYOUT_SCRIPT:
        return REPORT_LAYOUT_SCRIPT_MAX_BYTES
    return markdown_max_bytes


def inspect_word_file(path: Path) -> str | None:
    """Return an actionable packaging issue without judging the report's prose."""
    try:
        if path.is_symlink():
            return "must not be a symbolic link"
        if not path.is_file():
            return "is missing or not a regular file"
        size = path.stat().st_size
        if not 0 < size <= REPORT_WORD_MAX_BYTES:
            return "is empty or exceeds the Word resource limit"
        with ZipFile(path) as archive:
            members = archive.infolist()
            if sum(member.file_size for member in members) > REPORT_WORD_MAX_UNCOMPRESSED_BYTES:
                return "exceeds the expanded Word resource limit"
            names = {member.filename for member in members}
            if "[Content_Types].xml" not in names or "word/document.xml" not in names:
                return "is not a Word document package"
            if archive.testzip() is not None:
                return "contains a damaged Word package member"
            root = ElementTree.fromstring(archive.read("word/document.xml"))
        namespace = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        if root.find("w:body", namespace) is None:
            return "has no document body"
        if not any((node.text or "").strip() for node in root.findall(".//w:t", namespace)):
            return "has no readable report text"
    except (OSError, BadZipFile, LargeZipFile, ElementTree.ParseError, RuntimeError, ValueError) as exc:
        return f"could not be read as Word: {type(exc).__name__}"
    return None
