"""REST-layer tests for the fact graph over Collections write surface
(build order step 4 of
docs/superpowers/specs/2026-08-27-fact-graph-over-collections-design.md):
``POST /api/facts/ingest``, ``PUT/DELETE /api/facts/corrections/{kind}/{id}``,
``GET /api/facts/corrections``.

DuckDB backend (the default ``seeded_app`` fixture) — proves the HTTP
wiring: the router-level feature-flag gate, the admin/scheduler-token auth
gate, request validation shape, and the PG-only typed-501 fail-clean shape.
Every assertion here resolves BEFORE the PG-only ``facts_repo()`` call, so
none of it needs a real Postgres backend. Batch-cap enforcement,
itemization, corrections CRUD round-trips, the verbatim gate, the
collections-delete sweep hook, and the real upload -> ingest -> search
end-to-end path all need a genuine Postgres backend (``pg_engine`` is a
``tests/db_pg/`` fixture, not visible here) and live in
``tests/db_pg/test_facts_ingest_pg.py`` instead — same split
``tests/test_api_facts.py`` / ``tests/db_pg/test_facts_read_pg.py`` use for
the read surface.
"""

from __future__ import annotations

import pytest


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def facts_client(seeded_app, monkeypatch):
    monkeypatch.setenv("AGNES_FACTS_ENABLED", "1")
    return seeded_app


# ---------------------------------------------------------------------------
# flag gate — the whole router (including the new routes) disappears when off.
# ---------------------------------------------------------------------------


def test_ingest_404s_when_flag_off(seeded_app):
    r = seeded_app["client"].post("/api/facts/ingest", json={}, headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 404


def test_corrections_get_404s_when_flag_off(seeded_app):
    r = seeded_app["client"].get("/api/facts/corrections", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 404


def test_corrections_put_404s_when_flag_off(seeded_app):
    r = seeded_app["client"].put(
        "/api/facts/corrections/fact/f_x",
        json={"verdict": "wrong", "reason": "x"},
        headers=_auth(seeded_app["admin_token"]),
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# auth — anon 401, non-admin PAT 403, admin/scheduler pass the gate.
# ---------------------------------------------------------------------------


def test_ingest_requires_authentication(facts_client):
    r = facts_client["client"].post("/api/facts/ingest", json={})
    assert r.status_code == 401


def test_ingest_requires_admin_not_just_any_caller(facts_client):
    r = facts_client["client"].post("/api/facts/ingest", json={}, headers=_auth(facts_client["analyst_token"]))
    assert r.status_code == 403


def test_corrections_crud_requires_admin(facts_client):
    headers = _auth(facts_client["analyst_token"])
    put = facts_client["client"].put(
        "/api/facts/corrections/fact/f_x", json={"verdict": "wrong", "reason": "x"}, headers=headers
    )
    assert put.status_code == 403
    delete = facts_client["client"].delete("/api/facts/corrections/fact/f_x", headers=headers)
    assert delete.status_code == 403
    get = facts_client["client"].get("/api/facts/corrections", headers=headers)
    assert get.status_code == 403


def test_corrections_get_anon_401(facts_client):
    r = facts_client["client"].get("/api/facts/corrections")
    assert r.status_code == 401


def test_scheduler_token_passes_the_admin_gate_on_ingest(facts_client, monkeypatch):
    """The scheduler shared-secret resolves to the synthetic Admin-group
    user through get_current_user (app/auth/scheduler_token.py) — same
    dual-accept pattern app/api/jobs.py documents. Proven by NOT getting
    401/403; DuckDB backend still 501s past the gate (facts_repo() is
    PG-only), which is the assertion that the gate itself was cleared."""
    secret = "x" * 40
    monkeypatch.setenv("SCHEDULER_API_TOKEN", secret)
    r = facts_client["client"].post("/api/facts/ingest", json={}, headers=_auth(secret))
    assert r.status_code == 501, r.text
    assert r.json()["error"] == "requires_postgres_backend"


def test_scheduler_token_passes_the_admin_gate_on_corrections_export(facts_client, monkeypatch):
    secret = "y" * 40
    monkeypatch.setenv("SCHEDULER_API_TOKEN", secret)
    r = facts_client["client"].get("/api/facts/corrections", headers=_auth(secret))
    assert r.status_code == 501, r.text


# ---------------------------------------------------------------------------
# PG-only fail-clean on DuckDB.
# ---------------------------------------------------------------------------


def test_ingest_fails_clean_on_duckdb(facts_client):
    r = facts_client["client"].post("/api/facts/ingest", json={}, headers=_auth(facts_client["admin_token"]))
    assert r.status_code == 501, r.text
    assert r.json()["error"] == "requires_postgres_backend"


def test_corrections_put_fails_clean_on_duckdb(facts_client):
    r = facts_client["client"].put(
        "/api/facts/corrections/fact/f_x",
        json={"verdict": "wrong", "reason": "hallucinated"},
        headers=_auth(facts_client["admin_token"]),
    )
    assert r.status_code == 501, r.text


# ---------------------------------------------------------------------------
# request validation shape (runs before the PG-only repo is ever reached).
# ---------------------------------------------------------------------------


def test_corrections_put_invalid_verdict_is_422(facts_client):
    r = facts_client["client"].put(
        "/api/facts/corrections/fact/f_x",
        json={"verdict": "maybe", "reason": "x"},
        headers=_auth(facts_client["admin_token"]),
    )
    assert r.status_code == 422


def test_corrections_put_invalid_subject_kind_is_422(facts_client):
    r = facts_client["client"].put(
        "/api/facts/corrections/widget/f_x",
        json={"verdict": "wrong", "reason": "x"},
        headers=_auth(facts_client["admin_token"]),
    )
    assert r.status_code == 422


def test_corrections_put_empty_reason_is_422(facts_client):
    r = facts_client["client"].put(
        "/api/facts/corrections/fact/f_x",
        json={"verdict": "wrong", "reason": ""},
        headers=_auth(facts_client["admin_token"]),
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# GET /api/facts/ingest-runs — persisted run reports (spec §7.2/§13.2), the
# source card's data. Same DuckDB-side split as every other route on this
# router: flag/auth/fail-clean here, real reads in
# tests/db_pg/test_facts_ingest_runs_pg.py + the E2E extension in
# tests/db_pg/test_facts_ingest_pg.py.
# ---------------------------------------------------------------------------


def test_ingest_runs_404s_when_flag_off(seeded_app):
    r = seeded_app["client"].get("/api/facts/ingest-runs", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 404


def test_ingest_runs_anon_401(facts_client):
    r = facts_client["client"].get("/api/facts/ingest-runs")
    assert r.status_code == 401


def test_ingest_runs_requires_admin_not_just_any_caller(facts_client):
    r = facts_client["client"].get("/api/facts/ingest-runs", headers=_auth(facts_client["analyst_token"]))
    assert r.status_code == 403


def test_ingest_runs_fails_clean_on_duckdb(facts_client):
    r = facts_client["client"].get("/api/facts/ingest-runs", headers=_auth(facts_client["admin_token"]))
    assert r.status_code == 501, r.text
    assert r.json()["error"] == "requires_postgres_backend"


def test_ingest_runs_invalid_limit_is_422(facts_client):
    r = facts_client["client"].get(
        "/api/facts/ingest-runs", params={"limit": 0}, headers=_auth(facts_client["admin_token"])
    )
    assert r.status_code == 422
