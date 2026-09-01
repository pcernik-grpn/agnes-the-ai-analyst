"""Microsoft Graph change-notification SUBSCRIPTION lifecycle —
`connectors/sharepoint/subscriptions.py` plus its three admin routes
(`POST .../subscriptions/ensure`, `DELETE .../subscriptions`,
`POST /api/admin/sharepoint/subscriptions/run-due`).

Covers, against a mocked Graph transport (there is no live tenant): the
create/renew/unchanged/remove decision table and its expiry math, per-drive
failure isolation, idempotency (a second call makes no Graph writes at all),
the clientState-rotation repair, endpoint RBAC and every typed refusal (flag
off / no secret minted / no public URL — each naming its fix), and the
scheduler row's registration, mirroring the extraction sweep's own tests.
"""

from __future__ import annotations

import datetime

import httpx
import pytest

BASE = "/api/admin/sharepoint/connections"
RUN_DUE = "/api/admin/sharepoint/subscriptions/run-due"
PUBLIC_ORIGIN = "https://agnes.example.com"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _create_connection(client, token, *, name="subs-conn"):
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


def _patch_config(connection_id: str, patch: dict) -> None:
    from src.repositories import source_connections_repo

    source_connections_repo().config_patch(connection_id, patch)


def _config(connection_id: str) -> dict:
    from src.repositories import source_connections_repo

    return source_connections_repo().get(connection_id).get("config") or {}


def _scope(drive_id: str, *, scope_id: str | None = None) -> dict:
    return {
        "source_scope_id": scope_id or f"scope-{drive_id}",
        "display_path": f"Site/{drive_id}",
        "drive_id": drive_id,
        "collection_id": f"col-{drive_id}",
    }


