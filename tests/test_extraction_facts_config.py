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


class TestTransportOverride:
    """`transport` rides the same PATCH but is only touched when PRESENT in
    the body — so a retry-mode-only call cannot move a connection off the
    Batches API by accident."""

    def test_setting_batch_is_written_and_resolved_from_the_connection(self, seeded_app):
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token, name="corp-sharepoint-transport-set")

        r = client.patch(f"{BASE}/{conn_id}/extraction/facts-config", json={"transport": "batch"}, headers=_auth(token))

        assert r.status_code == 200, r.text
        assert r.json()["transport"] == {"value": "batch", "source": "connection"}
        from src.repositories import source_connections_repo

        row = source_connections_repo().get(conn_id)
        assert row["config"]["extraction"]["facts"]["transport"] == "batch"

    def test_a_retry_mode_only_patch_leaves_the_transport_alone(self, seeded_app):
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token, name="corp-sharepoint-transport-untouched")
        client.patch(f"{BASE}/{conn_id}/extraction/facts-config", json={"transport": "batch"}, headers=_auth(token))

        r = client.patch(f"{BASE}/{conn_id}/extraction/facts-config", json={"retry_mode": "off"}, headers=_auth(token))

        assert r.status_code == 200, r.text
        assert r.json()["retry_mode"] == {"value": "off", "source": "connection"}
        assert r.json()["transport"] == {"value": "batch", "source": "connection"}

    def test_an_explicit_null_clears_the_transport_override(self, seeded_app):
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token, name="corp-sharepoint-transport-clear")
        client.patch(f"{BASE}/{conn_id}/extraction/facts-config", json={"transport": "batch"}, headers=_auth(token))

        r = client.patch(f"{BASE}/{conn_id}/extraction/facts-config", json={"transport": None}, headers=_auth(token))

        assert r.status_code == 200, r.text
        assert r.json()["transport"] == {"value": "sync", "source": "instance"}

    def test_an_invalid_transport_is_refused_with_422(self, seeded_app):
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token, name="corp-sharepoint-transport-invalid")

        r = client.patch(
            f"{BASE}/{conn_id}/extraction/facts-config", json={"transport": "carrier-pigeon"}, headers=_auth(token)
        )

        assert r.status_code == 422


