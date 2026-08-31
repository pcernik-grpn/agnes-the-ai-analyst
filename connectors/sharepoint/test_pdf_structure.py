"""Tests for PDF structure reconstruction.

Fixture PDFs are hand-written here as raw bytes. pypdfium2 can only *read*
PDFs, and reportlab is not a dependency of this repo — but a PDF with a couple
of positioned text runs is small enough to author by hand, and doing so keeps
the test suite dependency-free and the fixtures completely explicit: every
assertion below is about glyphs at coordinates this file chose.
"""

from __future__ import annotations

import re

import pytest

from connectors.sharepoint.pdf_structure import (
    PAGE_SEPARATOR,
    DocumentMarkdown,
    StructuredPage,
    reconstruct_page,
    reconstruct_pdf,
)

pdfium = pytest.importorskip("pypdfium2")

PAGE_WIDTH, PAGE_HEIGHT = 612.0, 792.0

# --- minimal PDF writer -----------------------------------------------------

# One text run: text, x, y (baseline, PDF coords with y up), font size.
Run = tuple[str, float, float, float]


def _escape(text: str) -> bytes:
    escaped = text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
    return escaped.encode("latin-1")


def build_pdf(
    pages: list[list[Run]],
    width: float = PAGE_WIDTH,
    height: float = PAGE_HEIGHT,
    page_rotation: int = 0,
) -> bytes:
    """Assemble a minimal, uncompressed, classic-xref PDF from text runs.

    Object layout: 1 catalog, 2 page tree, 3 font, then a page object and a
    content-stream object per page. Each run becomes one
    ``BT /F1 <size> Tf <x> <y> Td (<text>) Tj ET``.

    Run coordinates are always *display* coordinates — where the reader sees
    the text, y up from the bottom-left of the displayed page. With
    ``page_rotation=90`` the page keeps its portrait ``MediaBox``, gains
    ``/Rotate 90``, and each run is written with the text matrix that makes it
    read horizontally once the viewer applies that rotation. That is how a real
    rotated page is built, and it is the only fixture shape that actually
    exercises the module's rotation handling.
    """
    if page_rotation not in (0, 90):
        raise ValueError("fixture supports /Rotate 0 and 90")
    objects: dict[int, bytes] = {}
    font_num = 3
    page_nums = [4 + 2 * i for i in range(len(pages))]
    content_nums = [5 + 2 * i for i in range(len(pages))]

    objects[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    kids = b" ".join(b"%d 0 R" % num for num in page_nums)
    objects[2] = b"<< /Type /Pages /Kids [ " + kids + b" ] /Count %d >>" % len(pages)
    objects[font_num] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"

    for index, runs in enumerate(pages):
        objects[page_nums[index]] = (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %.2f %.2f] /Rotate %d "
            b"/Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>"
            % (width, height, page_rotation, font_num, content_nums[index])
        )
        stream = b"\n".join(_run_ops(run, page_rotation, width) for run in runs)
        objects[content_nums[index]] = b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: dict[int, int] = {}
    for num in sorted(objects):
        offsets[num] = len(out)
        out += b"%d 0 obj\n" % num + objects[num] + b"\nendobj\n"

    xref_offset = len(out)
    size = max(objects) + 1
    out += b"xref\n0 %d\n" % size
    out += b"0000000000 65535 f \n"
    for num in range(1, size):
        out += b"%010d 00000 n \n" % offsets[num]
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (size, xref_offset)
    return bytes(out)


def _run_ops(run: Run, page_rotation: int, width: float) -> bytes:
    text, x, y, size = run
    if page_rotation == 90:
        # Display (x, y) -> page space: the glyphs run up the page, so that the
        # viewer's 90-degree clockwise rotation lays them out left to right.
        placement = b"0 1 -1 0 %.2f %.2f Tm" % (width - y, x)
    else:
        placement = b"%.2f %.2f Td" % (x, y)
    return b"BT /F1 %.2f Tf " % size + placement + b" (" + _escape(text) + b") Tj ET"


def write_pdf(tmp_path, pages: list[list[Run]], name: str = "fixture.pdf"):
    path = tmp_path / name
    path.write_bytes(build_pdf(pages))
    return path


def first_page(pages: list[list[Run]]) -> StructuredPage:
    """Reconstruct page 0 of an in-memory fixture."""
    document = pdfium.PdfDocument(build_pdf(pages))
    try:
        return reconstruct_page(document[0])
    finally:
        document.close()


#: Split on unescaped pipes only, so an escaped ``\|`` inside a cell does not
#: read back as a column boundary.
_CELL_SPLIT = re.compile(r"(?<!\\)\|")


