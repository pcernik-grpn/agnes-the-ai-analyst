"""Tests for `GET /api/memory/admin/detection-runs` (issue #1971 Part 3) on
the DuckDB backend — the RBAC gate and the PG-first ratchet's typed 501
clean-fail. The happy-path (200, real rows, pagination) needs Postgres and
lives in tests/db_pg/test_memory_detection_runs_api_pg.py.
"""


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_requires_admin(seeded_app):
    client = seeded_app["client"]
    r = client.get("/api/memory/admin/detection-runs", headers=_auth(seeded_app["analyst_token"]))
    assert r.status_code == 403


def test_requires_auth_at_all(seeded_app):
    client = seeded_app["client"]
    r = client.get("/api/memory/admin/detection-runs")
    assert r.status_code in (401, 403)


def test_fails_clean_not_crashing_on_duckdb_backend(seeded_app):
    """PG-first ratchet (A3): memory_detection_runs is Postgres-only, so an
    admin caller on a DuckDB-backed instance gets a TYPED 501, never a raw
    500 and never a misleading 200 with fabricated data."""
    client = seeded_app["client"]
    r = client.get("/api/memory/admin/detection-runs", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 501
    body = r.json()
    assert body["error"] == "requires_postgres_backend"
