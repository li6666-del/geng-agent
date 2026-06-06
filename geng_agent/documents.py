from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path


SUPPORTED_SUFFIXES = {".pdf", ".txt", ".md", ".markdown"}


@dataclass(frozen=True)
class PaperChunk:
    chunk_id: str
    page: int | None
    section: str | None
    text: str


def load_paper(path: Path, max_pages: int | None = None) -> dict:
    """Load a paper into small text chunks with source hints."""
    path = path.expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"论文文件不存在：{path}")
    if path.suffix.lower() not in SUPPORTED_SUFFIXES:
        raise ValueError(f"不支持的论文格式：{path.suffix}")

    if path.suffix.lower() == ".pdf":
        chunks = _load_pdf(path, max_pages=max_pages)
    else:
        chunks = _load_text(path)

    return {
        "source_path": str(path),
        "format": path.suffix.lower().lstrip("."),
        "chunk_count": len(chunks),
        "chunks": [asdict(chunk) for chunk in chunks],
    }


def _load_text(path: Path) -> list[PaperChunk]:
    text = _read_text_with_fallback(path)
    return [
        PaperChunk(
            chunk_id=f"text_c{index + 1}",
            page=None,
            section=_guess_section(piece),
            text=piece,
        )
        for index, piece in enumerate(split_text(text))
    ]


def _load_pdf(path: Path, max_pages: int | None) -> list[PaperChunk]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError("读取 PDF 需要安装 pypdf：python -m pip install pypdf") from exc

    reader = PdfReader(str(path))
    pages = reader.pages[:max_pages] if max_pages is not None else reader.pages
    chunks: list[PaperChunk] = []

    for page_index, page in enumerate(pages, start=1):
        text = page.extract_text() or ""
        text = text.strip()
        if not text:
            continue
        for chunk_index, piece in enumerate(split_text(text), start=1):
            chunks.append(
                PaperChunk(
                    chunk_id=f"p{page_index}_c{chunk_index}",
                    page=page_index,
                    section=_guess_section(piece),
                    text=piece,
                )
            )
    return chunks


def split_text(text: str, max_chars: int = 6000, overlap: int = 300) -> list[str]:
    text = "\n".join(line.rstrip() for line in text.splitlines()).strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        if end < len(text):
            newline = text.rfind("\n", start, end)
            if newline > start + max_chars // 2:
                end = newline
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(text):
            break
        start = max(0, end - overlap)
    return chunks


def _read_text_with_fallback(path: Path) -> str:
    for encoding in ("utf-8", "utf-8-sig", "gb18030"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    return path.read_text(encoding="utf-8", errors="replace")


def _guess_section(text: str) -> str | None:
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if len(stripped) <= 100 and any(
            token in stripped.lower()
            for token in (
                "abstract",
                "introduction",
                "system model",
                "simulation",
                "experiment",
                "results",
                "conclusion",
            )
        ):
            return stripped
        break
    return None
