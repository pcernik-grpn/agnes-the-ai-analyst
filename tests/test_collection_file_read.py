"""Reading one file's text — the surface an agent was missing.

`GET /api/collections/{cid}/files/{fid}/preview` has always returned the
extracted text (with a `truncated` flag, and a human `reason` when there is
nothing to show), and the web UI's preview modal uses it. Neither MCP nor
the CLI had any way to reach it, so an agent asked "what is in this file?"
could only guess words and search for them — observed live: six failed
`collections_search` calls, an invented `agnes collections cat`, and a wrong
"I don't have access to your files or collections" conclusion.

This adds the missing surfaces on top of the existing endpoint:
`collection_file_read` on BOTH MCP servers and `agnes collections cat`.
Registering it in only one MCP server is the trap this repo keeps
re-learning (#1236) — the stdio server is the one the in-chat agent talks
to, so the parity assertions below are the point, not a formality.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from cli.commands.collections import collections_app

ROOT = Path(__file__).resolve().parents[1]
HTTP_TOOLS = ROOT / "app" / "api" / "mcp" / "foundation_tools.py"
STDIO_TOOLS = ROOT / "cli" / "mcp" / "server.py"

TOOL = "collection_file_read"
runner = CliRunner()

# Rich styles each hyphen of a flag separately under a colour-forcing CI
# terminal, so the literal ``--offset`` never appears in the raw bytes of a
# help page or a usage error. Assert on what a reader sees.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _collection_with_file(seeded_app, token: str, body: bytes, name: str = "note.md") -> tuple:
    c = seeded_app["client"].post("/api/collections", json={"name": "Readable"}, headers=_auth(token))
    assert c.status_code == 201, c.text
    col = c.json()
    up = seeded_app["client"].post(
        f"/api/collections/{col['id']}/files",
        files={"files": (name, body, "text/markdown")},
        headers=_auth(token),
    )
    assert up.status_code == 201, up.text
    return col["id"], up.json()[0]["file_id"]


class TestBothMcpServersExposeIt:
    """The lesson from #1236: one server is not both servers."""

    def test_tool_is_in_the_foundation_set(self):
        from app.api.mcp.foundation_tools import FOUNDATION_TOOL_NAMES

        assert TOOL in FOUNDATION_TOOL_NAMES

    @pytest.mark.parametrize(("source", "which"), [(HTTP_TOOLS, "HTTP"), (STDIO_TOOLS, "stdio")])
    def test_tool_is_defined(self, source, which):
        text = source.read_text(encoding="utf-8")
        assert re.search(rf"def\s+{TOOL}\s*\(", text), f"{TOOL} missing from the {which} server"

    def test_stdio_server_actually_registers_it(self):
        """Source-level presence is not registration.

        A `def` without the `@tool()` decorator satisfies the check above
        while the in-chat agent — which talks to THIS server — never sees
        the tool. The HTTP transports are covered by
        `test_mcp_tool_parity.py` via `FOUNDATION_TOOL_NAMES`; the stdio
        server is a hand-maintained subset and needs its own runtime check.
        """
        import asyncio

        pytest.importorskip("mcp", reason="mcp package not installed")
        from cli.mcp import server as stdio_server

        names = {t.name for t in asyncio.run(stdio_server.mcp.list_tools())}
        assert TOOL in names, f"{TOOL} is defined but not registered on the stdio server"

    @pytest.mark.parametrize(("source", "which"), [(HTTP_TOOLS, "HTTP"), (STDIO_TOOLS, "stdio")])
    def test_docstring_points_back_at_search_for_long_files(self, source, which):
        """A read tool competes with retrieval; say when NOT to use it."""
        text = source.read_text(encoding="utf-8")
        m = re.search(rf"(?:async\s+)?def\s+{TOOL}\s*\(.*?\)\s*->[^:]*:\s*\"\"\"(.*?)\"\"\"", text, re.DOTALL)
        assert m, f"{TOOL} has no docstring in the {which} server"
        doc = m.group(1).lower()
        assert "truncat" in doc, "does not warn that long files are truncated"
        assert "search" in doc, "does not point at search for the many-documents case"

    @pytest.mark.parametrize(("source", "which"), [(HTTP_TOOLS, "HTTP"), (STDIO_TOOLS, "stdio")])
    def test_tool_takes_an_offset_on_both_servers(self, source, which):
        """The per-call cap stays; ``offset`` is how the rest is reached.

        Both servers, or an agent on one of them holds a prefix with no way
        to continue — `test_mcp_tool_parity.py` pins argument parity in
        general; this names the argument that matters here.
        """
        import ast

        tree = ast.parse(source.read_text(encoding="utf-8"))
        fn = next(
            n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == TOOL
        )
        params = {a.arg: None for a in fn.args.args}
        assert "offset" in params, f"{TOOL} on the {which} server takes no offset"
        defaults = dict(zip([a.arg for a in fn.args.args][-len(fn.args.defaults) :], fn.args.defaults))
        assert isinstance(defaults.get("offset"), ast.Constant) and defaults["offset"].value == 0, (
            f"{TOOL} on the {which} server must default offset to 0 so the first call needs no arguments"
        )

    @pytest.mark.parametrize(("source", "which"), [(HTTP_TOOLS, "HTTP"), (STDIO_TOOLS, "stdio")])
    def test_docstring_explains_how_to_continue(self, source, which):
        """A truncated read must tell the agent how to get the rest, not
        only that it is holding a prefix."""
        text = source.read_text(encoding="utf-8")
        m = re.search(rf"(?:async\s+)?def\s+{TOOL}\s*\(.*?\)\s*->[^:]*:\s*\"\"\"(.*?)\"\"\"", text, re.DOTALL)
        assert m, f"{TOOL} has no docstring in the {which} server"
        doc = m.group(1)
        assert "next_offset" in doc, "does not say to continue from next_offset"
        assert "offset=" in doc, "does not show the continuation call"
        assert "loop" in doc.lower(), "lost the do-not-loop-over-a-collection warning"
        assert "prefix" in doc.lower(), "lost the a-prefix-is-not-the-document warning"