def _iso(dt: datetime.datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


class _GraphRecorder:
    """A Graph transport that records every subscription write and answers
    from a small scripted policy. Deliberately NOT a generic mock: the point
    of these tests is which calls are made (and which are NOT), so the
    recorder is the assertion surface."""

    def __init__(self, *, create_status=201, patch_status=200, delete_status=204, fail_drives=()):
        self.creates: list[dict] = []
        self.patches: list[tuple[str, dict]] = []
        self.deletes: list[str] = []
        self.create_status = create_status
        self.patch_status = patch_status
        self.delete_status = delete_status
        self.fail_drives = set(fail_drives)
        self._next_id = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/oauth2/v2.0/token"):
            return httpx.Response(200, json={"access_token": "tok-abc"})
        if request.method == "POST" and path == "/v1.0/subscriptions":
            import json

            body = json.loads(request.content)
            self.creates.append(body)
            drive = body["resource"].split("/")[2]
            if drive in self.fail_drives or self.create_status != 201:
                return httpx.Response(
                    self.create_status if self.create_status != 201 else 403,
                    json={"error": {"code": "accessDenied"}},
                )
            self._next_id += 1
            return httpx.Response(
                201,
                json={"id": f"sub-{self._next_id}", "expirationDateTime": body["expirationDateTime"]},
            )
        if request.method == "PATCH" and path.startswith("/v1.0/subscriptions/"):
            import json

            sub_id = path.rsplit("/", 1)[-1]
            body = json.loads(request.content)
            self.patches.append((sub_id, body))
            if self.patch_status != 200:
                return httpx.Response(self.patch_status, json={"error": {"code": "notFound"}})
            return httpx.Response(200, json={"id": sub_id, "expirationDateTime": body["expirationDateTime"]})
        if request.method == "DELETE" and path.startswith("/v1.0/subscriptions/"):
            self.deletes.append(path.rsplit("/", 1)[-1])
            return httpx.Response(self.delete_status)
        raise AssertionError(f"unexpected Graph call: {request.method} {path}")


@pytest.fixture
def graph(monkeypatch):
    """Wire the recorder into the ONE HTTP seam every Graph call in this repo
    goes through (`graph_client._http_client`), so the subscription verbs are
    exercised through the real client rather than around it."""
    from connectors.sharepoint import graph_client as gc

    recorder = _GraphRecorder()

    def _client():
        return httpx.AsyncClient(transport=httpx.MockTransport(recorder), timeout=10)

    monkeypatch.setattr(gc, "_http_client", _client)
    return recorder


@pytest.fixture(autouse=True)
def _lifecycle_preconditions(monkeypatch):
    """The receiver flag on, a public origin configured, and a client-secret
    credential resolvable — the three things every non-refusal test needs.
    The refusal tests each undo exactly one of them."""
    monkeypatch.setenv("AGNES_SHAREPOINT_ENABLED", "1")
    monkeypatch.setenv("AGNES_BASE_URL", PUBLIC_ORIGIN)
    monkeypatch.setenv("SHAREPOINT_CLIENT_SECRET", "cs-abc")


def _ready_connection(client, token, *, drives=("drive-a",), name="subs-conn"):
    """A connection with a secret minted, a client-secret credential, and one
    confirmed scope per drive."""
    conn_id = _create_connection(client, token, name=name)
    _patch_config(
        conn_id,
        {
            "auth_method": "client_secret",
            "webhook_secret": "s3cr3t-value",
            "scopes": [_scope(d) for d in drives],
        },
    )
    return conn_id


# ---------------------------------------------------------------------------
# The lifecycle module
# ---------------------------------------------------------------------------


class TestEnsureDecisions:
    def _run(self, connection_id):
        import asyncio

        from connectors.sharepoint.subscriptions import ensure_subscriptions
        from src.repositories import source_connections_repo

        row = source_connections_repo().get(connection_id)
        return asyncio.run(ensure_subscriptions(row))

    def test_creates_one_subscription_per_drive_with_the_graph_contract(self, seeded_app, graph):
        c, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _ready_connection(c, token, drives=("drive-a", "drive-b"))

        result = self._run(conn_id)

        assert result["created"] == 2
        assert len(graph.creates) == 2
        body = graph.creates[0]
        assert body["changeType"] == "updated"
        assert body["resource"] == "/drives/drive-a/root"
        assert body["notificationUrl"] == f"{PUBLIC_ORIGIN}/api/webhooks/sharepoint/{conn_id}"
        assert body["clientState"] == "s3cr3t-value"
        assert body["expirationDateTime"].endswith("Z")

    def test_requested_expiry_is_25_days_out_and_under_graphs_30_day_ceiling(self, seeded_app, graph):
        c, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _ready_connection(c, token)
        self._run(conn_id)

        from connectors.sharepoint.subscriptions import _parse_iso

        requested = _parse_iso(graph.creates[0]["expirationDateTime"])
        delta = requested - datetime.datetime.now(datetime.timezone.utc)
        assert datetime.timedelta(days=24) < delta < datetime.timedelta(days=26)

    def test_state_is_persisted_on_the_connection_without_the_secret(self, seeded_app, graph):
        c, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _ready_connection(c, token)
        self._run(conn_id)

        state = _config(conn_id)["webhook_subscriptions"]
        assert [r["drive_id"] for r in state] == ["drive-a"]
        assert state[0]["subscription_id"] == "sub-1"
        assert state[0]["expires_at"]
        assert "s3cr3t-value" not in str(state)

    def test_second_call_is_a_no_op(self, seeded_app, graph):
        """Idempotency: a healthy, far-from-expiry record makes NO Graph
        write of any kind — not a create, not even a defensive PATCH."""
        c, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _ready_connection(c, token)
        self._run(conn_id)
        assert len(graph.creates) == 1

        second = self._run(conn_id)
        assert second["unchanged"] == 1
        assert second["created"] == 0
        assert len(graph.creates) == 1
        assert graph.patches == []

    def test_renews_within_the_72h_window(self, seeded_app, graph):
        c, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _ready_connection(c, token)
        soon = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=20)
        _patch_config(
            conn_id,
            {
                "webhook_subscriptions": [
                    {"drive_id": "drive-a", "subscription_id": "sub-old", "expires_at": _iso(soon)}
                ]
            },
        )

        result = self._run(conn_id)

        assert result["renewed"] == 1
        assert graph.creates == []
        assert graph.patches[0][0] == "sub-old"
        # A renewal carries ONLY a new expiry — resending notificationUrl /
        # clientState is how a renewal accidentally becomes a re-validation.
        assert set(graph.patches[0][1]) == {"expirationDateTime"}
        assert _config(conn_id)["webhook_subscriptions"][0]["subscription_id"] == "sub-old"

    def test_a_record_outside_the_window_is_left_alone(self, seeded_app, graph):
        c, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _ready_connection(c, token)
        later = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=10)
        _patch_config(
            conn_id,
            {"webhook_subscriptions": [{"drive_id": "drive-a", "subscription_id": "sub-x", "expires_at": _iso(later)}]},
        )

        result = self._run(conn_id)

        assert result["unchanged"] == 1
        assert graph.creates == [] and graph.patches == []

    def test_an_already_expired_record_is_recreated_not_patched(self, seeded_app, graph):
        """Graph will not PATCH a subscription it has already reaped, so an
        expired record must go straight to a create."""
        c, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _ready_connection(c, token)
        past = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)
        _patch_config(
            conn_id,
            {
                "webhook_subscriptions": [
                    {"drive_id": "drive-a", "subscription_id": "sub-dead", "expires_at": _iso(past)}
                ]
            },
        )

        result = self._run(conn_id)

        assert result["created"] == 1
        assert graph.patches == []
        assert len(graph.creates) == 1
        assert _config(conn_id)["webhook_subscriptions"][0]["subscription_id"] == "sub-1"

    def test_a_renewal_that_404s_falls_through_to_a_create(self, seeded_app, graph):
        c, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _ready_connection(c, token)
        soon = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=20)
        _patch_config(
            conn_id,
            {
                "webhook_subscriptions": [
                    {"drive_id": "drive-a", "subscription_id": "sub-gone", "expires_at": _iso(soon)}
                ]
            },
        )
        graph.patch_status = 404

        result = self._run(conn_id)

        assert result["created"] == 1 and result["failed"] == 0
        assert graph.patches and graph.creates

    def test_a_drive_that_left_scope_has_its_subscription_deleted(self, seeded_app, graph):
        c, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _ready_connection(c, token, drives=("drive-a",))
        later = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=10)
        _patch_config(
            conn_id,
            {
                "webhook_subscriptions": [
                    {"drive_id": "drive-a", "subscription_id": "sub-keep", "expires_at": _iso(later)},
                    {"drive_id": "drive-gone", "subscription_id": "sub-drop", "expires_at": _iso(later)},
                ]
            },
        )

        result = self._run(conn_id)

        assert result["removed"] == 1 and result["unchanged"] == 1
        assert graph.deletes == ["sub-drop"]
        assert [r["drive_id"] for r in _config(conn_id)["webhook_subscriptions"]] == ["drive-a"]

    def test_several_scopes_on_one_drive_share_one_subscription(self, seeded_app, graph):
        c, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="shared-drive")
        _patch_config(
            conn_id,
            {
                "auth_method": "client_secret",
                "webhook_secret": "s3cr3t-value",
                "scopes": [_scope("drive-a", scope_id="s1"), _scope("drive-a", scope_id="s2")],
            },
        )

        result = self._run(conn_id)

        assert result["created"] == 1
        assert len(graph.creates) == 1

    def test_a_scope_without_a_drive_id_is_skipped_not_fatal(self, seeded_app, graph):
        c, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="no-drive")
        _patch_config(
            conn_id,
            {
                "auth_method": "client_secret",
                "webhook_secret": "s3cr3t-value",
                "scopes": [{"source_scope_id": "legacy", "display_path": "Site/Old"}, _scope("drive-a")],
            },
        )

        result = self._run(conn_id)

        assert result["created"] == 1
        assert result["skipped_scopes"] == [{"source_scope_id": "legacy", "reason": "missing_drive_id"}]

    def test_a_drive_id_that_could_escape_its_url_path_is_refused(self, seeded_app, graph):
        """A stored drive_id goes straight into a Graph resource path, so it
        is validated first — never a Graph call built from it."""
        c, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="bad-drive")
        _patch_config(
            conn_id,
            {
                "auth_method": "client_secret",
                "webhook_secret": "s3cr3t-value",
                "scopes": [_scope("../../me/drive")],
            },
        )

        result = self._run(conn_id)

        assert graph.creates == []
        assert result["skipped_scopes"][0]["reason"] == "invalid_drive_id"


