"""GET /api/knowledge/search — unified knowledge search endpoint (K2, #797)."""

from __future__ import annotations

import io


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_unauthenticated_returns_401(seeded_app):
    c = seeded_app["client"]
    resp = c.get("/api/knowledge/search", params={"q": "anything"})
    assert resp.status_code == 401


def test_admin_gets_typed_results_shape(seeded_app):
    c = seeded_app["client"]
    admin = seeded_app["admin_token"]

    # Seed one collection + document so the chunk source has content.
    col = c.post("/api/collections", json={"name": "KS Col"}, headers=_auth(admin)).json()
    up = c.post(
        f"/api/collections/{col['id']}/files",
        files={"files": ("billing.md", io.BytesIO(b"# Billing\n\nInvoices are generated monthly."), "text/markdown")},
        headers=_auth(admin),
    )
    assert up.status_code == 201, up.text

    resp = c.get("/api/knowledge/search", params={"q": "invoices monthly", "k": 10}, headers=_auth(admin))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["query"] == "invoices monthly"
    assert isinstance(body["results"], list)
    assert body["results"], "expected at least the uploaded chunk to match"
    for hit in body["results"]:
        assert hit["type"] in ("chunk", "knowledge", "table", "metric", "glossary")
        assert "score" in hit
    chunk_hits = [h for h in body["results"] if h["type"] == "chunk"]
    assert any(h["filename"] == "billing.md" for h in chunk_hits)


