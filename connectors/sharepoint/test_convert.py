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
    UnsupportedConversionFormat,
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


# ------------------------------------------------------- legacy office (LibreOffice)


#: Legacy suffix → the OOXML target format LibreOffice must produce, per the
#: mapping in :mod:`connectors.sharepoint.convert`.
_LEGACY_OFFICE_CASES = [
    (".doc", "docx"),
    (".rtf", "docx"),
    (".odt", "docx"),
    (".ppt", "pptx"),
    (".odp", "pptx"),
    (".xls", "xlsx"),
    (".ods", "xlsx"),
]


def _stub_soffice(monkeypatch, convert_module, *, returncode=0, produce_output=True, side_effect=None):
    """Replace ``soffice`` with a fake that never shells out for real.

    Records every invocation's argv and the temp ``--outdir`` it was given
    (so a test can assert the dir is gone afterwards), and — unless told
    otherwise — drops a placeholder output file at the path LibreOffice
    itself would have written, so the caller's glob for the converted file
    succeeds without a real LibreOffice on the machine.
    """
    calls: list[dict] = []

    def _which(cmd):
        return "/usr/bin/soffice" if cmd == "soffice" else None

    def _run(argv, **kwargs):
        if side_effect is not None:
            raise side_effect
        outdir = Path(argv[argv.index("--outdir") + 1])
        target_format = argv[argv.index("--convert-to") + 1]
        source = Path(argv[-1])
        calls.append({"argv": argv, "outdir": outdir, "kwargs": kwargs})
        if produce_output:
            (outdir / f"{source.stem}.{target_format}").write_bytes(b"fake converted bytes")
        import subprocess

        return subprocess.CompletedProcess(argv, returncode, stdout=b"", stderr=b"")

    monkeypatch.setattr(convert_module.shutil, "which", _which)
    monkeypatch.setattr(convert_module.subprocess, "run", _run)
    return calls


@pytest.mark.parametrize("suffix, target_format", _LEGACY_OFFICE_CASES)
def test_legacy_office_suffix_is_preconverted_then_handed_to_markitdown(tmp_path, monkeypatch, suffix, target_format):
    import connectors.sharepoint.convert as convert_module

    path = tmp_path / f"legacy{suffix}"
    path.write_bytes(b"legacy office bytes")
    calls = _stub_soffice(monkeypatch, convert_module)

    markitdown_calls = []

    def _fake_markitdown(converted_path, filename):
        markitdown_calls.append((converted_path, filename))
        return "converted text from libreoffice output"

    monkeypatch.setattr(convert_module, "_convert_markitdown", _fake_markitdown)

    result = convert_to_markdown(path, "application/octet-stream")

    assert result.engine == "libreoffice+markitdown"
    assert result.markdown == "converted text from libreoffice output"

    assert len(calls) == 1
    argv = calls[0]["argv"]
    assert argv[0] == "soffice"
    assert "--headless" in argv
    assert "--norestore" in argv
    assert argv[argv.index("--convert-to") + 1] == target_format
    assert argv[-1] == str(path)

    # markitdown ran against the LibreOffice OUTPUT, not the original file,
    # and the reported filename is still the original (source) name.
    assert len(markitdown_calls) == 1
    converted_path, filename = markitdown_calls[0]
    assert converted_path.suffix == f".{target_format}"
    assert filename == path.name

    # the temp outdir is always cleaned up
    assert not calls[0]["outdir"].exists()


def test_legacy_office_temp_dir_is_removed_even_on_failure(tmp_path, monkeypatch):
    import connectors.sharepoint.convert as convert_module

    path = tmp_path / "legacy.doc"
    path.write_bytes(b"legacy office bytes")
    calls = _stub_soffice(monkeypatch, convert_module, returncode=1)

    with pytest.raises(ConversionError):
        convert_to_markdown(path, "application/msword")

    assert len(calls) == 1
    assert not calls[0]["outdir"].exists()


def test_missing_soffice_raises_missing_conversion_dependency(tmp_path, monkeypatch):
    import connectors.sharepoint.convert as convert_module

    path = tmp_path / "legacy.doc"
    path.write_bytes(b"legacy office bytes")
    monkeypatch.setattr(convert_module.shutil, "which", lambda cmd: None)

    with pytest.raises(MissingConversionDependency) as excinfo:
        convert_to_markdown(path, "application/msword")

    assert excinfo.value.filename == "legacy.doc"
    assert excinfo.value.package == "libreoffice"
    assert excinfo.value.engine == "libreoffice+markitdown"
    assert isinstance(excinfo.value, ConversionError)