class TestPerDriveFailureIsolation:
    def _run(self, connection_id):
        import asyncio

        from connectors.sharepoint.subscriptions import ensure_subscriptions
        from src.repositories import source_connections_repo

        return asyncio.run(ensure_subscriptions(source_connections_repo().get(connection_id)))

    def test_one_drive_failing_never_costs_the_others(self, seeded_app, graph):
        c, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _ready_connection(c, token, drives=("drive-a", "drive-bad", "drive-c"))
        graph.fail_drives = {"drive-bad"}

        result = self._run(conn_id)

        assert result["created"] == 2
        assert result["failed"] == 1
        failed = [d for d in result["drives"] if d["action"] == "failed"]
        assert failed[0]["drive_id"] == "drive-bad"
        assert [r["drive_id"] for r in _config(conn_id)["webhook_subscriptions"]] == ["drive-a", "drive-c"]

    def test_a_failed_renewal_keeps_its_record_so_the_next_sweep_retries(self, seeded_app, graph):
        c, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _ready_connection(c, token)
        soon = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=20)
        _patch_config(
            conn_id,
            {
                "webhook_subscriptions": [
                    {"drive_id": "drive-a", "subscription_id": "sub-keep", "expires_at": _iso(soon)}
                ]
            },
        )
        graph.patch_status = 503

        result = self._run(conn_id)

        assert result["failed"] == 1
        state = _config(conn_id)["webhook_subscriptions"]
        assert state[0]["subscription_id"] == "sub-keep"