class TestProviderOverride:
    """`provider` rides the same PATCH but is only touched when PRESENT in
    the body — the same convention `transport` uses, for the same reason:
    a retry-mode-only call must not silently move a connection off (or
    onto) Vertex."""

    def test_setting_vertex_is_written_and_resolved_from_the_connection(self, seeded_app, monkeypatch):
        monkeypatch.setattr("connectors.llm.factory.vertex_config_or_none", lambda *a, **k: None)
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token, name="corp-sharepoint-provider-set")

        r = client.patch(f"{BASE}/{conn_id}/extraction/facts-config", json={"provider": "vertex"}, headers=_auth(token))

        assert r.status_code == 200, r.text
        # An explicit connection override is honoured verbatim — the
        # "effective" client provider matches even though this instance has
        # no usable Vertex config (a real pass would fail loudly instead,
        # see FactsExtractionUnavailable; this endpoint only resolves).
        assert r.json()["provider"] == {
            "value": "vertex",
            "source": "connection",
            "effective": "vertex",
            "effective_source": "connection",
        }
        from src.repositories import source_connections_repo

        row = source_connections_repo().get(conn_id)
        assert row["config"]["extraction"]["facts"]["provider"] == "vertex"

    def test_a_retry_mode_only_patch_leaves_the_provider_alone(self, seeded_app):
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token, name="corp-sharepoint-provider-untouched")
        client.patch(f"{BASE}/{conn_id}/extraction/facts-config", json={"provider": "anthropic"}, headers=_auth(token))

        r = client.patch(f"{BASE}/{conn_id}/extraction/facts-config", json={"retry_mode": "off"}, headers=_auth(token))

        assert r.status_code == 200, r.text
        assert r.json()["retry_mode"] == {"value": "off", "source": "connection"}
        assert r.json()["provider"]["value"] == "anthropic"
        assert r.json()["provider"]["source"] == "connection"

    def test_an_explicit_null_clears_the_provider_override(self, seeded_app, monkeypatch):
        monkeypatch.setattr("connectors.llm.factory.vertex_config_or_none", lambda *a, **k: None)
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token, name="corp-sharepoint-provider-clear")
        client.patch(f"{BASE}/{conn_id}/extraction/facts-config", json={"provider": "vertex"}, headers=_auth(token))

        r = client.patch(f"{BASE}/{conn_id}/extraction/facts-config", json={"provider": None}, headers=_auth(token))

        assert r.status_code == 200, r.text
        # Cleared → falls back to the instance default ("inherit"), which in
        # turn resolves through ai.provider — no Vertex configured here, so
        # the effective client provider is anthropic.
        assert r.json()["provider"] == {
            "value": "inherit",
            "source": "instance",
            "effective": "anthropic",
            "effective_source": "instance:inherit",
        }

    def test_an_invalid_provider_is_refused_with_422(self, seeded_app):
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token, name="corp-sharepoint-provider-invalid")

        r = client.patch(f"{BASE}/{conn_id}/extraction/facts-config", json={"provider": "openai"}, headers=_auth(token))

        assert r.status_code == 422

    def test_inherit_follows_ai_provider_when_vertex_is_configured(self, seeded_app, monkeypatch):
        """The exact incident this knob exists for, from the resolver's
        side: a connection left on the default ('inherit') must follow
        ai.provider — regardless of a static Anthropic key sitting in the
        environment, which `resolve_effective_provider` never consults."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-still-set-but-workspace-is-exhausted")
        monkeypatch.setattr(
            "connectors.llm.factory.vertex_config_or_none", lambda *a, **k: ("my-project", "us-central1")
        )
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token, name="corp-sharepoint-provider-inherit-vertex")

        r = client.patch(
            f"{BASE}/{conn_id}/extraction/facts-config", json={"provider": "inherit"}, headers=_auth(token)
        )

        assert r.status_code == 200, r.text
        assert r.json()["provider"] == {
            "value": "inherit",
            "source": "connection",
            "effective": "vertex",
            "effective_source": "connection:inherit",
        }


class TestVertexRegionOverride:
    """`vertex_region` rides the same PATCH but is only touched when
    PRESENT in the body — the same convention `transport`/`provider` use,
    for the same reason: a retry-mode-only call must not silently pin (or
    unpin) a connection's Vertex region. Google enforces Claude-on-Vertex
    quotas PER REGION, so pinning different connections to different
    regions raises the account's effective throughput.

    Every region literal here is one of the DOCUMENTED buckets in
    `connectors.sharepoint.facts_extraction.VERTEX_REGION_MODEL_MATRIX` for
    the instance's default (Haiku) model — TCRD-296 synthesis F.25 refuses
    an undocumented region×model pairing with a 422, so a syntactically
    valid but unlisted region (e.g. "europe-west4") is no longer accepted
    here regardless of what this class is actually testing."""

    def test_setting_a_region_is_written_and_resolved_from_the_connection(self, seeded_app):
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token, name="corp-sharepoint-vertex-region-set")

        r = client.patch(
            f"{BASE}/{conn_id}/extraction/facts-config", json={"vertex_region": "europe-west1"}, headers=_auth(token)
        )

        assert r.status_code == 200, r.text
        assert r.json()["vertex_region"] == {"value": "europe-west1", "source": "connection"}
        from src.repositories import source_connections_repo

        row = source_connections_repo().get(conn_id)
        assert row["config"]["extraction"]["facts"]["vertex_region"] == "europe-west1"

    def test_a_region_is_lowercased(self, seeded_app):
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token, name="corp-sharepoint-vertex-region-lowercase")

        r = client.patch(
            f"{BASE}/{conn_id}/extraction/facts-config", json={"vertex_region": "US-EAST5"}, headers=_auth(token)
        )

        assert r.status_code == 200, r.text
        assert r.json()["vertex_region"] == {"value": "us-east5", "source": "connection"}

    def test_global_is_a_valid_region(self, seeded_app):
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token, name="corp-sharepoint-vertex-region-global")

        r = client.patch(
            f"{BASE}/{conn_id}/extraction/facts-config", json={"vertex_region": "global"}, headers=_auth(token)
        )

        assert r.status_code == 200, r.text
        assert r.json()["vertex_region"] == {"value": "global", "source": "connection"}

    def test_a_retry_mode_only_patch_leaves_the_vertex_region_alone(self, seeded_app):
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token, name="corp-sharepoint-vertex-region-untouched")
        set_resp = client.patch(
            f"{BASE}/{conn_id}/extraction/facts-config", json={"vertex_region": "us-east5"}, headers=_auth(token)
        )
        assert set_resp.status_code == 200, set_resp.text

        r = client.patch(f"{BASE}/{conn_id}/extraction/facts-config", json={"retry_mode": "off"}, headers=_auth(token))

        assert r.status_code == 200, r.text
        assert r.json()["retry_mode"] == {"value": "off", "source": "connection"}
        assert r.json()["vertex_region"] == {"value": "us-east5", "source": "connection"}

    def test_an_explicit_null_clears_the_vertex_region_override(self, seeded_app, monkeypatch):
        monkeypatch.setattr("connectors.llm.factory.vertex_config_or_none", lambda *a, **k: None)
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token, name="corp-sharepoint-vertex-region-clear")
        set_resp = client.patch(
            f"{BASE}/{conn_id}/extraction/facts-config", json={"vertex_region": "us-east5"}, headers=_auth(token)
        )
        assert set_resp.status_code == 200, set_resp.text

        r = client.patch(
            f"{BASE}/{conn_id}/extraction/facts-config", json={"vertex_region": None}, headers=_auth(token)
        )

        assert r.status_code == 200, r.text
        assert r.json()["vertex_region"] == {"value": None, "source": "none"}
        from src.repositories import source_connections_repo

        row = source_connections_repo().get(conn_id)
        assert "vertex_region" not in (row["config"]["extraction"].get("facts") or {})

    def test_falls_back_to_ai_vertex_region_when_no_override_is_set(self, seeded_app, monkeypatch):
        monkeypatch.setattr(
            "connectors.llm.factory.vertex_config_or_none", lambda *a, **k: ("my-project", "us-central1")
        )
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token, name="corp-sharepoint-vertex-region-fallback")

        r = client.patch(f"{BASE}/{conn_id}/extraction/facts-config", json={"retry_mode": "off"}, headers=_auth(token))

        assert r.status_code == 200, r.text
        assert r.json()["vertex_region"] == {"value": "us-central1", "source": "instance:ai.vertex"}

    def test_an_invalid_region_is_refused_with_422(self, seeded_app):
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token, name="corp-sharepoint-vertex-region-invalid")

        r = client.patch(
            f"{BASE}/{conn_id}/extraction/facts-config", json={"vertex_region": "not a region!"}, headers=_auth(token)
        )

        assert r.status_code == 422

    def test_a_region_with_no_quota_bucket_for_the_instances_model_is_refused(self, seeded_app):
        """TCRD-296 synthesis F.25, live finding (b): a syntactically valid
        region with no documented Claude-on-Vertex quota bucket for the
        instance's configured (default: Haiku) model answers 429 on every
        call — refused here instead of on the first live pass."""
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token, name="corp-sharepoint-vertex-region-no-bucket")

        r = client.patch(
            f"{BASE}/{conn_id}/extraction/facts-config", json={"vertex_region": "asia-northeast1"}, headers=_auth(token)
        )

        assert r.status_code == 422
        assert "no documented Claude-on-Vertex quota bucket" in r.json()["detail"]