def rows_of(markdown: str) -> list[list[str]]:
    """Parse a markdown table back into cells, dropping the ``---`` rule."""
    rows = []
    for line in markdown.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in _CELL_SPLIT.split(line)[1:-1]]
        if cells and all(c and set(c) <= {"-", ":"} for c in cells):
            continue
        rows.append(cells)
    return rows


# --- the fixture writer itself must produce something pypdfium2 can read ------


def test_fixture_pdf_actually_parses():
    document = pdfium.PdfDocument(build_pdf([[("Hello", 72, 700, 12)], [("World", 72, 700, 12)]]))
    try:
        assert len(document) == 2
        page = document[0]
        assert page.get_size() == (PAGE_WIDTH, PAGE_HEIGHT)
        textpage = page.get_textpage()
        assert textpage.count_chars() == 5
        assert "Hello" in textpage.get_text_bounded()
        left, bottom, right, top = textpage.get_charbox(0, loose=True)
        assert right > left and top > bottom
    finally:
        document.close()


# --- headings ---------------------------------------------------------------


def test_heading_tiers_by_font_size():
    """Three sizes above a body-text mode become #, ##, ### by size tier."""
    body = [("Ordinary paragraph text that carries the body size.", 72, y, 10) for y in range(600, 460, -14)]
    page = first_page(
        [
            [
                ("Document Title", 72, 720, 24),  # 2.4x body
                ("Section Heading", 72, 680, 16),  # 1.6x body
                ("Subsection", 72, 650, 13),  # 1.3x body
                *body,
            ]
        ]
    )

    lines = page.markdown.splitlines()
    assert "# Document Title" in lines
    assert "## Section Heading" in lines
    assert "### Subsection" in lines
    assert page.stats["headings"] == 3
    assert not page.degraded
    # The body itself is not a heading.
    assert not any(line.startswith("#") and "Ordinary" in line for line in lines)


def test_uniform_font_size_yields_no_headings():
    page = first_page([[(f"Line number {i} of plain prose.", 72, 700 - 14 * i, 11) for i in range(8)]])
    assert page.stats["headings"] == 0
    assert "#" not in page.markdown


def test_long_line_is_not_a_heading_despite_size():
    """Short + isolated + larger. A long line in a large face is prose."""
    long_text = " ".join(f"word{i}" for i in range(30))
    page = first_page(
        [
            [
                (long_text, 40, 720, 18),
                *[("small body text line here", 40, y, 10) for y in range(680, 560, -13)],
            ]
        ]
    )
    assert page.stats["headings"] == 0
    assert "word0" in page.markdown


# --- tables -----------------------------------------------------------------


def test_three_by_three_aligned_table():
    page = first_page(
        [
            [
                ("Region", 72, 600, 11),
                ("Q1", 220, 600, 11),
                ("Q2", 340, 600, 11),
                ("EMEA", 72, 585, 11),
                ("120", 220, 585, 11),
                ("130", 340, 585, 11),
                ("APAC", 72, 570, 11),
                ("90", 220, 570, 11),
                ("95", 340, 570, 11),
            ]
        ]
    )

    assert page.stats["tables"] == 1
    assert page.stats["fallbacks"] == 0
    assert rows_of(page.markdown) == [
        ["Region", "Q1", "Q2"],
        ["EMEA", "120", "130"],
        ["APAC", "90", "95"],
    ]
    # Header row is visually distinct (labels above numbers), so it is the
    # markdown header rather than an empty one.
    assert page.markdown.splitlines()[0].startswith("| Region")


def test_headerless_table_gets_an_empty_header_row():
    """All rows alike -> no distinct header -> empty header row, no data lost."""
    page = first_page(
        [
            [
                ("alpha", 72, 600, 11),
                ("beta", 220, 600, 11),
                ("gamma", 72, 585, 11),
                ("delta", 220, 585, 11),
                ("epsilon", 72, 570, 11),
                ("zeta", 220, 570, 11),
            ]
        ]
    )
    assert page.stats["tables"] == 1
    assert rows_of(page.markdown) == [
        ["", ""],
        ["alpha", "beta"],
        ["gamma", "delta"],
        ["epsilon", "zeta"],
    ]


def test_ragged_row_is_padded():
    page = first_page(
        [
            [
                ("Name", 72, 600, 11),
                ("Score", 220, 600, 11),
                ("Note", 340, 600, 11),
                ("Ada", 72, 585, 11),
                ("10", 220, 585, 11),
                ("Bob", 72, 570, 11),
                ("7", 220, 570, 11),
                ("late", 340, 570, 11),
            ]
        ]
    )
    assert page.stats["tables"] == 1
    assert rows_of(page.markdown) == [
        ["Name", "Score", "Note"],
        ["Ada", "10", ""],
        ["Bob", "7", "late"],
    ]


