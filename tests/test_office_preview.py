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

from src.office_preview import docx_paragraphs, is_office_preview_ext, pptx_slides


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
    assert is_office_preview_ext("xlsx") is None
    assert is_office_preview_ext("") is None


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


def test_a_zip_without_the_expected_parts_is_empty() -> None:
    """A ``.docx`` renamed to ``.pptx`` — the agent chose the extension, so it
    is a claim about the bytes and not a fact."""
    assert pptx_slides(_zip({"word/document.xml": _document("Hi")})) == ([], False)
    assert docx_paragraphs(_zip({"ppt/slides/slide1.xml": _slide("Hi")})) == ("", False)


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
