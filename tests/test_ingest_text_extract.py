"""Tests for src.ingest.text_extract — dependency-free fallback paths."""

from __future__ import annotations

import pytest

from src.ingest.text_extract import ExtractResult, UnsupportedDocument, extract_text


def _write(tmp_path, name: str, content: str) -> str:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return str(p)


def test_extract_plain_txt(tmp_path):
    path = _write(tmp_path, "a.txt", "hello world\nsecond line")
    res = extract_text(path, "txt")
    assert isinstance(res, ExtractResult)
    assert "hello world" in res.full_text


def test_extract_markdown(tmp_path):
    path = _write(tmp_path, "a.md", "# Title\n\nbody text here")
    res = extract_text(path, "md")
    assert "body text here" in res.full_text


def test_extract_html_strips_tags(tmp_path):
    path = _write(
        tmp_path,
        "a.html",
        "<html><head><style>.x{color:red}</style></head>"
        "<body><p>visible text</p><script>var x=1;</script></body></html>",
    )
    res = extract_text(path, "html")
    assert "visible text" in res.full_text
    assert "color:red" not in res.full_text
    assert "var x" not in res.full_text


def test_extract_no_extension_reads_as_text(tmp_path):
    path = _write(tmp_path, "README", "plain content")
    res = extract_text(path, None)
    assert "plain content" in res.full_text


def test_unextractable_type_raises(tmp_path):
    path = _write(tmp_path, "model.dwg", "binary-ish")
    with pytest.raises(UnsupportedDocument):
        extract_text(path, "dwg")


def test_pdf_without_reader_raises_unsupported(tmp_path, monkeypatch):
    # Force both docling and pypdf unavailable → PDF is unsupported, not a crash.
    import src.ingest.text_extract as te

    monkeypatch.setattr(te, "_try_docling", lambda path: None)
    monkeypatch.setattr(te, "_try_pdf", lambda path: None)
    path = _write(tmp_path, "doc.pdf", "%PDF-1.4 ...")
    with pytest.raises(UnsupportedDocument):
        extract_text(path, "pdf")


def test_pypdf_is_a_core_dependency(tmp_path, caplog):
    """Default installs must extract PDF text without the docling extra.

    Guards the dependency, not pypdf itself: a minimal one-page PDF with a
    text content stream must round-trip through extract_text.

    The xref table's byte offsets are *computed*, not hand-counted: each
    object's start offset is the running length of the buffer at the moment
    it's appended, and the trailer's startxref is the buffer length once all
    objects are written. A hand-written xref is easy to get subtly wrong
    (this fixture's previous version was) — pypdf's malformed-PDF recovery
    silently masks that, so the test would still pass even on a broken xref.
    Asserting no WARNING-level pypdf log record fires (pypdf reports
    recovered/non-compliant structure via ``logging``, not the ``warnings``
    module) additionally confirms the PDF parses cleanly, not just
    recoverably.
    """
    import pypdf  # noqa: F401  — core dep, not an extra

    stream_content = b"BT /F1 12 Tf 100 700 Td (Hello Agnes) Tj ET\n"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>\n",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>\n",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>\n",
        f"<< /Length {len(stream_content)} >>\nstream\n".encode("ascii") + stream_content + b"endstream\n",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\n",
    ]

    buf = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(buf))
        buf += f"{i} 0 obj\n".encode("ascii")
        buf += body
        buf += b"endobj\n"

    startxref = len(buf)
    buf += b"xref\n"
    buf += f"0 {len(objects) + 1}\n".encode("ascii")
    buf += b"0000000000 65535 f \n"
    for off in offsets:
        buf += f"{off:010d} 00000 n \n".encode("ascii")
    buf += b"trailer\n"
    buf += f"<< /Size {len(objects) + 1} /Root 1 0 R >>\n".encode("ascii")
    buf += b"startxref\n"
    buf += f"{startxref}\n".encode("ascii")
    buf += b"%%EOF\n"

    path = tmp_path / "hello.pdf"
    path.write_bytes(bytes(buf))

    with caplog.at_level("WARNING", logger="pypdf"):
        result = extract_text(str(path), "pdf")
    assert isinstance(result, ExtractResult)
    assert "Hello Agnes" in result.full_text
    # Scoped to pypdf's own records on purpose. `caplog.at_level(level,
    # logger=...)` sets the LEVEL for that logger but `caplog.records` still
    # collects from every logger, so the bare `caplog.records == []` this
    # replaces failed on warnings from unrelated libraries: in the `rich-extras`
    # job, where docling is installed, `extract_text` reaches huggingface_hub
    # and it logs "You are sending unauthenticated requests to the HF Hub"
    # (#1553). That says nothing about whether pypdf parsed the fixture.
    pypdf_warnings = [r for r in caplog.records if r.name.split(".")[0] == "pypdf"]
    assert pypdf_warnings == [], "pypdf should parse this fixture cleanly, not via malformed-PDF recovery"
