from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .outputs import write_json


PAPER_MEMORY_SCHEMA_VERSION = "2.0"
MEMORY_MANIFEST_SCHEMA_VERSION = "1.0"

_FIGURE_RE = re.compile(
    r"\bfig(?:ure)?s?\.?\s*(\d{1,3})(?!\d)(?:\s*\(([a-z])\)|([a-z])\b)?|"
    r"图\s*(\d{1,3})(?!\d)(?:\s*[（(]([a-z])[）)]|([a-z])\b)?",
    re.IGNORECASE,
)
_TABLE_RE = re.compile(r"\btables?\.?\s*(\d{1,3}|[IVX]{1,6})\b|表\s*(\d{1,3})", re.IGNORECASE)
_EQUATION_RE = re.compile(r"\b(?:eq(?:uation)?\.?\s*)\(?([0-9]{1,3})\)?|公式\s*\(?([0-9]{1,3})\)?", re.IGNORECASE)
_ALGORITHM_RE = re.compile(r"\balgorithm\s*([0-9]{1,3})\b|算法\s*([0-9]{1,3})", re.IGNORECASE)


def build_paper_memory(paper: dict[str, Any], source_path: Path) -> dict[str, Any]:
    chunks = [chunk for chunk in paper.get("chunks", []) if isinstance(chunk, dict)]
    entities: dict[str, dict[str, Any]] = {}
    cross_refs: set[tuple[str, str, str]] = set()

    for index, chunk in enumerate(chunks, start=1):
        chunk_id = str(chunk.get("chunk_id") or f"chunk_{index:04d}")
        section_id = f"section:{_safe_id(chunk_id)}"
        text = str(chunk.get("text") or "")
        page = chunk.get("page") if isinstance(chunk.get("page"), int) else None
        section = str(chunk.get("section") or "").strip()
        entities[section_id] = {
            "entity_id": section_id,
            "kind": "section",
            "label": section or chunk_id,
            "number": None,
            "subfigure": None,
            "page": page,
            "chunk_ids": [chunk_id],
            "text": text,
            "parent_id": None,
        }
        for entity in _entities_referenced_by_text(text, page=page, chunk_id=chunk_id):
            entity_id = entity["entity_id"]
            existing = entities.get(entity_id)
            if existing is None:
                entities[entity_id] = entity
            else:
                existing["chunk_ids"] = _stable_union(existing.get("chunk_ids", []), [chunk_id])
                if existing.get("page") is None and page is not None:
                    existing["page"] = page
                if len(str(entity.get("text") or "")) > len(str(existing.get("text") or "")):
                    existing["text"] = entity["text"]
            cross_refs.add((section_id, entity_id, "references"))
            parent_id = entity.get("parent_id")
            if parent_id:
                if parent_id not in entities:
                    entities[parent_id] = {
                        "entity_id": parent_id,
                        "kind": "figure",
                        "label": f"Fig. {entity.get('number')}",
                        "number": str(entity.get("number")),
                        "subfigure": None,
                        "page": page,
                        "chunk_ids": [chunk_id],
                        "text": entity.get("text", ""),
                        "parent_id": None,
                    }
                cross_refs.add((parent_id, entity_id, "contains"))

    source_record = {
        "path": str(source_path),
        "format": str(paper.get("format") or source_path.suffix.lower().lstrip(".")),
        "sha256": _sha256_file(source_path) if source_path.exists() else None,
        "page_count": _paper_page_count(paper),
    }
    document = {
        "schema_version": PAPER_MEMORY_SCHEMA_VERSION,
        "source": source_record,
        "entities": sorted(entities.values(), key=_entity_sort_key),
        "cross_references": [
            {"from_id": source, "to_id": target, "relation": relation}
            for source, target, relation in sorted(cross_refs)
        ],
        "metadata": {
            "builder": "deterministic_paper_memory_v2",
            "chunk_count": len(chunks),
            "entity_count": len(entities),
        },
    }
    document["memory_hash"] = hash_json(document)
    return document


def load_or_build_paper_memory(
    *,
    paper: dict[str, Any],
    source_path: Path,
    output_dir: Path,
    resume: bool,
) -> dict[str, Any]:
    path = output_dir / "paper_memory.json"
    source_sha = _sha256_file(source_path) if source_path.exists() else None
    if resume and path.exists():
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            if (
                isinstance(cached, dict)
                and cached.get("schema_version") == PAPER_MEMORY_SCHEMA_VERSION
                and isinstance(cached.get("source"), dict)
                and cached["source"].get("sha256") == source_sha
            ):
                return cached
        except Exception:
            pass
    memory = build_paper_memory(paper, source_path)
    write_json(path, memory)
    return memory


