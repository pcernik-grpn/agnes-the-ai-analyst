"""Tests for :mod:`connectors.sharepoint.convert`.

Every fixture is authored in-test rather than committed as a binary blob: a
checked-in PDF/DOCX is unreviewable in a diff, and the exact bytes matter here
(page order, scrambled content streams, a text layer that is deliberately
absent). ``_build_pdf`` hand-writes a conforming PDF — no authoring library
exists in this repo's dependency set, and pypdfium2 reads PDFs but cannot write
them — and ``_write_docx`` builds a minimal OOXML package with ``zipfile`` when
``python-docx`` is not installed, so the markitdown route is always exercised
rather than skipped.
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import pytest

from connectors.sharepoint.convert import (
    DEFAULT_MAX_CHARS,
    PAGE_BREAK,
    ConversionError,
    ConvertResult,
    MissingConversionDependency,
    convert_to_markdown,
)


pypdfium2 = pytest.importorskip("pypdfium2", reason="extraction extra not installed")
pytest.importorskip("markitdown", reason="extraction extra not installed")


# ----------------------------------------------------------------- fixtures


def _build_pdf(pages: list[list[tuple[str, float, float]]]) -> bytes:
    """Hand-write a PDF with a real text layer.

    ``pages`` is one list of ``(text, x, y)`` placements per page, emitted into
    the content stream in the order given — which is what lets a test control
    whether PDFium's native text order is reading order or scrambled.
    """
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    pages_num = len(objects) + 1
    objects.append(b"")  # placeholder, patched once the kids are known

    page_nums: list[int] = []
    for placements in pages:
        ops = [b"BT /F1 12 Tf"]
        for text, x, y in placements:
            escaped = text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
            ops.append(f"1 0 0 1 {x} {y} Tm ({escaped}) Tj".encode("latin-1"))
        ops.append(b"ET")
        stream = b"\n".join(ops)
        content = add(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream")
        page_nums.append(
            add(
                b"<< /Type /Page /Parent "
                + str(pages_num).encode()
                + b" 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 "
                + str(font).encode()
                + b" 0 R >> >> /Contents "
                + str(content).encode()
                + b" 0 R >>"
            )
        )

    kids = b" ".join(str(num).encode() + b" 0 R" for num in page_nums)
    objects[pages_num - 1] = b"<< /Type /Pages /Count " + str(len(page_nums)).encode() + b" /Kids [" + kids + b"] >>"
    catalog = add(b"<< /Type /Catalog /Pages " + str(pages_num).encode() + b" 0 R >>")

    out = bytearray(b"%PDF-1.4\n")
    offsets = [0] * (len(objects) + 1)
    for num, body in enumerate(objects, start=1):
        offsets[num] = len(out)
        out += str(num).encode() + b" 0 obj\n" + body + b"\nendobj\n"
    xref_offset = len(out)
    out += b"xref\n0 " + str(len(objects) + 1).encode() + b"\n0000000000 65535 f \n"
    for num in range(1, len(objects) + 1):
        out += f"{offsets[num]:010d} 00000 n \n".encode()
    out += (
        b"trailer\n<< /Size "
        + str(len(objects) + 1).encode()
        + b" /Root "
        + str(catalog).encode()
        + b" 0 R >>\nstartxref\n"
        + str(xref_offset).encode()
        + b"\n%%EOF\n"
    )
    return bytes(out)


_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""

_PACKAGE_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""

_DOCUMENT_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>
<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Agnes Handbook</w:t></w:r></w:p>
<w:p><w:r><w:t>Revenue is recognised on delivery.</w:t></w:r></w:p>
</w:body></w:document>"""


def _write_docx(path: Path) -> Path:
    """Write a two-paragraph .docx, with or without ``python-docx`` present."""
    try:
        import docx  # type: ignore[import-not-found]
    except ImportError:
        with zipfile.ZipFile(path, "w") as package:
            package.writestr("[Content_Types].xml", _CONTENT_TYPES)
            package.writestr("_rels/.rels", _PACKAGE_RELS)
            package.writestr("word/document.xml", _DOCUMENT_XML)
        return path

    document = docx.Document()
    document.add_heading("Agnes Handbook", level=1)
    document.add_paragraph("Revenue is recognised on delivery.")
    document.save(str(path))
    return path


