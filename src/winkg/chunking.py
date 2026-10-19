from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from collections.abc import Iterator


@dataclass(frozen=True)
class TextChunk:
    ordinal: int
    text: str
    char_start: int
    char_end: int
    page: int | None = None
    section: str | None = None


def stable_id(*parts: str) -> str:
    payload = "\x1f".join(parts).encode("utf-8", errors="replace")
    return hashlib.sha256(payload).hexdigest()


def _best_break(text: str, start: int, hard_end: int) -> int:
    if hard_end >= len(text):
        return len(text)
    window = text[start:hard_end]
    # Prefer paragraph boundary, then line boundary, then sentence boundary.
    for separator, minimum in (("\n\n", 0.55), ("\n", 0.60), (". ", 0.72), (" ", 0.82)):
        position = window.rfind(separator)
        if position >= int(len(window) * minimum):
            return start + position + len(separator)
    return hard_end


def _iter_chunks(text: str, size: int = 1800, overlap: int = 220) -> Iterator[TextChunk]:
    text = text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return
    if size < 200:
        raise ValueError("Chunk-Größe muss mindestens 200 Zeichen betragen.")
    if overlap < 0 or overlap >= size:
        raise ValueError("Overlap muss >= 0 und kleiner als die Chunk-Größe sein.")

    start = 0
    ordinal = 0
    page_markers = [(m.start(), int(m.group(1))) for m in re.finditer(r"(?im)^\s*#{1,3}\s*Page\s+(\d+)\s*$", text)]
    section_markers = [(m.start(), m.group(1).strip()) for m in re.finditer(r"(?m)^#{1,6}\s+(.+?)\s*$", text)]
    page_index = 0
    section_index = 0
    current_page: int | None = None
    current_section: str | None = None

    while start < len(text):
        while page_index < len(page_markers) and page_markers[page_index][0] <= start:
            current_page = page_markers[page_index][1]
            page_index += 1
        while section_index < len(section_markers) and section_markers[section_index][0] <= start:
            current_section = section_markers[section_index][1]
            section_index += 1

        hard_end = min(start + size, len(text))
        end = _best_break(text, start, hard_end)
        if end <= start:
            end = hard_end
        content = text[start:end].strip()
        if content:
            yield TextChunk(
                ordinal=ordinal,
                text=content,
                char_start=start,
                char_end=end,
                page=current_page,
                section=current_section,
            )
            ordinal += 1
        if end >= len(text):
            break
        next_start = max(start + 1, end - overlap)
        # Avoid starting the next chunk in the middle of a word.
        space = text.find(" ", next_start, min(next_start + 80, len(text)))
        start = space + 1 if space != -1 else next_start


def chunk_text(text: str, size: int = 1800, overlap: int = 220) -> list[TextChunk]:
    """Split text into chunks. Use chunk_text_limited for large, capped files."""
    return list(_iter_chunks(text, size=size, overlap=overlap))


def chunk_text_limited(
    text: str, size: int = 1800, overlap: int = 220, maximum: int = 500
) -> tuple[list[TextChunk], int]:
    """Keep at most maximum chunks in memory and return the number omitted.

    The source is still traversed to calculate an exact omitted count, but only the
    selected prefix is retained in RAM and passed to embeddings/Neo4j.
    """
    if maximum < 1:
        raise ValueError("Maximale Chunk-Anzahl muss mindestens 1 sein.")
    selected: list[TextChunk] = []
    omitted = 0
    for chunk in _iter_chunks(text, size=size, overlap=overlap):
        if len(selected) < maximum:
            selected.append(chunk)
        else:
            omitted += 1
    return selected, omitted


def limit_chunks(chunks: list[TextChunk], maximum: int) -> tuple[list[TextChunk], int]:
    """Keep the first maximum chunks and return how many chunks were omitted."""
    if maximum < 1:
        raise ValueError("Maximum chunks per file must be at least 1.")
    if len(chunks) <= maximum:
        return chunks, 0
    return chunks[:maximum], len(chunks) - maximum