def test_ambiguous_block_falls_back_to_text():
    """Columns that do not line up must not become a table.

    Each row's second run starts at a different x, far enough apart that they
    cannot be one column. A wrong table is worse than no table, so the block
    degrades to prose and the bail-out is counted.
    """
    page = first_page(
        [
            [
                ("alpha", 72, 600, 11),
                ("one", 200, 600, 11),
                ("beta", 72, 585, 11),
                ("two", 300, 585, 11),
                ("gamma", 72, 570, 11),
                ("three", 420, 570, 11),
            ]
        ]
    )

    assert page.stats["tables"] == 0
    assert page.stats["fallbacks"] == 1
    assert "|" not in page.markdown
    for token in ("alpha", "one", "beta", "two", "gamma", "three"):
        assert token in page.markdown


def test_prose_is_never_mistaken_for_a_table():
    page = first_page(
        [[("The quick brown fox jumps over the lazy dog and keeps running.", 72, y, 11) for y in range(600, 520, -14)]]
    )
    assert page.stats["tables"] == 0
    assert page.stats["fallbacks"] == 0
    assert "|" not in page.markdown


def test_two_column_prose_is_not_turned_into_a_table():
    """The nastiest false positive: a two-column page layout.

    Its lines share x-positions exactly as a table's rows do. What separates
    them is that the cells are sentences, so the block is refused and rendered
    as text — counted as a fallback, since it genuinely looked tabular.
    """
    left = "the quick brown fox jumped over the lazy sleeping dogs"
    right = "and afterwards it continued running through open fields"
    runs: list[Run] = []
    for y in range(700, 560, -13):
        runs.append((left, 60, y, 10))
        runs.append((right, 330, y, 10))

    page = first_page([runs])
    assert page.stats["tables"] == 0
    assert page.stats["fallbacks"] == 1
    assert "|" not in page.markdown
    assert left in page.markdown and right in page.markdown


def test_table_survives_a_long_cell_among_short_ones():
    """One wordy column does not disqualify an otherwise obvious table."""
    page = first_page(
        [
            [
                ("Code", 72, 600, 11),
                ("Count", 200, 600, 11),
                ("Note", 300, 600, 11),
                ("AA", 72, 585, 11),
                ("12", 200, 585, 11),
                ("needs review soon", 300, 585, 11),
                ("BB", 72, 570, 11),
                ("34", 200, 570, 11),
                ("approved", 300, 570, 11),
            ]
        ]
    )
    assert page.stats["tables"] == 1
    assert rows_of(page.markdown) == [
        ["Code", "Count", "Note"],
        ["AA", "12", "needs review soon"],
        ["BB", "34", "approved"],
    ]


def test_single_multi_column_line_is_not_a_table():
    """One line with a wide gap is a tab stop, not a table."""
    page = first_page(
        [
            [
                ("Invoice", 72, 600, 11),
                ("2026-01-04", 400, 600, 11),
                ("A paragraph of ordinary text follows below it.", 72, 586, 11),
                ("It continues on this line as well.", 72, 572, 11),
            ]
        ]
    )
    assert page.stats["tables"] == 0
    assert "|" not in page.markdown


def test_table_cell_pipes_are_escaped():
    page = first_page(
        [
            [
                ("a|b", 72, 600, 11),
                ("c", 220, 600, 11),
                ("d", 72, 585, 11),
                ("e|f", 220, 585, 11),
                ("g", 72, 570, 11),
                ("h", 220, 570, 11),
            ]
        ]
    )
    assert page.stats["tables"] == 1
    assert r"a\|b" in page.markdown
    # Escaped pipes do not add columns.
    assert all(len(row) == 2 for row in rows_of(page.markdown))


# --- paragraphs -------------------------------------------------------------


def test_soft_wrapped_lines_are_joined_and_blocks_kept_apart():
    page = first_page(
        [
            [
                ("The first paragraph starts here and", 72, 700, 11),
                ("continues onto a second line.", 72, 687, 11),
                ("A separate paragraph after a gap.", 72, 630, 11),
                ("It also wraps once.", 72, 617, 11),
            ]
        ]
    )
    blocks = [b for b in page.markdown.split("\n\n") if b.strip()]
    assert blocks == [
        "The first paragraph starts here and continues onto a second line.",
        "A separate paragraph after a gap. It also wraps once.",
    ]