# --------------------------------------------------------------- passthrough


@pytest.mark.parametrize(
    "name, mime",
    [
        ("notes.md", "text/markdown"),
        ("notes.txt", "text/plain"),
        ("rows.csv", "text/csv"),
        ("blob.json", "application/json"),
        ("conf.yaml", "text/yaml"),
        ("conf.yml", "text/yaml"),
    ],
)
def test_text_suffixes_pass_through_verbatim(tmp_path, name, mime):
    body = "# Heading\n\nline one\nline two\n"
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")

    result = convert_to_markdown(path, mime)

    assert isinstance(result, ConvertResult)
    assert result.engine == "passthrough"
    assert result.markdown == body


def test_passthrough_replaces_undecodable_bytes_instead_of_failing(tmp_path):
    path = tmp_path / "latin.txt"
    path.write_bytes(b"caf\xe9 revenue")

    result = convert_to_markdown(path, "text/plain")

    assert result.engine == "passthrough"
    assert "revenue" in result.markdown
    assert "�" in result.markdown


def test_passthrough_uses_mime_when_the_name_has_no_suffix(tmp_path):
    path = tmp_path / "README"
    path.write_text("plain body", encoding="utf-8")

    assert convert_to_markdown(path, "text/plain; charset=utf-8").engine == "passthrough"


def test_blank_text_file_is_reported_as_empty(tmp_path):
    path = tmp_path / "blank.txt"
    path.write_text("   \n\n\t\n", encoding="utf-8")

    result = convert_to_markdown(path, "text/plain")

    assert result == ConvertResult(markdown="", engine="empty")


# ---------------------------------------------------------------- size guard


def test_max_chars_truncates_with_a_marker(tmp_path):
    path = tmp_path / "big.txt"
    path.write_text("x" * 500, encoding="utf-8")

    result = convert_to_markdown(path, "text/plain", max_chars=100)

    assert result.engine == "passthrough"
    body, _, marker = result.markdown.partition("\n\n[truncated")
    assert body == "x" * 100
    assert "exceeded 100 characters" in marker


def test_output_at_the_cap_is_not_marked_truncated(tmp_path):
    path = tmp_path / "exact.txt"
    path.write_text("y" * 100, encoding="utf-8")

    result = convert_to_markdown(path, "text/plain", max_chars=100)

    assert result.markdown == "y" * 100