def test_response_carries_retrieval_mode(seeded_app, monkeypatch):
    """#898: the unified search response labels the chunk engine's mode so
    clients can tell hybrid results from the lexical-only degradation."""
    import src.ingest.retrieval as retrieval

    monkeypatch.setattr(retrieval, "embedding_capability", lambda: False)
    c = seeded_app["client"]
    resp = c.get(
        "/api/knowledge/search",
        params={"q": "anything"},
        headers=_auth(seeded_app["admin_token"]),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["retrieval"] == "lexical_only"

    monkeypatch.setattr(retrieval, "embedding_capability", lambda: True)
    resp = c.get(
        "/api/knowledge/search",
        params={"q": "anything"},
        headers=_auth(seeded_app["admin_token"]),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["retrieval"] == "hybrid"


def test_analyst_without_grants_sees_no_chunks(seeded_app):
    c = seeded_app["client"]
    admin = seeded_app["admin_token"]
    analyst = seeded_app["analyst_token"]

    col = c.post("/api/collections", json={"name": "Private Col"}, headers=_auth(admin)).json()
    up = c.post(
        f"/api/collections/{col['id']}/files",
        files={"files": ("secret.md", io.BytesIO(b"# Secret\n\nThe launch codes are hidden."), "text/markdown")},
        headers=_auth(admin),
    )
    assert up.status_code == 201, up.text

    resp = c.get("/api/knowledge/search", params={"q": "launch codes hidden"}, headers=_auth(analyst))
    assert resp.status_code == 200, resp.text
    chunk_hits = [h for h in resp.json()["results"] if h["type"] == "chunk"]
    assert chunk_hits == []  # fail-closed: no grant on the collection


def test_blank_query_rejected(seeded_app):
    c = seeded_app["client"]
    resp = c.get("/api/knowledge/search", params={"q": ""}, headers=_auth(seeded_app["admin_token"]))
    assert resp.status_code == 422


def test_admin_sees_table_hit_resolved_via_get_accessible_tables(seeded_app):
    """RBAC N+1 collapse (FAI-132): table filtering now goes through a single
    ``get_accessible_tables`` call instead of per-row ``can_access_table``.
    Admin (``get_accessible_tables`` -> None) must still see every table.
    """
    from src.db import get_system_db
    from src.repositories import table_registry_repo

    conn = get_system_db()
    table_registry_repo().register(
        id="ks_widgets_admin",
        name="ks_widgets_admin",
        description="widgets inventory catalog",
        source_type="keboola",
        query_mode="materialized",
    )
    conn.close()

    c = seeded_app["client"]
    resp = c.get(
        "/api/knowledge/search",
        params={"q": "widgets inventory catalog"},
        headers=_auth(seeded_app["admin_token"]),
    )
    assert resp.status_code == 200, resp.text
    table_hits = [h for h in resp.json()["results"] if h["type"] == "table"]
    assert any(h["table_id"] == "ks_widgets_admin" for h in table_hits)


def test_analyst_without_table_grant_sees_no_table_hit(seeded_app):
    """Analyst with no data-package grant on the table must not see it in
    results — the single accessible-set resolution must stay fail-closed.
    """
    from src.db import get_system_db
    from src.repositories import table_registry_repo

    conn = get_system_db()
    table_registry_repo().register(
        id="ks_widgets_private",
        name="ks_widgets_private",
        description="gizmos secret catalog",
        source_type="keboola",
        query_mode="materialized",
    )
    conn.close()

    c = seeded_app["client"]
    resp = c.get(
        "/api/knowledge/search",
        params={"q": "gizmos secret catalog"},
        headers=_auth(seeded_app["analyst_token"]),
    )
    assert resp.status_code == 200, resp.text
    table_hits = [h for h in resp.json()["results"] if h["type"] == "table"]
    assert not any(h["table_id"] == "ks_widgets_private" for h in table_hits)


def test_analyst_with_table_grant_sees_table_hit(seeded_app):
    """Once the table is granted via a data package, the analyst sees it —
    confirming the collapsed single-resolution path preserves the same
    grant semantics as the old per-row ``can_access_table`` loop.
    """
    from src.db import get_system_db
    from src.repositories import table_registry_repo

    from tests.conftest import grant_table_via_package

    conn = get_system_db()
    table_registry_repo().register(
        id="ks_widgets_granted",
        name="ks_widgets_granted",
        description="sprockets granted catalog",
        source_type="keboola",
        query_mode="materialized",
    )
    grant_table_via_package(conn, "ks_widgets_granted", "analyst1")
    conn.close()

    c = seeded_app["client"]
    resp = c.get(
        "/api/knowledge/search",
        params={"q": "sprockets granted catalog"},
        headers=_auth(seeded_app["analyst_token"]),
    )
    assert resp.status_code == 200, resp.text
    table_hits = [h for h in resp.json()["results"] if h["type"] == "table"]
    assert any(h["table_id"] == "ks_widgets_granted" for h in table_hits)


def test_knowledge_search_resolves_accessible_tables_once(seeded_app, monkeypatch):
    """N+1 regression guard (FAI-132 review): ``/api/knowledge/search`` must
    resolve the caller's accessible table set with a SINGLE
    ``get_accessible_tables`` call, not one ``can_access_table`` per
    registered table. Without this guard a regression back to the per-row
    loop would still pass the behavioral tests above.
    """
    from src.db import get_system_db
    from src.repositories import table_registry_repo

    conn = get_system_db()
    for n in range(3):
        table_registry_repo().register(
            id=f"ks_once_{n}",
            name=f"ks_once_{n}",
            description=f"widget catalog number {n}",
            source_type="keboola",
            query_mode="materialized",
        )
    conn.close()

    import app.api.knowledge_search as ks_module

    calls = {"get_accessible_tables": 0}
    real = ks_module.get_accessible_tables

    def _counting(*args, **kwargs):
        calls["get_accessible_tables"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(ks_module, "get_accessible_tables", _counting)

    c = seeded_app["client"]
    resp = c.get(
        "/api/knowledge/search",
        params={"q": "widget catalog number"},
        headers=_auth(seeded_app["analyst_token"]),
    )
    assert resp.status_code == 200, resp.text
    assert calls["get_accessible_tables"] == 1


# ---------------------------------------------------------------------------
# #2151: the chunk leg degrades to empty (with a disclosed note) instead of
# taking the whole combined search down when the chunk engine fails.
# ---------------------------------------------------------------------------


def _seed_col_with_chunk(seeded_app, name: str, text: str) -> str:
    c = seeded_app["client"]
    col = c.post("/api/collections", json={"name": name}, headers=_auth(seeded_app["admin_token"])).json()
    from src.repositories import corpus_chunks_repo, corpus_files_repo

    fid = corpus_files_repo().add(
        corpus_id=col["id"], filename="d.txt", sha256="s", file_type="txt", size_bytes=1, storage_path="/x"
    )
    corpus_chunks_repo().add_many([{"corpus_id": col["id"], "file_id": fid, "ordinal": 0, "text": text}])
    return col["id"]


def test_chunk_leg_memory_error_degrades_to_empty_with_note(seeded_app, monkeypatch):
    """A chunk-engine MemoryError must not take the combined search down —
    it degrades to an empty chunk leg with a disclosed note, and OTHER
    legs (here: the table catalog) still answer."""
    import app.api.knowledge_search as ks_module

    _seed_col_with_chunk(seeded_app, "KS Mem", "the magic keyword appears here")

    def _boom(*_a, **_kw):
        raise MemoryError("simulated OOM")

    monkeypatch.setattr(ks_module, "search_with_meta", _boom)
    c = seeded_app["client"]
    resp = c.get(
        "/api/knowledge/search",
        params={"q": "magic keyword"},
        headers=_auth(seeded_app["admin_token"]),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [h for h in body["results"] if h["type"] == "chunk"] == []
    assert body["degraded"] == {"chunk": "search_unavailable"}
    assert body.get("degraded_note")


def test_chunk_leg_operational_error_degrades_to_empty_other_legs_survive(seeded_app, monkeypatch):
    import sqlalchemy as sa

    import app.api.knowledge_search as ks_module
    from src.repositories import get_system_db, table_registry_repo

    conn = get_system_db()
    table_registry_repo().register(
        id="ks_survive_1",
        name="ks_survive_1",
        description="widget catalog for survival test",
        source_type="keboola",
        query_mode="materialized",
    )
    conn.close()
    _seed_col_with_chunk(seeded_app, "KS Op", "widget catalog entry")

    def _boom(*_a, **_kw):
        raise sa.exc.OperationalError("SELECT 1", {}, Exception("simulated"))

    monkeypatch.setattr(ks_module, "search_with_meta", _boom)
    c = seeded_app["client"]
    resp = c.get(
        "/api/knowledge/search",
        params={"q": "widget catalog"},
        headers=_auth(seeded_app["admin_token"]),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [h for h in body["results"] if h["type"] == "chunk"] == []
    assert any(h["type"] == "table" for h in body["results"])
    assert body["degraded"] == {"chunk": "search_unavailable"}


def test_chunk_leg_success_is_unaffected_by_degradation_wiring(seeded_app):
    """Regression pin: the normal (non-failing) path is unchanged."""
    _seed_col_with_chunk(seeded_app, "KS OK", "the magic keyword appears here")
    c = seeded_app["client"]
    resp = c.get(
        "/api/knowledge/search",
        params={"q": "magic keyword"},
        headers=_auth(seeded_app["admin_token"]),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "degraded" not in body
    assert any(h["type"] == "chunk" for h in body["results"])