def test_libreoffice_non_zero_exit_raises_conversion_error(tmp_path, monkeypatch):
    import connectors.sharepoint.convert as convert_module

    path = tmp_path / "legacy.doc"
    path.write_bytes(b"legacy office bytes")
    _stub_soffice(monkeypatch, convert_module, returncode=1, produce_output=False)

    with pytest.raises(ConversionError) as excinfo:
        convert_to_markdown(path, "application/msword")

    assert excinfo.value.filename == "legacy.doc"
    assert excinfo.value.engine == "libreoffice+markitdown"
    assert not isinstance(excinfo.value, MissingConversionDependency)


def test_libreoffice_timeout_raises_conversion_error(tmp_path, monkeypatch):
    import connectors.sharepoint.convert as convert_module
    import subprocess

    path = tmp_path / "legacy.ppt"
    path.write_bytes(b"legacy office bytes")
    _stub_soffice(
        monkeypatch,
        convert_module,
        side_effect=subprocess.TimeoutExpired(cmd="soffice", timeout=120),
    )

    with pytest.raises(ConversionError) as excinfo:
        convert_to_markdown(path, "application/vnd.ms-powerpoint")

    assert excinfo.value.filename == "legacy.ppt"
    assert excinfo.value.engine == "libreoffice+markitdown"
    assert "timed out" in str(excinfo.value)


def test_libreoffice_success_with_no_output_file_raises_conversion_error(tmp_path, monkeypatch):
    import connectors.sharepoint.convert as convert_module

    path = tmp_path / "legacy.xls"
    path.write_bytes(b"legacy office bytes")
    _stub_soffice(monkeypatch, convert_module, returncode=0, produce_output=False)

    with pytest.raises(ConversionError) as excinfo:
        convert_to_markdown(path, "application/vnd.ms-excel")

    assert excinfo.value.filename == "legacy.xls"
    assert excinfo.value.engine == "libreoffice+markitdown"


def test_legacy_office_suffixes_are_exported_and_exact(monkeypatch):
    import connectors.sharepoint.convert as convert_module

    assert convert_module.LEGACY_OFFICE_SUFFIXES == frozenset({".doc", ".rtf", ".odt", ".ppt", ".odp", ".xls", ".ods"})
    # never overlaps with the routes that already have their own engine
    assert not convert_module.LEGACY_OFFICE_SUFFIXES & convert_module.PASSTHROUGH_SUFFIXES
    assert ".pdf" not in convert_module.LEGACY_OFFICE_SUFFIXES


def test_other_suffixes_are_untouched_by_the_legacy_office_route(tmp_path):
    """.docx (the modern, already-supported sibling) must keep going straight
    to markitdown — the legacy pre-conversion step is additive, never a
    detour for a format markitdown already reads natively."""
    path = _write_docx(tmp_path / "modern.docx")

    result = convert_to_markdown(
        path,
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )

    assert result.engine == "markitdown"


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


@pytest.mark.parametrize("suffix", [".pbix", ".one"])
def test_a_format_with_no_registered_backend_raises_unsupported_not_a_plain_conversion_error(tmp_path, suffix):
    # Power BI (.pbix) and OneNote (.one): no markitdown converter's
    # `accepts()` matches these at all (verified empirically — see
    # `UnsupportedConversionFormat`'s docstring), so markitdown raises its
    # own `UnsupportedFormatException` before any backend is even tried.
    # Random bytes, not a crafted file: the point is that NOTHING attempts
    # this format, regardless of content.
    path = tmp_path / f"deck{suffix}"
    path.write_bytes(bytes((i * 37) % 256 for i in range(2048)))

    with pytest.raises(UnsupportedConversionFormat) as excinfo:
        convert_to_markdown(path, "application/octet-stream")

    assert isinstance(excinfo.value, ConversionError)
    assert excinfo.value.filename == f"deck{suffix}"
    assert excinfo.value.engine == "markitdown"


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


class _RaisingMetaPathFinder:
    """A ``sys.meta_path`` entry that raises ``exc`` instead of resolving
    ``module_name`` — simulates a lazy import failing with an arbitrary
    exception type (not just ``ImportError``), for both an ``import x`` and
    a ``from x import y`` statement, since it intercepts before ``x`` is
    even bound into ``sys.modules``."""

    def __init__(self, module_name: str, exc: BaseException) -> None:
        self._module_name = module_name
        self._exc = exc

    def find_spec(self, fullname, path, target=None):  # noqa: ANN001, ANN201
        if fullname == self._module_name:
            raise self._exc
        return None


