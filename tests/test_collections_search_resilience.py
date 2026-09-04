"""Resilience of the Collections/knowledge search surfaces at scale (#2151).

Issue #2151: a post-ingest corpus in the 100k+ chunk range made
``GET /api/collections/search`` materialize every accessible chunk (full
``text`` + ``embedding``) into Python with no cap, blowing past the API
container's memory budget under load and surfacing as an anonymous 500 six
times out of six on a live instance.

Covers, at the HTTP layer (the unit-level mechanics — two-phase hybrid
fetch, the SQL prefilter, the config resolver — have their own tests in
``tests/test_ingest_retrieval.py``, ``tests/db_pg/test_corpus_chunks_
contract.py`` and ``tests/test_collections_search_config.py``):

- Fault injection: a ``MemoryError``/``OperationalError`` from the repo's
  candidate fetch is a typed ``503`` on ``/api/collections/search``, and
  degrades to an empty, disclosed chunk leg (other legs unaffected) on
  ``/api/knowledge/search``.
- The server-side chunk cap: over it, a 200 with ``truncated: true`` (not a
  crash or a silent partial answer); at/under it, unchanged.
- A query with no usable term to narrow an over-cap corpus by is a typed
  ``422``, never an arbitrary slice of the corpus.
- A query-time embedding failure degrades the response's ``retrieval``
  label to ``"lexical_only"`` instead of a 500.
"""

from __future__ import annotations

import sqlalchemy as sa

from src.repositories.corpus_chunks import CorpusChunksRepository


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _seed_corpus(seeded_app, name: str, texts: list[str]) -> str:
    from src.repositories import corpus_chunks_repo, corpus_files_repo

    c = seeded_app["client"]
    corpus_id = c.post("/api/collections", json={"name": name}, headers=_auth(seeded_app["admin_token"])).json()["id"]
    fid = corpus_files_repo().add(
        corpus_id=corpus_id, filename="d.txt", sha256="s", file_type="txt", size_bytes=1, storage_path="/x"
    )
    corpus_chunks_repo().add_many(
        [{"corpus_id": corpus_id, "file_id": fid, "ordinal": i, "text": t} for i, t in enumerate(texts)]
    )
    return corpus_id


# ---------------------------------------------------------------------------
# Fault injection — the repo's candidate fetch raising must never surface as
# an anonymous 500.
# ---------------------------------------------------------------------------


