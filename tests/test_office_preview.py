"""Tests for ``src/office_preview`` — the dependency-free OOXML glance.

The module exists because the browser cannot draw a ``.pptx`` and the
ingestion extractor can only read one through Docling, an optional extra a
stock install does not have. So these tests care about two things: that it
reads a real archive's words in the right order, and that every hostile or
malformed shape degrades to "no preview" rather than to an exception, a hang,
or unbounded memory — the caller's fallback is a download button, which is a
fine outcome; a 500 in the file drawer is not.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from src.office_preview import docx_paragraphs, is_office_preview_ext, pptx_slides, xlsx_sheets


def _zip(members: dict[str, str | bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, body in members.items():
            zf.writestr(name, body)
    return buf.getvalue()


def _slide(*paragraphs: str) -> str:
    runs = "".join(f"<a:p><a:r><a:t>{p}</a:t></a:r></a:p>" for p in paragraphs)
    return f'<?xml version="1.0"?><p:sld xmlns:p="p" xmlns:a="a"><p:txBody>{runs}</p:txBody></p:sld>'


def _document(*paragraphs: str) -> str:
    runs = "".join(f"<w:p><w:pPr/><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    return f'<?xml version="1.0"?><w:document xmlns:w="w"><w:body>{runs}</w:body></w:document>'


# ---------------------------------------------------------------------------
# Reading a real archive
# ---------------------------------------------------------------------------


def test_slides_come_back_in_deck_order_not_archive_order() -> None:
    """``slide10`` sorts before ``slide2`` as a string and after it as a
    number, and zip order is whatever the writer felt like. Deck order is the
    only order a reader can check against the deck."""
    data = _zip(
        {
            "ppt/slides/slide10.xml": _slide("Tenth"),
            "ppt/slides/slide2.xml": _slide("Second"),
            "ppt/slides/slide1.xml": _slide("First"),
        }
    )
    slides, truncated = pptx_slides(data)
    assert [s.title for s in slides] == ["First", "Second", "Tenth"]
    assert [s.index for s in slides] == [1, 2, 3]
    assert truncated is False


def test_a_heading_split_across_formatting_runs_is_one_line() -> None:
    """The reason this groups by paragraph instead of concatenating text
    runs: a generated deck bolds one word and PowerPoint splits the heading
    into three ``<a:t>`` runs. Flattening per run renders the title as three
    bullet lines."""
    data = _zip(
        {
            "ppt/slides/slide1.xml": (
                '<?xml version="1.0"?><p:sld xmlns:p="p" xmlns:a="a">'
                "<a:p><a:r><a:t>Engagement </a:t></a:r>"
                '<a:r><a:rPr b="1"/><a:t>Type</a:t></a:r>'
                "<a:r><a:t> Breakdown</a:t></a:r></a:p>"
                "<a:p><a:r><a:t>Rapid: 14</a:t></a:r></a:p></p:sld>"
            )
        }
    )
    slides, _ = pptx_slides(data)
    assert slides[0].title == "Engagement Type Breakdown"
    assert slides[0].lines == ["Rapid: 14"]


def test_xml_wrapping_whitespace_is_markup_not_content() -> None:
    data = _zip({"ppt/slides/slide1.xml": _slide("Line\n     with    wrapping")})
    slides, _ = pptx_slides(data)
    assert slides[0].title == "Line with wrapping"


def test_entities_and_character_references_are_resolved() -> None:
    data = _zip({"ppt/slides/slide1.xml": _slide("R&amp;D &#8212; Q3 &lt;draft&gt;")})
    slides, _ = pptx_slides(data)
    assert slides[0].title == "R&D — Q3 <draft>"


def test_a_slide_with_no_text_is_kept_in_position() -> None:
    """Dropping it would renumber every slide after it, so a reader comparing
    the preview to the deck would be off by one from the first image slide on."""
    data = _zip(
        {
            "ppt/slides/slide1.xml": _slide("Title"),
            "ppt/slides/slide2.xml": _slide(),
            "ppt/slides/slide3.xml": _slide("Third"),
        }
    )
    slides, _ = pptx_slides(data)
    assert [(s.index, s.title) for s in slides] == [(1, "Title"), (2, ""), (3, "Third")]


def test_docx_paragraphs_read_the_body_in_order() -> None:
    data = _zip({"word/document.xml": _document("Heading", "First para", "Second para")})
    text, truncated = docx_paragraphs(data)
    assert text == "Heading\nFirst para\nSecond para"
    assert truncated is False


def test_the_two_formats_share_the_local_element_names() -> None:
    """``w:p``/``w:t`` and ``a:p``/``a:t`` differ only by prefix, which is why
    one pair of patterns serves both. Pin it: a future edit that hard-codes
    one prefix silently breaks the other format."""
    assert is_office_preview_ext("pptx") == "slides"
    assert is_office_preview_ext(".DOCX") == "text"
    assert is_office_preview_ext("xlsx") == "sheets"
    assert is_office_preview_ext("csv") is None
    assert is_office_preview_ext("") is None


def _workbook(*sheet_names: str) -> str:
    """``xl/workbook.xml`` — the tab order, which is not the archive order."""
    sheets = "".join(
        f'<sheet name="{name}" sheetId="{n}" r:id="rId{n}"/>' for n, name in enumerate(sheet_names, start=1)
    )
    return f'<?xml version="1.0"?><workbook xmlns:r="r"><sheets>{sheets}</sheets></workbook>'


def _workbook_rels(count: int) -> str:
    rels = "".join(
        f'<Relationship Id="rId{n}" Type="t" Target="worksheets/sheet{n}.xml"/>' for n in range(1, count + 1)
    )
    return f'<?xml version="1.0"?><Relationships>{rels}</Relationships>'


def _shared(*strings: str) -> str:
    items = "".join(f"<si><t>{s}</t></si>" for s in strings)
    return f'<?xml version="1.0"?><sst count="{len(strings)}">{items}</sst>'


def _sheet(*rows: str) -> str:
    """``rows`` are pre-rendered ``<row>`` fragments — see :func:`_row`."""
    return f'<?xml version="1.0"?><worksheet xmlns="x"><sheetData>{"".join(rows)}</sheetData></worksheet>'


def _row(number: int, cells: dict[str, str]) -> str:
    """One ``<row>``; ``cells`` maps a column letter to a raw ``<c>`` body."""
    body = "".join(f'<c r="{col}{number}"{frag}' for col, frag in cells.items())
    return f'<row r="{number}">{body}</row>'


def _num(value: str) -> str:
    return f"><v>{value}</v></c>"


def _sst(index: int) -> str:
    return f' t="s"><v>{index}</v></c>'


def _one_sheet_workbook(*rows: str, name: str = "Data", strings: tuple[str, ...] = ()) -> bytes:
    members: dict[str, str | bytes] = {
        "xl/workbook.xml": _workbook(name),
        "xl/_rels/workbook.xml.rels": _workbook_rels(1),
        "xl/worksheets/sheet1.xml": _sheet(*rows),
    }
    if strings:
        members["xl/sharedStrings.xml"] = _shared(*strings)
    return _zip(members)


# ---------------------------------------------------------------------------
# Workbooks (kind: "sheets")
# ---------------------------------------------------------------------------


def test_sheets_come_back_in_tab_order_not_archive_order() -> None:
    """The workbook's ``<sheets>`` order is what a reader sees as tabs; the
    archive can hold the parts in any order at all, and a third sheet whose
    part is written first must not be previewed first."""
    data = _zip(
        {
            "xl/worksheets/sheet3.xml": _sheet(_row(1, {"A": _num("3")})),
            "xl/worksheets/sheet1.xml": _sheet(_row(1, {"A": _num("1")})),
            "xl/worksheets/sheet2.xml": _sheet(_row(1, {"A": _num("2")})),
            "xl/_rels/workbook.xml.rels": _workbook_rels(3),
            "xl/workbook.xml": _workbook("First", "Second", "Third"),
        }
    )
    sheets, truncated = xlsx_sheets(data)
    assert [s.name for s in sheets] == ["First", "Second", "Third"]
    assert [s.rows for s in sheets] == [[["1"]], [["2"]], [["3"]]]
    assert truncated is False


def test_a_shared_string_cell_reads_the_table_not_its_index() -> None:
    """``t="s"`` means ``<v>`` holds an INDEX. Rendering it verbatim would
    show a column of small integers where the labels should be."""
    data = _one_sheet_workbook(
        _row(1, {"A": _sst(0), "B": _sst(1)}),
        _row(2, {"A": _sst(1), "B": _num("42")}),
        strings=("Region", "Revenue"),
    )
    sheets, _ = xlsx_sheets(data)
    assert sheets[0].rows == [["Region", "Revenue"], ["Revenue", "42"]]


def test_a_shared_index_the_table_does_not_have_is_blank_not_an_error() -> None:
    """A malformed workbook is a blank cell, never an IndexError that 500s the
    whole preview. The row then holds nothing, so it drops out like any other
    valueless row — the sheet survives, empty."""
    data = _one_sheet_workbook(_row(1, {"A": _sst(7), "B": _sst(0)}), strings=("only one",))
    sheets, _ = xlsx_sheets(data)
    assert [(s.name, s.rows) for s in sheets] == [("Data", [["", "only one"]])]


def test_cells_are_placed_by_reference_so_a_gap_keeps_its_column() -> None:
    """SpreadsheetML omits empty cells entirely: a row whose only value is in
    column D is written as one ``<c r="D2">``. Appending it in part order
    would slide it under column A and misreport the whole grid."""
    data = _one_sheet_workbook(
        _row(1, {"A": _num("1"), "B": _num("2"), "C": _num("3"), "D": _num("4")}),
        _row(2, {"D": _num("9")}),
    )
    sheets, _ = xlsx_sheets(data)
    assert sheets[0].rows == [["1", "2", "3", "4"], ["", "", "", "9"]]


def test_rows_are_padded_to_one_width() -> None:
    """The client draws a grid; a ragged ``rows`` would make it re-derive the
    column count and get a different answer per row."""
    data = _one_sheet_workbook(
        _row(1, {"A": _num("1"), "B": _num("2")}),
        _row(2, {"A": _num("3")}),
    )
    sheets, _ = xlsx_sheets(data)
    assert sheets[0].rows == [["1", "2"], ["3", ""]]


def test_an_inline_string_and_a_boolean_read_as_text() -> None:
    data = _one_sheet_workbook(
        _row(1, {"A": ' t="inlineStr"><is><t>inline value</t></is></c>', "B": ' t="b"><v>1</v></c>'}),
        _row(2, {"A": ' t="str"><v>formula result</v></c>', "B": ' t="b"><v>0</v></c>'}),
    )
    sheets, _ = xlsx_sheets(data)
    assert sheets[0].rows == [["inline value", "TRUE"], ["formula result", "FALSE"]]


def test_a_styled_but_valueless_row_is_dropped() -> None:
    """A row of formatting with no ``<v>`` anywhere is not data; keeping it
    would push the reader's eye down a preview that shows nothing."""
    data = _one_sheet_workbook(
        _row(1, {"A": _num("1")}),
        _row(2, {"A": ' s="3"/>', "B": ' s="3"/>'}),
        _row(3, {"A": _num("2")}),
    )
    sheets, _ = xlsx_sheets(data)
    assert sheets[0].rows == [["1"], ["2"]]


