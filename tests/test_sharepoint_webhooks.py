"""Microsoft Graph change-notification receiver — `POST /api/webhooks/
sharepoint/{connection_id}` (`app/api/sharepoint_webhooks.py`).

Covers: the validation handshake (echo verbatim, no side effects, no
connection lookup), constant-time `clientState` verification (bad state
dropped silently, `202` regardless — never an existence/secret oracle),
the `corpus-extraction` enqueue on a verified notification (shared
idempotency key + debounced `run_after`, so a burst collapses onto one
job), the `extraction_webhook.enabled` feature gate (`404` when off), the
hard request-size cap, and secret rotation invalidating the old
`clientState`.
"""

from __future__ import annotations

import pytest

BASE = "/api/webhooks/sharepoint"
ADMIN_BASE = "/api/admin/sharepoint/connections"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _create_connection(client, token, *, name="webhook-sp-conn"):
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


def _set_webhook_secret(connection_id: str, secret: str) -> None:
    from src.repositories import source_connections_repo

    repo = source_connections_repo()
    row = repo.get(connection_id)
    config = {**(row.get("config") or {}), "webhook_secret": secret}
    repo.update(connection_id, config=config)


def _config_get_value(config: dict):
    """Drop-in ``app.instance_config.get_value`` fake — same idiom as
    ``tests/test_admin_sharepoint.py``'s helper of the same name
    (duplicated, not imported, so the two test modules never couple on a
    shared fixture)."""

    def _get(*keys, default=None):
        current = config
        for key in keys:
            if isinstance(current, dict) and key in current:
                current = current[key]
            else:
                return default
        return current

    return _get


_ENABLED_EXTRACTION_CONFIG = {
    "extraction": {
        "enabled": True,
        "producer": {"command": "python -m fake_producer"},
        "timeout_s": 60,
    }
}


@pytest.fixture(autouse=True)
def _webhook_route_enabled(monkeypatch):
    """Every test in this module exercises the receiver itself, so the
    router's own feature gate is on by default — ``TestFeatureGate`` below
    overrides it back off per test."""
    monkeypatch.setenv("AGNES_EXTRACTION_WEBHOOK_ENABLED", "1")


class TestFeatureGate:
    def test_route_404s_when_the_switch_is_off(self, seeded_app, monkeypatch):
        monkeypatch.setenv("AGNES_EXTRACTION_WEBHOOK_ENABLED", "0")
        r = seeded_app["client"].post(f"{BASE}/does-not-exist", json={"value": []})
        assert r.status_code == 404

    def test_validation_handshake_also_404s_when_off(self, seeded_app, monkeypatch):
        """The router-level gate closes the WHOLE surface — even the
        side-effect-free handshake branch never runs when the switch is off."""
        monkeypatch.setenv("AGNES_EXTRACTION_WEBHOOK_ENABLED", "0")
        r = seeded_app["client"].post(f"{BASE}/does-not-exist", params={"validationToken": "abc123"})
        assert r.status_code == 404


class TestValidationHandshake:
    def test_echoes_the_token_verbatim_as_text_plain(self, seeded_app):
        r = seeded_app["client"].post(f"{BASE}/does-not-exist", params={"validationToken": "tok_verbatim_123"})
        assert r.status_code == 200
        assert r.text == "tok_verbatim_123"
        assert r.headers["content-type"].startswith("text/plain")

    def test_handshake_never_reads_the_body(self, seeded_app):
        """No side effects: an oversized/garbage body must not matter at
        all when a validationToken is present."""
        r = seeded_app["client"].post(
            f"{BASE}/does-not-exist",
            params={"validationToken": "tok"},
            content=b"{not json",
        )
        assert r.status_code == 200
        assert r.text == "tok"


