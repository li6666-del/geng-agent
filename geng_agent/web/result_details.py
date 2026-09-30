"""Read a delivered archive for the portal, without unpacking or judging results."""
from __future__ import annotations

import io
import re
import zipfile
import zlib
from pathlib import Path
from xml.etree import ElementTree

from .delivery import DELIVERY_ROOT, REPORTS

REPORT_MEMBERS = {
    "comparison": REPORTS["result_review.docx"],
    "reproduction": REPORTS["reproduction_report.docx"],
}
MAX_REPORT_BYTES = 32 * 1024 * 1024
MAX_README_BYTES = 256 * 1024
MAX_DOCUMENT_XML_BYTES = 4 * 1024 * 1024
ZIP_READ_ERRORS = (OSError, ValueError, zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError, zlib.error)
_WORD_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _read_member(archive: zipfile.ZipFile, member: str, limit: int) -> bytes:
    info = archive.getinfo(member)
    if info.is_dir():
        raise KeyError(member)
    if info.file_size > limit:
        raise ValueError("文件超过在线预览或单独下载的大小限制，请下载完整交付包")
    with archive.open(info) as stream:
        content = stream.read(limit + 1)
    if len(content) > limit:
        raise ValueError("文件超过在线预览或单独下载的大小限制，请下载完整交付包")
    return content


def read_report(path: Path, report_id: str) -> bytes:
    """Only the two final reports are addressable; paths never come from callers."""
    member = REPORT_MEMBERS[report_id]
    with zipfile.ZipFile(path) as archive:
        return _read_member(archive, member, MAX_REPORT_BYTES)


def _report_excerpt(archive: zipfile.ZipFile) -> list[str]:
    try:
        document = _read_member(archive, REPORT_MEMBERS["comparison"], MAX_REPORT_BYTES)
        with zipfile.ZipFile(io.BytesIO(document)) as word:
            xml = _read_member(word, "word/document.xml", MAX_DOCUMENT_XML_BYTES)
        # Word documents do not need DTDs or custom entities for text extraction.
        if b"<!DOCTYPE" in xml.upper() or b"<!ENTITY" in xml.upper():
            return []
        root = ElementTree.fromstring(xml)
        body = root.find(f"{_WORD_NS}body")
        if body is None:
            return []
        paragraphs: list[str] = []
        remaining = 1600
        for paragraph in body.iter(f"{_WORD_NS}p"):
            text = "".join(node.text or "" for node in paragraph.iter(f"{_WORD_NS}t")).strip()
            if not text:
                continue
            paragraphs.append(text[:remaining])
            remaining -= len(paragraphs[-1])
            if len(paragraphs) >= 8 or remaining <= 0:
                break
        return paragraphs
    except (*ZIP_READ_ERRORS, LookupError, ElementTree.ParseError):
        return []


def _task_details(archive: zipfile.ZipFile) -> list[dict]:
    prefix = f"{DELIVERY_ROOT}/复现任务/"
    tasks: dict[str, dict] = {}
    # Duplicate archive names represent a single visible file, just as getinfo does.
    for name, info in {item.filename: item for item in archive.infolist()}.items():
        if not name.startswith(prefix) or "\\" in name:
            continue
        parts = name[len(prefix):].rstrip("/").split("/")
        if not parts or any(part in {"", ".", ".."} for part in parts):
            continue
        directory = parts[0]
        task = tasks.setdefault(directory, {
            "directory": directory, "name": directory, "code_files": 0,
            "result_files": 0, "readme": None,
        })
        if not info.is_dir() and len(parts) >= 3:
            if parts[1] == "代码":
                task["code_files"] += 1
            elif parts[1] == "复现结果":
                task["result_files"] += 1
        if not info.is_dir() and len(parts) == 2 and parts[1].lower() == "readme.md":
            try:
                readme = _read_member(archive, name, MAX_README_BYTES).decode("utf-8-sig", errors="replace")
                task["readme"] = readme
                title = re.search(r"^#{1,6}[ \t]+(.+?)\s*#*\s*$", readme, flags=re.MULTILINE)
                if title:
                    task["name"] = title.group(1).strip()
            except (*ZIP_READ_ERRORS, KeyError):
                pass
    return [tasks[name] for name in sorted(tasks)]


def result_details(path: Path, case_id: str, finished_at: str | None) -> dict:
    result = {
        "case_id": case_id, "available": False, "message": "交付包可下载，详情暂时无法读取。",
        "finished_at": finished_at,
        "bundle": {"download_url": f"/api/v1/cases/{case_id}/download", "size_bytes": path.stat().st_size},
        "reports": [], "excerpt": [], "tasks": [],
    }
    try:
        with zipfile.ZipFile(path) as archive:
            for report_id, member in REPORT_MEMBERS.items():
                try:
                    info = archive.getinfo(member)
                except KeyError:
                    continue
                if not info.is_dir():
                    result["reports"].append({
                        "id": report_id, "name": member.rsplit("/", 1)[-1],
                        "size_bytes": info.file_size,
                        "download_url": f"/api/v1/cases/{case_id}/reports/{report_id}",
                    })
            result["excerpt"] = _report_excerpt(archive)
            result["tasks"] = _task_details(archive)
        if result["reports"] or result["tasks"]:
            result["available"] = True
            result["message"] = "报告与复现任务已整理完成，可查看详情或下载。"
        else:
            result["message"] = "交付包可下载，暂未读取到可预览的报告和任务。"
    except ZIP_READ_ERRORS:
        pass
    return result
