"""POST /api/admin/register-table — connection_id acceptance and validation.

Tests the optional ``connection_id`` field added to the register-table endpoint
and the CLI command.  Covers:

- registering without connection_id → 201 (baseline, no regression)
- registering with a valid connection_id → 201
- registering with an unknown connection_id → 400
"""

from __future__ import annotations

import pytest


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _base_payload(name: str = "my_table") -> dict:
    return {
        "name": name,
        "source_type": "keboola",
        "bucket": "in.c-main",
        "source_table": "events",
        "query_mode": "local",
    }


def _sf_payload(name: str = "sf_table", **overrides) -> dict:
    p = {
        "name": name,
        "source_type": "snowflake",
        "bucket": "public",
        "source_table": "orders",
        "query_mode": "remote",
    }
    p.update(overrides)
    return p


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def _conn_id(seeded_app) -> str:
    """Create a source connection and return its id."""
    from src.repositories import source_connections_repo

    repo = source_connections_repo()
    cid = "conn-test-abc123"
    repo.create(
        id=cid,
        name="test-keboola-conn",
        source_type="keboola",
        config={"stack_url": "https://connection.keboola.com"},
        token_env="KEBOOLA_STORAGE_TOKEN",
        is_default=False,
    )
    return cid


@pytest.fixture
def _sf_conn_id(seeded_app) -> str:
    """Create a Snowflake source connection and return its id."""
    from src.repositories import source_connections_repo

    repo = source_connections_repo()
    cid = "conn-test-sf001"
    repo.create(
        id=cid,
        name="test-snowflake-conn",
        source_type="snowflake",
        config={"account": "xy12345"},
        token_env="SNOWFLAKE_PASSWORD",
        is_default=False,
    )
    return cid


@pytest.fixture
def snowflake_instance(monkeypatch):
    """Patch instance.yaml so Snowflake settings resolve, mirroring
    tests/test_snowflake_connector.py's fixture of the same name."""
    cfg = {
        "data_source": {
            "type": "snowflake",
            "snowflake": {
                "account": "xy12345",
                "user": "alice",
                "database": "analytics",
                "warehouse": "compute_wh",
                "role": "analyst",
                "token_env": "SNOWFLAKE_PASSWORD",
            },
        },
    }
    monkeypatch.setattr("app.instance_config.load_instance_config", lambda: cfg, raising=False)
    monkeypatch.setenv("SNOWFLAKE_PASSWORD", "secret")
    from app.instance_config import reset_cache

    reset_cache()
    yield cfg
    reset_cache()


@pytest.fixture
def stub_snowflake_extract(monkeypatch):
    from unittest.mock import MagicMock

    rebuild = MagicMock(return_value={"tables_registered": 1, "errors": [], "skipped": False})
    monkeypatch.setattr("connectors.snowflake.extract_init.rebuild_from_registry", rebuild)
    return rebuild


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_register_table_without_connection_id(seeded_app):
    """Baseline: register without connection_id still returns 201."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]

    r = c.post(
        "/api/admin/register-table",
        json=_base_payload("no_conn_table"),
        headers=_auth(token),
    )
    assert r.status_code == 201
    data = r.json()
    assert data["id"] == "no_conn_table"


def test_register_table_with_valid_connection_id(seeded_app, _conn_id):
    """Providing a connection_id that exists → 201; id round-trips in response."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]

    payload = _base_payload("with_conn_table")
    payload["connection_id"] = _conn_id

    r = c.post(
        "/api/admin/register-table",
        json=payload,
        headers=_auth(token),
    )
    assert r.status_code == 201
    assert r.json()["id"] == "with_conn_table"


def test_register_table_with_unknown_connection_id(seeded_app):
    """Providing a connection_id that does not exist → 400."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]

    payload = _base_payload("bad_conn_table")
    payload["connection_id"] = "does-not-exist"

    r = c.post(
        "/api/admin/register-table",
        json=payload,
        headers=_auth(token),
    )
    assert r.status_code == 400
    assert "does-not-exist" in r.json().get("detail", "")


# ---------------------------------------------------------------------------
# Snowflake — the acceptance-critical regression coverage. Every Snowflake
# registration path (CLI, web UI) previously left connection_id NULL; these
# prove the field round-trips through POST /api/admin/register-table for
# Snowflake specifically, the same way it already did for Keboola above.
# ---------------------------------------------------------------------------


def test_register_snowflake_table_without_connection_id(seeded_app, snowflake_instance, stub_snowflake_extract):
    """Baseline: register without connection_id still returns 201."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]

    r = c.post(
        "/api/admin/register-table",
        json=_sf_payload("sf_no_conn_table"),
        headers=_auth(token),
    )
    assert r.status_code == 201
    assert r.json()["id"] == "sf_no_conn_table"


def test_register_snowflake_table_with_valid_connection_id(
    seeded_app, snowflake_instance, stub_snowflake_extract, _sf_conn_id
):
    """A valid connection_id round-trips into the persisted row — the
    regression this PR fixes: pre-fix, no Snowflake registration path ever
    sent connection_id, so it was always NULL regardless of what the admin
    picked."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]

    payload = _sf_payload("sf_with_conn_table")
    payload["connection_id"] = _sf_conn_id

    r = c.post(
        "/api/admin/register-table",
        json=payload,
        headers=_auth(token),
    )
    assert r.status_code == 201
    assert r.json()["id"] == "sf_with_conn_table"

    from src.repositories import table_registry_repo

    row = table_registry_repo().get("sf_with_conn_table")
    assert row is not None
    assert row["connection_id"] == _sf_conn_id


def test_register_snowflake_table_with_unknown_connection_id(seeded_app, snowflake_instance):
    """Providing a connection_id that does not exist → 400, same as Keboola."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]

    payload = _sf_payload("sf_bad_conn_table")
    payload["connection_id"] = "does-not-exist"

    r = c.post(
        "/api/admin/register-table",
        json=payload,
        headers=_auth(token),
    )
    assert r.status_code == 400
    assert "does-not-exist" in r.json().get("detail", "")


# ---------------------------------------------------------------------------
# PUT /api/admin/registry/{id} — fixing an existing NULL connection_id row
# without delete + recreate.
# ---------------------------------------------------------------------------


def test_update_table_sets_connection_id_on_null_row(
    seeded_app, snowflake_instance, stub_snowflake_extract, _sf_conn_id
):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]

    r = c.post(
        "/api/admin/register-table",
        json=_sf_payload("sf_fix_conn_table"),
        headers=_auth(token),
    )
    assert r.status_code == 201

    from src.repositories import table_registry_repo

    assert table_registry_repo().get("sf_fix_conn_table")["connection_id"] is None

    r = c.put(
        "/api/admin/registry/sf_fix_conn_table",
        json={"connection_id": _sf_conn_id},
        headers=_auth(token),
    )
    assert r.status_code == 200
    assert table_registry_repo().get("sf_fix_conn_table")["connection_id"] == _sf_conn_id


def test_update_table_with_unknown_connection_id(seeded_app, snowflake_instance, stub_snowflake_extract):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]

    r = c.post(
        "/api/admin/register-table",
        json=_sf_payload("sf_fix_conn_bad"),
        headers=_auth(token),
    )
    assert r.status_code == 201

    r = c.put(
        "/api/admin/registry/sf_fix_conn_bad",
        json={"connection_id": "does-not-exist"},
        headers=_auth(token),
    )
    assert r.status_code == 400
    assert "does-not-exist" in r.json().get("detail", "")