def test_hyphenated_line_break_is_healed():
    page = first_page(
        [
            [
                ("The organisation was thoroughly reor-", 72, 700, 11),
                ("ganised during the second quarter.", 72, 687, 11),
            ]
        ]
    )
    assert "reorganised" in page.markdown
    assert "reor-" not in page.markdown


# --- documents --------------------------------------------------------------


def test_multi_page_separator(tmp_path):
    path = write_pdf(
        tmp_path,
        [
            [("Page One Title", 72, 720, 24), *[("body text line", 72, y, 10) for y in range(680, 600, -13)]],
            [("Page Two Title", 72, 720, 24), *[("body text line", 72, y, 10) for y in range(680, 600, -13)]],
            [("Page Three Title", 72, 720, 24), *[("body text line", 72, y, 10) for y in range(680, 600, -13)]],
        ],
    )
    markdown = reconstruct_pdf(path)

    parts = markdown.split(PAGE_SEPARATOR)
    assert len(parts) == 3
    assert parts[0].startswith("# Page One Title")
    assert parts[1].startswith("# Page Two Title")
    assert parts[2].startswith("# Page Three Title")
    assert markdown.stats["pages"] == 3
    assert markdown.stats["headings"] == 3
    assert markdown.stats["degraded_pages"] == 0


def test_max_pages_truncates(tmp_path):
    path = write_pdf(tmp_path, [[("Alpha", 72, 700, 12)], [("Beta", 72, 700, 12)]])
    markdown = reconstruct_pdf(path, max_pages=1)
    assert PAGE_SEPARATOR not in markdown
    assert "Alpha" in markdown
    assert "Beta" not in markdown
    assert markdown.stats["pages"] == 1


def test_reconstruct_pdf_returns_a_plain_str_that_carries_stats(tmp_path):
    path = write_pdf(tmp_path, [[("Alpha", 72, 700, 12)]])
    markdown = reconstruct_pdf(path)
    assert isinstance(markdown, str)
    assert isinstance(markdown, DocumentMarkdown)
    assert set(markdown.stats) == {"pages", "headings", "tables", "fallbacks", "degraded_pages"}


def test_output_is_deterministic(tmp_path):
    pages = [
        [
            ("Title", 72, 720, 20),
            ("Region", 72, 600, 11),
            ("Q1", 220, 600, 11),
            ("EMEA", 72, 585, 11),
            ("120", 220, 585, 11),
            ("Some closing prose about the numbers above.", 72, 540, 11),
        ]
    ]
    path = write_pdf(tmp_path, pages)
    assert reconstruct_pdf(path) == reconstruct_pdf(path)


def test_body_height_is_document_wide(tmp_path):
    """A table-only page still gets its headings right, using the doc's body size.

    Page 2 is nothing but a 24pt title. Judged on its own it has no body text
    to compare against, so it would never be a heading; judged against the
    document's 10pt body it clearly is one.
    """
    path = write_pdf(
        tmp_path,
        [
            [("plain body prose line here", 72, y, 10) for y in range(700, 560, -13)],
            [("Late Title", 72, 720, 24)],
        ],
    )
    markdown = reconstruct_pdf(path)
    assert "# Late Title" in markdown.split(PAGE_SEPARATOR)[1]


# --- robustness -------------------------------------------------------------


def test_empty_page_does_not_crash():
    page = first_page([[]])
    assert page.markdown == ""
    assert page.stats["pages"] == 1
    assert not page.degraded


def test_empty_document_reconstructs_to_empty(tmp_path):
    path = write_pdf(tmp_path, [[], [], []])
    markdown = reconstruct_pdf(path)
    assert markdown.strip(" \n-") == ""
    assert markdown.stats["pages"] == 3
    assert markdown.stats["degraded_pages"] == 0


def test_broken_page_degrades_instead_of_raising():
    """Whatever a page object does, reconstruct_page returns a StructuredPage."""

    class ExplodingPage:
        def get_textpage(self):
            raise RuntimeError("no text layer")

        def get_size(self):
            return (PAGE_WIDTH, PAGE_HEIGHT)

        def get_rotation(self):
            return 0

    page = reconstruct_page(ExplodingPage())
    assert isinstance(page, StructuredPage)
    assert page.degraded
    assert page.markdown == ""
    assert page.stats["degraded_pages"] == 1