def test_a_sheet_past_the_row_ceiling_reports_truncated_on_that_sheet() -> None:
    """Per sheet, not per workbook: one small tab can sit beside a 50 000-row
    export, and a modal-wide notice would be wrong about the small one."""
    from src.office_preview import _MAX_ROWS

    big = _sheet(*[_row(n, {"A": _num(str(n))}) for n in range(1, _MAX_ROWS + 20)])
    data = _zip(
        {
            "xl/workbook.xml": _workbook("Big", "Small"),
            "xl/_rels/workbook.xml.rels": _workbook_rels(2),
            "xl/worksheets/sheet1.xml": big,
            "xl/worksheets/sheet2.xml": _sheet(_row(1, {"A": _num("1")})),
        }
    )
    sheets, workbook_truncated = xlsx_sheets(data)
    assert len(sheets[0].rows) == _MAX_ROWS
    assert sheets[0].truncated is True
    assert sheets[1].truncated is False
    assert workbook_truncated is False


def test_a_workbook_past_the_sheet_ceiling_reports_truncated() -> None:
    from src.office_preview import _MAX_SHEETS

    count = _MAX_SHEETS + 3
    members: dict[str, str | bytes] = {
        "xl/workbook.xml": _workbook(*[f"S{n}" for n in range(1, count + 1)]),
        "xl/_rels/workbook.xml.rels": _workbook_rels(count),
    }
    for n in range(1, count + 1):
        members[f"xl/worksheets/sheet{n}.xml"] = _sheet(_row(1, {"A": _num(str(n))}))
    sheets, truncated = xlsx_sheets(_zip(members))
    assert len(sheets) == _MAX_SHEETS
    assert truncated is True