class TestClientStateRotation:
    """A rotated webhook secret leaves a live subscription signing with a
    value the receiver now drops SILENTLY — unexpired, still listed, and
    completely dead. Ensure has to notice."""

    def _run(self, connection_id):
        import asyncio

        from connectors.sharepoint.subscriptions import ensure_subscriptions
        from src.repositories import source_connections_repo

        return asyncio.run(ensure_subscriptions(source_connections_repo().get(connection_id)))

    def test_rotating_the_secret_forces_a_recreate(self, seeded_app, graph):
        c, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _ready_connection(c, token)
        self._run(conn_id)
        assert len(graph.creates) == 1

        r = c.post(f"{BASE}/{conn_id}/webhook", headers=_auth(token))
        assert r.status_code == 200

        result = self._run(conn_id)

        assert result["created"] == 1
        assert graph.deletes == ["sub-1"]
        assert graph.creates[-1]["clientState"] == r.json()["secret"]

    def test_a_pre_fingerprint_record_is_not_churned(self, seeded_app, graph):
        """An instance upgrading into this feature must not re-create every
        subscription it already has just because the fingerprint is absent."""
        c, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _ready_connection(c, token)
        later = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=10)
        _patch_config(
            conn_id,
            {
                "webhook_subscriptions": [
                    {"drive_id": "drive-a", "subscription_id": "sub-old", "expires_at": _iso(later)}
                ]
            },
        )

        result = self._run(conn_id)

        assert result["unchanged"] == 1
        assert graph.creates == [] and graph.deletes == []