@pytest.mark.parametrize(
    "module, name, mime",
    [
        ("markitdown", "handbook.docx", "application/octet-stream"),
        ("pypdfium2", "report.pdf", "application/pdf"),
    ],
)
def test_missing_backend_import_failure_carries_the_causes_type_and_text(tmp_path, monkeypatch, module, name, mime):
    """A live finding: a MemoryError surfacing from deep inside a lazy
    ``import markitdown``/``import pypdfium2`` (a conversion child's own
    numpy/OpenBLAS init failing under its RLIMIT_AS) must not read as a
    plain "not installed" — that cost 30 minutes to diagnose. The message
    must name the real exception type and carry its text."""
    monkeypatch.delitem(sys.modules, module, raising=False)
    finder = _RaisingMetaPathFinder(
        module, MemoryError("OpenBLAS error: Memory allocation still failed after 10 retries.")
    )
    monkeypatch.setattr(sys, "meta_path", [finder, *sys.meta_path])
    path = tmp_path / name
    path.write_bytes(b"irrelevant, the import fails first")

    with pytest.raises(MissingConversionDependency) as excinfo:
        convert_to_markdown(path, mime)

    assert excinfo.value.package == module
    message = str(excinfo.value)
    assert "MemoryError" in message
    assert "OpenBLAS error: Memory allocation still failed after 10 retries." in message
    assert "conversion child's memory limit" in message
    assert isinstance(excinfo.value, ConversionError)


def test_missing_backend_import_failure_message_truncates_a_long_cause(tmp_path, monkeypatch):
    """The cause's text is capped at 200 chars so one pathological exception
    message can't blow up the crawl's error log/report."""
    monkeypatch.delitem(sys.modules, "markitdown", raising=False)
    long_text = "x" * 500
    finder = _RaisingMetaPathFinder("markitdown", RuntimeError(long_text))
    monkeypatch.setattr(sys, "meta_path", [finder, *sys.meta_path])
    path = tmp_path / "handbook.docx"
    path.write_bytes(b"irrelevant, the import fails first")

    with pytest.raises(MissingConversionDependency) as excinfo:
        convert_to_markdown(path, "application/octet-stream")

    message = str(excinfo.value)
    assert ("x" * 200) in message
    assert ("x" * 201) not in message


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


def test_structure_pass_failure_raises_conversion_error(tmp_path, monkeypatch):
    """``pdf_structure.reconstruct_pdf`` is the ONLY PDF route (owner
    decision 2026-08-31 — one pipeline, no dual modes). It already degrades
    per page inside itself, so an exception escaping it means the FILE is not
    convertible: that surfaces as a ``ConversionError`` naming the file, which
    the crawler counts in ``convert_failed`` and walks past — never a silent
    second attempt through a parallel plain-text implementation."""
    from connectors.sharepoint import pdf_structure

    def _boom(path, max_pages=None):
        raise RuntimeError("structure pass broken on purpose")

    monkeypatch.setattr(pdf_structure, "reconstruct_pdf", _boom)

    path = tmp_path / "fallback.pdf"
    path.write_bytes(_build_pdf([[("Hello Agnes", 72, 700), ("Second line", 72, 660)]]))

    with pytest.raises(ConversionError) as excinfo:
        convert_to_markdown(path, "application/pdf")

    assert excinfo.value.filename == "fallback.pdf"
    assert excinfo.value.engine == "pypdfium2"


def test_legacy_office_runs_on_a_per_process_profile_and_serializes(tmp_path, monkeypatch):
    """Two headless LibreOffice instances on the SAME user profile do not
    coexist — the second exits 1 (measured live: 3 of 6 concurrent
    conversions failed). Every soffice call must therefore carry a
    ``-env:UserInstallation=`` pointing at a directory private to this
    process, reused across calls (a fresh profile costs seconds on first
    start), and calls within one process are serialized by a lock."""
    import os

    import connectors.sharepoint.convert as convert_module

    monkeypatch.setattr(convert_module, "_LIBREOFFICE_PROFILES", {})
    monkeypatch.setattr(convert_module, "_convert_markitdown", lambda p, f: "text")
    calls = _stub_soffice(monkeypatch, convert_module)

    for name in ("a.doc", "b.xls"):
        path = tmp_path / name
        path.write_bytes(b"legacy")
        convert_to_markdown(path, "application/octet-stream")

    profiles = []
    for call in calls:
        env = [a for a in call["argv"] if a.startswith("-env:UserInstallation=file://")]
        assert len(env) == 1, call["argv"]
        profiles.append(env[0].split("file://", 1)[1])
    # same private profile for both calls of this process, and it exists
    assert profiles[0] == profiles[1]
    assert os.path.isdir(profiles[0])
    assert str(os.getpid()) in profiles[0]
    # the profile the calls used is exactly the one the helper hands out for
    # this pid (module identity resolved through the function that ran, so a
    # second import path of the same file cannot fool the check)
    import sys

    live = sys.modules[convert_to_markdown.__module__]
    assert profiles[0] == live._libreoffice_profile_dir()
    assert os.getpid() in live._LIBREOFFICE_PROFILES
    assert live._LIBREOFFICE_LOCK is not None
