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


def test_response_carries_candidates_capped_when_the_bound_is_hit(seeded_app, monkeypatch):
    """P0 OOM fix, 2026-09: `search()`'s bounded candidate scan surfaces an
    additive `candidates_capped: true` on the response when it hit the
    configured limit — never present (and never `false`) otherwise."""
    import src.ingest.retrieval as retrieval

    c = seeded_app["client"]
    admin = seeded_app["admin_token"]

    col = c.post("/api/collections", json={"name": "Cap Col"}, headers=_auth(admin)).json()
    for name in ("one.md", "two.md"):
        up = c.post(
            f"/api/collections/{col['id']}/files",
            files={"files": (name, io.BytesIO(b"widget revenue widget revenue"), "text/markdown")},
            headers=_auth(admin),
        )
        assert up.status_code == 201, up.text

    monkeypatch.setattr(retrieval, "_max_candidate_chunks", lambda: 1)
    resp = c.get("/api/knowledge/search", params={"q": "widget", "k": 10}, headers=_auth(admin))
    assert resp.status_code == 200, resp.text
    assert resp.json().get("candidates_capped") is True

    monkeypatch.setattr(retrieval, "_max_candidate_chunks", lambda: 100)
    resp = c.get("/api/knowledge/search", params={"q": "widget", "k": 10}, headers=_auth(admin))
    assert resp.status_code == 200, resp.text
    assert "candidates_capped" not in resp.json()


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