class TestFaultInjection:
    def test_collections_search_memory_error_is_typed_503(self, seeded_app, monkeypatch):
        _seed_corpus(seeded_app, "Fault Mem", ["the magic keyword appears here"])
        monkeypatch.setattr(
            CorpusChunksRepository,
            "list_for_corpora",
            lambda self, *a, **kw: (_ for _ in ()).throw(MemoryError("simulated OOM")),
        )
        c = seeded_app["client"]
        resp = c.get(
            "/api/collections/search",
            params={"q": "magic keyword"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 503, resp.text
        assert resp.json()["detail"]["error"] == "search_unavailable"

    def test_collections_search_operational_error_is_typed_503(self, seeded_app, monkeypatch):
        _seed_corpus(seeded_app, "Fault Op", ["the magic keyword appears here"])

        def _boom(self, *a, **kw):
            raise sa.exc.OperationalError("SELECT 1", {}, Exception("simulated"))

        monkeypatch.setattr(CorpusChunksRepository, "list_for_corpora", _boom)
        c = seeded_app["client"]
        resp = c.get(
            "/api/collections/search",
            params={"q": "magic keyword"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 503, resp.text
        assert resp.json()["detail"]["error"] == "search_unavailable"

    def test_collections_search_unrelated_exception_still_propagates(self, seeded_app, monkeypatch):
        """ "Everything else still propagates (a genuine bug should stay
        loud)" — only the three named exception types are translated.

        ``TestClient`` re-raises an exception the ASGI app doesn't itself
        turn into a response by default (``raise_server_exceptions=True``);
        a dedicated non-raising client is this suite's established way to
        see the app-wide catch-all's actual 500 response instead (see e.g.
        ``tests/test_chat_uploads.py``).
        """
        from fastapi.testclient import TestClient

        _seed_corpus(seeded_app, "Fault Unrelated", ["the magic keyword appears here"])

        def _boom(self, *a, **kw):
            raise RuntimeError("a genuine bug, not a capacity problem")

        monkeypatch.setattr(CorpusChunksRepository, "list_for_corpora", _boom)
        c = TestClient(seeded_app["client"].app, raise_server_exceptions=False)
        resp = c.get(
            "/api/collections/search",
            params={"q": "anything"},
            headers=_auth(seeded_app["admin_token"]),
        )
        # The app-wide catch-all still turns this into a 500 — it must NOT
        # be silently absorbed into the 503 typed path.
        assert resp.status_code == 500, resp.text

    def test_knowledge_search_chunk_leg_degrades_other_legs_survive(self, seeded_app, monkeypatch):
        from src.repositories import get_system_db, table_registry_repo

        conn = get_system_db()
        table_registry_repo().register(
            id="ks_resilience_1",
            name="ks_resilience_1",
            description="widget catalog for resilience test",
            source_type="keboola",
            query_mode="materialized",
        )
        conn.close()
        _seed_corpus(seeded_app, "Fault KS", ["widget catalog entry"])

        monkeypatch.setattr(
            CorpusChunksRepository,
            "list_for_corpora",
            lambda self, *a, **kw: (_ for _ in ()).throw(MemoryError("simulated OOM")),
        )
        c = seeded_app["client"]
        resp = c.get(
            "/api/knowledge/search",
            params={"q": "widget catalog"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert [h for h in body["results"] if h["type"] == "chunk"] == []
        assert any(h["type"] == "table" for h in body["results"]), "the table leg must still answer"
        assert body["degraded"] == {"chunk": "search_unavailable"}
        assert body.get("degraded_note")


# ---------------------------------------------------------------------------
# Server-side chunk cap — bound the candidate set instead of an unbounded
# fetch, disclosed rather than a silent partial answer.
# ---------------------------------------------------------------------------


class TestChunkCap:
    def test_over_cap_returns_200_with_truncated_and_hint(self, seeded_app, monkeypatch):
        import src.ingest.retrieval as retrieval

        monkeypatch.setattr(retrieval, "_search_max_chunks", lambda: 2)
        _seed_corpus(
            seeded_app,
            "Cap Over",
            [
                "kubernetes cluster guide",
                "totally unrelated weather report",
                "another unrelated row about nothing",
            ],
        )
        c = seeded_app["client"]
        resp = c.get(
            "/api/collections/search",
            params={"q": "kubernetes"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["truncated"] is True
        assert body["truncated_cap"] == 2
        assert "truncated_note" in body and body["truncated_note"]
        assert any("kubernetes" in (r.get("text") or "") for r in body["results"])

    def test_at_cap_is_not_truncated_and_matches_unbounded_search(self, seeded_app, monkeypatch):
        """Regression pin: identical results to the pre-#2151 unconditional
        fetch when the corpus is at/under the cap — no prefilter, no cap."""
        import src.ingest.retrieval as retrieval

        monkeypatch.setattr(retrieval, "_search_max_chunks", lambda: 10_000)
        _seed_corpus(
            seeded_app,
            "Cap Under",
            ["the quick brown fox jumps over", "completely unrelated weather report"],
        )
        c = seeded_app["client"]
        resp = c.get(
            "/api/collections/search",
            params={"q": "brown fox"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "truncated" not in body
        assert body["results"]
        assert body["results"][0]["text"].startswith("the quick brown fox")

    def test_over_cap_stopword_only_query_is_typed_422(self, seeded_app, monkeypatch):
        import src.ingest.retrieval as retrieval

        monkeypatch.setattr(retrieval, "_search_max_chunks", lambda: 1)
        _seed_corpus(
            seeded_app,
            "Cap Broad",
            ["kubernetes cluster guide", "totally unrelated weather report"],
        )
        c = seeded_app["client"]
        resp = c.get(
            "/api/collections/search",
            params={"q": "the and is"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 422, resp.text
        detail = resp.json()["detail"]
        assert detail["error"] == "search_query_too_broad"
        assert detail["cap"] == 1
        assert "hint" in detail


# ---------------------------------------------------------------------------
# Query-time embed failure — degrades the response label, never a 500.
# ---------------------------------------------------------------------------


class TestEncodeGuard:
    def test_encode_failure_degrades_retrieval_label_to_lexical_only(self, seeded_app, monkeypatch):
        import src.ingest.embeddings as embeddings

        class _BoomModel:
            def encode(self, *_a, **_kw):
                raise RuntimeError("simulated encode failure")

        # embedding_capability() reports True (the extra IS "installed" from
        # this test's point of view) so retrieval_mode() would otherwise say
        # "hybrid" — the encode failure inside rank_chunks's embed_query call
        # must still degrade the label truthfully.
        monkeypatch.setattr(embeddings, "_model", _BoomModel())
        _seed_corpus(seeded_app, "Encode Guard", ["the magic keyword appears here"])
        c = seeded_app["client"]
        resp = c.get(
            "/api/collections/search",
            params={"q": "magic keyword"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["retrieval"] == "lexical_only"
        assert any("magic" in (r.get("text") or "") for r in body["results"])
