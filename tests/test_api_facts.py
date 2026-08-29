"""REST-layer tests for the fact graph over Collections read surface
(build order steps 2+3 of
docs/superpowers/specs/2026-08-27-fact-graph-over-collections-design.md).

DuckDB backend (the default ``seeded_app`` fixture) — proves the HTTP
wiring: the router-level feature-flag gate, request validation caps, auth,
and the PG-only typed-501 fail-clean shape. RBAC/projection depth against a
REAL Postgres backend lives in ``tests/db_pg/test_facts_read_pg.py`` (the
repository directly) and ``tests/db_pg/test_endpoints_behavioral.py``'s
``TestFactsReadSurfaceSmoke`` (round-trip through HTTP on Postgres).
"""

from __future__ import annotations

import pytest


@pytest.fixture
def facts_client(seeded_app, monkeypatch):
    monkeypatch.setenv("AGNES_FACTS_ENABLED", "1")
    return seeded_app


def _headers(app):
    return {"Authorization": f"Bearer {app['admin_token']}"}


# ---------------------------------------------------------------------------
# feature flag gate — the whole router disappears when off.
# ---------------------------------------------------------------------------


def test_search_404s_when_flag_off(seeded_app):
    r = seeded_app["client"].post("/api/facts/search", json={}, headers=_headers(seeded_app))
    assert r.status_code == 404


def test_neighbors_404s_when_flag_off(seeded_app):
    r = seeded_app["client"].post("/api/facts/neighbors", json={"subject_id": "f_x"}, headers=_headers(seeded_app))
    assert r.status_code == 404


def test_claims_404s_when_flag_off(seeded_app):
    r = seeded_app["client"].get("/api/facts/f_x/claims", headers=_headers(seeded_app))
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# auth — any authenticated caller, no admin gate (enforcement is repo-side).
# ---------------------------------------------------------------------------


def test_search_requires_authentication(facts_client):
    r = facts_client["client"].post("/api/facts/search", json={})
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# PG-only fail-clean on DuckDB.
# ---------------------------------------------------------------------------


def test_search_fails_clean_on_duckdb(facts_client):
    r = facts_client["client"].post("/api/facts/search", json={}, headers=_headers(facts_client))
    assert r.status_code == 501, r.text
    body = r.json()
    assert body["error"] == "requires_postgres_backend"
    assert body["feature"] == "facts"


def test_claims_fails_clean_on_duckdb(facts_client):
    r = facts_client["client"].get("/api/facts/f_doesnotmatter/claims", headers=_headers(facts_client))
    assert r.status_code == 501, r.text
    assert r.json()["error"] == "requires_postgres_backend"


def test_neighbors_fails_clean_on_duckdb(facts_client):
    r = facts_client["client"].post("/api/facts/neighbors", json={"subject_id": "f_x"}, headers=_headers(facts_client))
    assert r.status_code == 501, r.text
    assert r.json()["error"] == "requires_postgres_backend"


# ---------------------------------------------------------------------------
# request validation caps (spec §12: limit<=100, depth<=1(max 2), fanout<=100,
# limit<=500) — 422 beyond, before the request ever reaches the repository.
# ---------------------------------------------------------------------------


def test_search_limit_over_100_is_422(facts_client):
    r = facts_client["client"].post("/api/facts/search", json={"limit": 101}, headers=_headers(facts_client))
    assert r.status_code == 422


def test_search_limit_zero_is_422(facts_client):
    r = facts_client["client"].post("/api/facts/search", json={"limit": 0}, headers=_headers(facts_client))
    assert r.status_code == 422


def test_neighbors_requires_subject_id(facts_client):
    r = facts_client["client"].post("/api/facts/neighbors", json={}, headers=_headers(facts_client))
    assert r.status_code == 422


def test_neighbors_depth_over_2_is_422(facts_client):
    r = facts_client["client"].post(
        "/api/facts/neighbors",
        json={"subject_id": "f_x", "depth": 3},
        headers=_headers(facts_client),
    )
    assert r.status_code == 422


def test_neighbors_fanout_over_100_is_422(facts_client):
    r = facts_client["client"].post(
        "/api/facts/neighbors",
        json={"subject_id": "f_x", "fanout": 101},
        headers=_headers(facts_client),
    )
    assert r.status_code == 422


def test_neighbors_limit_over_500_is_422(facts_client):
    r = facts_client["client"].post(
        "/api/facts/neighbors",
        json={"subject_id": "f_x", "limit": 501},
        headers=_headers(facts_client),
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# extra='forbid' (TCRD follow-up): an unknown request field must 422, never
# be silently swallowed by pydantic's default extra='ignore' and degrade a
# filtered search into an unfiltered, id-ordered dump. This is a stopgap for
# EVERY field name typo, not just the `q` free-text one added alongside it.
# ---------------------------------------------------------------------------


def test_search_unknown_field_is_422_not_silently_ignored(facts_client):
    r = facts_client["client"].post(
        "/api/facts/search", json={"type": "person", "bogus": "nope"}, headers=_headers(facts_client)
    )
    assert r.status_code == 422


def test_neighbors_unknown_field_is_422_not_silently_ignored(facts_client):
    r = facts_client["client"].post(
        "/api/facts/neighbors", json={"subject_id": "f_x", "bogus": "nope"}, headers=_headers(facts_client)
    )
    assert r.status_code == 422


def test_search_accepts_q_field(facts_client):
    """`q` is a real, modeled field — not swallowed, not a 422 — it just
    can't get past the PG-only gate on this DuckDB-backed fixture."""
    r = facts_client["client"].post(
        "/api/facts/search", json={"type": "person", "q": "Alice"}, headers=_headers(facts_client)
    )
    assert r.status_code == 501, r.text


# ---------------------------------------------------------------------------
# type map — same three HTTP-level contracts as the rest of the router.
# (Counting/visibility depth lives in tests/db_pg/test_facts_read_pg.py.)
# ---------------------------------------------------------------------------


def test_type_map_404s_when_flag_off(seeded_app):
    r = seeded_app["client"].get("/api/facts/type-map", headers=_headers(seeded_app))
    assert r.status_code == 404


def test_type_map_requires_authentication(facts_client):
    r = facts_client["client"].get("/api/facts/type-map")
    assert r.status_code == 401


def test_type_map_fails_clean_on_duckdb(facts_client):
    r = facts_client["client"].get("/api/facts/type-map", headers=_headers(facts_client))
    assert r.status_code == 501, r.text
    body = r.json()
    assert body["error"] == "requires_postgres_backend"
    assert body["feature"] == "facts"


def test_facets_404s_when_flag_off(seeded_app):
    r = seeded_app["client"].get("/api/facts/facets", headers=_headers(seeded_app))
    assert r.status_code == 404


def test_facets_requires_authentication(facts_client):
    r = facts_client["client"].get("/api/facts/facets")
    assert r.status_code == 401


def test_facets_rejects_an_absurd_type_list_before_touching_the_repo(facts_client):
    """Validation runs ahead of the PG-only repo, so this 422s on DuckDB too
    rather than reaching the 501."""
    many = ",".join(f"t{i}" for i in range(13))
    r = facts_client["client"].get(f"/api/facts/facets?types={many}", headers=_headers(facts_client))
    assert r.status_code == 422, r.text


def test_facets_fails_clean_on_duckdb(facts_client):
    r = facts_client["client"].get("/api/facts/facets", headers=_headers(facts_client))
    assert r.status_code == 501, r.text
    assert r.json()["error"] == "requires_postgres_backend"