class TestRemoveSubscriptions:
    def _run(self, connection_id):
        import asyncio

        from connectors.sharepoint.subscriptions import remove_subscriptions
        from src.repositories import source_connections_repo

        return asyncio.run(remove_subscriptions(source_connections_repo().get(connection_id)))

    def test_deletes_every_record(self, seeded_app, graph):
        c, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _ready_connection(c, token, drives=("drive-a", "drive-b"))
        import asyncio

        from connectors.sharepoint.subscriptions import ensure_subscriptions
        from src.repositories import source_connections_repo

        asyncio.run(ensure_subscriptions(source_connections_repo().get(conn_id)))

        result = self._run(conn_id)

        assert result["removed"] == 2
        assert sorted(graph.deletes) == ["sub-1", "sub-2"]
        assert _config(conn_id)["webhook_subscriptions"] == []

    def test_a_404_counts_as_removed(self, seeded_app, graph):
        c, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _ready_connection(c, token)
        later = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=10)
        _patch_config(
            conn_id,
            {
                "webhook_subscriptions": [
                    {"drive_id": "drive-a", "subscription_id": "sub-gone", "expires_at": _iso(later)}
                ]
            },
        )
        graph.delete_status = 404

        result = self._run(conn_id)

        assert result["removed"] == 1
        assert _config(conn_id)["webhook_subscriptions"] == []

    def test_a_failed_delete_keeps_the_record(self, seeded_app, graph):
        c, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _ready_connection(c, token)
        later = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=10)
        _patch_config(
            conn_id,
            {"webhook_subscriptions": [{"drive_id": "drive-a", "subscription_id": "sub-x", "expires_at": _iso(later)}]},
        )
        graph.delete_status = 500

        result = self._run(conn_id)

        assert result["failed"] == 1
        assert _config(conn_id)["webhook_subscriptions"][0]["subscription_id"] == "sub-x"

    def test_no_records_is_a_clean_no_op_with_no_token_fetch(self, seeded_app, graph):
        c, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _ready_connection(c, token)

        result = self._run(conn_id)

        assert result == {"connection_id": conn_id, "subscriptions": [], "removed": 0, "failed": 0}
        assert graph.deletes == []


# ---------------------------------------------------------------------------
# The admin surface
# ---------------------------------------------------------------------------


class TestEnsureEndpoint:
    def test_requires_auth(self, seeded_app):
        assert seeded_app["client"].post(f"{BASE}/nope/subscriptions/ensure").status_code == 401

    def test_requires_admin(self, seeded_app):
        r = seeded_app["client"].post(f"{BASE}/nope/subscriptions/ensure", headers=_auth(seeded_app["analyst_token"]))
        assert r.status_code == 403

    def test_404_for_unknown_connection(self, seeded_app):
        r = seeded_app["client"].post(
            f"{BASE}/does-not-exist/subscriptions/ensure", headers=_auth(seeded_app["admin_token"])
        )
        assert r.status_code == 404

    def test_creates_and_returns_per_drive_results(self, seeded_app, graph):
        c, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _ready_connection(c, token, drives=("drive-a", "drive-b"))

        r = c.post(f"{BASE}/{conn_id}/subscriptions/ensure", headers=_auth(token))

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["created"] == 2
        assert {d["drive_id"] for d in body["drives"]} == {"drive-a", "drive-b"}
        assert body["notification_url"].endswith(f"/api/webhooks/sharepoint/{conn_id}")

    def test_writes_an_audit_row_with_counts_not_content(self, seeded_app, graph):
        from src.repositories import audit_repo

        c, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _ready_connection(c, token)
        c.post(f"{BASE}/{conn_id}/subscriptions/ensure", headers=_auth(token))

        rows, _ = audit_repo().query(action="sharepoint_connection.subscriptions_ensure", limit=50)
        assert rows, "expected an audit row for the ensure action"
        assert "s3cr3t-value" not in str(rows[0])


