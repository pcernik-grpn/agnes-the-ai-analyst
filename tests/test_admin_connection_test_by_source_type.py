"""``POST /api/admin/source-connections/{id}/test`` branches by source type.

The endpoint used to be Keboola-shaped for every connection: it required a
``stack_url`` and called ``GET {stack_url}/v2/storage/tokens/verify``
regardless of what the row actually pointed at. Testing a Snowflake
connection therefore failed with "invalid stack_url" — a message about a
field that source type does not even have, for a connection that may be
perfectly healthy.

Now:

- ``keboola`` — unchanged (the token-verify probe, pinned here so the
  branch cannot silently swallow it).
- ``snowflake`` — a real, cheap connectivity check through the connector's
  own credential resolution + host-allowlist gate
  (``connectors.snowflake.discovery.probe_connection``).
- anything else — an honest ``{"ok": false, "status": "unsupported",
  "detail": ...}`` naming the type, rather than a Keboola-shaped failure.

The endpoint reports failure as HTTP 200 with ``ok: false`` (only a missing
connection is a status code — 404); the unsupported answer follows that
same convention so every existing caller keeps reading it.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

BASE = "/api/admin/source-connections"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _create(client, token: str, **payload) -> str:
    resp = client.post(BASE, json=payload, headers=_auth(token))
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


@pytest.fixture
def keboola_conn(seeded_app):
    return _create(
        seeded_app["client"],
        seeded_app["admin_token"],
        name="kbc",
        source_type="keboola",
        config={"stack_url": "https://connection.example.com"},
        token_env="KEBOOLA_STORAGE_TOKEN",
    )


@pytest.fixture
def snowflake_conn(seeded_app):
    return _create(
        seeded_app["client"],
        seeded_app["admin_token"],
        name="sf",
        source_type="snowflake",
        config={
            "account": "acct-123",
            "user": "SVC",
            "database": "PROD",
            "warehouse": "WH",
        },
    )


@pytest.fixture
def databricks_conn(seeded_app):
    return _create(
        seeded_app["client"],
        seeded_app["admin_token"],
        name="dbx",
        source_type="databricks",
        config={"host": "https://example.cloud.databricks.com", "warehouse_id": "abc123"},
    )


class TestKeboolaIsUnchanged:
    def test_it_still_verifies_the_storage_token(self, seeded_app, keboola_conn):
        c = seeded_app["client"]
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"owner": {"id": 7, "name": "Test Project"}}
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_response)

        with (
            patch("app.api.admin_source_connections.httpx.AsyncClient", return_value=mock_client),
            patch("app.api.admin._validate_url_not_private", return_value=None),
            patch.dict("os.environ", {"KEBOOLA_STORAGE_TOKEN": "fake-token"}),
        ):
            r = c.post(f"{BASE}/{keboola_conn}/test", headers=_auth(seeded_app["admin_token"]))

        assert r.status_code == 200, r.text
        assert r.json() == {"ok": True, "project_name": "Test Project"}
        assert mock_client.get.call_args[0][0].endswith("/v2/storage/tokens/verify")


class TestSnowflake:
    def test_a_reachable_account_reports_ok(self, seeded_app, snowflake_conn, monkeypatch):
        seen: dict = {}

        def _probe(connection=None):
            seen["connection"] = connection
            return {"account": "acct-123", "database": "PROD", "warehouse": "WH"}

        monkeypatch.setattr("connectors.snowflake.discovery.probe_connection", _probe)
        c = seeded_app["client"]

        r = c.post(f"{BASE}/{snowflake_conn}/test", headers=_auth(seeded_app["admin_token"]))

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        assert body["project_name"] == "acct-123/PROD"
        # The probe is handed THIS connection's row, not the instance default —
        # otherwise "Test" would report on whichever connection happens to be
        # the default rather than the one the admin clicked.
        assert seen["connection"]["id"] == snowflake_conn

    def test_an_unconfigured_connection_says_so(self, seeded_app, snowflake_conn, monkeypatch):
        monkeypatch.setattr("connectors.snowflake.discovery.probe_connection", lambda connection=None: None)
        c = seeded_app["client"]

        r = c.post(f"{BASE}/{snowflake_conn}/test", headers=_auth(seeded_app["admin_token"]))

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is False
        assert "credential" in body["error"]

    def test_a_driver_failure_is_classified_not_echoed(self, seeded_app, snowflake_conn, monkeypatch):
        """The raw driver text can carry SQLSTATE/request ids; the caller gets
        the one-sentence classification the browse endpoint already uses."""

        def _boom(connection=None):
            raise RuntimeError("Incorrect username or password was specified (390100)")

        monkeypatch.setattr("connectors.snowflake.discovery.probe_connection", _boom)
        c = seeded_app["client"]

        r = c.post(f"{BASE}/{snowflake_conn}/test", headers=_auth(seeded_app["admin_token"]))

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is False
        assert "390100" not in body["error"]
        assert body["error"]

    def test_a_host_outside_the_allowlist_is_reported_verbatim(self, seeded_app, snowflake_conn, monkeypatch):
        """An operator misconfiguration, not an upstream fault — the message
        names the allowlist so it points somewhere useful."""

        def _refuse(connection=None):
            raise ValueError("Snowflake host is not in AGNES_REMOTE_ATTACH_HOST_ALLOWLIST")

        monkeypatch.setattr("connectors.snowflake.discovery.probe_connection", _refuse)
        c = seeded_app["client"]

        r = c.post(f"{BASE}/{snowflake_conn}/test", headers=_auth(seeded_app["admin_token"]))

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is False
        assert "ALLOWLIST" in body["error"]

    def test_it_never_asks_for_a_stack_url(self, seeded_app, snowflake_conn, monkeypatch):
        """The regression this whole branch exists for: a Snowflake row has no
        `stack_url`, and the Keboola-shaped handler failed on that field."""
        monkeypatch.setattr(
            "connectors.snowflake.discovery.probe_connection",
            lambda connection=None: {"account": "a", "database": "d", "warehouse": "w"},
        )
        c = seeded_app["client"]

        r = c.post(f"{BASE}/{snowflake_conn}/test", headers=_auth(seeded_app["admin_token"]))

        assert r.status_code == 200, r.text
        assert "stack_url" not in r.text


class TestUnsupportedSourceType:
    def test_databricks_gets_an_honest_answer(self, seeded_app, databricks_conn):
        c = seeded_app["client"]

        r = c.post(f"{BASE}/{databricks_conn}/test", headers=_auth(seeded_app["admin_token"]))

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is False
        assert body["status"] == "unsupported"
        assert body["detail"] == "connection test is not implemented for databricks yet"
        # No Keboola vocabulary in an answer about a Databricks connection.
        assert "stack_url" not in r.text
        assert "token" not in r.text


class TestMissingConnection:
    def test_unknown_id_is_still_a_404(self, seeded_app):
        c = seeded_app["client"]
        r = c.post(f"{BASE}/nope/test", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 404, r.text