def test_degraded_page_keeps_the_plain_text():
    """When analysis fails, the page's text still comes through."""

    class HalfBrokenPage:
        def __init__(self, real):
            self._real = real
            self._calls = 0

        def get_textpage(self):
            return self._real.get_textpage()

        def get_size(self):
            self._calls += 1
            if self._calls == 1:
                raise RuntimeError("boom")  # swallowed; falls back to 0,0
            return self._real.get_size()

        def get_rotation(self):
            raise RuntimeError("boom")  # swallowed too

    document = pdfium.PdfDocument(build_pdf([[("Survivor text", 72, 700, 12)]]))
    try:
        page = reconstruct_page(HalfBrokenPage(document[0]))
        assert "Survivor text" in page.markdown
    finally:
        document.close()


def test_charbox_failures_do_not_crash_the_page(monkeypatch):
    """Every per-character box failing leaves an empty, non-degraded page."""
    document = pdfium.PdfDocument(build_pdf([[("Some text", 72, 700, 12)]]))
    try:
        page = document[0]
        real_get_textpage = page.get_textpage

        def broken_textpage():
            textpage = real_get_textpage()
            monkeypatch.setattr(
                type(textpage),
                "get_charbox",
                lambda *a, **k: (_ for _ in ()).throw(RuntimeError("nope")),
                raising=True,
            )
            return textpage

        monkeypatch.setattr(page, "get_textpage", broken_textpage, raising=False)
        result = reconstruct_page(page)
        assert result.markdown == ""
        assert not result.degraded
    finally:
        document.close()


def test_rotated_page_is_turned_upright():
    """A /Rotate 90 page is rotated into display space before lines are grouped.

    The glyph boxes pdfium reports ignore ``/Rotate``, so without the transform
    the y-based line grouping would slice this page down its columns and
    produce one word per line.
    """
    # Display page is 792 wide by 612 tall, so the runs are placed for that.
    runs: list[Run] = [
        ("Rotated Title", 72, 520, 24),
        ("Region", 72, 460, 11),
        ("Q1", 220, 460, 11),
        ("EMEA", 72, 445, 11),
        ("120", 220, 445, 11),
        *[("body prose set on a page that the viewer rotates", 72, y, 11) for y in range(400, 320, -14)],
    ]
    document = pdfium.PdfDocument(build_pdf([runs], page_rotation=90))
    try:
        page = document[0]
        assert page.get_rotation() == 90
        result = reconstruct_page(page)
    finally:
        document.close()

    assert "# Rotated Title" in result.markdown
    assert rows_of(result.markdown) == [["Region", "Q1"], ["EMEA", "120"]]
    assert "body prose set on a page that the viewer rotates" in result.markdown


def test_unloadable_page_degrades_and_keeps_the_separators(tmp_path):
    """A page pdfium cannot load costs that page, not the document.

    Blanking page two's object header leaves a structurally broken page. The
    surviving page must still come through, the page separator must still be
    there so page numbering downstream stays aligned, and the loss must be
    visible in the stats rather than raised.
    """
    good = build_pdf([[("Alpha", 72, 700, 12)], [("Beta", 72, 700, 12)]])
    marker = b"<< /Type /Page /Parent 2 0 R"
    at = good.index(marker, good.index(b"6 0 obj"))
    broken = good[:at] + b"%" * len(marker) + good[at + len(marker) :]
    path = tmp_path / "broken.pdf"
    path.write_bytes(broken)

    markdown = reconstruct_pdf(path)
    assert markdown.stats["pages"] == 2
    assert markdown.stats["degraded_pages"] == 1
    assert markdown.split(PAGE_SEPARATOR) == ["Alpha", ""]


def test_a_file_that_is_not_a_pdf_raises(tmp_path):
    """File-level failure is the caller's to handle; only pages degrade."""
    path = tmp_path / "nope.pdf"
    path.write_bytes(b"this is not a pdf at all")
    with pytest.raises(pdfium.PdfiumError):
        reconstruct_pdf(path)


def test_truncated_pdf_never_raises_an_unexpected_error(tmp_path):
    """Every prefix of a valid PDF either parses or fails as a PdfiumError."""
    good = build_pdf([[("Alpha", 72, 700, 12), ("Beta", 220, 700, 12)]])
    path = tmp_path / "cut.pdf"
    for cut in range(1, len(good), 29):
        path.write_bytes(good[:cut])
        try:
            reconstruct_pdf(path)
        except pdfium.PdfiumError:
            pass


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("#1 in the market this year", r"\#1 in the market this year"),
        ("--- end of section ---", r"\--- end of section ---"),
        ("| not actually a table row", r"\| not actually a table row"),
    ],
)
def test_prose_starting_with_markdown_structure_is_escaped(raw, expected):
    """A paragraph must not read back as a heading, a row, or a page break."""
    page = first_page([[(raw, 72, 700, 11)]])
    assert page.markdown == expected
    assert page.stats["headings"] == 0
