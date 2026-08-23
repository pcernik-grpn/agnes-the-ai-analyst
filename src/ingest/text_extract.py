"""Text extraction for prose documents.

Docling is an OPTIONAL extra (``agnes[docling]``) — it pulls heavy ML deps, so
it is never imported at module top. When importable it gives richer element
structure (and tables); otherwise a lightweight per-format fallback handles the
common text formats with no extra dependencies. Formats that need a parser we
don't have raise :class:`UnsupportedDocument` so the caller can mark the file
``rejected`` rather than indexing garbage.
"""

from __future__ import annotations

import html.parser
import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

# Plain-text formats handled by a direct read (no dependency).
_PLAIN_EXTS = {"txt", "md", "markdown", "rtf", "text", "log"}
_HTML_EXTS = {"html", "htm"}

# Formats the upload allowlist accepts (``src/corpus_allowlist.py``
# TIER1_EXTENSIONS) that ONLY Docling can read — there is no lightweight
# fallback for them. On an image built without the extra these are accepted at
# upload and then rejected by the background task, so the rejection has to say
# which of the two ends is missing: the file is fine, the image lacks a parser.
#
# Kept to the formats Docling actually reads. Naming one it does NOT read
# would be worse than the generic message: it would send an operator to
# rebuild the image for a capability the rebuild does not add.
_DOCLING_ONLY_EXTS = {"docx", "pptx"}

# Allowlisted, and readable by NO build — see ``_UNREADABLE_TIER1`` usage in
# ``extract_text``. Written down rather than left implicit so the drift guard
# in ``tests/test_ingest_optional_extras.py`` stays meaningful: a new
# allowlist entry with no reader fails that test instead of quietly joining
# this set.
#   msg  — Outlook's binary format; needs a third-party parser (extract-msg).
#   epub — no extractor wired up; Docling does not read it either.
_UNREADABLE_TIER1 = {"msg", "epub"}


@dataclass
class ExtractResult:
    """Extracted document text.

    ``elements`` is an optional list of ``(section_path, text)`` pairs when the
    extractor recovers structure (e.g. Docling); ``full_text`` is always set.
    """

    full_text: str
    elements: List[Tuple[Optional[str], str]] = field(default_factory=list)


class UnsupportedDocument(Exception):
    """Raised when no available extractor can read the document."""


def _ext_of(path: str, file_type: Optional[str]) -> str:
    if file_type and "/" not in file_type:
        return file_type.lower().lstrip(".")
    if "." in path:
        return path.rsplit(".", 1)[-1].lower()
    return ""


def _read_text(path: str) -> str:
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


class _HTMLTextExtractor(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._parts: List[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: object) -> None:
        if tag in ("script", "style"):
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip and data.strip():
            self._parts.append(data)

    def text(self) -> str:
        return re.sub(r"\n{3,}", "\n\n", "\n".join(self._parts)).strip()


def _strip_html(raw: str) -> str:
    parser = _HTMLTextExtractor()
    parser.feed(raw)
    return parser.text()


def docling_capability() -> bool:
    """Whether the ``docling`` extra is importable in this deployment.

    An import-spec probe, never an import: Docling pulls torch, and a
    capability question asked while composing an error message (or a
    readiness payload) must not pay that cost. Mirrors
    ``src/ingest/embeddings.py::embedding_capability``, which separates a
    hybrid deployment from a lexical-only one the same way.
    """
    import importlib.util

    return importlib.util.find_spec("docling") is not None


def _try_docling(path: str) -> Optional[ExtractResult]:
    """Use Docling if installed. Returns None when the extra is absent."""
    try:
        from docling.document_converter import DocumentConverter  # type: ignore
    except Exception:
        return None
    try:
        result = DocumentConverter().convert(path)
        md = result.document.export_to_markdown()
        return ExtractResult(full_text=md, elements=[(None, md)])
    except Exception:
        # Docling is present but failed on this doc — fall through to the
        # lightweight path rather than crashing ingestion.
        return None


def _read_email(path: str) -> str:
    """Flatten an RFC-822 message (``.eml``) into indexable text.

    Stdlib only — no extra required. ``.eml`` is on the upload allowlist and
    had no reader at all, on any build: every one was accepted and then
    rejected. Email threads are ordinary corpus material, so a parser that
    ships with Python is the right answer rather than another extra.

    Headers worth searching (who/when/subject) are kept above the body; the
    text/plain part is preferred and HTML is stripped when that is all there
    is. Attachments are skipped: their bytes are not this file's text, and a
    file's own attachments arrive as their own uploads.
    """
    import email
    import email.policy

    with open(path, "rb") as fh:
        msg = email.message_from_binary_file(fh, policy=email.policy.default)

    lines = []
    for header in ("From", "To", "Cc", "Date", "Subject"):
        value = msg.get(header)
        if value:
            lines.append(f"{header}: {value}")

    body = ""
    if msg.is_multipart():
        plain = [p for p in msg.walk() if p.get_content_type() == "text/plain" and not p.get_filename()]
        html = [p for p in msg.walk() if p.get_content_type() == "text/html" and not p.get_filename()]
        chosen = plain or html
        parts = []
        for part in chosen:
            try:
                text = part.get_content()
            except Exception:
                continue
            parts.append(_strip_html(text) if part.get_content_type() == "text/html" else text)
        body = "\n\n".join(p for p in parts if p)
    else:
        try:
            raw = msg.get_content()
        except Exception:
            raw = ""
        body = _strip_html(raw) if msg.get_content_type() == "text/html" else raw

    if body:
        lines.append("")
        lines.append(body.strip())
    return "\n".join(lines).strip()


def _try_pdf(path: str) -> Optional[str]:
    """Extract PDF text with pypdf if importable; None when unavailable."""
    try:
        from pypdf import PdfReader  # type: ignore
    except Exception:
        return None
    try:
        reader = PdfReader(path)
        return "\n\n".join((page.extract_text() or "") for page in reader.pages).strip()
    except Exception:
        return None


def extract_text(path: str, file_type: Optional[str] = None) -> ExtractResult:
    """Extract text from a prose document.

    Order: Docling (if installed) → per-format lightweight fallback. Raises
    :class:`UnsupportedDocument` when nothing can read the file.
    """
    ext = _ext_of(path, file_type)

    doc = _try_docling(path)
    if doc is not None and doc.full_text.strip():
        return doc

    if ext in _PLAIN_EXTS or ext == "":
        return ExtractResult(full_text=_read_text(path))
    if ext in _HTML_EXTS:
        return ExtractResult(full_text=_strip_html(_read_text(path)))
    if ext == "eml":
        return ExtractResult(full_text=_read_email(path))
    if ext == "pdf":
        text = _try_pdf(path)
        if text is not None and text.strip():
            return ExtractResult(full_text=text)
        raise UnsupportedDocument("PDF text extraction needs the 'docling' extra or pypdf; neither is available")

    if ext in _DOCLING_ONLY_EXTS and not docling_capability():
        # Deliberately names the cause and the fix: this file type is on the
        # upload allowlist, so "no text extractor" reads as "your file is
        # broken" when the truth is that this image was built without the
        # parser. Uploading it again — the obvious next move — cannot help.
        raise UnsupportedDocument(
            f"'.{ext}' needs the 'docling' extra, which this deployment was built without — "
            "the file was stored but cannot be indexed here. An operator can switch this "
            "instance to the '-rich' image (see docs/DEPLOYMENT.md → 'The -rich image variant')."
        )

    raise UnsupportedDocument(f"no text extractor for '.{ext}'")
