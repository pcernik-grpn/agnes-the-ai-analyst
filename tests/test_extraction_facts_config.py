"""``PATCH /api/admin/sharepoint/connections/{id}/extraction/facts-config`` —
per-connection override for ``extraction.facts.retry_mode`` (cost-levers
task, lever A). One high-value connection can keep the corrective retry ON
(a dropped quote there is a lost citation on stage) while a long-tail
connection runs with it OFF, without an instance.yaml edit that would flip
every connection at once.

The resolver itself (``connectors.sharepoint.facts_extraction.
resolve_retry_mode`` — connection override wins, absent falls back to the
instance default) is unit-tested in ``tests/test_facts_extraction.py``; a
full behavioral end-to-end (an actual extra retry call driven by the
connection's own config) lives in ``tests/db_pg/test_facts_extraction_pg.py``.
This module covers the HTTP surface: RBAC, the write, the resolved-value
response shape, validation, and the audit row.
"""

from __future__ import annotations

BASE = "/api/admin/sharepoint/connections"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _create_connection(client, token, *, name="corp-sharepoint-facts-config"):
    resp = client.post(
        "/api/admin/source-connections",
        json={
            "name": name,
            "source_type": "sharepoint",
            "config": {"tenant_id": "tenant-1", "client_id": "client-1"},
        },
        headers=_auth(token),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


class TestAuthGating:
    def test_requires_auth(self, seeded_app):
        r = seeded_app["client"].patch(f"{BASE}/nope/extraction/facts-config", json={"retry_mode": "off"})
        assert r.status_code == 401

    def test_requires_admin(self, seeded_app):
        token = seeded_app["analyst_token"]
        r = seeded_app["client"].patch(
            f"{BASE}/nope/extraction/facts-config", json={"retry_mode": "off"}, headers=_auth(token)
        )
        assert r.status_code == 403


class TestUnknownConnection:
    def test_unknown_connection_is_404(self, seeded_app):
        client, token = seeded_app["client"], seeded_app["admin_token"]
        r = client.patch(
            f"{BASE}/does-not-exist/extraction/facts-config", json={"retry_mode": "off"}, headers=_auth(token)
        )
        assert r.status_code == 404
        assert r.json()["detail"] == "connection_not_found"

    def test_non_sharepoint_connection_is_404(self, seeded_app):
        client, token = seeded_app["client"], seeded_app["admin_token"]
        created = client.post(
            "/api/admin/source-connections",
            json={
                "name": "kbc-facts-config",
                "source_type": "keboola",
                "config": {"stack_url": "https://connection.example.com"},
            },
            headers=_auth(token),
        )
        assert created.status_code == 201, created.text
        r = client.patch(
            f"{BASE}/{created.json()['id']}/extraction/facts-config",
            json={"retry_mode": "off"},
            headers=_auth(token),
        )
        assert r.status_code == 404


class TestPatch:
    def test_setting_the_override_returns_it_as_the_resolved_source(self, seeded_app):
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token)

        r = client.patch(
            f"{BASE}/{conn_id}/extraction/facts-config", json={"retry_mode": "always"}, headers=_auth(token)
        )

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["connection_id"] == conn_id
        assert body["retry_mode"] == {"value": "always", "source": "connection"}

    def test_the_override_is_actually_written_to_the_connection_row(self, seeded_app):
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token, name="corp-sharepoint-facts-config-write")

        client.patch(f"{BASE}/{conn_id}/extraction/facts-config", json={"retry_mode": "off"}, headers=_auth(token))

        from src.repositories import source_connections_repo

        row = source_connections_repo().get(conn_id)
        assert row["config"]["extraction"]["facts"]["retry_mode"] == "off"
        # A shallow PATCH of `extraction` must not clobber sibling connect-
        # wizard config already on the row.
        assert row["config"]["tenant_id"] == "tenant-1"

    def test_a_null_retry_mode_clears_a_previously_set_override(self, seeded_app):
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token, name="corp-sharepoint-facts-config-clear")
        client.patch(f"{BASE}/{conn_id}/extraction/facts-config", json={"retry_mode": "off"}, headers=_auth(token))

        r = client.patch(f"{BASE}/{conn_id}/extraction/facts-config", json={}, headers=_auth(token))

        assert r.status_code == 200, r.text
        assert r.json()["retry_mode"] == {"value": "on_gate_fail", "source": "instance"}

        from src.repositories import source_connections_repo

        row = source_connections_repo().get(conn_id)
        assert "facts" not in (row["config"].get("extraction") or {})

    def test_an_invalid_retry_mode_is_refused_with_422(self, seeded_app):
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token, name="corp-sharepoint-facts-config-invalid")

        r = client.patch(
            f"{BASE}/{conn_id}/extraction/facts-config", json={"retry_mode": "sometimes"}, headers=_auth(token)
        )

        assert r.status_code == 422

    def test_works_regardless_of_sharepoint_enabled(self, seeded_app, monkeypatch):
        monkeypatch.delenv("AGNES_SHAREPOINT_ENABLED", raising=False)
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token, name="corp-sharepoint-facts-config-disabled")

        r = client.patch(f"{BASE}/{conn_id}/extraction/facts-config", json={"retry_mode": "off"}, headers=_auth(token))

        assert r.status_code == 200


class TestAudit:
    def test_the_patch_is_audited_with_the_value_and_its_resolution(self, seeded_app):
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token, name="corp-sharepoint-facts-config-audit")

        r = client.patch(
            f"{BASE}/{conn_id}/extraction/facts-config", json={"retry_mode": "always"}, headers=_auth(token)
        )
        assert r.status_code == 200

        import json

        from src.repositories import audit_repo

        rows, _ = audit_repo().query(action="extraction.facts_retry_mode_set", limit=50)
        matches = [row for row in rows if conn_id in (row.get("resource") or "")]
        assert matches, "the handler's own log_safe row is missing"
        assert matches[0]["user_id"] == "admin1"
        raw_params = matches[0]["params"]
        params = json.loads(raw_params) if isinstance(raw_params, str) else raw_params
        assert params["retry_mode"] == "always"
        assert params["resolved"] == "always"
        assert params["source"] == "connection"
