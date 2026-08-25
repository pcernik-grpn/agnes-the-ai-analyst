"""Parity test: POST /auth/bootstrap grants real Admin-group membership on both
backends.

`/auth/bootstrap` (the first-time-setup wizard's submit) creates the first admin
and adds them to the Admin system group. It looked the group up with a raw
`SELECT id FROM user_groups WHERE name=?` on the always-DuckDB `_get_db`
connection — on a Postgres instance that returned the DuckDB Admin-group id, and
the membership row written to PG referenced an id absent from PG, so the
bootstrapped first admin had NO admin access. The lookup now goes through
`user_groups_repo().get_by_name(...)`.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def _fresh_app(state_backend, tmp_path, monkeypatch, shared_app):
    """A fresh app (no users) on the active backend. System groups exist
    (DuckDB seeds them on connect; the PG fixture pre-seeds them), so bootstrap
    can find Admin to grant membership."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-32chars-minimum!!!!!")
    for sub in ("extracts", "analytics", "state", "notifications"):
        (tmp_path / sub).mkdir(exist_ok=True)
    if state_backend == "duckdb":
        from src.db import close_system_db, get_system_db

        close_system_db()
        get_system_db()

    from fastapi.testclient import TestClient


    return TestClient(shared_app), state_backend


def test_bootstrap_grants_admin_on_both_backends(_fresh_app):
    client, backend = _fresh_app

    resp = client.post(
        "/auth/bootstrap",
        json={"email": "boot@example.com", "name": "Boot", "password": "pw-min-8-chars"},
    )
    assert resp.status_code == 200, f"[{backend}] bootstrap failed: {resp.text}"
    body = resp.json()
    assert body["role"] == "admin"
    user_id = body["user_id"]

    # The membership must actually resolve as admin on the active backend.
    from app.auth.access import is_user_admin

    assert is_user_admin(user_id) is True, (
        f"[{backend}] bootstrapped user is not in the Admin group — the group "
        f"lookup wrote a membership the backend can't resolve."
    )
