"""Tests for src/corpus_allowlist.py — extension→tier classification.

TDD-first: written before the implementation.
"""

from __future__ import annotations


from src.corpus_allowlist import MAX_UPLOAD_BYTES, TIER1_EXTENSIONS, TIER2_EXTENSIONS, classify


class TestTier1Extensions:
    def test_pdf_is_tier1(self):
        assert classify("report.pdf") == "tier1"

    def test_txt_is_tier1(self):
        assert classify("notes.txt") == "tier1"

    def test_md_is_tier1(self):
        assert classify("README.md") == "tier1"

    def test_csv_is_tier1(self):
        assert classify("data.csv") == "tier1"

    def test_json_is_tier1(self):
        assert classify("config.json") == "tier1"

    def test_jsonl_is_tier1(self):
        assert classify("events.jsonl") == "tier1"

    def test_xlsx_is_tier1(self):
        assert classify("spreadsheet.xlsx") == "tier1"

    def test_parquet_is_tier1(self):
        assert classify("table.parquet") == "tier1"

    def test_docx_is_tier1(self):
        assert classify("document.docx") == "tier1"

    def test_pptx_is_tier1(self):
        assert classify("slides.pptx") == "tier1"

    def test_html_is_tier1(self):
        assert classify("page.html") == "tier1"

    def test_epub_is_tier1(self):
        assert classify("book.epub") == "tier1"

    def test_eml_is_tier1(self):
        assert classify("email.eml") == "tier1"

    def test_rtf_is_tier1(self):
        assert classify("doc.rtf") == "tier1"

    def test_tsv_is_tier1(self):
        assert classify("data.tsv") == "tier1"

    def test_msg_is_not_allowlisted(self):
        """Outlook's ``.msg`` is deliberately absent — no reader exists for it
        on any build, and the only one available (``extract-msg``) resolves to
        15 transitive packages, among them a Tkinter GUI toolkit and two
        VBA/malware-analysis tools. Accepting a format we cannot index buys the
        uploader nothing but a delayed rejection, so it is refused in the upload
        response instead. Same reasoning as TIFF's absence from tier 2.
        Exporting the mail as ``.eml`` works on every build."""
        assert classify("email.msg") is None


class TestTier2Extensions:
    def test_png_is_tier2(self):
        assert classify("image.png") == "tier2"

    def test_jpg_is_tier2(self):
        assert classify("photo.jpg") == "tier2"

    def test_jpeg_is_tier2(self):
        assert classify("photo.jpeg") == "tier2"

    def test_gif_is_tier2(self):
        assert classify("anim.gif") == "tier2"

    def test_webp_is_tier2(self):
        assert classify("image.webp") == "tier2"

    def test_tiff_is_rejected(self):
        # TIFF is not accepted by the vision API, so it must be rejected up
        # front rather than accepted as tier2 and later marked rejected by the
        # background ingest task (the tier2 contract is "stored now, processed
        # later" — only honour-able for vision-supported formats).
        assert classify("scan.tif") is None
        assert classify("scan.tiff") is None


class TestCaseInsensitive:
    def test_uppercase_pdf(self):
        assert classify("REPORT.PDF") == "tier1"

    def test_uppercase_png(self):
        assert classify("IMAGE.PNG") == "tier2"

    def test_mixed_case_docx(self):
        assert classify("File.Docx") == "tier1"

    def test_mixed_case_jpeg(self):
        assert classify("Photo.JPEG") == "tier2"


class TestUnsupportedExtensions:
    def test_dwg_is_none(self):
        assert classify("drawing.dwg") is None

    def test_exe_is_none(self):
        assert classify("setup.exe") is None

    def test_tarball_is_none(self):
        # zip moved to the bundle tier (K1); other archive formats stay rejected.
        assert classify("archive.tar") is None

    def test_mp4_is_none(self):
        assert classify("video.mp4") is None

    def test_no_extension_is_none(self):
        assert classify("filename_without_extension") is None

    def test_empty_string_is_none(self):
        assert classify("") is None

    def test_dotfile_only_is_none(self):
        # ".gitignore" has no real extension — the part after the last dot
        # would be "gitignore"; that's not in any tier.
        assert classify(".gitignore") is None


class TestBundleTier:
    def test_zip_is_bundle_tier(self):
        assert classify("dump.zip") == "bundle"
        assert classify("SPACE-export.ZIP") == "bundle"

    def test_other_archives_still_rejected(self):
        assert classify("a.tar.gz") is None
        assert classify("a.7z") is None
        assert classify("a.rar") is None

    def test_bundle_disjoint_from_other_tiers(self):
        from src.corpus_allowlist import BUNDLE_EXTENSIONS

        assert BUNDLE_EXTENSIONS.isdisjoint(TIER1_EXTENSIONS)
        assert BUNDLE_EXTENSIONS.isdisjoint(TIER2_EXTENSIONS)


class TestConstants:
    def test_max_upload_bytes_positive(self):
        assert MAX_UPLOAD_BYTES > 0

    def test_max_upload_bytes_at_least_10mb(self):
        # Must be generous enough for realistic document uploads (≥ 10 MiB).
        assert MAX_UPLOAD_BYTES >= 10 * 1024 * 1024

    def test_tier1_extensions_is_set(self):
        assert isinstance(TIER1_EXTENSIONS, (set, frozenset))
        assert "pdf" in TIER1_EXTENSIONS

    def test_tier2_extensions_is_set(self):
        assert isinstance(TIER2_EXTENSIONS, (set, frozenset))
        assert "png" in TIER2_EXTENSIONS

    def test_tiers_do_not_overlap(self):
        assert TIER1_EXTENSIONS.isdisjoint(TIER2_EXTENSIONS)