def test_a_workbook_with_no_rels_falls_back_to_the_conventional_part_names() -> None:
    """A workbook we can still show under its real tab names beats one we
    refuse because one auxiliary part was unreadable."""
    data = _zip(
        {
            "xl/workbook.xml": _workbook("Alpha", "Beta"),
            "xl/worksheets/sheet1.xml": _sheet(_row(1, {"A": _num("1")})),
            "xl/worksheets/sheet2.xml": _sheet(_row(1, {"A": _num("2")})),
        }
    )
    sheets, _ = xlsx_sheets(data)
    assert [(s.name, s.rows) for s in sheets] == [("Alpha", [["1"]]), ("Beta", [["2"]])]


def test_a_namespace_prefixed_relationship_part_still_resolves_the_tab_names() -> None:
    """The relationship element is conventionally unprefixed (the part uses a
    default namespace), but a prefixed form is legal XML. Matching only the
    bare name would drop to the positional fallback, which on a workbook whose
    tabs are not in part order labels every sheet with the wrong name."""
    rels = (
        '<?xml version="1.0"?><r:Relationships xmlns:r="rel">'
        '<r:Relationship Id="rId1" Type="t" Target="worksheets/sheet2.xml"/>'
        '<r:Relationship Id="rId2" Type="t" Target="worksheets/sheet1.xml"/>'
        "</r:Relationships>"
    )
    data = _zip(
        {
            "xl/workbook.xml": _workbook("Alpha", "Beta"),
            "xl/_rels/workbook.xml.rels": rels,
            "xl/worksheets/sheet1.xml": _sheet(_row(1, {"A": _num("beta-part")})),
            "xl/worksheets/sheet2.xml": _sheet(_row(1, {"A": _num("alpha-part")})),
        }
    )
    sheets, _ = xlsx_sheets(data)
    assert [(s.name, s.rows) for s in sheets] == [("Alpha", [["alpha-part"]]), ("Beta", [["beta-part"]])]


