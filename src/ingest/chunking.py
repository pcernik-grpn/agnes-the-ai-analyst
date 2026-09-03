"""Structure-aware chunking for retrieval.

Char-based (≈4 chars/token) to stay dependency-free — no tokenizer. When the
extractor recovered ``elements`` we chunk along their boundaries (never split a
section mid-way unless it alone exceeds the target); otherwise we fall back to
fixed-size windows with overlap over the full text.

This is also the ingest boundary that sanitizes control characters out of
converted text (see :func:`_sanitize_control_chars`) — every converter
(SharePoint markdown, uploaded-document extraction, vision OCR) funnels
through :func:`chunk_text` before a row reaches ``corpus_chunks_repo()
.add_many()``, so fixing it here, once, covers all of them without touching
either the DuckDB or the PostgreSQL ``corpus_chunks``/``corpus_files``
repository — neither repo's contract changes, only the text a caller hands
it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional

from src.ingest.text_extract import ExtractResult

# ~800 tokens * ~4 chars/token; overlap ~100 tokens.
_TARGET_CHARS = 3200
_OVERLAP_CHARS = 400

# NUL (0x00) and the rest of the C0 control range, EXCEPT tab/LF/CR — those
# three are legitimate structure in converted markdown (a table cell
# separator, line breaks), everything else in this range is binary-export
# debris that travels alongside the NUL byte a real ingest failure was
# built around: PostgreSQL's `text` type rejects NUL outright
# ("PostgreSQL text fields cannot contain NUL (0x00) bytes"), which surfaced
# live as 261 rejected documents — an Oracle table export and several
# ordinary documents whose converted markdown embedded a stray NUL mid-word.
# The rest of the C0 range is not REJECTED by Postgres, but stripping it
# alongside NUL in the same pass is simpler and safer than special-casing
# NUL alone, since it is the same class of artifact.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _sanitize_control_chars(text: str) -> str:
    """Strip NUL and other non-tab/LF/CR C0 control characters from ``text``.

    A no-op for the overwhelming majority of documents (plain converted
    markdown never contains these bytes) — cheap enough to apply
    unconditionally rather than sniff first.
    """
    if not text:
        return text
    return _CONTROL_CHARS_RE.sub("", text)


@dataclass
class Chunk:
    ordinal: int
    text: str
    section_path: Optional[str] = None


def _window(text: str, target: int, overlap: int) -> List[str]:
    text = text.strip()
    if not text:
        return []
    if len(text) <= target:
        return [text]
    out: List[str] = []
    step = max(1, target - overlap)
    start = 0
    while start < len(text):
        out.append(text[start : start + target])
        start += step
    return out


def chunk_text(
    source: "ExtractResult | str",
    *,
    target_chars: int = _TARGET_CHARS,
    overlap_chars: int = _OVERLAP_CHARS,
) -> List[Chunk]:
    """Chunk an :class:`ExtractResult` (or raw string) into ordered chunks.

    Sanitizes NUL/C0 control characters out of every piece of text before
    windowing — see :func:`_sanitize_control_chars` and this module's own
    docstring for why this is the right (and only needed) place to do it.
    """
    if isinstance(source, str):
        elements: List[tuple[Optional[str], str]] = []
        full = _sanitize_control_chars(source)
    else:
        elements = [
            (_sanitize_control_chars(section_path) if section_path else section_path, _sanitize_control_chars(text))
            for section_path, text in source.elements
        ]
        full = _sanitize_control_chars(source.full_text)

    chunks: List[Chunk] = []
    ordinal = 0

    if elements:
        for section_path, text in elements:
            for piece in _window(text, target_chars, overlap_chars):
                chunks.append(Chunk(ordinal=ordinal, text=piece, section_path=section_path))
                ordinal += 1
    else:
        for piece in _window(full, target_chars, overlap_chars):
            chunks.append(Chunk(ordinal=ordinal, text=piece))
            ordinal += 1

    return chunks
