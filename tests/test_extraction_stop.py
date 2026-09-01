"""``POST /api/admin/sharepoint/connections/{id}/extraction/stop`` —
cooperative stop (owner-frustration fix, 2026-09-01: "it's a black box, I
can't see what's happening in the extraction and I can't stop it").

The crawl-side cadence/drain mechanics (``_StopWatcher``, ``request_stop``,
``CrawlStopped``) are covered end to end in ``tests/test_sharepoint_crawler.py``.
This module covers the HTTP surface: RBAC, the 202 shape, the connection-row
write, the audit row (emitted by the fallback middleware — the handler writes
none of its own), and the ``activity``/``RESUMABLE_STOP_REASONS`` projection
rules in ``app/api/admin_extraction.py``.
"""

from __future__ import annotations

BASE = "/api/admin/sharepoint/connections"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _create_connection(client, token, *, name="corp-sharepoint-stop"):
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
        r = seeded_app["client"].post(f"{BASE}/nope/extraction/stop")
        assert r.status_code == 401

    def test_requires_admin(self, seeded_app):
        token = seeded_app["analyst_token"]
        r = seeded_app["client"].post(f"{BASE}/nope/extraction/stop", headers=_auth(token))
        assert r.status_code == 403


class TestUnknownConnection:
    def test_unknown_connection_is_404(self, seeded_app):
        client, token = seeded_app["client"], seeded_app["admin_token"]
        r = client.post(f"{BASE}/does-not-exist/extraction/stop", headers=_auth(token))
        assert r.status_code == 404
        assert r.json()["detail"] == "connection_not_found"

    def test_non_sharepoint_connection_is_404(self, seeded_app):
        client, token = seeded_app["client"], seeded_app["admin_token"]
        created = client.post(
            "/api/admin/source-connections",
            json={
                "name": "kbc-stop",
                "source_type": "keboola",
                "config": {"stack_url": "https://connection.example.com"},
            },
            headers=_auth(token),
        )
        assert created.status_code == 201, created.text
        r = client.post(f"{BASE}/{created.json()['id']}/extraction/stop", headers=_auth(token))
        assert r.status_code == 404


class TestStopRequest:
    def test_stop_returns_202_with_the_recorded_timestamp(self, seeded_app):
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token)

        r = client.post(f"{BASE}/{conn_id}/extraction/stop", headers=_auth(token))

        assert r.status_code == 202, r.text
        body = r.json()
        assert body["connection_id"] == conn_id
        assert body["stop_requested_at"]

    def test_the_flag_is_actually_written_to_the_connection_row(self, seeded_app):
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token)

        r = client.post(f"{BASE}/{conn_id}/extraction/stop", headers=_auth(token))
        stamp = r.json()["stop_requested_at"]

        from src.repositories import source_connections_repo

        row = source_connections_repo().get(conn_id)
        assert row["config"]["extraction"]["stop_requested_at"] == stamp

    def test_a_note_explains_run_liveness_cannot_be_checked_on_duckdb(self, seeded_app):
        """The default test backend is DuckDB, so `extraction_runs` (A3,
        PG-only) is unavailable — the stop itself must still succeed, with a
        note explaining the limitation rather than a 501."""
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token)

        r = client.post(f"{BASE}/{conn_id}/extraction/stop", headers=_auth(token))

        assert r.status_code == 202
        assert r.json()["note"]

    def test_a_second_stop_request_is_idempotent_not_an_error(self, seeded_app):
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token)

        first = client.post(f"{BASE}/{conn_id}/extraction/stop", headers=_auth(token))
        second = client.post(f"{BASE}/{conn_id}/extraction/stop", headers=_auth(token))

        assert first.status_code == 202
        assert second.status_code == 202

    def test_works_regardless_of_sharepoint_enabled(self, seeded_app, monkeypatch):
        """Unlike `app.api.admin_sharepoint`'s router, this module's surface
        (observability + this one control) is never gated on
        `sharepoint.enabled` — an admin can still stop a run left over from
        before the connector was disabled."""
        monkeypatch.delenv("AGNES_SHAREPOINT_ENABLED", raising=False)
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token, name="corp-sharepoint-stop-disabled")

        r = client.post(f"{BASE}/{conn_id}/extraction/stop", headers=_auth(token))

        assert r.status_code == 202


class TestAudit:
    def test_stop_is_audited_under_its_declared_action(self, seeded_app):
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token, name="corp-sharepoint-stop-audit")

        r = client.post(f"{BASE}/{conn_id}/extraction/stop", headers=_auth(token))
        assert r.status_code == 202

        from src.repositories import audit_repo

        rows, _ = audit_repo().query(action="extraction.stop_requested", limit=50)
        matches = [row for row in rows if conn_id in (row.get("resource") or "")]
        assert matches, "the fallback middleware did not audit the declared action"
        assert matches[0]["user_id"] == "admin1"


class TestCanStop:
    def test_status_reports_can_stop_true_on_the_pg_only_501(self, seeded_app):
        """`status` itself still 501s on DuckDB (extraction_runs is PG-only,
        A3) — `can_stop` is only meaningfully observed on Postgres — but the
        STOP endpoint must never be gated by that same limitation (covered
        above)."""
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token, name="corp-sharepoint-stop-canstop")

        r = client.get(f"{BASE}/{conn_id}/extraction/status", headers=_auth(token))
        assert r.status_code == 501
