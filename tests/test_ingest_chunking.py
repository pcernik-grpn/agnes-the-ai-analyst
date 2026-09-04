"""Tests for src.ingest.chunking."""

from __future__ import annotations

from src.ingest.chunking import Chunk, chunk_text
from src.ingest.text_extract import ExtractResult


def test_short_text_single_chunk():
    chunks = chunk_text("a short doc")
    assert len(chunks) == 1
    assert chunks[0].ordinal == 0
    assert chunks[0].text == "a short doc"


def test_empty_text_no_chunks():
    assert chunk_text("") == []
    assert chunk_text("   ") == []


def test_long_text_multiple_ordered_chunks():
    text = "x" * 10000
    chunks = chunk_text(text, target_chars=3200, overlap_chars=400)
    assert len(chunks) > 1
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))
    assert all(len(c.text) <= 3200 for c in chunks)


def test_elements_respect_boundaries():
    res = ExtractResult(
        full_text="ignored",
        elements=[("Intro", "first section text"), ("Body", "second section text")],
    )
    chunks = chunk_text(res)
    assert len(chunks) == 2
    assert chunks[0].section_path == "Intro"
    assert chunks[1].section_path == "Body"
    assert chunks[0].text == "first section text"


def test_large_element_is_windowed():
    res = ExtractResult(full_text="", elements=[("Big", "y" * 8000)])
    chunks = chunk_text(res, target_chars=3200, overlap_chars=400)
    assert len(chunks) > 1
    assert all(c.section_path == "Big" for c in chunks)
    assert isinstance(chunks[0], Chunk)


def test_nul_byte_mid_word_is_stripped_from_a_plain_string_source():
    """PostgreSQL `text` columns reject NUL outright — see
    `src.ingest.chunking._sanitize_control_chars`'s docstring for the live
    finding (261 rejected documents) this closes."""
    chunks = chunk_text("in.c_keboola_ex_db_or\x00acle_ap_suppliers")
    assert len(chunks) == 1
    assert "\x00" not in chunks[0].text
    assert chunks[0].text == "in.c_keboola_ex_db_oracle_ap_suppliers"


def test_other_c0_control_chars_are_stripped_but_tab_newline_cr_survive():
    # Trailing whitespace-like characters are stripped by `chunk_text`
    # itself (unrelated to sanitization), so `\r` sits mid-string here to
    # isolate what this test is actually asserting.
    text = "before\x01\x02\x1fafter\tmid\rline\nend"
    chunks = chunk_text(text)
    assert chunks[0].text == "beforeafter\tmid\rline\nend"


def test_nul_bytes_are_stripped_from_extract_result_full_text_and_elements():
    res = ExtractResult(
        full_text="ignored\x00",
        elements=[("Sec\x00tion", "first \x00section text"), ("Body", "second section text")],
    )
    chunks = chunk_text(res)
    assert chunks[0].section_path == "Section"
    assert chunks[0].text == "first section text"
    assert "\x00" not in chunks[1].text
