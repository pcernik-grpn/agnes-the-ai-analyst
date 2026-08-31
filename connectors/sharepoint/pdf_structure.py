"""PDF structure reconstruction — headings and tables from positional text.

The base PDF converter extracts a page's plain text in reading order; what it
cannot recover is *structure*: which lines were headings, and which runs of
lines were laid out as a table. Neither is encoded in a normal PDF — both are
purely visual, so both have to be inferred from where the glyphs sit on the
page.

This module is that post-pass. It reads per-character bounding boxes from
``pypdfium2``'s textpage API and rebuilds, bottom-up:

    chars -> words (x-gap) -> lines (y-overlap) -> blocks (vertical gap)

and then classifies each block:

* **Heading** — a short, isolated line whose glyph height is at least
  :data:`HEADING_MIN_RATIO` times the document's body-text height (the mode of
  line heights). Size tiers map to ``#`` / ``##`` / ``###``.
* **Table** — two or more consecutive lines that share at least two aligned
  column x-positions, inferred from intra-line gaps. Emitted as a GitHub
  markdown table.
* **Paragraph** — everything else, with soft-wrap healing (the lines of a block
  are joined into one paragraph, hyphenated line breaks are stitched).

Two rules govern the whole module:

* **A wrong table is worse than no table.** Whenever column inference is
  ambiguous — a segment that fits no column, two segments landing in the same
  column, a block that is mostly single-column prose — the block degrades to
  plain text and the fallback is counted in ``stats["fallbacks"]``.
* **Never raise on a weird page.** Any failure while analysing a page degrades
  that page to its plain sorted text and is counted in
  ``stats["degraded_pages"]``.

Output is deterministic: every ordering is total, and no iteration depends on
set or dict hash order.

Licensing note: this deliberately builds on ``pypdfium2`` (Apache-2.0/BSD-3).
PyMuPDF/``fitz`` is AGPL and is banned in this repository.

Usage::

    from connectors.sharepoint.pdf_structure import reconstruct_pdf

    markdown = reconstruct_pdf(Path("report.pdf"))
    print(markdown.stats)   # {"pages": 4, "headings": 7, "tables": 2, ...}
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median
from typing import Any

__all__ = [
    "PAGE_SEPARATOR",
    "StructuredPage",
    "DocumentMarkdown",
    "reconstruct_page",
    "reconstruct_pdf",
]

# The separator the base converter uses between pages, so this module can be
# swapped in as the pdf engine without changing downstream splitting.
PAGE_SEPARATOR = "\n\n---\n\n"

# --- tuning -----------------------------------------------------------------
# All ratios are relative to a glyph-height unit, never absolute points, so the
# heuristics hold for a 9pt legal document and a 14pt report alike.

#: A line at or above this multiple of body height is a heading candidate.
HEADING_MIN_RATIO = 1.25
#: Size tiers for ``###`` / ``##`` / ``#``.
HEADING_H2_RATIO = 1.45
HEADING_H1_RATIO = 1.80
#: A heading is short. Longer lines are prose set in a larger face.
HEADING_MAX_WORDS = 14
#: A heading is isolated: it is (nearly) alone in its block.
HEADING_MAX_BLOCK_LINES = 2

#: Horizontal gap, as a fraction of glyph height, that separates two words.
#: Only a fallback: an explicit space character in the content stream is the
#: primary signal, and this catches the PDFs that position each word instead.
#: Glyph height here is the font's bounding box (~1.17 em for a text face), so
#: this is roughly 0.19 em — comfortably under a space (~0.25 em) and well over
#: the gap between two letters of one word.
WORD_GAP_RATIO = 0.16
#: Horizontal gap, as a fraction of body height, that separates two columns.
#: Deliberately far above any plausible inter-word space (including the
#: stretched spaces of justified text) — an over-eager column split is the main
#: way to hallucinate a table.
COLUMN_GAP_RATIO = 0.90
#: How far a segment's left edge may sit from a column's centre and still be
#: considered part of it.
COLUMN_TOLERANCE_RATIO = 0.60
COLUMN_TOLERANCE_MIN = 3.0
#: A column must be supported by at least this many rows to be real.
COLUMN_MIN_ROWS = 2
#: At least this fraction of a block's lines must be multi-column for the block
#: to be considered a table at all.
TABLE_MIN_MULTI_FRACTION = 0.6
#: A table cell is a label, a number, or a short phrase. Above this median word
#: count the "columns" are far more likely to be a multi-column *prose* layout,
#: whose lines share x-positions exactly as a table's rows do but whose cells
#: are sentences. Turning that into a table would be badly wrong, so it is
#: refused and rendered as text.
TABLE_MAX_MEDIAN_CELL_WORDS = 6

#: Vertical gap, as a fraction of body height, that ends a block.
BLOCK_GAP_RATIO = 0.60
#: A change of this magnitude in line height also ends a block, so that a
#: heading is always isolated from the paragraph that follows it.
BLOCK_SIZE_CHANGE_RATIO = 1.20

#: Above this page count, body height is computed per page rather than
#: document-wide, so a huge PDF is never fully held in memory.
MAX_PAGES_FOR_DOCUMENT_WIDE_BODY = 500

_NUMERIC_RE = re.compile(r"^[\s(]*[-+$€£¥]?\s*\d[\d\s.,]*\s*[%)]?\s*$")
_STAT_KEYS = ("pages", "headings", "tables", "fallbacks", "degraded_pages")


def _empty_stats() -> dict[str, int]:
    return dict.fromkeys(_STAT_KEYS, 0)


# --- public result types ----------------------------------------------------


@dataclass
class StructuredPage:
    """One page's reconstructed markdown, plus what the reconstruction did.

    ``markdown`` is the contract; ``stats`` and ``degraded`` are additive and
    carry defaults, so ``StructuredPage(markdown=...)`` stays valid.
    """

    markdown: str
    stats: dict[str, int] = field(default_factory=_empty_stats)
    degraded: bool = False


class DocumentMarkdown(str):
    """The document markdown. Is a ``str``; also carries ``.stats``.

    :func:`reconstruct_pdf` returns this so a caller that just wants the text
    can treat it as a plain string, while a caller that wants to know how much
    structure was recovered (or how often the module bailed out) can read
    ``.stats`` without a second entry point.
    """

    # No __slots__: a str subclass cannot declare a non-empty one.
    stats: dict[str, int]

    def __new__(cls, text: str, stats: dict[str, int] | None = None) -> DocumentMarkdown:
        obj = super().__new__(cls, text)
        obj.stats = dict(stats) if stats else _empty_stats()
        return obj


# --- geometry primitives ----------------------------------------------------


@dataclass(frozen=True)
class _Char:
    text: str
    x0: float
    x1: float
    bottom: float
    top: float
    #: The preceding entry in the page's character array was whitespace, so a
    #: word ends here regardless of geometry.
    break_before: bool = False

    @property
    def height(self) -> float:
        return self.top - self.bottom

    @property
    def ycenter(self) -> float:
        return (self.top + self.bottom) / 2.0


@dataclass(frozen=True)
class _Word:
    text: str
    x0: float
    x1: float
    height: float


@dataclass
class _Line:
    words: list[_Word]
    x0: float
    x1: float
    bottom: float
    top: float
    height: float

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)


def _rotate(
    x0: float, bottom: float, x1: float, top: float, rotation: int, w: float, h: float
) -> tuple[float, float, float, float]:
    """Map a page-space box into upright display space.

    Character boxes come back in page space, which ignores ``/Rotate``. Line
    grouping is y-based, so on a rotated page it would slice the text the wrong
    way round unless the coordinates are turned upright first.
    """
    if rotation == 90:
        pts = [(bottom, w - x0), (top, w - x1)]
    elif rotation == 180:
        pts = [(w - x0, h - bottom), (w - x1, h - top)]
    elif rotation == 270:
        pts = [(h - bottom, x0), (h - top, x1)]
    else:
        return x0, bottom, x1, top
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def _extract_chars(textpage: Any, rotation: int, width: float, height: float) -> list[_Char]:
    """Read every positioned glyph off a textpage.

    Boxes come from pdfium's *loose* box, which is the glyph's advance width by
    the font's bounding box. That is the right primitive for both axes:

    * Horizontally it is advance-based, so two letters of one word touch
      (gap 0) while a dropped space leaves a gap of exactly one space advance.
      The tight box would instead leave a wide gap between ``1`` and ``2``,
      whose ink is far narrower than their advance, and split every number in
      the document into separate words.
    * Vertically it is the font bounding box, so it is proportional to the font
      size whatever the glyph is — ``x`` and ``T`` in one run measure the same,
      which is what makes the heading size tiers stable.

    The tight box is kept only as a fallback for glyphs with no loose box.
    """
    count = int(textpage.count_chars())
    if count <= 0:
        return []

    bulk: str | None
    try:
        bulk = textpage.get_text_range()
    except Exception:
        bulk = None
    if bulk is not None and len(bulk) != count:
        bulk = None  # indices no longer line up with the char array

    chars: list[_Char] = []
    pending_break = False
    for index in range(count):
        if bulk is not None:
            char = bulk[index]
        else:
            try:
                char = textpage.get_text_range(index, 1)
            except Exception:
                continue
            if len(char) != 1:
                continue
        if char == "\ufffe":
            # pdfium's marker for a hyphen that ends a line. Restoring it as a
            # hyphen is what lets soft-wrap healing stitch the word back up.
            char = "-"
        if not char.strip() or char.isspace():
            pending_break = True  # spaces, and the \r\n pdfium puts between lines
            continue

        tight = _charbox(textpage, index, loose=False)
        loose = _charbox(textpage, index, loose=True)
        if tight is None and loose is None:
            continue

        horizontal = loose if loose and loose[2] > loose[0] else tight
        vertical = loose if loose and loose[3] > loose[1] else tight
        if horizontal is None or vertical is None:
            continue
        x0, x1 = horizontal[0], horizontal[2]
        bottom, top = vertical[1], vertical[3]
        if top <= bottom or x1 < x0:
            continue  # degenerate box: no usable position

        x0, bottom, x1, top = _rotate(x0, bottom, x1, top, rotation, width, height)
        chars.append(_Char(text=char, x0=x0, x1=x1, bottom=bottom, top=top, break_before=pending_break))
        pending_break = False
    return chars


def _charbox(textpage: Any, index: int, *, loose: bool) -> tuple[float, float, float, float] | None:
    try:
        left, bottom, right, top = textpage.get_charbox(index, loose=loose)
    except Exception:
        return None
    if any(v != v for v in (left, bottom, right, top)):  # NaN
        return None
    return float(left), float(bottom), float(right), float(top)


# --- chars -> words -> lines ------------------------------------------------


def _group_lines(chars: list[_Char]) -> list[_Line]:
    """Cluster characters into lines by vertical overlap, then into words."""
    if not chars:
        return []

    ordered = sorted(chars, key=lambda c: (-c.ycenter, c.x0, c.text))
    groups: list[list[_Char]] = []
    current: list[_Char] = []
    top = bottom = 0.0

    for char in ordered:
        if current:
            center = (top + bottom) / 2.0
            # "centre inside the other box", symmetrically: tolerant enough for
            # a cell whose baseline drifts, tight enough not to chain a heading
            # into the paragraph beneath it.
            same = (bottom <= char.ycenter <= top) or (char.bottom <= center <= char.top)
            if not same:
                groups.append(current)
                current = []
        if current:
            current.append(char)
            top = max(top, char.top)
            bottom = min(bottom, char.bottom)
        else:
            current = [char]
            top, bottom = char.top, char.bottom
    if current:
        groups.append(current)

    lines = [line for line in (_build_line(g) for g in groups) if line is not None]
    lines.sort(key=lambda ln: (-ln.top, ln.x0))
    return lines


def _build_line(chars: list[_Char]) -> _Line | None:
    if not chars:
        return None
    ordered = sorted(chars, key=lambda c: (c.x0, c.x1, c.text))
    size = median([c.height for c in ordered])
    gap_threshold = max(WORD_GAP_RATIO * size, 0.1)

    words: list[_Word] = []
    bucket: list[_Char] = [ordered[0]]
    right = ordered[0].x1
    for char in ordered[1:]:
        if char.break_before or char.x0 - right > gap_threshold:
            words.append(_make_word(bucket))
            bucket = []
        bucket.append(char)
        right = max(right, char.x1)
    words.append(_make_word(bucket))

    words = [w for w in words if w.text]
    if not words:
        return None
    return _Line(
        words=words,
        x0=min(w.x0 for w in words),
        x1=max(w.x1 for w in words),
        bottom=min(c.bottom for c in ordered),
        top=max(c.top for c in ordered),
        height=size,
    )


def _make_word(chars: list[_Char]) -> _Word:
    text = "".join(c.text for c in chars)
    return _Word(
        text=text,
        x0=min(c.x0 for c in chars),
        x1=max(c.x1 for c in chars),
        height=median([c.height for c in chars]),
    )


def _body_height(lines: list[_Line]) -> float:
    """The document's body-text height: the mode of line heights.

    Weighted by characters, so a page of body text outvotes a stack of headings
    even when the headings are more numerous.
    """
    if not lines:
        return 0.0
    votes: Counter[float] = Counter()
    for line in lines:
        votes[round(line.height * 2.0) / 2.0] += max(len(line.text), 1)
    best = sorted(votes.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
    if best > 0:
        return best
    positive = [ln.height for ln in lines if ln.height > 0]
    return median(positive) if positive else 0.0


# --- lines -> blocks --------------------------------------------------------


def _group_blocks(lines: list[_Line], body: float) -> list[list[_Line]]:
    if not lines:
        return []
    if body <= 0:
        body = max((ln.height for ln in lines), default=1.0) or 1.0
    # The typical gap is the document's *leading*, so it is estimated only from
    # gaps small enough to be leading: folding the paragraph and section gaps
    # into the median would inflate the threshold until nothing ever split.
    leading = [gap for gap in (prev.bottom - nxt.top for prev, nxt in zip(lines, lines[1:])) if gap <= body]
    typical_gap = median(leading) if leading else 0.0
    threshold = max(BLOCK_GAP_RATIO * body, typical_gap + 0.5 * body)

    blocks: list[list[_Line]] = []
    current = [lines[0]]
    for prev, line in zip(lines, lines[1:]):
        smaller = max(min(prev.height, line.height), 1e-6)
        size_change = max(prev.height, line.height) / smaller >= BLOCK_SIZE_CHANGE_RATIO
        if prev.bottom - line.top > threshold or size_change:
            blocks.append(current)
            current = []
        current.append(line)
    blocks.append(current)
    return blocks


# --- table inference --------------------------------------------------------


@dataclass(frozen=True)
class _Segment:
    text: str
    x0: float
    x1: float


def _segments(line: _Line, column_gap: float) -> list[_Segment]:
    """Split a line into cell candidates at gaps wide enough to be columns."""
    segments: list[_Segment] = []
    bucket: list[_Word] = [line.words[0]]
    for prev, word in zip(line.words, line.words[1:]):
        if word.x0 - prev.x1 > column_gap:
            segments.append(_join(bucket))
            bucket = []
        bucket.append(word)
    segments.append(_join(bucket))
    return segments


def _join(words: list[_Word]) -> _Segment:
    return _Segment(
        text=" ".join(w.text for w in words),
        x0=min(w.x0 for w in words),
        x1=max(w.x1 for w in words),
    )


def _cluster_columns(lefts: list[tuple[float, int]], tolerance: float) -> list[float]:
    """Cluster segment left edges into column positions.

    ``lefts`` is ``(x, row_index)``; a column survives only if at least
    :data:`COLUMN_MIN_ROWS` distinct rows put a segment there.
    """
    columns: list[float] = []
    bucket_x: list[float] = []
    bucket_rows: set[int] = set()
    for x, row in sorted(lefts):
        if bucket_x and x - bucket_x[0] > tolerance:
            if len(bucket_rows) >= COLUMN_MIN_ROWS:
                columns.append(median(bucket_x))
            bucket_x, bucket_rows = [], set()
        bucket_x.append(x)
        bucket_rows.add(row)
    if bucket_x and len(bucket_rows) >= COLUMN_MIN_ROWS:
        columns.append(median(bucket_x))
    return columns


def _infer_table(block: list[_Line], body: float) -> list[list[str]] | None:
    """Return a padded grid of cells, or ``None`` when inference is ambiguous.

    ``None`` is the safe answer and the caller renders plain text instead. It is
    returned for: fewer than two rows; fewer than two supported columns; a block
    that is mostly single-column prose; a segment that matches no column; two
    segments colliding in one column; or cells that read as sentences rather
    than as cells.
    """
    if len(block) < 2:
        return None

    column_gap = COLUMN_GAP_RATIO * max(body, max(ln.height for ln in block))
    rows = [_segments(line, column_gap) for line in block]

    multi = [i for i, segs in enumerate(rows) if len(segs) >= 2]
    if len(multi) < 2:
        return None
    if not any(b - a == 1 for a, b in zip(multi, multi[1:])):
        return None  # the multi-column lines are not consecutive
    if len(multi) < TABLE_MIN_MULTI_FRACTION * len(rows):
        return None  # mostly prose with the odd wide gap

    tolerance = max(COLUMN_TOLERANCE_RATIO * body, COLUMN_TOLERANCE_MIN)
    columns = _cluster_columns([(seg.x0, i) for i, segs in enumerate(rows) for seg in segs], tolerance)
    if len(columns) < 2:
        return None

    grid: list[list[str]] = []
    for segs in rows:
        cells = [""] * len(columns)
        for seg in segs:
            index, distance = _nearest(columns, seg.x0)
            if distance > tolerance:
                return None  # a cell that belongs to no column
            if cells[index]:
                return None  # two cells claiming one column
            cells[index] = seg.text
        grid.append(cells)

    filled = [cell for row in grid for cell in row if cell]
    if filled and median([len(cell.split()) for cell in filled]) > TABLE_MAX_MEDIAN_CELL_WORDS:
        return None  # sentences in every cell: a prose layout, not a table
    return grid


def _nearest(columns: list[float], x: float) -> tuple[int, float]:
    best_index, best_distance = 0, abs(columns[0] - x)
    for index, center in enumerate(columns[1:], start=1):
        distance = abs(center - x)
        if distance < best_distance:
            best_index, best_distance = index, distance
    return best_index, best_distance


def _header_is_distinct(block: list[_Line], grid: list[list[str]]) -> bool:
    """Is the first row visually a header rather than just the first data row?"""
    if len(grid) < 2:
        return False
    rest = [ln.height for ln in block[1:]]
    if rest and block[0].height >= 1.05 * median(rest):
        return True
    # A header labels; the rows below it usually measure. If row 0 carries no
    # numbers and most rows below do, row 0 is a header.
    if any(_is_numeric(c) for c in grid[0] if c):
        return False
    numeric_rows = sum(1 for row in grid[1:] if any(_is_numeric(c) for c in row if c))
    return numeric_rows * 2 >= len(grid) - 1 and numeric_rows > 0


def _is_numeric(cell: str) -> bool:
    return bool(_NUMERIC_RE.match(cell))


def _render_table(grid: list[list[str]], header: bool) -> str:
    width = max(len(row) for row in grid)
    padded = [[_escape_cell(c) for c in row] + [""] * (width - len(row)) for row in grid]
    if header:
        head, body_rows = padded[0], padded[1:]
    else:
        head, body_rows = [""] * width, padded
    lines = [_row(head), _row(["---"] * width)]
    lines.extend(_row(r) for r in body_rows)
    return "\n".join(lines)


def _row(cells: list[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def _escape_cell(text: str) -> str:
    return text.replace("|", r"\|").strip()


# --- rendering --------------------------------------------------------------


def _heading_level(ratio: float) -> int:
    if ratio >= HEADING_H1_RATIO:
        return 1
    if ratio >= HEADING_H2_RATIO:
        return 2
    return 3


def _as_heading(block: list[_Line], body: float) -> list[str] | None:
    if body <= 0 or len(block) > HEADING_MAX_BLOCK_LINES:
        return None
    out: list[str] = []
    for line in block:
        ratio = line.height / body
        if ratio < HEADING_MIN_RATIO or len(line.words) > HEADING_MAX_WORDS:
            return None
        text = line.text.strip()
        if not text:
            return None
        out.append("#" * _heading_level(ratio) + " " + text)
    return out or None


#: Prose that begins with one of these would read back as structure we did not
#: infer — a heading, a table row, or (worst) a page separator.
_STRUCTURAL_PREFIXES = ("#", "|", "---", ">")


def _escape_paragraph(text: str) -> str:
    """Neutralise a leading markdown structure character in body prose."""
    for prefix in _STRUCTURAL_PREFIXES:
        if text.startswith(prefix):
            return "\\" + text
    return text


def _as_paragraph(block: list[_Line]) -> str:
    out = ""
    for line in block:
        text = line.text.strip()
        if not text:
            continue
        if not out:
            out = text
        elif len(out) >= 2 and out.endswith("-") and out[-2].isalpha() and text[:1].islower():
            out = out[:-1] + text  # healed a hyphenated line break
        else:
            out = out + " " + text
    return _escape_paragraph(out)


def _render(lines: list[_Line], body: float, stats: dict[str, int]) -> str:
    parts: list[str] = []
    for block in _group_blocks(lines, body):
        heading = _as_heading(block, body)
        if heading is not None:
            stats["headings"] += len(heading)
            parts.extend(heading)
            continue
        if len(block) >= 2:
            grid = _infer_table(block, body)
            if grid is not None:
                stats["tables"] += 1
                parts.append(_render_table(grid, _header_is_distinct(block, grid)))
                continue
            if _looks_tabular(block, body):
                stats["fallbacks"] += 1
        paragraph = _as_paragraph(block)
        if paragraph:
            parts.append(paragraph)
    return "\n\n".join(parts)


def _looks_tabular(block: list[_Line], body: float) -> bool:
    """Did this block *try* to be a table? Only those count as fallbacks."""
    column_gap = COLUMN_GAP_RATIO * max(body, max(ln.height for ln in block))
    multi = sum(1 for line in block if len(_segments(line, column_gap)) >= 2)
    return multi >= 2


# --- public API -------------------------------------------------------------


def _page_lines(page: Any) -> list[_Line]:
    textpage = page.get_textpage()
    try:
        try:
            width, height = page.get_size()
        except Exception:
            width, height = 0.0, 0.0
        try:
            rotation = int(page.get_rotation() or 0)
        except Exception:
            rotation = 0
        chars = _extract_chars(textpage, rotation, float(width), float(height))
        return _group_lines(chars)
    finally:
        _close(textpage)


def _safe_page(pdf: Any, index: int) -> Any | None:
    """Load one page, or ``None``. A damaged file can fail to load a page."""
    try:
        return pdf[index]
    except Exception:
        return None


def _safe_lines(page: Any) -> list[_Line] | None:
    """``_page_lines``, but ``None`` instead of an exception on a weird page."""
    try:
        return _page_lines(page)
    except Exception:
        return None


def _plain_text(page: Any) -> str:
    """Last resort: whatever text pypdfium2 will give us, or nothing."""
    try:
        textpage = page.get_textpage()
    except Exception:
        return ""
    try:
        return (textpage.get_text_bounded() or "").strip()
    except Exception:
        return ""
    finally:
        _close(textpage)


def _close(obj: Any) -> None:
    try:
        obj.close()
    except Exception:
        pass


def reconstruct_page(page: Any, *, body_height: float | None = None) -> StructuredPage:
    """Reconstruct one page's markdown from its glyph positions.

    Args:
        page: a ``pypdfium2.PdfPage``.
        body_height: the document-wide body-text height, when the caller has
            computed one across pages. Omit it and the page's own mode is used
            — fine for a page of ordinary prose, less good for a page that is
            nothing but a table or a title.

    Never raises: a page that cannot be analysed degrades to its plain text and
    is reported as ``degraded``.
    """
    stats = _empty_stats()
    stats["pages"] = 1
    try:
        lines = _page_lines(page)
        body = body_height if body_height and body_height > 0 else _body_height(lines)
        markdown = _render(lines, body, stats)
        return StructuredPage(markdown=markdown, stats=stats, degraded=False)
    except Exception:
        stats = _empty_stats()
        stats["pages"] = 1
        stats["degraded_pages"] = 1
        return StructuredPage(markdown=_plain_text(page), stats=stats, degraded=True)


def reconstruct_pdf(pdf_path: Path | str | bytes, max_pages: int | None = None) -> DocumentMarkdown:
    """Reconstruct a whole PDF as markdown, pages joined by ``---``.

    Args:
        pdf_path: path to a PDF (raw ``bytes`` are accepted too).
        max_pages: stop after this many pages. ``None`` reads all of them.

    Returns:
        The document markdown. It is a ``str``; it also carries ``.stats``
        (``pages``, ``headings``, ``tables``, ``fallbacks``, ``degraded_pages``).

    Raises:
        pypdfium2.PdfiumError: the file itself cannot be opened as a PDF. Page-
            level damage never raises — the page degrades to its plain text (or
            to nothing, if it will not load at all) and is counted in
            ``stats["degraded_pages"]``, keeping the page separators aligned
            with the real page numbers.
    """
    # Imported lazily: only the whole-file path needs it, and it is a native
    # extension the caller may not have installed for the page-level API.
    import pypdfium2 as pdfium  # type: ignore[import-untyped]

    source: Any = pdf_path
    if isinstance(source, Path):
        source = str(source)

    stats = _empty_stats()
    pdf = pdfium.PdfDocument(source)
    try:
        total = len(pdf)
        if max_pages is not None:
            total = max(0, min(total, int(max_pages)))

        # Body height is a document-wide property: a page that is nothing but a
        # table, or nothing but a title, has no body text of its own to take the
        # mode of. So the pages are analysed once up front and the mode is taken
        # across all of them. Past MAX_PAGES_FOR_DOCUMENT_WIDE_BODY that would
        # mean holding the whole document's geometry in memory, so very long
        # PDFs fall back to a per-page mode.
        document_wide = total <= MAX_PAGES_FOR_DOCUMENT_WIDE_BODY
        analysed: list[list[_Line] | None] = []
        every_line: list[_Line] = []

        if document_wide:
            for index in range(total):
                page = _safe_page(pdf, index)
                lines = _safe_lines(page) if page is not None else None
                analysed.append(lines)
                if lines:
                    every_line.extend(lines)
                _close(page)
        body = _body_height(every_line) if document_wide else 0.0

        chunks: list[str] = []
        for index in range(total):
            page = _safe_page(pdf, index)
            try:
                stats["pages"] += 1
                if page is None:
                    # A page pdfium cannot even load. It contributes an empty
                    # chunk so the page separators still line up with the file.
                    stats["degraded_pages"] += 1
                    chunks.append("")
                    continue
                lines = analysed[index] if document_wide else _safe_lines(page)
                if lines is None:
                    stats["degraded_pages"] += 1
                    chunks.append(_plain_text(page))
                    continue
                try:
                    page_body = body if body > 0 else _body_height(lines)
                    chunks.append(_render(lines, page_body, stats))
                except Exception:
                    stats["degraded_pages"] += 1
                    chunks.append(_plain_text(page))
            finally:
                _close(page)

        return DocumentMarkdown(PAGE_SEPARATOR.join(chunks), stats)
    finally:
        _close(pdf)
