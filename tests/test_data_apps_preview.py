"""Tests for the wave 3C in-chat preview loop: the scoped preview-grant REST
endpoint (`app/api/data_apps.py`, Task 5) and the four chat-surface MCP tools
(`app/api/mcp/foundation_tools.py`, Task 4).

Two fixture idioms:

  - ``preview_api_env`` — the ``api_env`` idiom of ``tests/test_data_apps_api.py``
    (real user/token/app rows, a real ``TestClient(app)``) for direct REST
    assertions against ``POST /{slug}/preview-grant``.
  - ``preview_env`` — ``(client, call_tool)``, mirroring the ASGI-replay idiom
    of ``tests/test_broker_routes.py``: the foundation tools' internal
    ``httpx.AsyncClient()`` self-calls are routed into the SAME in-process
    app via ``httpx.ASGITransport`` (no real network hop), so the MCP tool
    tests exercise the real `preview-grant`/`GET {slug}` endpoints end to
    end rather than mocking their response bodies.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid

import httpx
import pytest
import yaml
from cryptography.fernet import Fernet


def _auth(pat: str) -> dict:
    return {"Authorization": f"Bearer {pat}"}


def _enable_data_apps(data_dir) -> None:
    state = data_dir / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "instance.yaml").write_text(yaml.dump({"data_apps": {"enabled": True}}))
    import app.instance_config as instance_config

    instance_config._instance_config = None


@pytest.fixture
def preview_api_env(e2e_env, monkeypatch, shared_app):
    """Real user/token/app rows + TestClient(app), data_apps enabled — for
    direct REST assertions against `POST /{slug}/preview-grant`."""
    from app.auth.jwt import create_access_token
    from src.db import get_system_db
    from src.repositories.access_tokens import AccessTokenRepository
    from src.repositories.data_apps import DataAppsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.user_groups import UserGroupsRepository
    from src.repositories.users import UserRepository

    data_dir = e2e_env["data_dir"]
    monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())
    _enable_data_apps(data_dir)

    conn = get_system_db()
    try:
        users = UserRepository(conn)
        users.create(id="owner1", email="owner@test.local", name="Owner")
        users.create(id="other1", email="other@test.local", name="Other")
        users.create(id="grantee1", email="grantee@test.local", name="Grantee")

        ug = UserGroupsRepository(conn)
        gid = ug.create("Analysts", is_system=False)["id"]
        UserGroupMembersRepository(conn).add_member("grantee1", gid, source="test")

        token_repo = AccessTokenRepository(conn)
        pats: dict[str, str] = {}
        for uid, email in [
            ("owner1", "owner@test.local"),
            ("other1", "other@test.local"),
            ("grantee1", "grantee@test.local"),
        ]:
            tid = str(uuid.uuid4())
            jwt_token = create_access_token(uid, email, token_id=tid, typ="pat")
            token_repo.create(
                id=tid,
                user_id=uid,
                name=f"{uid}-pat",
                token_hash=hashlib.sha256(jwt_token.encode()).hexdigest(),
                prefix=tid.replace("-", "")[:8],
                expires_at=None,
            )
            pats[uid] = jwt_token

        apps_repo = DataAppsRepository(conn)
        apps_repo.create(slug="dash", name="DASH", owner_user_id="owner1")
        # Second app owned by the same user — two apps previewed by one browser
        # is the collision case (`test_two_apps_previews_coexist`).
        apps_repo.create(slug="dash-two", name="DASH TWO", owner_user_id="owner1")

        from src.repositories.resource_grants import ResourceGrantsRepository

        ResourceGrantsRepository(conn).create(group_id=gid, resource_type="data_app", resource_id="dash")
    finally:
        conn.close()

    app = shared_app
    from fastapi.testclient import TestClient

    client = TestClient(app)
    return {"client": client, "owner_pat": pats["owner1"], "other_pat": pats["other1"], "grantee_pat": pats["grantee1"]}


class TestPreviewGrantEndpoint:
    def test_owner_gets_scoped_cookie(self, preview_api_env):
        env = preview_api_env
        r = env["client"].post("/api/data-apps/dash/preview-grant", headers=_auth(env["owner_pat"]))
        assert r.status_code == 200, r.text
        body = r.json()
        assert "preview_cookie" in body and "expires_at" in body
        cookie = body["preview_cookie"]
        # `Path=/`, NOT `/apps/dash/`. The holding page polls
        # `/api/data-apps/dash/readiness`, which the narrower path does not
        # cover — a browser would never attach the credential, so the poll
        # 401'd forever and the preview spun while the app was up. The
        # per-app pin lives in the token scope (`data-app-preview:dash`),
        # never in the cookie path, so the wider path grants nothing extra.
        assert "Path=/;" in cookie
        assert "Path=/apps/" not in cookie
        assert "SameSite=Lax" in cookie
        assert "HttpOnly" in cookie
        # …and the per-app scoping the path used to provide implicitly now sits
        # in the NAME, so two apps' credentials still occupy two jar slots.
        assert cookie.startswith("adp_preview_dash=")
        # The cookie must ALSO ride a real Set-Cookie response header — an
        # HttpOnly cookie can only be installed by the browser via the server's
        # Set-Cookie (the frontend fetches this endpoint same-origin), never
        # through document.cookie, which silently discards HttpOnly cookies.
        set_cookie = r.headers.get("set-cookie", "")
        assert set_cookie.startswith("adp_preview_dash=") and "HttpOnly" in set_cookie

    def test_two_apps_previews_coexist(self, preview_api_env):
        """Previewing a second app must not knock out the first one's preview.

        A browser keys a cookie on `(name, domain, path)`. With `Path=/` — which
        the readiness poll needs — one shared cookie NAME means the second app's
        grant lands in the same jar slot and evicts the first's, and since the
        slug pin lives in the token scope, the survivor resolves to `(None,
        False)` for the other app: its poll 401s forever while the app is
        healthy. Two chat tabs previewing two apps is the ordinary case.

        The readiness poll goes out with NO `Authorization` header on purpose —
        that is exactly how the holding page's `fetch(..., {credentials: ...})`
        calls it, so the cookie jar is the only credential under test.
        """
        env = preview_api_env
        client = env["client"]

        assert client.post("/api/data-apps/dash/preview-grant", headers=_auth(env["owner_pat"])).status_code == 200
        assert client.post("/api/data-apps/dash-two/preview-grant", headers=_auth(env["owner_pat"])).status_code == 200

        first = client.get("/api/data-apps/dash/readiness")
        assert first.status_code == 200, f"the first app's preview was evicted by the second: {first.text}"
        second = client.get("/api/data-apps/dash-two/readiness")
        assert second.status_code == 200, second.text

        # Minting in the other order must be symmetric — neither app is special.
        client.cookies.clear()
        assert client.post("/api/data-apps/dash-two/preview-grant", headers=_auth(env["owner_pat"])).status_code == 200
        assert client.post("/api/data-apps/dash/preview-grant", headers=_auth(env["owner_pat"])).status_code == 200
        assert client.get("/api/data-apps/dash-two/readiness").status_code == 200
        assert client.get("/api/data-apps/dash/readiness").status_code == 200

    def test_a_preview_cookie_does_not_authorize_another_app(self, preview_api_env):
        """Per-app cookie naming must not become a way to reach a second app.

        The slug pin lives in the token's `data-app-preview:<slug>` scope, and
        that check is what a widened `Path` (and now a per-app name) leans on.
        A jar holding ONLY `dash`'s credential must still be refused on
        `dash-two`.
        """
        env = preview_api_env
        client = env["client"]

        assert client.post("/api/data-apps/dash/preview-grant", headers=_auth(env["owner_pat"])).status_code == 200
        assert client.get("/api/data-apps/dash/readiness").status_code == 200
        assert client.get("/api/data-apps/dash-two/readiness").status_code == 401

    def test_grantee_can_request_a_grant(self, preview_api_env):
        """Unlike git-credential, preview-grant is view-access, not
        owner/Admin-only — a group grantee (`_can_view` passes via
        `resource_grants`) may mint their own preview cookie."""
        env = preview_api_env
        r = env["client"].post("/api/data-apps/dash/preview-grant", headers=_auth(env["grantee_pat"]))
        assert r.status_code == 200, r.text

    def test_grant_minted_under_requester_not_app_owner(self, preview_api_env):
        """The preview token belongs to the CALLER who requested it (a grantee
        here, `grantee1`), not the app's owner (`owner1`). This is what lets the
        SessionEnd revoke — keyed on the chat session's own user — actually tear
        the grant down, so a grantee-previews-a-draft grant isn't left live for
        the full TTL after their session ends."""
        from src.repositories import access_token_repo
        from app.api.data_apps import revoke_preview_tokens_for_user

        env = preview_api_env
        r = env["client"].post("/api/data-apps/dash/preview-grant", headers=_auth(env["grantee_pat"]))
        assert r.status_code == 200, r.text

        repo = access_token_repo()

        def _live_preview_names(uid):
            return [
                t["name"]
                for t in repo.list_for_user(uid, include_revoked=False)
                if t["name"].startswith("data-app-preview:")
            ]

        assert _live_preview_names("grantee1") == ["data-app-preview:dash"], "token must be under the requester"
        assert _live_preview_names("owner1") == [], "token must NOT be under the app owner"

        # SessionEnd revoke keyed on the requester's id revokes it (the hard cap works;
        # pat_resolver rejects revoked tokens, so it stops serving immediately).
        revoke_preview_tokens_for_user("grantee1")
        assert _live_preview_names("grantee1") == []

    def test_stranger_forbidden(self, preview_api_env):
        env = preview_api_env
        r = env["client"].post("/api/data-apps/dash/preview-grant", headers=_auth(env["other_pat"]))
        assert r.status_code == 403

    def test_missing_app_404s(self, preview_api_env):
        env = preview_api_env
        r = env["client"].post("/api/data-apps/does-not-exist/preview-grant", headers=_auth(env["owner_pat"]))
        assert r.status_code == 404

    def test_mint_refusal_is_a_clean_400_not_a_500(self, preview_api_env, monkeypatch):
        """`_mint_preview_token` refuses (SlugNotCookieSafeError) a slug that
        is unsafe in a cookie name — a deliberate, now side-effect-free
        refusal, which the endpoint must surface as a 400 with a stable
        machine token rather than an unhandled 500 (Devin Review on this PR).
        Patched rather than seeded: every create path validates slugs, so a
        DB row that trips the check cannot be built through the API."""
        import app.api.data_apps as data_apps_api

        def _refuse(row, requester, **kw):
            raise data_apps_api.SlugNotCookieSafeError("data app slug is not safe in a cookie name")

        monkeypatch.setattr(data_apps_api, "_mint_preview_token", _refuse)
        env = preview_api_env
        r = env["client"].post("/api/data-apps/dash/preview-grant", headers=_auth(env["owner_pat"]))
        assert r.status_code == 400, r.text
        assert r.json()["detail"] == "slug_not_cookie_safe"

    def test_an_unrelated_mint_failure_is_not_relabelled_a_bad_slug(self, preview_api_env, monkeypatch):
        """A plain ValueError from the mint's internals (token claims, repo
        validation) is a backend fault, not a naming problem — it must
        surface as a 500, not don the `slug_not_cookie_safe` costume that
        sends users chasing a non-existent naming issue (Devin Review on
        this PR)."""
        import app.api.data_apps as data_apps_api

        def _backend_fault(row, requester, **kw):
            raise ValueError("access_tokens insert: expires_at out of range")

        monkeypatch.setattr(data_apps_api, "_mint_preview_token", _backend_fault)
        env = preview_api_env
        try:
            r = env["client"].post("/api/data-apps/dash/preview-grant", headers=_auth(env["owner_pat"]))
        except ValueError:
            return  # TestClient re-raises unhandled server exceptions: same verdict — not a 400
        assert r.status_code == 500, r.text

    def test_disabled_feature_404s(self, preview_api_env, monkeypatch):
        import app.api.data_apps as data_apps_api

        monkeypatch.setattr(data_apps_api, "feature_enabled", lambda *a, **k: False)
        env = preview_api_env
        r = env["client"].post("/api/data-apps/dash/preview-grant", headers=_auth(env["owner_pat"]))
        assert r.status_code == 404
        assert r.json()["detail"] == "data_apps_disabled"


# ---------------------------------------------------------------------------
# MCP tools — real REST round-trip via httpx.ASGITransport (no mocked bodies)
# ---------------------------------------------------------------------------


@pytest.fixture
def preview_env(e2e_env, monkeypatch, shared_app):
    """`(client, call_tool)` — real app + rows, `call_tool` invokes a
    registered foundation tool directly (mirrors `app.api.mcp_http.<name>`
    unit-test idiom), with the tool's internal `httpx.AsyncClient()`
    self-calls routed into the SAME in-process app via `ASGITransport`."""
    pytest.importorskip("mcp", reason="mcp package not installed")
    from app.auth.jwt import create_access_token
    from src.db import get_system_db
    from src.repositories.access_tokens import AccessTokenRepository
    from src.repositories.data_apps import DataAppsRepository
    from src.repositories.users import UserRepository

    data_dir = e2e_env["data_dir"]
    monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())
    _enable_data_apps(data_dir)

    conn = get_system_db()
    try:
        UserRepository(conn).create(id="owner1", email="owner@test.local", name="Owner")

        tid = str(uuid.uuid4())
        owner_pat = create_access_token("owner1", "owner@test.local", token_id=tid, typ="pat")
        AccessTokenRepository(conn).create(
            id=tid,
            user_id="owner1",
            name="owner1-pat",
            token_hash=hashlib.sha256(owner_pat.encode()).hexdigest(),
            prefix=tid.replace("-", "")[:8],
            expires_at=None,
        )

        apps_repo = DataAppsRepository(conn)
        apps_repo.create(slug="dash", name="DASH", owner_user_id="owner1")
        apps_repo.create(slug="dash--init", name="DASH INIT", owner_user_id="owner1")
    finally:
        conn.close()

    app = shared_app
    from fastapi.testclient import TestClient

    client = TestClient(app)

    import app.api.mcp_http as mcp_mod

    _RealAsyncClient = httpx.AsyncClient

    def _asgi_async_client(*args, **kwargs):
        return _RealAsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")

    monkeypatch.setattr(httpx, "AsyncClient", _asgi_async_client)

    def call_tool(name: str, **kwargs):
        token = mcp_mod._current_token.set(owner_pat)
        try:
            fn = getattr(mcp_mod, name)
            return asyncio.run(fn(**kwargs))
        finally:
            mcp_mod._current_token.reset(token)

    return client, call_tool


def test_preview_placeholder_then_live(preview_env):
    client, call_tool = preview_env
    first = call_tool("agnes_data_app_preview", slug="dash--init")
    assert first["render"] == "data_app_preview" and first["url"] is None
    # The scoped preview cookie is a live bearer credential and must NOT leak
    # into the tool result (archived in the session transcript). The frontend
    # installs it via a same-origin re-fetch of the grant endpoint instead.
    assert "preview_cookie" not in first

    live = call_tool("agnes_data_app_preview", slug="dash--init", url="/apps/dash--init/")
    assert live["render"] == "data_app_preview"
    assert live["slug"] == "dash--init"
    assert live["url"] == "/apps/dash--init/"
    assert "preview_cookie" not in live


def test_preview_refresh_render_directive(preview_env):
    client, call_tool = preview_env
    r = call_tool("agnes_data_app_refresh", slug="dash")
    assert r == {"render": "data_app_preview_refresh", "slug": "dash"}


def test_preview_close_render_directive(preview_env):
    client, call_tool = preview_env
    r = call_tool("agnes_data_app_close", slug="dash")
    assert r == {"render": "data_app_preview_close", "slug": "dash"}


def test_credentials_is_terminal_render(preview_env):
    client, call_tool = preview_env
    r = call_tool("agnes_data_app_credentials", slug="dash")
    assert r["render"] == "data_app_credentials" and r["url"]
    assert r["slug"] == "dash"


def test_preview_live_url_friendly_when_disabled(preview_env, monkeypatch):
    import app.api.data_apps as data_apps_api

    monkeypatch.setattr(data_apps_api, "feature_enabled", lambda *a, **k: False)
    client, call_tool = preview_env
    r = call_tool("agnes_data_app_preview", slug="dash", url="/apps/dash/")
    assert r == {
        "error": "data_apps_disabled",
        "message": "Data apps are disabled on this instance.",
    }


def test_credentials_friendly_when_disabled(preview_env, monkeypatch):
    import app.api.data_apps as data_apps_api

    monkeypatch.setattr(data_apps_api, "feature_enabled", lambda *a, **k: False)
    client, call_tool = preview_env
    r = call_tool("agnes_data_app_credentials", slug="dash")
    assert r["error"] == "data_apps_disabled"