class TestEnsureRefusals:
    """Every refusal is typed, lands BEFORE any Graph call, and names the
    exact next step — the difference between a wizard an admin can finish
    and one that says 500."""

    def _refusal(self, client, token, conn_id):
        r = client.post(f"{BASE}/{conn_id}/subscriptions/ensure", headers=_auth(token))
        return r.status_code, r.json()["detail"]

    def test_refuses_when_the_connector_is_off(self, seeded_app, graph, monkeypatch):
        """With the single `sharepoint.enabled` flag, the WHOLE admin router
        refuses first (`feature_disabled`, the #1944 gate) — the module's own
        `sharepoint_disabled` check stays as defense in depth for callers
        that bypass HTTP, but through the API the router speaks."""
        c, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _ready_connection(c, token)
        monkeypatch.setenv("AGNES_SHAREPOINT_ENABLED", "0")

        status, detail = self._refusal(c, token, conn_id)

        assert status == 409
        assert detail["error"] == "feature_disabled"
        assert "sharepoint.enabled" in detail["message"]
        assert graph.creates == []

    def test_refuses_when_no_secret_has_been_minted(self, seeded_app, graph):
        c, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="no-secret")
        _patch_config(conn_id, {"auth_method": "client_secret", "scopes": [_scope("drive-a")]})

        status, detail = self._refusal(c, token, conn_id)

        assert status == 409
        assert detail["error"] == "webhook_secret_missing"
        assert "/webhook" in detail["message"]
        assert graph.creates == []

    @pytest.mark.parametrize("origin", ["http://agnes.example.com", "https://localhost:8000", "https://agnes"])
    def test_refuses_a_non_public_origin(self, seeded_app, graph, monkeypatch, origin):
        c, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _ready_connection(c, token)
        monkeypatch.setenv("AGNES_BASE_URL", origin)

        status, detail = self._refusal(c, token, conn_id)

        assert status == 409
        assert detail["error"] == "public_url_not_configured"
        assert "AGNES_BASE_URL" in detail["message"]
        assert graph.creates == []

    def test_refuses_when_the_credential_does_not_resolve(self, seeded_app, graph, monkeypatch):
        c, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _ready_connection(c, token)
        monkeypatch.delenv("SHAREPOINT_CLIENT_SECRET", raising=False)
        monkeypatch.delenv("SHAREPOINT_CERT_PRIVATE_KEY", raising=False)

        status, detail = self._refusal(c, token, conn_id)

        assert status == 409
        assert detail["error"] == "sharepoint_cert_unresolved"

    def test_a_renewal_only_pass_does_not_need_a_public_origin(self, seeded_app, graph, monkeypatch):
        """A PATCH carries no notification URL, and the nightly sweep has no
        request to derive an origin from — refusing there would let live
        subscriptions lapse over a setting they do not depend on."""
        import asyncio

        from connectors.sharepoint.subscriptions import ensure_subscriptions
        from src.repositories import source_connections_repo

        c, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _ready_connection(c, token)
        soon = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=20)
        _patch_config(
            conn_id,
            {"webhook_subscriptions": [{"drive_id": "drive-a", "subscription_id": "sub-r", "expires_at": _iso(soon)}]},
        )
        monkeypatch.delenv("AGNES_BASE_URL", raising=False)

        result = asyncio.run(ensure_subscriptions(source_connections_repo().get(conn_id)))

        assert result["renewed"] == 1


class TestDeleteEndpoint:
    def test_requires_auth(self, seeded_app):
        assert seeded_app["client"].delete(f"{BASE}/nope/subscriptions").status_code == 401

    def test_requires_admin(self, seeded_app):
        r = seeded_app["client"].delete(f"{BASE}/nope/subscriptions", headers=_auth(seeded_app["analyst_token"]))
        assert r.status_code == 403

    def test_404_for_unknown_connection(self, seeded_app):
        r = seeded_app["client"].delete(
            f"{BASE}/does-not-exist/subscriptions", headers=_auth(seeded_app["admin_token"])
        )
        assert r.status_code == 404

    def test_teardown_with_the_connector_off_answers_the_router_gate(self, seeded_app, graph, monkeypatch):
        """There is no separate receiver flag anymore: `sharepoint.enabled`
        off means the whole connector — router included — is off, so
        teardown answers the same `feature_disabled` 409 as every other
        admin route. An operator cleans up BEFORE turning the connector off
        (or re-enables it to do so); Graph itself reaps subscriptions whose
        receiver stops answering."""
        c, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _ready_connection(c, token)
        c.post(f"{BASE}/{conn_id}/subscriptions/ensure", headers=_auth(token))
        monkeypatch.setenv("AGNES_SHAREPOINT_ENABLED", "0")

        r = c.delete(f"{BASE}/{conn_id}/subscriptions", headers=_auth(token))

        assert r.status_code == 409, r.text
        assert r.json()["detail"]["error"] == "feature_disabled"
        assert graph.deletes == []

    def test_teardown_works_while_the_connector_is_on(self, seeded_app, graph):
        c, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _ready_connection(c, token)
        c.post(f"{BASE}/{conn_id}/subscriptions/ensure", headers=_auth(token))

        r = c.delete(f"{BASE}/{conn_id}/subscriptions", headers=_auth(token))

        assert r.status_code == 200, r.text
        assert r.json()["removed"] == 1
        assert graph.deletes == ["sub-1"]

    def test_no_records_is_a_no_op_not_a_404(self, seeded_app, graph):
        c, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _ready_connection(c, token)

        r = c.delete(f"{BASE}/{conn_id}/subscriptions", headers=_auth(token))

        assert r.status_code == 200
        assert r.json()["removed"] == 0


