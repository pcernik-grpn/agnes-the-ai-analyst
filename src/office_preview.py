"""Dependency-free structural preview of OOXML deliverables (``.pptx`` / ``.docx``).

**Why this exists.** A chat agent's headline deliverable is a deck or a
document, and the browser cannot draw either one: a ``.pptx`` handed to an
``<iframe>`` is a download prompt, not a preview. Until the reader downloads
it and opens PowerPoint, the only thing the session-files drawer can say about
a deck is its filename and its byte count — which is exactly the complaint
that motivated this module.

**Why not the ingestion extractor.** ``src/ingest/text_extract.py`` already
reads these formats, but only through Docling (``_DOCLING_ONLY_EXTS``), an
optional heavy extra that a stock install does not have. A preview that works
on some deployments and not others is not a preview, so this reads the
archives itself. It is a *glance*, not an extraction: no styling, no images,
no speaker notes, no tables — the deck's words, in order, so a reader can
confirm the agent built the thing they asked for before spending a download.

**Why regexes and not an XML parser.** Same call ``_try_epub`` in
``src/ingest/text_extract.py`` made, for the same reasons: an OOXML part is
untrusted input (the agent's tool calls chose every byte),
``xml.etree.ElementTree`` is explicitly not safe against entity expansion, and
``defusedxml`` would be a new dependency for a format we need three element
names out of. Every pattern here is linear-time — character classes with no
nested quantifier and no alternation that can backtrack — and every read off
the archive is byte-budgeted, so neither a zip bomb nor a hostile part can
turn a preview into the expensive part of the request.
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from io import BytesIO

__all__ = ["Slide", "docx_paragraphs", "is_office_preview_ext", "pptx_slides"]


# --- Budgets ---------------------------------------------------------------
#
# A preview is a glance. These ceilings are deliberately far below what the
# formats allow: past them the answer is "download it", not "render more".

#: Decompressed bytes read from any single XML part. A slide part is a few KB
#: in practice; a part claiming more than this is either pathological or
#: carrying embedded media we do not read anyway.
_MAX_PART_BYTES = 4 * 1024 * 1024

#: Archive members examined at all. Bounds the namelist walk on an archive
#: that declares tens of thousands of entries.
_MAX_MEMBERS = 5_000

#: Slides / paragraphs returned. Beyond this the caller reports ``truncated``.
_MAX_SLIDES = 60
_MAX_PARAGRAPHS = 400

#: Characters kept per single line, and in total. A generated deck can carry a
#: whole table flattened into one text run.
_MAX_LINE_CHARS = 2_000
_MAX_TOTAL_CHARS = 40_000

#: Extensions this module can preview, mapped to the kind the caller reports.
_OFFICE_EXTS = {"pptx": "slides", "docx": "text"}


# --- Patterns --------------------------------------------------------------
#
# ``[^<]*`` (never ``.*?``) is what keeps these linear: the body of a text run
# cannot contain ``<``, so there is exactly one way to match and nothing to
# backtrack over. The optional namespace prefix is matched with a bounded
# character class for the same reason.

#: A DrawingML paragraph boundary (``<a:p>`` / ``<a:p …>``), used to group runs
#: — without it, "Engagement Type Breakdown" split across three formatting
#: runs would render as three separate lines.
_RE_A_PARA = re.compile(r"<(?:[\w.-]{1,32}:)?p[\s>]")
#: A DrawingML text run body: ``<a:t>text</a:t>``.
_RE_A_TEXT = re.compile(r"<(?:[\w.-]{1,32}:)?t(?:\s[^<>]{0,4096})?>([^<]*)</(?:[\w.-]{1,32}:)?t>")

#: WordprocessingML equivalents. ``w:p`` and ``w:t`` share the local names
#: ``p``/``t`` with DrawingML, so the same two patterns serve both formats —
#: which is why they carry no fixed prefix.
_RE_W_PARA = _RE_A_PARA
_RE_W_TEXT = _RE_A_TEXT

#: ``ppt/slides/slide12.xml`` — the number is what orders the deck; archive
#: order does not. Bounded to 4 digits so a crafted name cannot make the int()
#: below the expensive part.
_RE_SLIDE_MEMBER = re.compile(r"^ppt/slides/slide(\d{1,4})\.xml$")

#: XML character references, resolved after extraction. Bounded digit counts
#: keep this linear and keep ``int()`` cheap.
_RE_CHARREF = re.compile(r"&#(?:x([0-9A-Fa-f]{1,6})|(\d{1,7}));")

#: Runs of whitespace collapse to one space — OOXML wraps its XML for
#: readability, and the newlines are markup, not content.
_RE_WS = re.compile(r"\s+")

_NAMED_ENTITIES = {"&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&apos;": "'"}


@dataclass
class Slide:
    """One slide's words. ``title`` is the first non-empty paragraph — a
    heuristic, not the placeholder-type read a real renderer would do, and
    honest for the generated decks this previews (their first paragraph IS
    the title). ``lines`` holds every paragraph after it."""

    index: int
    title: str = ""
    lines: list[str] = field(default_factory=list)


def is_office_preview_ext(ext: str) -> str | None:
    """The preview kind this module produces for ``ext``, or ``None``.

    Kept here rather than in the caller so "which formats does this reach"
    has one answer, next to the code that implements it.
    """
    return _OFFICE_EXTS.get((ext or "").lower().lstrip("."))


def _unescape(raw: str) -> str:
    """Resolve the entity forms an OOXML text run can legally carry.

    Only the five predefined entities plus character references: a part that
    *defines* its own entity is exactly the input an XML parser would expand
    for us, and declining to is the point.
    """
    if "&" not in raw:
        return raw
    for entity, char in _NAMED_ENTITIES.items():
        raw = raw.replace(entity, char)

    def _char(match: re.Match[str]) -> str:
        hex_digits, dec_digits = match.group(1), match.group(2)
        try:
            code = int(hex_digits, 16) if hex_digits else int(dec_digits)
        except ValueError:  # pragma: no cover - the pattern bounds the digits
            return match.group(0)
        # Surrogates and out-of-range code points are not text; drop them
        # rather than raise on an archive we can otherwise read.
        if code > 0x10FFFF or 0xD800 <= code <= 0xDFFF:
            return ""
        return chr(code)

    return _RE_CHARREF.sub(_char, raw)


def _paragraph_lines(xml: str, para_re: re.Pattern[str], text_re: re.Pattern[str], limit: int) -> list[str]:
    """Split one XML part into paragraphs and flatten each to a single line.

    Splitting on the paragraph element and joining the text runs *within* each
    piece is what turns a formatting-fragmented heading back into one line.
    """
    lines: list[str] = []
    # ``para_re.split`` keeps the leading fragment (everything before the first
    # paragraph — the part's own header), which carries no text runs and so
    # contributes an empty line that is dropped below.
    for chunk in para_re.split(xml):
        if len(lines) >= limit:
            break
        text = "".join(_unescape(m.group(1)) for m in text_re.finditer(chunk))
        text = _RE_WS.sub(" ", text).strip()
        if text:
            lines.append(text[:_MAX_LINE_CHARS])
    return lines


def _open(data: bytes) -> zipfile.ZipFile | None:
    try:
        return zipfile.ZipFile(BytesIO(data))
    except Exception:
        # Not a zip, truncated, or encrypted — all "no preview", never an error
        # the caller has to distinguish.
        return None


def _part_text(zf: zipfile.ZipFile, name: str) -> str:
    """Read one member under the byte budget, never trusting its declared size."""
    try:
        with zf.open(name) as fh:
            raw = fh.read(_MAX_PART_BYTES)
    except Exception:
        return ""
    return raw.decode("utf-8", errors="replace")


def pptx_slides(data: bytes) -> tuple[list[Slide], bool]:
    """Slide-by-slide text of a ``.pptx``, in deck order.

    Returns ``([], False)`` for anything unreadable — a caller's fallback is
    "no preview", which is a better outcome than an exception for an archive
    whose only sin is being a format we guessed wrong about.

    The bool is ``truncated``: more slides existed than the ceiling returns,
    or the total character budget ran out mid-deck.
    """
    zf = _open(data)
    if zf is None:
        return [], False
    with zf:
        numbered: list[tuple[int, str]] = []
        for name in zf.namelist()[:_MAX_MEMBERS]:
            match = _RE_SLIDE_MEMBER.match(name)
            if match:
                numbered.append((int(match.group(1)), name))
        if not numbered:
            return [], False
        numbered.sort()
        truncated = len(numbered) > _MAX_SLIDES

        slides: list[Slide] = []
        spent = 0
        for position, (_, name) in enumerate(numbered[:_MAX_SLIDES], start=1):
            lines = _paragraph_lines(_part_text(zf, name), _RE_A_PARA, _RE_A_TEXT, _MAX_PARAGRAPHS)
            spent += sum(len(line) for line in lines)
            slides.append(
                Slide(
                    index=position,
                    title=lines[0] if lines else "",
                    lines=lines[1:],
                )
            )
            if spent >= _MAX_TOTAL_CHARS:
                # Stop at a slide boundary rather than mid-deck with a
                # half-rendered slide: the reader can tell a short preview
                # from a broken one.
                truncated = truncated or position < len(numbered)
                break
        return slides, truncated


def docx_paragraphs(data: bytes) -> tuple[str, bool]:
    """Body text of a ``.docx`` as newline-joined paragraphs.

    Same contract as :func:`pptx_slides`: ``("", False)`` when unreadable, and
    the bool reports that the ceilings clipped the result.
    """
    zf = _open(data)
    if zf is None:
        return "", False
    with zf:
        if "word/document.xml" not in set(zf.namelist()[:_MAX_MEMBERS]):
            return "", False
        lines = _paragraph_lines(
            _part_text(zf, "word/document.xml"),
            _RE_W_PARA,
            _RE_W_TEXT,
            _MAX_PARAGRAPHS,
        )
    text = "\n".join(lines)
    truncated = len(lines) >= _MAX_PARAGRAPHS or len(text) > _MAX_TOTAL_CHARS
    return text[:_MAX_TOTAL_CHARS], truncated
