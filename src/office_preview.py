"""Dependency-free structural preview of OOXML deliverables (``.pptx`` / ``.docx`` / ``.xlsx``).

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
A workbook is the same promise in grid form: the cells as they are STORED,
which means a formula shows as its cached result and a date as the serial
number the format keeps it as. Number formats live in a different part of the
archive and applying them is a renderer's job, not a glance's.

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

__all__ = [
    "Sheet",
    "Slide",
    "docx_paragraphs",
    "is_office_preview_ext",
    "pptx_slides",
    "xlsx_sheets",
]


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

#: Workbook ceilings. A generated ``.xlsx`` is usually one modest sheet; a
#: pulled export can be 200 000 rows, and the honest answer for that one is
#: "download it", not "scroll the modal".
_MAX_SHEETS = 12
_MAX_ROWS = 100
_MAX_COLS = 30
_MAX_CELL_CHARS = 200

#: Extensions this module can preview, mapped to the kind the caller reports.
_OFFICE_EXTS = {"pptx": "slides", "docx": "text", "xlsx": "sheets"}


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

#: SpreadsheetML. Same discipline as the two above: split on the element
#: boundary, then read attributes off the fragment that follows — never one
#: pattern that has to span a whole element with ``.*?`` inside it.
_RE_SHEET_TAG = re.compile(r"<(?:[\w.-]{1,32}:)?sheet[\s>]")
_RE_REL_TAG = re.compile(r"<Relationship[\s>]")
_RE_SST_ITEM = re.compile(r"<(?:[\w.-]{1,32}:)?si[\s>]")
_RE_ROW = re.compile(r"<(?:[\w.-]{1,32}:)?row[\s>]")
_RE_CELL = re.compile(r"<(?:[\w.-]{1,32}:)?c[\s>]")

#: The attribute block of the element a split above left us at the front of —
#: everything up to the first ``>``. ``[^>]`` cannot cross the boundary, so
#: there is nothing to backtrack over.
_RE_ELEM_ATTRS = re.compile(r"^([^>]{0,4096})>")

#: Individual attributes, read off that block. Each value is a bounded
#: ``[^"]`` run for the same reason.
_RE_ATTR_NAME = re.compile(r'\bname="([^"]{0,255})"')
_RE_ATTR_ID = re.compile(r'\b(?:[\w.-]{1,32}:)?[Ii]d="([^"]{0,64})"')
_RE_ATTR_TARGET = re.compile(r'\bTarget="([^"]{0,512})"')
_RE_ATTR_REF = re.compile(r'\br="([A-Za-z]{1,3})\d{1,7}"')
_RE_ATTR_TYPE = re.compile(r'\bt="([A-Za-z]{1,12})"')

#: A cell's stored value: ``<v>42</v>``. For ``t="s"`` that is an index into
#: the shared-string table, not the text.
_RE_VALUE = re.compile(r"<(?:[\w.-]{1,32}:)?v(?:\s[^<>]{0,1024})?>([^<]*)</(?:[\w.-]{1,32}:)?v>")

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


@dataclass
class Sheet:
    """One worksheet's cells. ``rows`` is rectangular — every row is padded to
    the widest one, so the client can render a grid without re-deriving the
    column count — and a value is always a string, because that is what a
    glance shows. ``truncated`` is per sheet: a workbook can hold one small
    tab next to a 50 000-row export, and saying "showing the first rows" over
    the whole modal would be wrong about the small one."""

    name: str
    rows: list[list[str]] = field(default_factory=list)
    truncated: bool = False


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


def _elem_attrs(fragment: str) -> str:
    """The attribute block of the element ``fragment`` starts inside.

    A split on ``<row``/``<c``/``<sheet`` leaves the attributes at the front
    of the piece and the body after the first ``>``; this returns the former.
    """
    match = _RE_ELEM_ATTRS.match(fragment)
    return match.group(1) if match else ""


def _column_index(letters: str) -> int:
    """``A`` -> 0, ``B`` -> 1, ``AA`` -> 26. Bounded to three letters by the
    pattern that produced it, so this cannot run away."""
    index = 0
    for char in letters.upper():
        index = index * 26 + (ord(char) - 64)
    return index - 1


def _shared_strings(zf: zipfile.ZipFile) -> list[str]:
    """The workbook's shared-string table, in index order.

    Absent, unreadable or empty is not an error: a workbook written with only
    inline strings (or only numbers) legitimately has no table, and a cell
    whose index falls outside it simply previews blank.
    """
    xml = _part_text(zf, "xl/sharedStrings.xml")
    if not xml:
        return []
    out: list[str] = []
    # The leading fragment before the first <si> is the part header — it has
    # no text runs, so the join below yields "" and it is dropped by the
    # enumerate offset rather than by a guard.
    for chunk in _RE_SST_ITEM.split(xml)[1:]:
        text = "".join(_unescape(m.group(1)) for m in _RE_A_TEXT.finditer(chunk))
        out.append(_RE_WS.sub(" ", text).strip()[:_MAX_CELL_CHARS])
    return out


def _sheet_parts(zf: zipfile.ZipFile, members: set[str]) -> list[tuple[str, str]]:
    """``(sheet name, archive member)`` in WORKBOOK order, which is the order
    the tabs are shown in and is NOT the archive's own order.

    The mapping is name -> ``r:id`` (``xl/workbook.xml``) -> target
    (``xl/_rels/workbook.xml.rels``). When either part is missing or the
    relationship does not resolve, the sheet falls back to the conventional
    ``xl/worksheets/sheet<N>.xml`` for its position — a workbook we can still
    show under generated names beats one we refuse.
    """
    rels: dict[str, str] = {}
    rels_xml = _part_text(zf, "xl/_rels/workbook.xml.rels")
    for chunk in _RE_REL_TAG.split(rels_xml)[1:]:
        attrs = _elem_attrs(chunk)
        rel_id = _RE_ATTR_ID.search(attrs)
        rel_target = _RE_ATTR_TARGET.search(attrs)
        if rel_id and rel_target:
            rels[rel_id.group(1)] = rel_target.group(1)

    out: list[tuple[str, str]] = []
    workbook_xml = _part_text(zf, "xl/workbook.xml")
    for position, chunk in enumerate(_RE_SHEET_TAG.split(workbook_xml)[1:], start=1):
        attrs = _elem_attrs(chunk)
        name_match = _RE_ATTR_NAME.search(attrs)
        name = _unescape(name_match.group(1)).strip() if name_match else ""
        rel_id = _RE_ATTR_ID.search(attrs)
        target = rels.get(rel_id.group(1), "") if rel_id else ""
        # Targets are written relative to xl/ ("worksheets/sheet1.xml"), and
        # occasionally absolute ("/xl/worksheets/sheet1.xml").
        member = target.lstrip("/")
        if member and not member.startswith("xl/"):
            member = "xl/" + member
        if member not in members:
            member = f"xl/worksheets/sheet{position}.xml"
        if member in members:
            out.append((name or f"Sheet{position}", member))
        if len(out) >= _MAX_SHEETS:
            break
    return out


def _cell_text(fragment: str, attrs: str, strings: list[str]) -> str:
    """One cell's value as the string a glance shows.

    ``t="s"`` is an index into the shared-string table; ``t="inlineStr"`` and
    ``t="str"`` (a formula's cached string result) carry their own ``<t>``
    runs; ``t="b"`` is a 0/1 boolean. Anything else — including a date, which
    SpreadsheetML stores as a number — is the stored value verbatim.
    """
    cell_type = _RE_ATTR_TYPE.search(attrs)
    kind = cell_type.group(1) if cell_type else ""
    if kind == "inlineStr":
        text = "".join(_unescape(m.group(1)) for m in _RE_A_TEXT.finditer(fragment))
        return _RE_WS.sub(" ", text).strip()[:_MAX_CELL_CHARS]
    value_match = _RE_VALUE.search(fragment)
    if value_match is None:
        # No <v> at all: an empty styled cell, or a formula with no cached
        # result. Both are blank to a reader, and blank is the honest answer.
        return ""
    raw = _unescape(value_match.group(1)).strip()
    if kind == "s":
        try:
            return strings[int(raw)]
        except (ValueError, IndexError):
            # A shared index the table does not have is a malformed workbook,
            # not a reason to fail the whole preview.
            return ""
    if kind == "b":
        return "TRUE" if raw == "1" else "FALSE"
    return _RE_WS.sub(" ", raw)[:_MAX_CELL_CHARS]


def _sheet_rows(xml: str, strings: list[str]) -> tuple[list[list[str]], bool]:
    """One worksheet part as a rectangular grid of strings.

    Cells are placed by their ``r`` reference rather than by their order in
    the part, because SpreadsheetML omits empty cells entirely: a row whose
    only value is in column D is written as one ``<c r="D7">``, and appending
    it would slide it under column A. A cell with no usable reference falls
    back to the next free column, which is what the position-based reading
    would have done for the whole row.
    """
    rows: list[list[str]] = []
    truncated = False
    for chunk in _RE_ROW.split(xml)[1:]:
        if len(rows) >= _MAX_ROWS:
            truncated = True
            break
        cells: dict[int, str] = {}
        next_free = 0
        for cell_fragment in _RE_CELL.split(chunk)[1:]:
            attrs = _elem_attrs(cell_fragment)
            ref = _RE_ATTR_REF.search(attrs)
            column = _column_index(ref.group(1)) if ref else next_free
            next_free = column + 1
            if column < 0 or column >= _MAX_COLS:
                truncated = True
                continue
            text = _cell_text(cell_fragment, attrs, strings)
            if text:
                cells[column] = text
        if not cells:
            # A row of nothing but styling. Keeping it would push the reader's
            # eye down a preview that shows no data.
            continue
        width = max(cells) + 1
        rows.append([cells.get(i, "") for i in range(width)])
    if not rows:
        return [], truncated
    width = max(len(row) for row in rows)
    for row in rows:
        row.extend([""] * (width - len(row)))
    return rows, truncated


def xlsx_sheets(data: bytes) -> tuple[list[Sheet], bool]:
    """Sheet-by-sheet cells of an ``.xlsx``, in workbook (tab) order.

    Same contract as :func:`pptx_slides`: ``([], False)`` for anything
    unreadable, since a caller's fallback is "no preview" and that is a better
    outcome than an exception for an archive we merely guessed the format of.

    The outer bool is ``truncated`` for the WORKBOOK — more sheets existed
    than the ceiling returns, or the total character budget ran out between
    sheets. Each :class:`Sheet` carries its own ``truncated`` for the rows and
    columns clipped inside it.
    """
    zf = _open(data)
    if zf is None:
        return [], False
    with zf:
        members = set(zf.namelist()[:_MAX_MEMBERS])
        parts = _sheet_parts(zf, members)
        if not parts:
            return [], False
        # `_sheet_parts` stops at the ceiling, so "were there more" has to be
        # asked of the archive rather than of its result.
        worksheet_members = {name for name in members if name.startswith("xl/worksheets/sheet")}
        truncated = len(worksheet_members) > len(parts)
        strings = _shared_strings(zf)

        sheets: list[Sheet] = []
        spent = 0
        for name, member in parts:
            rows, clipped = _sheet_rows(_part_text(zf, member), strings)
            spent += sum(len(cell) for row in rows for cell in row)
            sheets.append(Sheet(name=name, rows=rows, truncated=clipped))
            if spent >= _MAX_TOTAL_CHARS:
                # Stop at a sheet boundary for the same reason the deck stops
                # at a slide one: a short preview is readable, a half-rendered
                # grid is not.
                truncated = truncated or len(sheets) < len(parts)
                break
        return sheets, truncated
