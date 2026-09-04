"""REST-layer tests for the fact-graph collection-stats summary rebuild
(TCRD-296 synthesis E.21) — ``POST /api/admin/facts/stats/rebuild``.

DuckDB backend (the default ``seeded_app`` fixture) proves the HTTP wiring:
auth, the admin gate, and the PG-only typed-501 fail-clean shape. The
maintenance/reader-parity behavior against a REAL Postgres backend lives in
``tests/db_pg/test_fact_collection_stats_pg.py``.
"""

from __future__ import annotations


def _headers(app, token_key="admin_token"):
    return {"Authorization": f"Bearer {app[token_key]}"}


def test_rebuild_requires_authentication(seeded_app):
    r = seeded_app["client"].post("/api/admin/facts/stats/rebuild")
    assert r.status_code == 401


def test_rebuild_requires_admin(seeded_app):
    r = seeded_app["client"].post("/api/admin/facts/stats/rebuild", headers=_headers(seeded_app, "analyst_token"))
    assert r.status_code == 403


def test_rebuild_fails_clean_on_duckdb(seeded_app, duckdb_backend_pinned):
    r = seeded_app["client"].post("/api/admin/facts/stats/rebuild", headers=_headers(seeded_app))
    assert r.status_code == 501, r.text
    body = r.json()
    assert body["error"] == "requires_postgres_backend"
    assert body["feature"] == "facts"


def test_rebuild_rejects_unknown_body_fields(seeded_app, duckdb_backend_pinned):
    # extra='forbid' — a typo'd field 422s before the (PG-only) repo is ever
    # reached, same discipline as FactsSearchRequest.
    r = seeded_app["client"].post(
        "/api/admin/facts/stats/rebuild", json={"unexpected": True}, headers=_headers(seeded_app)
    )
    assert r.status_code == 422