class TestEndpointBehaviourItWraps:
    def test_returns_the_text(self, seeded_app):
        tok = seeded_app["admin_token"]
        cid, fid = _collection_with_file(seeded_app, tok, b"# Title\n\nalpha bravo charlie\n")

        r = seeded_app["client"].get(f"/api/collections/{cid}/files/{fid}/preview", headers=_auth(tok))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["kind"] == "text"
        assert "alpha bravo charlie" in body["text"]
        assert body["truncated"] is False

    def test_long_file_is_truncated_not_refused(self, seeded_app):
        """The context guard is the endpoint's, not the caller's."""
        tok = seeded_app["admin_token"]
        cid, fid = _collection_with_file(seeded_app, tok, ("x " * 30_000).encode())

        body = seeded_app["client"].get(f"/api/collections/{cid}/files/{fid}/preview", headers=_auth(tok)).json()
        assert body["truncated"] is True
        assert len(body["text"]) <= 20_000
        # A capped page is a PREFIX the caller can continue from, not a wall:
        # the response says where it stopped and how much there is in all.
        assert body["offset"] == 0
        assert body["next_offset"] == len(body["text"])
        assert body["total_chars"] == 60_000

    def test_offset_pages_through_to_the_end(self, seeded_app):
        """The complaint that produced this: a deck whose text was ~4 pages
        long could only ever be read up to page one. Chaining ``next_offset``
        must reassemble the whole file, one context-sized page at a time."""
        tok = seeded_app["admin_token"]
        full = "".join(f"slide {i:05d} " for i in range(6_000))  # ~72k chars
        cid, fid = _collection_with_file(seeded_app, tok, full.encode())

        pages: list[str] = []
        offset: int | None = 0
        while offset is not None:
            body = (
                seeded_app["client"]
                .get(
                    f"/api/collections/{cid}/files/{fid}/preview",
                    params={"offset": offset},
                    headers=_auth(tok),
                )
                .json()
            )
            assert body["offset"] == offset, "the response must echo the offset it actually used"
            assert len(body["text"]) <= 20_000, "one call must never exceed the per-call cap"
            assert body["total_chars"] == len(full)
            pages.append(body["text"])
            if body["next_offset"] is None:
                assert body["truncated"] is False, "the last page is the end of the text"
            else:
                assert body["truncated"] is True
                assert body["next_offset"] == offset + len(body["text"])
            offset = body["next_offset"]

        assert len(pages) == 4
        assert "".join(pages) == full

    def test_offset_and_limit_are_clamped_not_refused(self, seeded_app):
        """The per-call cap is the endpoint's guarantee: a caller asking for
        more gets the cap, a caller asking for nonsense gets a sane page."""
        tok = seeded_app["admin_token"]
        cid, fid = _collection_with_file(seeded_app, tok, ("y" * 50_000).encode())
        url = f"/api/collections/{cid}/files/{fid}/preview"

        big = seeded_app["client"].get(url, params={"limit": 999_999}, headers=_auth(tok)).json()
        assert len(big["text"]) == 20_000
        assert big["next_offset"] == 20_000

        tiny = seeded_app["client"].get(url, params={"limit": 0, "offset": -7}, headers=_auth(tok)).json()
        assert tiny["offset"] == 0
        assert len(tiny["text"]) == 1
        assert tiny["next_offset"] == 1

        small = seeded_app["client"].get(url, params={"limit": 5, "offset": 10}, headers=_auth(tok)).json()
        assert small["text"] == "yyyyy"
        assert small["offset"] == 10
        assert small["next_offset"] == 15
        assert small["truncated"] is True

    def test_offset_past_the_end_is_empty_not_an_error(self, seeded_app):
        tok = seeded_app["admin_token"]
        cid, fid = _collection_with_file(seeded_app, tok, b"short")

        body = (
            seeded_app["client"]
            .get(f"/api/collections/{cid}/files/{fid}/preview", params={"offset": 500}, headers=_auth(tok))
            .json()
        )
        assert body["kind"] == "text"
        assert body["text"] == ""
        assert body["next_offset"] is None
        assert body["truncated"] is False
        assert body["total_chars"] == 5

    def test_extracted_text_is_assembled_whole(self, monkeypatch):
        """``_extracted_text`` used to stop joining chunks at the cap, so no
        offset could ever reach chunk 21 of a docx. It must build the whole
        file; the endpoint pages over it. Only the text column is read —
        a page has no use for the embeddings ``list_for_file`` carries."""
        from app.api import collections as mod

        chunks = [f"chunk {i:03d} " + "z" * 990 for i in range(40)]

        class _Repo:
            def list_text_for_file(self, file_id):
                assert file_id == "cf_x"
                return chunks

            def list_for_file(self, file_id):  # pragma: no cover - must not be used
                raise AssertionError("the preview must not load embeddings")

        monkeypatch.setattr(mod, "corpus_chunks_repo", lambda: _Repo())

        text = mod._extracted_text("cf_x")
        assert len(text) > 20_000
        assert text.endswith("chunk 039 " + "z" * 990)
        assert text.count("\n\n") == 39

    def test_joined_chunks_do_not_repeat_the_overlap_window(self):
        """The chunker keeps the tail of each window at the head of the
        next, so a verbatim join repeated every boundary passage. Real
        windowing, reassembled, must give the source text back."""
        from app.api.collections import _join_chunks
        from src.ingest.chunking import _OVERLAP_CHARS, _TARGET_CHARS, _window

        full = " ".join(f"w{i:05d}" for i in range(2_000))  # 14k chars, unique tokens
        pieces = _window(full, _TARGET_CHARS, _OVERLAP_CHARS)
        assert len(pieces) >= 5, "the fixture must span several windows"
        assert full.count(pieces[1][:_OVERLAP_CHARS]) == 1
        assert "\n\n".join(pieces).count(pieces[1][:200]) == 2, "verbatim join really does repeat"

        assert _join_chunks(pieces) == full

    def test_repetitive_text_loses_nothing_at_the_seams(self):
        """A document that repeats itself — identical table rows, a footer
        on every slide — has the next chunk's head occurring EARLIER in the
        previous chunk's tail than the real overlap. A probe that reaches
        past the window takes that earlier match and deletes genuine
        content (Devin Review on this PR); bounded to the window, the
        longest match IS the overlap."""
        from app.api.collections import _join_chunks
        from src.ingest.chunking import _OVERLAP_CHARS, _TARGET_CHARS, _window

        full = ("| n/a | n/a | n/a | n/a |\n" * 900).strip()  # 17-char period, ~23k chars
        pieces = _window(full, _TARGET_CHARS, _OVERLAP_CHARS)
        assert len(pieces) >= 8

        assert _join_chunks(pieces) == full

    def test_chunks_without_a_shared_edge_keep_the_blank_line_join(self):
        """Element-based chunks never overlap; a short coincidence at the
        edge is not the window and must not be merged away."""
        from app.api.collections import _join_chunks

        assert _join_chunks(["Heading", "Body of the section"]) == "Heading\n\nBody of the section"
        edge = "same twenty chars.."  # 20 < the 40-character threshold
        joined = _join_chunks(["first element ends with " + edge, edge + " begins the second element"])
        assert joined.count(edge) == 2
        assert _join_chunks(["", "  ", "only"]) == "only"

    def test_pages_of_a_windowed_document_do_not_repeat_boundaries(self, seeded_app, monkeypatch):
        """End to end: a document chunked by the real windowing, read page
        by page through the endpoint, must contain every token exactly
        once when the pages are concatenated."""
        from app.api import collections as mod
        from src.ingest.chunking import _OVERLAP_CHARS, _TARGET_CHARS, _window

        tok = seeded_app["admin_token"]
        c = seeded_app["client"].post("/api/collections", json={"name": "Windowed"}, headers=_auth(tok))
        cid = c.json()["id"]
        up = seeded_app["client"].post(
            f"/api/collections/{cid}/files",
            files={"files": ("deck.pptx", b"PK\x03\x04 fake", "application/octet-stream")},
            headers=_auth(tok),
        )
        assert up.status_code == 201, up.text
        fid = up.json()[0]["file_id"]
        full = " ".join(f"w{i:05d}" for i in range(6_000))  # ~42k chars, 3 pages, 15 windows
        pieces = _window(full, _TARGET_CHARS, _OVERLAP_CHARS)

        class _Repo:
            def list_text_for_file(self, file_id):
                return pieces if file_id == fid else []

        monkeypatch.setattr(mod, "corpus_chunks_repo", lambda: _Repo())

        pages: list[str] = []
        offset: int | None = 0
        while offset is not None:
            body = (
                seeded_app["client"]
                .get(f"/api/collections/{cid}/files/{fid}/preview", params={"offset": offset}, headers=_auth(tok))
                .json()
            )
            pages.append(body["text"])
            offset = body["next_offset"]
        assembled = "".join(pages)
        assert assembled == full
        assert len(pages) == 3
        assert all(assembled.count(f"w{i:05d}") == 1 for i in range(6_000))

    def test_extracted_branch_pages_too(self, seeded_app, monkeypatch):
        """A pdf/docx has no readable bytes: its pages come from chunk text
        and must chain exactly like a textual file's."""
        tok = seeded_app["admin_token"]
        c = seeded_app["client"].post("/api/collections", json={"name": "Deck"}, headers=_auth(tok))
        cid = c.json()["id"]
        up = seeded_app["client"].post(
            f"/api/collections/{cid}/files",
            files={
                "files": (
                    "deck.pptx",
                    b"PK\x03\x04 fake",
                    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                )
            },
            headers=_auth(tok),
        )
        assert up.status_code == 201, up.text
        fid = up.json()[0]["file_id"]
        full = "".join(f"slide {i:05d}\n" for i in range(4_000))  # ~48k chars
        monkeypatch.setattr("app.api.collections._extracted_text", lambda _fid: full)

        first = seeded_app["client"].get(f"/api/collections/{cid}/files/{fid}/preview", headers=_auth(tok)).json()
        assert first["kind"] == "text"
        assert first["source"] == "extracted"
        assert first["truncated"] is True
        assert first["next_offset"] == 20_000
        assert first["total_chars"] == len(full)

        rest = (
            seeded_app["client"]
            .get(
                f"/api/collections/{cid}/files/{fid}/preview",
                params={"offset": first["next_offset"]},
                headers=_auth(tok),
            )
            .json()
        )
        assert rest["offset"] == 20_000
        assert first["text"] + rest["text"] == full[:40_000]
        assert rest["next_offset"] == 40_000

        last = (
            seeded_app["client"]
            .get(f"/api/collections/{cid}/files/{fid}/preview", params={"offset": 40_000}, headers=_auth(tok))
            .json()
        )
        assert last["next_offset"] is None
        assert last["truncated"] is False
        assert first["text"] + rest["text"] + last["text"] == full

    def test_a_foreign_collection_is_not_readable(self, seeded_app):
        tok = seeded_app["admin_token"]
        cid, fid = _collection_with_file(seeded_app, tok, b"secret")

        r = seeded_app["client"].get(
            f"/api/collections/{cid}/files/{fid}/preview",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert r.status_code == 404


class TestInlineMediaStillCarriesItsText:
    """A PDF has extracted text — the read surfaces must get it.

    `preview_file` answers every `_PREVIEW_INLINE_MEDIA` extension (pdf, png,
    jpg, …) with `{kind: "pdf"|"image", raw_url}` and returns BEFORE the
    `_extracted_text()` branch. Correct for the modal, which draws the file
    from `raw_url` — but a CLI or an agent cannot draw anything, and PDFs are
    a primary collection format. Without this, "what is in this PDF?" — the
    exact question the read tool exists for — answered "no text preview is
    available" while the text sat in `corpus_chunks`. Devin Review on #1240.
    """

    def _pdf_row(self, seeded_app, token: str) -> tuple:
        c = seeded_app["client"].post("/api/collections", json={"name": "Docs"}, headers=_auth(token))
        col = c.json()
        up = seeded_app["client"].post(
            f"/api/collections/{col['id']}/files",
            files={"files": ("paper.pdf", b"%PDF-1.4 fake", "application/pdf")},
            headers=_auth(token),
        )
        assert up.status_code == 201, up.text
        return col["id"], up.json()[0]["file_id"]

    def test_pdf_preview_includes_extracted_text(self, seeded_app, monkeypatch):
        tok = seeded_app["admin_token"]
        cid, fid = self._pdf_row(seeded_app, tok)
        monkeypatch.setattr("app.api.collections._extracted_text", lambda _fid: "Quarterly revenue was 4.2M.")

        body = seeded_app["client"].get(f"/api/collections/{cid}/files/{fid}/preview", headers=_auth(tok)).json()

        assert body["kind"] == "pdf", "the modal's contract must not change"
        assert body["raw_url"], "the modal still needs its draw URL"
        assert body["text"] == "Quarterly revenue was 4.2M.", "extracted text not surfaced"

    def test_pdf_page_past_the_end_is_empty_not_null(self, seeded_app, monkeypatch):
        """``text: null`` is the modal's "this medium has no text" signal.
        A medium that HAS text, read at an offset past its end, is an empty
        page: ``""`` with ``reason: null``, so a paging reader can tell the
        two apart instead of reporting a readable file as unreadable."""
        tok = seeded_app["admin_token"]
        cid, fid = self._pdf_row(seeded_app, tok)
        monkeypatch.setattr("app.api.collections._extracted_text", lambda _fid: "Quarterly revenue was 4.2M.")

        body = (
            seeded_app["client"]
            .get(f"/api/collections/{cid}/files/{fid}/preview", params={"offset": 500}, headers=_auth(tok))
            .json()
        )

        assert body["kind"] == "pdf"
        assert body["text"] == ""
        assert body["reason"] is None
        assert body["next_offset"] is None
        assert body["total_chars"] == len("Quarterly revenue was 4.2M.")

    def test_pdf_without_extracted_text_explains_itself(self, seeded_app, monkeypatch):
        """No text is fine — a silent `text: null` with no reason is not."""
        tok = seeded_app["admin_token"]
        cid, fid = self._pdf_row(seeded_app, tok)
        monkeypatch.setattr("app.api.collections._extracted_text", lambda _fid: "")

        body = seeded_app["client"].get(f"/api/collections/{cid}/files/{fid}/preview", headers=_auth(tok)).json()

        assert body["kind"] == "pdf"
        assert not body.get("text")
        assert body.get("reason"), "a text-less PDF must say why, not return a bare null"


class TestAPdfWhoseBytesAreGoneStillReads:
    """Devin Review on this PR (follow-up): the 404 came before the text.

    A collection file can lose its blob — an ingestion that recorded the row
    and never landed the bytes, or storage cleaned up underneath it — while
    its extracted text stays in `corpus_chunks`. The inline-media branch
    404'd `file_blob_missing` before it ever looked, so the new non-browser
    readers reported a hard error for a file whose answer was available. The
    textual branch has always degraded in exactly this situation.

    The 404 is kept for the case it was written for: a text-less medium with
    no bytes, where the modal would otherwise draw a broken embed. And in the
    degraded case `raw_url` is withheld, so the modal has no URL to break on.
    """

    def _pdf_row_gone(self, seeded_app, token: str, monkeypatch) -> tuple:
        """A PDF row whose blob no longer resolves.

        Simulated by neutralising `_blob_path_or_none` rather than deleting
        the file: that helper is also what `_blob_path_or_404` consults, so
        one patch makes both agree the bytes are gone — which is the real
        shape of the failure (row present, blob unreadable).
        """
        c = seeded_app["client"].post("/api/collections", json={"name": "Gone"}, headers=_auth(token))
        col = c.json()
        up = seeded_app["client"].post(
            f"/api/collections/{col['id']}/files",
            files={"files": ("paper.pdf", b"%PDF-1.4 fake", "application/pdf")},
            headers=_auth(token),
        )
        assert up.status_code == 201, up.text
        monkeypatch.setattr("app.api.collections._blob_path_or_none", lambda _row: None)
        return col["id"], up.json()[0]["file_id"]

    def test_missing_blob_with_text_degrades_instead_of_404(self, seeded_app, monkeypatch):
        tok = seeded_app["admin_token"]
        monkeypatch.setattr("app.api.collections._extracted_text", lambda _fid: "Quarterly revenue was 4.2M.")
        cid, fid = self._pdf_row_gone(seeded_app, tok, monkeypatch)

        r = seeded_app["client"].get(f"/api/collections/{cid}/files/{fid}/preview", headers=_auth(tok))

        assert r.status_code == 200, f"the text was available; a 404 throws it away: {r.text}"
        body = r.json()
        assert body["text"] == "Quarterly revenue was 4.2M."
        assert body["raw_url"] is None, "a URL that would 404 must not be handed to the modal"
        # `kind` is what file_preview.js switches on — NOT `raw_url`. Leaving
        # it "pdf" builds an <iframe> and assigns the URL unconditionally, so
        # a null URL renders a blank frame with no error handler: the broken
        # embed the 404 existed to prevent, reached another way.
        assert body["kind"] == "text", "the modal would draw an embed it has no source for"
        assert body["source"] == "extracted", "the modal's provenance note keys on this"

    def test_missing_blob_without_text_still_404s(self, seeded_app, monkeypatch):
        """The case the 404 was written for must not regress."""
        tok = seeded_app["admin_token"]
        monkeypatch.setattr("app.api.collections._extracted_text", lambda _fid: "")
        cid, fid = self._pdf_row_gone(seeded_app, tok, monkeypatch)

        r = seeded_app["client"].get(f"/api/collections/{cid}/files/{fid}/preview", headers=_auth(tok))

        assert r.status_code == 404
        assert r.json()["detail"] == "file_blob_missing"


class TestCliCat:
    def test_cat_prints_the_text_of_a_pdf(self):
        """The CLI keys on `text`, not on `kind` — a PDF with text is readable."""
        payload = {
            "kind": "pdf",
            "text": "Quarterly revenue was 4.2M.",
            "truncated": False,
            "filename": "paper.pdf",
        }
        with patch("cli.commands.collections.api_get_json", return_value=payload):
            r = runner.invoke(collections_app, ["cat", "col_1", "cf_1"])
        assert r.exit_code == 0, r.output
        assert "Quarterly revenue was 4.2M." in r.output

    def test_cat_on_a_textless_image_relays_the_reason(self):
        payload = {"kind": "image", "text": None, "reason": "Images carry no extractable text."}
        with patch("cli.commands.collections.api_get_json", return_value=payload):
            r = runner.invoke(collections_app, ["cat", "col_1", "cf_1"])
        assert r.exit_code == 1
        assert "no extractable text" in r.output.lower()

    def test_help_lists_cat(self):
        r = runner.invoke(collections_app, ["--help"])
        assert r.exit_code == 0
        assert "cat" in r.output

    def test_cat_prints_the_text(self):
        payload = {"kind": "text", "text": "alpha bravo", "truncated": False, "filename": "n.md"}
        with patch("cli.commands.collections.api_get_json", return_value=payload):
            r = runner.invoke(collections_app, ["cat", "col_1", "cf_1"])
        assert r.exit_code == 0, r.output
        assert "alpha bravo" in r.output

    def test_cat_says_when_output_was_truncated(self):
        """Silently cutting a document is how a wrong summary gets written."""
        payload = {"kind": "text", "text": "alpha", "truncated": True, "filename": "n.md"}
        with patch("cli.commands.collections.api_get_json", return_value=payload):
            r = runner.invoke(collections_app, ["cat", "col_1", "cf_1"])
        assert r.exit_code == 0, r.output
        assert "truncated" in r.output.lower()

    def test_cat_relays_the_reason_when_there_is_no_text(self):
        payload = {"kind": "none", "reason": "This file hasn't been indexed yet."}
        with patch("cli.commands.collections.api_get_json", return_value=payload):
            r = runner.invoke(collections_app, ["cat", "col_1", "cf_1"])
        assert "hasn't been indexed" in r.output

    def test_cat_json_mode_emits_the_payload(self):
        payload = {"kind": "text", "text": "alpha", "truncated": False}
        with patch("cli.commands.collections.api_get_json", return_value=payload):
            r = runner.invoke(collections_app, ["cat", "col_1", "cf_1", "--json"])
        assert json.loads(r.output)["text"] == "alpha"


class TestCliCatPagesThroughTheFile:
    """`cat` means the whole file. The server pages at ~20k characters; the
    command follows ``next_offset`` so the terminal reader never has to."""

    FULL = "".join(f"line {i:04d}\n" for i in range(600))  # 6 000 chars

    @classmethod
    def _server(cls, page_size: int = 2_500):
        """A fake preview endpoint with the real paging contract, so the test
        exercises the client's loop and not a hand-written page list."""
        calls: list[dict] = []

        def fake(path, **params):
            offset = max(0, int(params.get("offset", 0)))
            limit = max(1, min(int(params.get("limit", page_size)), page_size))
            calls.append({"offset": offset, "limit": limit})
            page = cls.FULL[offset : offset + limit]
            end = offset + len(page)
            nxt = end if end < len(cls.FULL) else None
            return {
                "kind": "text",
                "text": page,
                "offset": offset,
                "next_offset": nxt,
                "total_chars": len(cls.FULL),
                "truncated": nxt is not None,
                "filename": "long.txt",
            }

        return fake, calls

    def test_default_prints_the_whole_file_with_no_warning(self):
        fake, calls = self._server()
        with patch("cli.commands.collections.api_get_json", side_effect=fake):
            r = runner.invoke(collections_app, ["cat", "col_1", "cf_1"])
        assert r.exit_code == 0, r.output
        assert r.output.startswith(self.FULL), "pages must be concatenated verbatim"
        assert "truncated" not in r.output.lower(), "the whole file was printed; nothing to warn about"
        assert [c["offset"] for c in calls] == [0, 2_500, 5_000]

    def test_limit_prints_a_prefix_and_says_so(self):
        fake, calls = self._server()
        with patch("cli.commands.collections.api_get_json", side_effect=fake):
            r = runner.invoke(collections_app, ["cat", "col_1", "cf_1", "--limit", "100"])
        assert r.exit_code == 0, r.output
        assert r.output.startswith(self.FULL[:100])
        assert self.FULL[:101] not in r.output
        assert "truncated" in r.output.lower()
        assert "--offset 100" in r.output, "the warning must name the offset to continue from"
        assert calls == [{"offset": 0, "limit": 100}]

    def test_limit_larger_than_a_page_keeps_paging_up_to_the_limit(self):
        fake, calls = self._server()
        with patch("cli.commands.collections.api_get_json", side_effect=fake):
            r = runner.invoke(collections_app, ["cat", "col_1", "cf_1", "--limit", "4000"])
        assert r.exit_code == 0, r.output
        assert r.output.startswith(self.FULL[:4_000])
        assert self.FULL[:4_001] not in r.output
        assert [c["offset"] for c in calls] == [0, 2_500]
        assert calls[-1]["limit"] == 1_500, "the last page asks only for what the limit leaves"
        assert "--offset 4000" in r.output

    def test_offset_starts_there_and_reads_to_the_end(self):
        fake, calls = self._server()
        with patch("cli.commands.collections.api_get_json", side_effect=fake):
            r = runner.invoke(collections_app, ["cat", "col_1", "cf_1", "--offset", "5900"])
        assert r.exit_code == 0, r.output
        assert r.output.startswith(self.FULL[5_900:])
        assert "truncated" not in r.output.lower()
        assert calls == [{"offset": 5_900, "limit": 2_500}]

    def test_json_emits_exactly_one_page(self):
        fake, calls = self._server()
        with patch("cli.commands.collections.api_get_json", side_effect=fake):
            r = runner.invoke(collections_app, ["cat", "col_1", "cf_1", "--json", "--offset", "2500"])
        assert r.exit_code == 0, r.output
        body = json.loads(r.output)
        assert body["offset"] == 2_500
        assert body["next_offset"] == 5_000
        assert body["text"] == self.FULL[2_500:5_000]
        assert len(calls) == 1, "--json is one page, never a loop"

    def test_says_when_the_server_itself_could_not_reach_the_end(self):
        """``truncated: true`` with no ``next_offset`` is the server's byte
        cap on a huge textual file: the text continues but is not reachable
        here. Silence would be a prefix passed off as the file."""
        payload = {
            "kind": "text",
            "text": "beginning",
            "offset": 0,
            "next_offset": None,
            "total_chars": 9,
            "truncated": True,
        }
        with patch("cli.commands.collections.api_get_json", return_value=payload):
            r = runner.invoke(collections_app, ["cat", "col_1", "cf_1"])
        assert r.exit_code == 0, r.output
        assert "truncated" in r.output.lower()
        assert "search" in r.output.lower(), "must point at the way to reach the rest"

    def test_offset_past_the_end_names_the_range_not_no_text(self):
        """An empty PAGE is not "no text". The server answers an offset past
        the end with ``text: ""`` and the file's ``total_chars``; printing the
        no-preview sentence there denies the file has text at all."""
        fake, _calls = self._server()
        with patch("cli.commands.collections.api_get_json", side_effect=fake):
            r = runner.invoke(collections_app, ["cat", "col_1", "cf_1", "--offset", "9000"])
        assert r.exit_code == 1
        assert "past the end" in r.output.lower()
        assert str(len(self.FULL)) in r.output, "must name the file's length so the caller can pick a valid offset"
        assert "no text preview" not in r.output.lower()

    @pytest.mark.parametrize("argv", [["--limit", "0"], ["--limit", "-5"], ["--offset", "-1"]])
    def test_out_of_range_flags_are_a_usage_error_not_a_clamp(self, argv):
        """``--limit 0`` used to be clamped to one character and print it —
        data handed back for a request that promised none. Reject at the
        flag, before any request; the server keeps its own clamp for
        direct API callers."""
        fake, calls = self._server()
        with patch("cli.commands.collections.api_get_json", side_effect=fake):
            r = runner.invoke(collections_app, ["cat", "col_1", "cf_1", *argv])
        assert r.exit_code == 2, r.output
        plain = _ANSI.sub("", r.output)
        assert argv[0] in plain, plain
        assert "not in the range" in plain, plain
        assert calls == [], "a rejected flag must not reach the server"

    def test_does_not_loop_on_a_server_that_does_not_advance(self):
        """Defensive: a ``next_offset`` that does not move forward must end
        the loop, not spin it."""
        payload = {"kind": "text", "text": "same", "offset": 0, "next_offset": 0, "total_chars": 99, "truncated": True}
        with patch("cli.commands.collections.api_get_json", return_value=payload) as api:
            r = runner.invoke(collections_app, ["cat", "col_1", "cf_1"])
        assert r.exit_code == 0, r.output
        assert api.call_count == 1