def write_memory_manifest(output_dir: Path, artifacts: dict[str, Path | None]) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    for role, path in sorted(artifacts.items()):
        if path is None:
            continue
        resolved = path.expanduser().resolve()
        if not resolved.exists() or not resolved.is_file():
            continue
        try:
            relative = resolved.relative_to(output_dir.resolve()).as_posix()
        except ValueError:
            relative = str(resolved)
        records.append(
            {
                "role": role,
                "path": relative,
                "sha256": _sha256_file(resolved),
                "size_bytes": resolved.stat().st_size,
            }
        )
    manifest = {
        "schema_version": MEMORY_MANIFEST_SCHEMA_VERSION,
        "artifacts": records,
    }
    manifest["snapshot_hash"] = hash_json(manifest)
    write_json(output_dir / "memory_manifest.json", manifest)
    return manifest


def hash_json(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def paper_memory_summary(memory: dict[str, Any], max_entities: int = 120) -> dict[str, Any]:
    entities = memory.get("entities") if isinstance(memory.get("entities"), list) else []
    compact = []
    for entity in entities[:max_entities]:
        if not isinstance(entity, dict):
            continue
        compact.append(
            {
                "entity_id": entity.get("entity_id"),
                "kind": entity.get("kind"),
                "label": entity.get("label"),
                "page": entity.get("page"),
                "chunk_ids": entity.get("chunk_ids", []),
            }
        )
    return {
        "schema_version": memory.get("schema_version"),
        "memory_hash": memory.get("memory_hash"),
        "entities": compact,
        "truncated": len(entities) > len(compact),
    }


def _entities_referenced_by_text(text: str, *, page: int | None, chunk_id: str) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for match in _FIGURE_RE.finditer(text):
        number = match.group(1) or match.group(4)
        subfigure = (match.group(2) or match.group(3) or match.group(5) or match.group(6) or "").lower() or None
        parent_id = f"fig:{number}" if subfigure else None
        entity_id = f"fig:{number}:{subfigure}" if subfigure else f"fig:{number}"
        found.append(_entity(entity_id, "figure", _display_figure(number, subfigure), number, subfigure, page, chunk_id, text, parent_id))
    for match in _TABLE_RE.finditer(text):
        number = match.group(1) or match.group(2)
        found.append(_entity(f"table:{str(number).upper()}", "table", f"Table {number}", str(number).upper(), None, page, chunk_id, text, None))
    for match in _EQUATION_RE.finditer(text):
        number = match.group(1) or match.group(2)
        found.append(_entity(f"equation:{number}", "equation", f"Equation {number}", number, None, page, chunk_id, text, None))
    for match in _ALGORITHM_RE.finditer(text):
        number = match.group(1) or match.group(2)
        found.append(_entity(f"algorithm:{number}", "algorithm", f"Algorithm {number}", number, None, page, chunk_id, text, None))
    return found


def _entity(
    entity_id: str,
    kind: str,
    label: str,
    number: str,
    subfigure: str | None,
    page: int | None,
    chunk_id: str,
    text: str,
    parent_id: str | None,
) -> dict[str, Any]:
    return {
        "entity_id": entity_id,
        "kind": kind,
        "label": label,
        "number": str(number),
        "subfigure": subfigure,
        "page": page,
        "chunk_ids": [chunk_id],
        "text": _context_excerpt(text, label),
        "parent_id": parent_id,
    }


def _context_excerpt(text: str, label: str, radius: int = 350) -> str:
    lowered = text.lower()
    needle = label.lower().replace("figure", "fig.")
    index = lowered.find(needle)
    if index < 0:
        index = 0
    start = max(0, index - radius)
    return text[start : index + radius].strip()


def _display_figure(number: str, subfigure: str | None) -> str:
    return f"Fig. {number}({subfigure})" if subfigure else f"Fig. {number}"


def _safe_id(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "_", value).strip("._-") or "unknown"


def _stable_union(left: list[Any], right: list[Any]) -> list[Any]:
    result = list(left)
    for item in right:
        if item not in result:
            result.append(item)
    return result


def _entity_sort_key(entity: dict[str, Any]) -> tuple[int, str, str]:
    page = entity.get("page") if isinstance(entity.get("page"), int) else 10**9
    return page, str(entity.get("kind")), str(entity.get("entity_id"))


def _paper_page_count(paper: dict[str, Any]) -> int | None:
    metadata = paper.get("metadata") if isinstance(paper.get("metadata"), dict) else {}
    for value in (paper.get("page_count"), metadata.get("page_count")):
        if isinstance(value, int):
            return value
    pages = [chunk.get("page") for chunk in paper.get("chunks", []) if isinstance(chunk, dict)]
    numeric = [page for page in pages if isinstance(page, int)]
    return max(numeric) if numeric else None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