def test_max_chars_must_be_positive(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("body", encoding="utf-8")

    with pytest.raises(ValueError):
        convert_to_markdown(path, "text/plain", max_chars=0)


def test_default_max_chars_is_five_million():
    assert DEFAULT_MAX_CHARS == 5_000_000


# ----------------------------------------------------------------------- pdf


def test_pdf_extracts_text_in_reading_order(tmp_path):
    path = tmp_path / "hello.pdf"
    path.write_bytes(_build_pdf([[("Hello Agnes", 72, 700), ("Second line", 72, 660)]]))

    result = convert_to_markdown(path, "application/pdf")

    assert result.engine == "pypdfium2"
    assert result.markdown == "Hello Agnes\n\nSecond line"


def test_pdf_pages_are_separated_by_a_horizontal_rule(tmp_path):
    path = tmp_path / "two.pdf"
    path.write_bytes(
        _build_pdf(
            [
                [("Page one body", 72, 700)],
                [("Page two body", 72, 700)],
            ]
        )
    )

    result = convert_to_markdown(path, "application/pdf")

    assert result.engine == "pypdfium2"
    assert result.markdown == f"Page one body{PAGE_BREAK}Page two body"


def test_scrambled_content_stream_is_sorted_top_to_bottom_left_to_right(tmp_path):
    # Emitted bottom-first, and right-before-left on the top line: PDFium's
    # native order is "the lower line, then the upper line".
    path = tmp_path / "scrambled.pdf"
    path.write_bytes(
        _build_pdf(
            [
                [
                    ("bottom of the page", 72, 600),
                    ("rightmost column", 300, 700),
                    ("leftmost column", 72, 700),
                ]
            ]
        )
    )

    result = convert_to_markdown(path, "application/pdf")

    assert result.engine == "pypdfium2"
    lines = [line for line in result.markdown.splitlines() if line]
    assert lines[0] == "leftmost column rightmost column"
    assert lines[1] == "bottom of the page"


def test_native_reading_order_is_preserved_not_re_sorted(tmp_path):
    path = tmp_path / "ordered.pdf"
    path.write_bytes(
        _build_pdf(
            [
                [
                    ("alpha heading", 72, 700),
                    ("beta paragraph", 72, 660),
                    ("gamma footer", 72, 620),
                ]
            ]
        )
    )

    result = convert_to_markdown(path, "application/pdf")

    assert result.markdown == "alpha heading\n\nbeta paragraph\n\ngamma footer"


def test_pdf_without_a_text_layer_is_empty_not_an_error(tmp_path):
    path = tmp_path / "scan.pdf"
    path.write_bytes(_build_pdf([[], []]))

    result = convert_to_markdown(path, "application/pdf")

    assert result == ConvertResult(markdown="", engine="empty")


def test_pdf_with_a_near_empty_text_layer_is_empty(tmp_path):
    # A scanned page frequently yields a stray page number and nothing else.
    path = tmp_path / "near_empty.pdf"
    path.write_bytes(_build_pdf([[("7", 300, 60)]]))

    assert convert_to_markdown(path, "application/pdf").engine == "empty"


def test_pdf_uses_mime_when_the_name_has_no_suffix(tmp_path):
    path = tmp_path / "downloaded"
    path.write_bytes(_build_pdf([[("Hello Agnes from Graph", 72, 700)]]))

    result = convert_to_markdown(path, "application/pdf")

    assert result.engine == "pypdfium2"
    assert "Hello Agnes" in result.markdown


def test_pdf_output_is_truncated_at_max_chars(tmp_path):
    page = [("word " * 40, 20, 700 - 20 * row) for row in range(20)]
    path = tmp_path / "long.pdf"
    path.write_bytes(_build_pdf([page, page, page]))

    result = convert_to_markdown(path, "application/pdf", max_chars=200)

    assert result.engine == "pypdfium2"
    assert "truncated" in result.markdown


# --------------------------------------------------------------- markitdown


def test_docx_routes_to_markitdown(tmp_path):
    path = _write_docx(tmp_path / "handbook.docx")

    result = convert_to_markdown(
        path,
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )

    assert result.engine == "markitdown"
    assert "Agnes Handbook" in result.markdown
    assert "Revenue is recognised on delivery." in result.markdown


def test_html_routes_to_markitdown(tmp_path):
    path = tmp_path / "page.html"
    path.write_text(
        "<html><body><h1>Quarterly</h1><p>EMEA grew.</p></body></html>",
        encoding="utf-8",
    )

    result = convert_to_markdown(path, "text/html")

    assert result.engine == "markitdown"
    assert "Quarterly" in result.markdown
    assert "EMEA grew." in result.markdown


# ------------------------------------------------------------------ failures


def test_corrupt_pdf_raises_conversion_error_naming_the_file(tmp_path):
    path = tmp_path / "broken.pdf"
    path.write_bytes(b"%PDF-1.4\nthis is not a pdf body at all\n")

    with pytest.raises(ConversionError) as excinfo:
        convert_to_markdown(path, "application/pdf")

    assert excinfo.value.filename == "broken.pdf"
    assert "broken.pdf" in str(excinfo.value)
    assert excinfo.value.engine == "pypdfium2"


def test_corrupt_office_file_raises_conversion_error_naming_the_file(tmp_path):
    # A truncated OOXML package: the zip header is there, the archive is not.
    # markitdown surfaces this as its own FileConversionException wrapping a
    # zipfile.BadZipFile — one of many backend exception types this module
    # deliberately funnels into ConversionError.
    path = tmp_path / "broken.xlsx"
    path.write_bytes(b"PK\x03\x04\x00\x00truncated archive")

    with pytest.raises(ConversionError) as excinfo:
        convert_to_markdown(path, "application/vnd.openxmlformats-officedocument")

    assert excinfo.value.filename == "broken.xlsx"
    assert excinfo.value.engine == "markitdown"


def test_missing_file_raises_conversion_error(tmp_path):
    with pytest.raises(ConversionError) as excinfo:
        convert_to_markdown(tmp_path / "gone.docx", "application/octet-stream")

    assert excinfo.value.filename == "gone.docx"


def test_a_directory_is_not_convertible(tmp_path):
    directory = tmp_path / "folder.pdf"
    directory.mkdir()

    with pytest.raises(ConversionError):
        convert_to_markdown(directory, "application/pdf")


def test_conversion_error_never_kills_a_crawl_loop(tmp_path):
    """The contract the crawler relies on: one bad file, one caught error."""
    good = tmp_path / "good.txt"
    good.write_text("fine", encoding="utf-8")
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"not a pdf")

    statuses = []
    for path in (good, bad, good):
        try:
            statuses.append(convert_to_markdown(path, "application/octet-stream").engine)
        except ConversionError as exc:
            statuses.append(f"error:{exc.filename}")

    assert statuses == ["passthrough", "error:bad.pdf", "passthrough"]


