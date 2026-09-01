"""`GET /api/memory/admin/detection-runs` — the Postgres happy path (issue
#1971 Part 3).

The DuckDB half of this route (admin gating, the typed
``501 requires_postgres_backend``) is
``tests/test_memory_detection_runs_api.py``. This file is the other side of
that fork: with the PG backend actually present, recorded runs must reach
the admin panel through the API in the shape it renders.
"""

from __future__ import annotations

from datetime import datetime, timezone


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _pg_client(tmp_path, monkeypatch, pg_engine):
    from tests.db_pg._parity_sweep_util import build_seeded_client

    return build_seeded_client("pg", tmp_path, monkeypatch, pg_engine)


def test_empty_before_any_run_recorded(tmp_path, monkeypatch, pg_engine):
    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    r = client.get("/api/memory/admin/detection-runs", headers=_auth(token))
    assert r.status_code == 200
    body = r.json()
    assert body["runs"] == []
    assert body["total"] == 0


def test_recorded_runs_surface_newest_first(tmp_path, monkeypatch, pg_engine):
    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)

    from src.repositories import memory_detection_runs_repo

    repo = memory_detection_runs_repo()
    for i in range(3):
        repo.create(
            source="session_transcripts",
            started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
            sessions_scanned=1,
            items_proposed=i,
        )

    r = client.get("/api/memory/admin/detection-runs", headers=_auth(token))
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 3
    # newest first: the last-created row (items_proposed=2) comes first
    assert body["runs"][0]["items_proposed"] == 2


def test_pagination_params_are_honored(tmp_path, monkeypatch, pg_engine):
    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)

    from src.repositories import memory_detection_runs_repo

    repo = memory_detection_runs_repo()
    for _ in range(5):
        repo.create(source="claude_local_md", started_at=datetime.now(timezone.utc))

    r = client.get("/api/memory/admin/detection-runs?page=1&per_page=2", headers=_auth(token))
    body = r.json()
    assert len(body["runs"]) == 2
    assert body["total"] == 5
    assert body["page"] == 1
    assert body["per_page"] == 2


def test_non_admin_still_gets_403_on_pg_backend(tmp_path, monkeypatch, pg_engine):
    from app.auth.jwt import create_access_token

    client, _admin_token = _pg_client(tmp_path, monkeypatch, pg_engine)
    analyst_token = create_access_token("analyst1", "analyst@test.com")
    r = client.get("/api/memory/admin/detection-runs", headers=_auth(analyst_token))
    assert r.status_code == 403