class TestNotificationDelivery:
    def _connection_with_secret(self, seeded_app, secret="s3cr3t-value-0123456789abcdef"):
        c, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(c, token)
        _set_webhook_secret(conn_id, secret)
        return conn_id, secret

    def test_unknown_connection_returns_202_with_nothing_done(self, seeded_app):
        r = seeded_app["client"].post(
            f"{BASE}/does-not-exist",
            json={"value": [{"clientState": "anything"}]},
        )
        assert r.status_code == 202

    def test_bad_clientstate_is_dropped_silently(self, seeded_app, monkeypatch):
        # Extraction fully usable (readiness would happily enqueue) — this
        # isolates the assertion to the clientState check itself, rather
        # than passing vacuously because the readiness gate alone would
        # have skipped the enqueue anyway.
        monkeypatch.delenv("AGNES_EXTRACTION_ENABLED", raising=False)
        monkeypatch.setattr("app.instance_config.get_value", _config_get_value(_ENABLED_EXTRACTION_CONFIG))
        conn_id, _secret = self._connection_with_secret(seeded_app)
        r = seeded_app["client"].post(
            f"{BASE}/{conn_id}",
            json={"value": [{"clientState": "wrong-secret", "resourceData": {"id": "x"}}]},
        )
        assert r.status_code == 202

        from src.repositories import jobs_repo

        jobs = jobs_repo().list(kind="corpus-extraction")
        assert not any(j["payload_json"] == {"connection_id": conn_id} for j in jobs)

    def test_no_secret_configured_yet_returns_202_with_nothing_done(self, seeded_app, monkeypatch):
        monkeypatch.delenv("AGNES_EXTRACTION_ENABLED", raising=False)
        monkeypatch.setattr("app.instance_config.get_value", _config_get_value(_ENABLED_EXTRACTION_CONFIG))
        c, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="webhook-no-secret-conn")
        r = c.post(f"{BASE}/{conn_id}", json={"value": [{"clientState": "whatever"}]})
        assert r.status_code == 202

        from src.repositories import jobs_repo

        jobs = jobs_repo().list(kind="corpus-extraction")
        assert not any(j["payload_json"] == {"connection_id": conn_id} for j in jobs)

    def test_valid_notification_enqueues_corpus_extraction(self, seeded_app, monkeypatch):
        monkeypatch.delenv("AGNES_EXTRACTION_ENABLED", raising=False)
        monkeypatch.setattr("app.instance_config.get_value", _config_get_value(_ENABLED_EXTRACTION_CONFIG))
        conn_id, secret = self._connection_with_secret(seeded_app)

        r = seeded_app["client"].post(
            f"{BASE}/{conn_id}",
            json={"value": [{"clientState": secret, "resourceData": {"id": "item1"}}]},
        )
        assert r.status_code == 202

        from src.repositories import jobs_repo

        jobs = [
            j for j in jobs_repo().list(kind="corpus-extraction") if j["payload_json"] == {"connection_id": conn_id}
        ]
        assert len(jobs) == 1
        job = jobs[0]
        assert job["idempotency_key"] == f"corpus-extraction:{conn_id}"
        # Debounced, not immediate — a burst should coalesce (see below).
        assert job["run_after"] is not None

    def test_burst_of_notifications_collapses_onto_one_job(self, seeded_app, monkeypatch):
        monkeypatch.delenv("AGNES_EXTRACTION_ENABLED", raising=False)
        monkeypatch.setattr("app.instance_config.get_value", _config_get_value(_ENABLED_EXTRACTION_CONFIG))
        conn_id, secret = self._connection_with_secret(seeded_app)

        c = seeded_app["client"]
        for _ in range(3):
            r = c.post(
                f"{BASE}/{conn_id}",
                json={"value": [{"clientState": secret, "resourceData": {"id": "item1"}}]},
            )
            assert r.status_code == 202

        from src.repositories import jobs_repo

        jobs = [
            j for j in jobs_repo().list(kind="corpus-extraction") if j["payload_json"] == {"connection_id": conn_id}
        ]
        assert len(jobs) == 1

    def test_skips_enqueue_when_extraction_is_not_usable(self, seeded_app, monkeypatch):
        """The webhook never queues a job doomed to fail: same readiness
        gate the manual admin trigger checks BEFORE enqueueing."""
        monkeypatch.setattr("app.instance_config.get_value", _config_get_value({}))
        conn_id, secret = self._connection_with_secret(seeded_app)

        r = seeded_app["client"].post(
            f"{BASE}/{conn_id}",
            json={"value": [{"clientState": secret}]},
        )
        assert r.status_code == 202

        from src.repositories import jobs_repo

        jobs = jobs_repo().list(kind="corpus-extraction")
        assert not any(j["payload_json"] == {"connection_id": conn_id} for j in jobs)

    def test_oversized_body_is_rejected(self, seeded_app):
        conn_id, secret = self._connection_with_secret(seeded_app)
        huge_payload = {"value": [{"clientState": secret, "junk": "x" * (2 * 1024 * 1024)}]}
        r = seeded_app["client"].post(f"{BASE}/{conn_id}", json=huge_payload)
        assert r.status_code == 413

    def test_secret_rotation_invalidates_the_old_clientstate(self, seeded_app, monkeypatch):
        monkeypatch.delenv("AGNES_EXTRACTION_ENABLED", raising=False)
        monkeypatch.setattr("app.instance_config.get_value", _config_get_value(_ENABLED_EXTRACTION_CONFIG))
        c, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="webhook-rotation-conn")

        old_secret = c.post(f"{ADMIN_BASE}/{conn_id}/webhook", headers=_auth(token)).json()["secret"]
        new_secret = c.post(f"{ADMIN_BASE}/{conn_id}/webhook", headers=_auth(token)).json()["secret"]
        assert old_secret != new_secret

        # The OLD secret no longer verifies.
        r = c.post(f"{BASE}/{conn_id}", json={"value": [{"clientState": old_secret}]})
        assert r.status_code == 202
        from src.repositories import jobs_repo

        assert not any(
            j["payload_json"] == {"connection_id": conn_id} for j in jobs_repo().list(kind="corpus-extraction")
        )

        # The NEW secret does.
        r = c.post(f"{BASE}/{conn_id}", json={"value": [{"clientState": new_secret}]})
        assert r.status_code == 202
        assert any(j["payload_json"] == {"connection_id": conn_id} for j in jobs_repo().list(kind="corpus-extraction"))