class TestRenewalSweepEndpoint:
    def test_requires_admin(self, seeded_app):
        r = seeded_app["client"].post(RUN_DUE, headers=_auth(seeded_app["analyst_token"]))
        assert r.status_code == 403

    def test_the_router_gate_answers_when_the_connector_is_off(self, seeded_app, monkeypatch):
        """A stale scheduler row firing against a switched-off instance gets
        the router's typed 409 — still harmless (the sweep only exists to
        renew subscriptions a live receiver needs), just refused one layer
        earlier than the module's own `sharepoint_disabled` no-op, which now
        guards only non-HTTP callers."""
        monkeypatch.setenv("AGNES_SHAREPOINT_ENABLED", "0")
        r = seeded_app["client"].post(RUN_DUE, headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["error"] == "feature_disabled"

    def test_renews_a_due_connection_and_leaves_a_healthy_one_alone(self, seeded_app, graph):
        c, token = seeded_app["client"], seeded_app["admin_token"]
        due = _ready_connection(c, token, drives=("drive-due",), name="due-conn")
        healthy = _ready_connection(c, token, drives=("drive-ok",), name="healthy-conn")
        soon = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=10)
        later = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=10)
        _patch_config(
            due,
            {
                "webhook_subscriptions": [
                    {"drive_id": "drive-due", "subscription_id": "sub-due", "expires_at": _iso(soon)}
                ]
            },
        )
        _patch_config(
            healthy,
            {
                "webhook_subscriptions": [
                    {"drive_id": "drive-ok", "subscription_id": "sub-ok", "expires_at": _iso(later)}
                ]
            },
        )

        r = c.post(RUN_DUE, headers=_auth(token))

        assert r.status_code == 200, r.text
        body = r.json()
        assert [p["connection_id"] for p in body["processed"]] == [due]
        assert graph.patches and graph.patches[0][0] == "sub-due"

    def test_one_misconfigured_connection_never_stops_another(self, seeded_app, graph):
        c, token = seeded_app["client"], seeded_app["admin_token"]
        broken = _create_connection(c, token, name="broken-conn")
        _patch_config(broken, {"auth_method": "client_secret", "scopes": [_scope("drive-broken")]})  # no secret
        good = _ready_connection(c, token, drives=("drive-good",), name="good-conn")

        r = c.post(RUN_DUE, headers=_auth(token))

        assert r.status_code == 200, r.text
        body = r.json()
        assert [p["connection_id"] for p in body["processed"]] == [good]
        assert body["errors"] == [{"connection_id": broken, "error": "webhook_secret_missing"}]

    def test_sweep_back_fills_a_newly_confirmed_scope(self, seeded_app, graph):
        """A scope confirmed today gets its subscription from tonight's
        sweep — ensure is ensure, not renew-only."""
        c, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _ready_connection(c, token, drives=("drive-new",))

        r = c.post(RUN_DUE, headers=_auth(token))

        assert [p["connection_id"] for p in r.json()["processed"]] == [conn_id]
        assert r.json()["processed"][0]["created"] == 1
        assert len(graph.creates) == 1


# ---------------------------------------------------------------------------
# Scheduler registration — mirrors tests/test_scheduler_sidecar.py's
# extraction-sweep block.
# ---------------------------------------------------------------------------

RENEW_ROW = "sharepoint-subscriptions-renew"


def _sched_env_clean(monkeypatch):
    monkeypatch.delenv("SCHEDULER_SUBSCRIPTION_RENEWAL_SCHEDULE", raising=False)
    monkeypatch.delenv("AGNES_SHAREPOINT_ENABLED", raising=False)


