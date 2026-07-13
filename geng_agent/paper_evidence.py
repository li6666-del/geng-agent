from __future__ import annotations

import base64
import io
import re
from pathlib import Path
from typing import Any

from .json_utils import pretty_json
from .llm import LLMImage
from .pipeline_helpers import wrap_untrusted


MAX_IMAGE_DIMENSION = 1600
MAX_TASK_PAPER_CONTEXT_CHARS = 14000
RESULT_KEYWORDS = (
    "figure",
    "fig.",
    "table",
    "simulation",
    "experiment",
    "result",
    "ber",
    "ser",
    "bler",
    "throughput",
    "snr",
    "baseline",
)


def facts_for_task(facts: dict[str, Any], task: dict[str, Any], max_facts: int = 20) -> dict[str, Any]:
    required = {
        (str(ref.get("type")), str(ref.get("name")).lower())
        for ref in task.get("required_facts", [])
        if isinstance(ref, dict)
    }
    selected = []
    for fact in facts.get("engineering_facts", []):
        if not isinstance(fact, dict):
            continue
        key = (str(fact.get("type")), str(fact.get("name")).lower())
        if key in required or fact.get("type") in {"metric", "figure_claim", "baseline"}:
            selected.append(fact)
    if not selected:
        selected = [fact for fact in facts.get("engineering_facts", []) if isinstance(fact, dict)][:max_facts]
    return {
        "paper_domain": facts.get("paper_domain"),
        "paper_repro_type": facts.get("paper_repro_type"),
        "engineering_facts": selected[:max_facts],
        "missing_information": facts.get("missing_information", []),
    }


def paper_context_for_task(*, paper: dict[str, Any], task: dict[str, Any]) -> str:
    figure_numbers = _extract_figure_numbers(str(task.get("figure_or_claim", "")))
    tokens = task_match_tokens(task)
    scored_chunks = []
    for index, chunk in enumerate(paper.get("chunks", [])):
        if not isinstance(chunk, dict):
            continue
        text = " ".join(str(chunk.get(key, "")) for key in ("section", "text")).lower()
        score = 0
        if any(f"fig. {number}" in text or f"figure {number}" in text for number in figure_numbers):
            score += 6
        if any(keyword in text for keyword in RESULT_KEYWORDS):
            score += 2
        score += sum(1 for token in tokens if token and token in text)
        if score > 0:
            scored_chunks.append((score, index, chunk))

    if not scored_chunks:
        scored_chunks = [
            (1, index, chunk)
            for index, chunk in enumerate(paper.get("chunks", [])[:3])
            if isinstance(chunk, dict)
        ]

    selected = []
    total = 0
    for _, _, chunk in sorted(scored_chunks, key=lambda item: (item[0], -item[1]), reverse=True):
        text = str(chunk.get("text", ""))
        if not text:
            continue
        remaining = MAX_TASK_PAPER_CONTEXT_CHARS - total
        if remaining <= 0:
            break
        copied = {
            "chunk_id": chunk.get("chunk_id"),
            "page": chunk.get("page"),
            "section": chunk.get("section"),
            "text": text[:remaining],
        }
        selected.append(copied)
        total += len(copied["text"])

    return wrap_untrusted("paper_context_json", pretty_json({"chunks": selected}))


def task_match_tokens(task: dict[str, Any]) -> set[str]:
    text_parts = [
        str(task.get("task_id", "")),
        str(task.get("target", "")),
        str(task.get("metric", "")),
        str(task.get("figure_or_claim", "")),
        " ".join(str(item) for item in task.get("output_columns", []) if item),
    ]
    tokens = set(re.findall(r"[a-z0-9_]+", " ".join(text_parts).lower()))
    tokens.update(_extract_figure_numbers(str(task.get("figure_or_claim", ""))))
    return {token for token in tokens if len(token) >= 2}


