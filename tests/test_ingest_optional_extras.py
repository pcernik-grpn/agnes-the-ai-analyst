"""What the optional extras change, and what the absence of them must say.

Two capabilities ship as opt-in extras because both pull torch:
``[docling]`` (office-document parsing) and ``[embeddings]`` (hybrid
retrieval). A deployment built without them still ACCEPTS docx/pptx uploads
— they are on the upload allowlist — and then cannot index them, and ranks
search results lexically only. Neither degradation is wrong; both are
invisible, which is the problem this suite pins:

* the rejection names the missing extra, so "why was my file rejected"
  has an answer that is not "your file is broken"
* the capability probes agree with what is actually installed

The extras-present direction runs only where the extras ARE installed
(``-rich`` image, and the ``rich-extras`` CI job) — skipped elsewhere rather
than mocked, because a mocked docling proves nothing about whether a real
docx parses.
"""

from __future__ import annotations

import zipfile

import pytest

from src.ingest.text_extract import UnsupportedDocument, docling_capability, extract_text

_HAS_DOCLING = docling_capability()


def _minimal_docx(tmp_path, name="doc.docx") -> str:
    """A real (if minimal) OOXML docx — a zip with the parts a parser needs."""
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.'
            'wordprocessingml.document.main+xml"/></Types>',
        )
        z.writestr(
            "_rels/.rels",
            '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
            'officeDocument" Target="word/document.xml"/></Relationships>',
        )
        z.writestr(
            "word/document.xml",
            '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body><w:p><w:r><w:t>Quarterly staffing plan for Northwind</w:t></w:r></w:p></w:body></w:document>",
        )
    return str(path)


# ---------------------------------------------------------------------------
# Without the extra: the degradation has to say what it is
# ---------------------------------------------------------------------------


@pytest.mark.skipif(_HAS_DOCLING, reason="docling IS installed — this pins the degraded deployment")
@pytest.mark.parametrize("ext", ["docx", "pptx"])
def test_docling_only_formats_name_the_missing_extra(tmp_path, ext):
    """These formats are on the upload allowlist (``TIER1_EXTENSIONS``) and
    have no lightweight fallback, so on an image without the extra a user
    uploads successfully and the file is rejected afterwards. A bare "no text
    extractor for '.docx'" reads as "your file is broken" and invites the one
    action that cannot help — uploading it again."""
    path = tmp_path / f"a.{ext}"
    path.write_bytes(b"PK\x03\x04not-really-an-office-file")

    with pytest.raises(UnsupportedDocument) as exc:
        extract_text(str(path), ext)

    msg = str(exc.value)
    assert "docling" in msg, "the rejection must name the extra that is missing"
    assert "built without" in msg, "…and say it is the deployment's shape, not the file's"
    assert "-rich" in msg and "DEPLOYMENT.md" in msg, "…and point an operator at the fix"


def test_allowlisted_formats_without_a_reader_are_exactly_the_ones_we_name():
    """Guard against drift between the upload allowlist and the extraction
    story: any tier-1 extension that neither the allowlist-independent
    fallbacks nor docling-only set covers would reject with the old generic
    message, i.e. an accepted upload with an unexplained failure."""
    from src.corpus_allowlist import TIER1_EXTENSIONS
    from src.ingest.runner import TABULAR_EXTS
    from src.ingest.text_extract import (
        _DOCLING_ONLY_EXTS,
        _HTML_EXTS,
        _PLAIN_EXTS,
        _UNREADABLE_TIER1,
    )

    covered = (
        set(_PLAIN_EXTS)
        | set(_HTML_EXTS)
        | set(TABULAR_EXTS)
        | set(_DOCLING_ONLY_EXTS)
        | set(_UNREADABLE_TIER1)
        | {"pdf", "eml"}
    )
    unexplained = set(TIER1_EXTENSIONS) - covered
    assert unexplained == set(), (
        f"tier-1 uploads with no reader and no named reason: {sorted(unexplained)} — either add a "
        "fallback, add them to _DOCLING_ONLY_EXTS so the rejection names the missing extra, or "
        "record them in _UNREADABLE_TIER1 (with the reason) if nothing can read them"
    )


def test_eml_is_parsed_by_the_stdlib_on_every_build(tmp_path):
    """``.eml`` is allowlisted and used to be rejected on every build — no
    reader existed, with or without extras. Email threads are ordinary
    corpus material, so this one is parsed with the stdlib rather than
    behind an extra."""
    path = tmp_path / "thread.eml"
    path.write_text(
        "From: partner@example.com\n"
        "To: team@example.com\n"
        "Subject: Northwind staffing\n"
        "Date: Mon, 4 Aug 2026 09:12:00 +0200\n"
        "\n"
        "Confirming two analysts for the September phase.\n",
        encoding="utf-8",
    )
    res = extract_text(str(path), "eml")
    assert "Northwind staffing" in res.full_text, "the subject must be searchable"
    assert "partner@example.com" in res.full_text, "so must the correspondents"
    assert "two analysts" in res.full_text, "and the body"


def test_unreadable_allowlisted_formats_still_reject_clearly(tmp_path):
    """A format nothing can read must not claim an extra would fix it."""
    from src.ingest.text_extract import _UNREADABLE_TIER1

    for ext in sorted(_UNREADABLE_TIER1):
        path = tmp_path / f"a.{ext}"
        path.write_bytes(b"\x00binary")
        with pytest.raises(UnsupportedDocument) as exc:
            extract_text(str(path), ext)
        assert "docling" not in str(exc.value), (
            f"'.{ext}' cannot be read even with the extra — the message must not send an "
            "operator to rebuild the image for nothing"
        )


def test_capability_probe_does_not_import_the_heavy_dependency(monkeypatch):
    """``docling_capability`` is asked while composing an error message and
    while answering readiness — it must stay an import-spec probe, never an
    import, or a rejection path would pay torch's load cost."""
    import sys

    before = set(sys.modules)
    docling_capability()
    newly_imported = {m for m in set(sys.modules) - before if m.startswith("docling")}
    assert newly_imported == set(), f"capability probe imported {newly_imported}"


# ---------------------------------------------------------------------------
# With the extras: the capability is real
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_DOCLING, reason="needs the [docling] extra (rich image / rich-extras CI job)")
def test_docx_ingests_when_the_extra_is_present(tmp_path):
    """The whole point of the rich image: an office document yields text
    rather than a rejection."""
    res = extract_text(_minimal_docx(tmp_path), "docx")
    assert res.full_text.strip(), "docling returned no text for a valid docx"
    assert "Northwind" in res.full_text


def test_retrieval_mode_matches_the_installed_extra():
    """``retrieval_mode`` is the label a client reads to tell hybrid results
    from degraded ones, so it must track what is installed rather than a
    config value someone can set optimistically."""
    from src.ingest.embeddings import embedding_capability
    from src.ingest.retrieval import retrieval_mode

    expected = "hybrid" if embedding_capability() else "lexical_only"
    assert retrieval_mode() == expected