class TestSchedulerRegistration:
    def test_no_row_when_the_receiver_is_disabled(self, monkeypatch):
        _sched_env_clean(monkeypatch)
        monkeypatch.setenv("AGNES_SHAREPOINT_ENABLED", "0")
        from services.scheduler.__main__ import _subscription_renewal_schedule, build_jobs

        assert _subscription_renewal_schedule() is None
        assert RENEW_ROW not in {j[0] for j in build_jobs()}

    def test_row_registered_with_a_daily_default_when_enabled(self, monkeypatch):
        _sched_env_clean(monkeypatch)
        monkeypatch.setenv("AGNES_SHAREPOINT_ENABLED", "1")
        from services.scheduler.__main__ import _subscription_renewal_schedule, build_jobs

        assert _subscription_renewal_schedule() == "daily 04:30"
        jobs = {name: schedule for name, schedule, *_ in build_jobs()}
        assert jobs[RENEW_ROW] == "daily 04:30"

    def test_env_override_retimes_it(self, monkeypatch):
        _sched_env_clean(monkeypatch)
        monkeypatch.setenv("AGNES_SHAREPOINT_ENABLED", "1")
        monkeypatch.setenv("SCHEDULER_SUBSCRIPTION_RENEWAL_SCHEDULE", "daily 02:00")
        from services.scheduler.__main__ import _subscription_renewal_schedule

        assert _subscription_renewal_schedule() == "daily 02:00"

    def test_garbage_override_falls_back_to_the_default_not_to_disabled(self, monkeypatch):
        """The opposite of the extraction sweep's choice, deliberately: a
        renewal sweep that silently never runs costs the subscriptions."""
        _sched_env_clean(monkeypatch)
        monkeypatch.setenv("AGNES_SHAREPOINT_ENABLED", "1")
        monkeypatch.setenv("SCHEDULER_SUBSCRIPTION_RENEWAL_SCHEDULE", "not-a-schedule")
        from services.scheduler.__main__ import _subscription_renewal_schedule

        assert _subscription_renewal_schedule() == "daily 04:30"

    def test_row_targets_the_run_due_endpoint(self, monkeypatch):
        _sched_env_clean(monkeypatch)
        monkeypatch.setenv("AGNES_SHAREPOINT_ENABLED", "1")
        from services.scheduler.__main__ import build_jobs

        name, schedule, endpoint, method, _timeout = next(j for j in build_jobs() if j[0] == RENEW_ROW)
        assert endpoint == RUN_DUE
        assert method == "POST"

    def test_build_jobs_does_not_raise_with_the_row_present(self, monkeypatch):
        """Its `daily …` cadence must stay out of the tick-guard's smallest-
        interval computation, same as the extraction row."""
        _sched_env_clean(monkeypatch)
        monkeypatch.setenv("AGNES_SHAREPOINT_ENABLED", "1")
        from services.scheduler.__main__ import build_jobs

        assert any(j[0] == RENEW_ROW for j in build_jobs())


# ---------------------------------------------------------------------------
# Config carry-forward — the erasure class this repo has already been bitten
# by twice (scopes, extraction).
# ---------------------------------------------------------------------------


def test_an_unrelated_connection_edit_does_not_erase_subscription_state(seeded_app, graph):
    """`PUT /api/admin/source-connections/{id}` replaces `config` wholesale.
    Losing `webhook_subscriptions` would not merely lose bookkeeping — it
    would ORPHAN live Graph subscriptions Agnes could then neither renew nor
    delete."""
    c, token = seeded_app["client"], seeded_app["admin_token"]
    conn_id = _ready_connection(c, token)
    c.post(f"{BASE}/{conn_id}/subscriptions/ensure", headers=_auth(token))
    assert _config(conn_id)["webhook_subscriptions"]

    r = c.put(
        f"/api/admin/source-connections/{conn_id}",
        json={"name": "renamed", "config": {"tenant_id": "tenant-1", "client_id": "client-1"}},
        headers=_auth(token),
    )
    assert r.status_code == 200, r.text

    state = _config(conn_id)["webhook_subscriptions"]
    assert state and state[0]["subscription_id"] == "sub-1"