# ------------------------------------------------------- missing dependencies


@pytest.mark.parametrize(
    "module, name, mime",
    [
        ("markitdown", "handbook.docx", "application/octet-stream"),
        ("pypdfium2", "report.pdf", "application/pdf"),
    ],
)
def test_missing_backend_raises_a_typed_error_naming_the_extra(tmp_path, monkeypatch, module, name, mime):
    # ``None`` in sys.modules makes ``from <module> import X`` raise ImportError,
    # which is exactly what an uninstalled extra looks like.
    monkeypatch.setitem(sys.modules, module, None)
    path = tmp_path / name
    path.write_bytes(b"irrelevant, the import fails first")

    with pytest.raises(MissingConversionDependency) as excinfo:
        convert_to_markdown(path, mime)

    assert excinfo.value.package == module
    assert "extraction" in str(excinfo.value)
    assert isinstance(excinfo.value, ConversionError)


def test_module_imports_without_the_extraction_extra(monkeypatch):
    """Import-time cost is zero: the backends are only imported on demand."""
    monkeypatch.setitem(sys.modules, "markitdown", None)
    monkeypatch.setitem(sys.modules, "pypdfium2", None)
    monkeypatch.delitem(sys.modules, "connectors.sharepoint.convert", raising=False)

    import importlib

    module = importlib.import_module("connectors.sharepoint.convert")

    assert module.convert_to_markdown is not None


# --------------------------------------------------------- licence invariant


#: Modules this converter may never import, whatever the quality argument for
#: them: Agnes ships under PolyForm Small Business 1.0.0 and cannot vendor
#: AGPL-3.0 code (spec §9.1). Matched on the imported module, not on the file
#: text, so the docstring can keep explaining *why* they are banned.
_AGPL_MODULES = {"fitz", "pymupdf", "pymupdf4llm", "frontend"}


def test_no_agpl_dependency_is_imported_by_the_converter():
    import ast

    source = Path(__file__).with_name("convert.py").read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert not imported & _AGPL_MODULES, f"AGPL dependency imported: {sorted(imported & _AGPL_MODULES)}"
    assert {"markitdown", "pypdfium2"} <= imported


def test_structure_pass_failure_degrades_to_plain_extraction(tmp_path, monkeypatch):
    """The structure-first PDF route must never be load-bearing: when
    ``pdf_structure.reconstruct_pdf`` raises, ``_convert_pdf`` falls back to
    the plain reading-order loop (single-newline joins, no block spacing)."""
    from connectors.sharepoint import pdf_structure

    def _boom(path, max_pages=None):
        raise RuntimeError("structure pass broken on purpose")

    monkeypatch.setattr(pdf_structure, "reconstruct_pdf", _boom)

    path = tmp_path / "fallback.pdf"
    path.write_bytes(_build_pdf([[("Hello Agnes", 72, 700), ("Second line", 72, 660)]]))

    result = convert_to_markdown(path, "application/pdf")

    assert result.engine == "pypdfium2"
    assert result.markdown == "Hello Agnes\nSecond line"