def thesis_comparisons_for_task(paper_thesis: dict[str, Any] | None, task: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(paper_thesis, dict):
        return []
    comparisons = paper_thesis.get("comparisons")
    if not isinstance(comparisons, list) or not comparisons:
        return []
    task_figs = _extract_figure_numbers(str(task.get("figure_or_claim", "")))
    task_words: set[str] = set()
    for token in task_match_tokens(task):
        task_words.update(re.findall(r"[a-z0-9]+", token))
    matched: list[dict[str, Any]] = []
    for comparison in comparisons:
        if not isinstance(comparison, dict) or not str(comparison.get("expected_ordering") or "").strip():
            continue
        comparison_figs = _extract_figure_numbers(str(comparison.get("figure_ref", "")))
        if comparison_figs:
            if task_figs & comparison_figs:
                matched.append(comparison)
            continue
        metric_words = set(re.findall(r"[a-z0-9]+", str(comparison.get("metric", "")).lower()))
        if metric_words & task_words:
            matched.append(comparison)
    return matched


def thesis_ordering_anchor_for_task(paper_thesis: dict[str, Any] | None, task: dict[str, Any]) -> str:
    matched = thesis_comparisons_for_task(paper_thesis, task)
    if not matched:
        return ""
    lines = []
    for comparison in matched:
        segment = f"  - {str(comparison.get('expected_ordering')).strip()}"
        regime = str(comparison.get("regime") or "").strip()
        if regime:
            segment += f"（成立条件：{regime}）"
        note = str(comparison.get("mechanism_note") or "").strip()
        if note:
            segment += f"；机制：{note}"
        lines.append(segment)
    return (
        "\n\n# 【论文断言的方法排序·重点核对｜优先级最高】\n"
        "针对本图，论文主张的方法相对高低如下：\n"
        + "\n".join(lines)
        + "\n请把它当作核对基准：\n"
        "- 本地曲线/数值的相对高低（谁在上、谁在下）是否与上面一致？\n"
        "- 若相反或明显不一致：baseline_comparison 维度判 weak 或 missing，scientific_verdict 倾向 "
        "does_not_support_paper_claim；并在 differences 写清“论文应是谁在上、本地实际谁在上”，"
        "在 possible_causes 给出最可能的建模原因（例如空时/多普勒维度没建出条件数优势）。\n"
        "- regime 区分：若本地跑的是 smoke / 缩规模配置、并不落在该排序成立的区间，请在 limitations 注明，"
        "**不要据此判 mismatch**。\n"
    )


def safe_label(value: str) -> str:
    label = re.sub(r"[^a-zA-Z0-9_.-]+", "_", value.strip())[:60].strip("._-")
    return label or "task"


def render_pdf_pages_for_llm(
    paper_path: Path,
    pages: list[int] | None = None,
    max_pages: int | None = None,
) -> list[LLMImage]:
    try:
        import fitz
    except ImportError as exc:
        raise RuntimeError("PyMuPDF is required to render paper pages for evidence extraction.") from exc

    images: list[LLMImage] = []
    document = fitz.open(str(paper_path))
    try:
        if pages is None:
            pages = list(range(1, document.page_count + 1))
        page_numbers = pages if max_pages is None else pages[:max_pages]
        for page_number in page_numbers:
            if page_number < 1 or page_number > document.page_count:
                continue
            page = document.load_page(page_number - 1)
            pixmap = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5), alpha=False)
            images.append(encode_png_bytes_for_llm(pixmap.tobytes("png"), label=f"paper_page:{page_number}"))
    finally:
        document.close()
    return images


def encode_png_bytes_for_llm(data: bytes, label: str) -> LLMImage:
    try:
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError("Pillow is required for paper evidence image packing.") from exc
    with Image.open(io.BytesIO(data)) as image:
        image.load()
        image = image.convert("RGBA")
        image.thumbnail((MAX_IMAGE_DIMENSION, MAX_IMAGE_DIMENSION))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
    return LLMImage(
        label=label,
        mime_type="image/png",
        data_b64=base64.b64encode(buffer.getvalue()).decode("ascii"),
    )


def _extract_figure_numbers(text: str) -> set[str]:
    numbers = set(re.findall(r"\bfig(?:\.|ure)?[._\s:-]*([0-9]+)", text.lower()))
    numbers.update(re.findall(r"图\s*([0-9]+)", text))
    return numbers
