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
import logging
import posixpath
import re
import zipfile
from dataclasses import dataclass, field
from typing import List, Optional, Tuple
from urllib.parse import unquote

logger = logging.getLogger(__name__)

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

# Allowlisted, and readable by NO build. EMPTY on purpose: every tier-1
# extension now has a reader somewhere. It stays as the third option the drift
# guard in ``tests/test_ingest_optional_extras.py`` offers a future allowlist
# entry — add a fallback, name the missing extra, or record it here with the
# reason — so an entry with no reader fails a test instead of quietly becoming
# another accepted-then-rejected upload.
#
# It emptied out because the two formats it named went opposite ways: ``epub``
# got the stdlib reader below (a zip of XHTML, no dependency), and ``msg`` left
# the upload allowlist entirely (see ``src/corpus_allowlist.py``) rather than
# stay accepted with nothing able to read it. An extension named here MUST
# still be on the allowlist — the guard asserts that, because a name left
# behind after an allowlist removal describes a path no upload can reach.
_UNREADABLE_TIER1: set[str] = set()

# An EPUB is an untrusted zip, so extraction runs under ceilings rather than
# reading whatever the archive claims: a few KB of deflate can inflate to
# gigabytes, and the member count is unbounded.
_EPUB_MAX_TEXT_BYTES = 32 * 1024 * 1024
_EPUB_MAX_MEMBERS = 2000
# container.xml and the OPF are small by spec; capping them keeps a hostile
# archive from turning metadata parsing into the expensive part.
_EPUB_MAX_MANIFEST_BYTES = 1024 * 1024
_EPUB_DOC_SUFFIXES = (".xhtml", ".html", ".htm")

# The OPF/container pair is read with linear-time regexes rather than an XML
# parser on purpose. ``xml.etree.ElementTree`` is explicitly not safe against
# hostile input (entity expansion), all we need is three attributes, and
# ``defusedxml`` would be a new dependency for a format we just decided not to
# take one for.
#
# Two shapes these have to tolerate, both legal XML and both found by probing
# real-world variants rather than by reading the spec: attribute values in
# SINGLE quotes, and OPF elements carrying a namespace PREFIX (``<opf:item>``)
# instead of relying on the default namespace. Missing either does not fail
# loudly — it drops the spine and silently falls back to archive order, i.e.
# chapters in an arbitrary sequence.
#
# ``\b`` after the element name is load-bearing: without it ``item`` would also
# match ``itemref``. The attribute patterns anchor on a preceding space so
# ``id`` does not match inside ``data-id``.
_ELEM = r"<(?:[\w.-]+:)?%s\b"
_ATTR = r"""(?:^|\s)%s\s*=\s*(["'])([^"']*)\1"""
_RE_ROOTFILE = re.compile(_ELEM % "rootfile" + r"[^>]*" + _ATTR % "full-path", re.I)
_RE_ITEM = re.compile(_ELEM % "item" + r"[^>]*>", re.I)
_RE_ITEMREF = re.compile(_ELEM % "itemref" + r"[^>]*" + _ATTR % "idref", re.I)
_RE_ATTR_ID = re.compile(_ATTR % "id", re.I)
_RE_ATTR_HREF = re.compile(_ATTR % "href", re.I)


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


def _epub_member_bytes(zf: zipfile.ZipFile, name: str, budget: int) -> bytes:
    """Read at most ``budget`` bytes of one member. Never trusts the header's
    declared size — a zip bomb's whole trick is lying about it, so the ceiling
    is applied to the decompressed stream."""
    if budget <= 0:
        return b""
    try:
        with zf.open(name) as fh:
            return fh.read(budget)
    except Exception:
        return b""


def _epub_spine_order(zf: zipfile.ZipFile) -> List[str]:
    """Member names in spine (reading) order, or ``[]`` when unusable.

    File names inside an EPUB are arbitrary, so the spine is the only thing
    that states chapter order — and order is load-bearing downstream, where
    chunking hands a reader neighbouring text as context.
    """
    names = set(zf.namelist())
    if "META-INF/container.xml" not in names:
        return []
    container = _epub_member_bytes(zf, "META-INF/container.xml", _EPUB_MAX_MANIFEST_BYTES)
    root = _RE_ROOTFILE.search(container.decode("utf-8", errors="replace"))
    if not root:
        return []
    opf_name = posixpath.normpath(unquote(root.group(2)).lstrip("/"))
    if opf_name not in names:
        return []
    opf = _epub_member_bytes(zf, opf_name, _EPUB_MAX_MANIFEST_BYTES).decode("utf-8", errors="replace")
    base = posixpath.dirname(opf_name)

    # Two passes per <item> rather than one regex over both attributes: they
    # appear in either order, and a single pattern spanning them would need a
    # backtracking-prone alternation.
    href_by_id = {}
    for tag in _RE_ITEM.finditer(opf):
        chunk = tag.group(0)
        item_id = _RE_ATTR_ID.search(chunk)
        href = _RE_ATTR_HREF.search(chunk)
        if item_id and href:
            href_by_id[item_id.group(2)] = href.group(2)

    ordered: List[str] = []
    for ref in _RE_ITEMREF.finditer(opf):
        href = href_by_id.get(ref.group(2))
        if not href:
            continue
        # An href is relative to the OPF's own directory and may be
        # percent-encoded; a fragment addresses a spot inside the document.
        target = posixpath.normpath(posixpath.join(base, unquote(href.split("#", 1)[0])))
        if target in names and target not in ordered:
            ordered.append(target)
    return ordered


def _try_epub(path: str) -> Optional[str]:
    """Extract text from an EPUB with the stdlib. ``None`` when unreadable.

    An EPUB is a zip of XHTML documents, so this needs no dependency — the
    same call made for ``.eml``.
    """
    try:
        with zipfile.ZipFile(path) as zf:
            members = _epub_spine_order(zf)
            if not members:
                # EPUBs in the wild are malformed. Every XHTML member in
                # archive order is a worse reading order than the spine, but
                # it is real text — better than rejecting a file we can read.
                members = [n for n in zf.namelist() if n.lower().endswith(_EPUB_DOC_SUFFIXES)]

            parts: List[str] = []
            consumed = 0
            truncated = False
            for name in members[:_EPUB_MAX_MEMBERS]:
                remaining = _EPUB_MAX_TEXT_BYTES - consumed
                if remaining <= 0:
                    truncated = True
                    break
                raw = _epub_member_bytes(zf, name, remaining)
                consumed += len(raw)
                text = _strip_html(raw.decode("utf-8", errors="replace"))
                if text:
                    parts.append(text)
            if truncated or len(members) > _EPUB_MAX_MEMBERS:
                # Not silent: a truncated index answers questions about the
                # first half of a book and looks complete while doing it.
                logger.warning(
                    "epub text truncated: read %d of %d members, %d bytes (caps: %d members, %d bytes): %s",
                    min(len(members), _EPUB_MAX_MEMBERS),
                    len(members),
                    consumed,
                    _EPUB_MAX_MEMBERS,
                    _EPUB_MAX_TEXT_BYTES,
                    path,
                )
    except Exception:
        # Corrupt archive, unreadable central directory, etc.
        return None

    return "\n\n".join(parts).strip() or None


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
    if ext == "epub":
        text = _try_epub(path)
        if text:
            return ExtractResult(full_text=text)
        raise UnsupportedDocument(
            "this '.epub' could not be read — an EPUB is a zip of XHTML documents, and this "
            "archive is either corrupt or carries no text. The file was stored. No extra or "
            "image variant changes this; the archive itself is the problem."
        )
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