def test_an_empty_sheet_is_a_sheet_with_no_rows_not_a_dropped_tab() -> None:
    """An empty sheet is an answer — it tells the reader the agent made the
    tab and wrote nothing into it, which a missing tab would not."""
    data = _one_sheet_workbook(name="Blank")
    sheets, _ = xlsx_sheets(data)
    assert [(s.name, s.rows) for s in sheets] == [("Blank", [])]


# ---------------------------------------------------------------------------
# Malformed and hostile input — every one of these is a `none` preview
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(b"", id="empty"),
        pytest.param(b"not a zip at all", id="not-a-zip"),
        pytest.param(b"PK\x03\x04truncated", id="truncated-zip"),
    ],
)
def test_unreadable_input_is_empty_never_an_exception(data: bytes) -> None:
    assert pptx_slides(data) == ([], False)
    assert docx_paragraphs(data) == ("", False)
    assert xlsx_sheets(data) == ([], False)


def test_a_zip_without_the_expected_parts_is_empty() -> None:
    """A ``.docx`` renamed to ``.pptx`` — the agent chose the extension, so it
    is a claim about the bytes and not a fact."""
    assert pptx_slides(_zip({"word/document.xml": _document("Hi")})) == ([], False)
    assert docx_paragraphs(_zip({"ppt/slides/slide1.xml": _slide("Hi")})) == ("", False)
    assert xlsx_sheets(_zip({"word/document.xml": _document("Hi")})) == ([], False)


