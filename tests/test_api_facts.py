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
def facts_client(seeded_app, duckdb_backend_pinned, monkeypatch):
    # `duckdb_backend_pinned`: the `*_fails_clean_on_duckdb` tests below must
    # resolve DuckDB regardless of a `tests/db_pg/` test having run earlier
    # in this worker process (issue #1658) — see `tests/_backend_pin.py`.
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


# ---------------------------------------------------------------------------
# edges (TCRD-295) — the relationship-shaped read; same three HTTP-level
# contracts as its siblings, plus the new request caps. Visibility depth lives
# in tests/db_pg/test_facts_edges_pg.py.
# ---------------------------------------------------------------------------


def test_edges_404s_when_flag_off(seeded_app):
    r = seeded_app["client"].post("/api/facts/edges", json={"edge_type": "owned_by"}, headers=_headers(seeded_app))
    assert r.status_code == 404


def test_edges_requires_authentication(facts_client):
    r = facts_client["client"].post("/api/facts/edges", json={"edge_type": "owned_by"})
    assert r.status_code == 401


def test_edges_fails_clean_on_duckdb(facts_client):
    r = facts_client["client"].post("/api/facts/edges", json={"edge_type": "owned_by"}, headers=_headers(facts_client))
    assert r.status_code == 501, r.text
    assert r.json()["error"] == "requires_postgres_backend"


def test_edges_requires_edge_type(facts_client):
    r = facts_client["client"].post("/api/facts/edges", json={}, headers=_headers(facts_client))
    assert r.status_code == 422


def test_edges_limit_over_100_is_422(facts_client):
    r = facts_client["client"].post(
        "/api/facts/edges", json={"edge_type": "owned_by", "limit": 101}, headers=_headers(facts_client)
    )
    assert r.status_code == 422


def test_edges_include_claims_over_3_is_422(facts_client):
    r = facts_client["client"].post(
        "/api/facts/edges", json={"edge_type": "owned_by", "include_claims": 4}, headers=_headers(facts_client)
    )
    assert r.status_code == 422


def test_edges_extend_from_must_name_an_endpoint(facts_client):
    r = facts_client["client"].post(
        "/api/facts/edges",
        json={"edge_type": "owned_by", "extend_edge_type": "in_industry", "extend_from": "middle"},
        headers=_headers(facts_client),
    )
    assert r.status_code == 422


def test_edges_unknown_field_is_422_not_silently_ignored(facts_client):
    r = facts_client["client"].post(
        "/api/facts/edges", json={"edge_type": "owned_by", "bogus": 1}, headers=_headers(facts_client)
    )
    assert r.status_code == 422


def test_neighbors_include_claims_over_3_is_422(facts_client):
    r = facts_client["client"].post(
        "/api/facts/neighbors", json={"subject_id": "f_x", "include_claims": 4}, headers=_headers(facts_client)
    )
    assert r.status_code == 422


def test_search_statement_timeout_is_a_typed_hinted_error(seeded_app, monkeypatch):
    """A `FactsQueryTimeout` from the repository (the statement outlived
    its Postgres statement timeout) is translated to a typed `504` whose
    `detail` carries a stable `reason` and the repository's own forward-
    pointing `hint` — never an unhandled `500` with the driver message. The
    CLI's `render_error` already pretty-prints `{"reason", "hint"}` dicts,
    so the same body reads well on every surface."""
    monkeypatch.setenv("AGNES_FACTS_ENABLED", "1")
    import app.api.facts as facts_mod
    from src.repositories.facts_pg import FactsQueryTimeout

    class _FakeRepo:
        def search(self, user, **kwargs):
            raise FactsQueryTimeout("The fact search did not finish in time. Narrow `q` to a longer name.")

    monkeypatch.setattr(facts_mod, "facts_repo", lambda: _FakeRepo())

    r = seeded_app["client"].post(
        "/api/facts/search", json={"type": "person", "q": "llr"}, headers=_headers(seeded_app)
    )
    assert r.status_code == 504, r.text
    detail = r.json()["detail"]
    assert detail["reason"] == "facts_search_timeout"
    assert "Narrow `q`" in detail["hint"]


def test_search_include_claims_over_3_is_422(facts_client):
    r = facts_client["client"].post("/api/facts/search", json={"include_claims": 4}, headers=_headers(facts_client))
    assert r.status_code == 422


def test_claims_limit_over_200_is_422(facts_client):
    r = facts_client["client"].get("/api/facts/f_x/claims?limit=201", headers=_headers(facts_client))
    assert r.status_code == 422


def test_claims_limit_zero_is_422(facts_client):
    r = facts_client["client"].get("/api/facts/f_x/claims?limit=0", headers=_headers(facts_client))
    assert r.status_code == 422