def test_a_slide_path_that_only_looks_like_one_is_ignored() -> None:
    """``ppt/slides/_rels/slide1.xml.rels`` and ``…/slideLayout1.xml`` sit
    next to the real parts and are not slides. Matching them would render the
    deck's relationship XML as content."""
    data = _zip(
        {
            "ppt/slides/_rels/slide1.xml.rels": "<Relationships/>",
            "ppt/slideLayouts/slideLayout1.xml": _slide("Layout placeholder"),
            "ppt/slides/slide1.xml": _slide("Real"),
        }
    )
    slides, _ = pptx_slides(data)
    assert [s.title for s in slides] == ["Real"]


def test_a_decompression_bomb_is_read_only_up_to_the_part_budget() -> None:
    """A few KB of deflate expands to gigabytes, and the zip header's declared
    size is exactly what a bomb lies about. The ceiling is applied to the
    decompressed stream, so this returns promptly with a clipped line rather
    than exhausting memory."""
    from src.office_preview import _MAX_PART_BYTES

    bomb = (
        '<?xml version="1.0"?><p:sld xmlns:a="a"><a:p><a:r><a:t>'
        + ("A" * (_MAX_PART_BYTES * 4))
        + "</a:t></a:r></a:p></p:sld>"
    )
    slides, _ = pptx_slides(_zip({"ppt/slides/slide1.xml": bomb}))
    # The part was clipped mid-run, so the closing tag never arrived and the
    # run matched nothing — an empty slide, not a 4 MB string in the response.
    assert len(slides) == 1
    assert len(slides[0].title) < _MAX_PART_BYTES


def test_a_deck_past_the_slide_ceiling_reports_truncated() -> None:
    from src.office_preview import _MAX_SLIDES

    data = _zip({f"ppt/slides/slide{n}.xml": _slide(f"Slide {n}") for n in range(1, _MAX_SLIDES + 5)})
    slides, truncated = pptx_slides(data)
    assert len(slides) == _MAX_SLIDES
    assert truncated is True


def test_an_entity_the_document_defines_itself_is_not_expanded() -> None:
    """The billion-laughs shape. An XML parser would expand this; reading the
    text runs with a linear-time pattern never does — which is the reason
    this module does not use one."""
    bomb = (
        '<?xml version="1.0"?>'
        '<!DOCTYPE sld [<!ENTITY a "AAAAAAAAAA"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]>'
        '<p:sld xmlns:a="a"><a:p><a:r><a:t>&b;&b;&b;</a:t></a:r></a:p></p:sld>'
    )
    slides, _ = pptx_slides(_zip({"ppt/slides/slide1.xml": bomb}))
    # Left verbatim as the undefined references they are, not expanded.
    assert slides[0].title == "&b;&b;&b;"


def test_a_very_long_single_line_is_clipped() -> None:
    from src.office_preview import _MAX_LINE_CHARS

    data = _zip({"ppt/slides/slide1.xml": _slide("B" * (_MAX_LINE_CHARS * 3))})
    slides, _ = pptx_slides(data)
    assert len(slides[0].title) == _MAX_LINE_CHARS


def test_a_document_past_the_paragraph_ceiling_reports_truncated() -> None:
    from src.office_preview import _MAX_PARAGRAPHS

    data = _zip({"word/document.xml": _document(*[f"Para {n}" for n in range(_MAX_PARAGRAPHS + 10)])})
    text, truncated = docx_paragraphs(data)
    assert truncated is True
    assert len(text.splitlines()) == _MAX_PARAGRAPHS
