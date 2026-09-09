"""Route tests for the `/apps` web pages (Task 12): the data-apps list page
(`GET /apps`) and detail page (`GET /apps/detail/{slug}`).

Follows the `api_env`-fixture idiom of `tests/test_data_apps_api.py` — real
user/token rows via the DuckDB repos, `data_apps.enabled` flipped on in an
`instance.yaml` overlay, a real `TestClient(app)`.

Includes the route-collision regression test: the proxy's ingress catch-all
(`app/api/data_apps_proxy.py`, `GET /apps/{slug}/{path:path}`) would swallow
`/apps/detail/<slug>` as slug="detail" if it were registered before this
page's route — see `app/main.py`'s `include_router` ordering comment and
`app/web/router.py`'s `apps_web_router`.
"""

from __future__ import annotations

import hashlib
import uuid

import pytest
import yaml
from cryptography.fernet import Fernet


def _auth(pat: str) -> dict:
    return {"Authorization": f"Bearer {pat}"}


@pytest.fixture
def web_env(e2e_env, monkeypatch, shared_app):
    """Real user/token/group rows + TestClient(app), data_apps enabled."""
    from app.auth.jwt import create_access_token
    from src.db import get_system_db
    from src.repositories.users import UserRepository
    from src.repositories.access_tokens import AccessTokenRepository
    from src.repositories.user_groups import UserGroupsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository

    data_dir = e2e_env["data_dir"]
    monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())

    state = data_dir / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "instance.yaml").write_text(yaml.dump({"data_apps": {"enabled": True}}))
    import app.instance_config as instance_config

    instance_config._instance_config = None

    conn = get_system_db()
    try:
        users = UserRepository(conn)
        users.create(id="owner1", email="owner@test.local", name="Owner")
        users.create(id="other1", email="other@test.local", name="Other")
        users.create(id="admin1", email="admin@test.local", name="Admin")

        ug = UserGroupsRepository(conn)
        ug.ensure_system("Admin", "system")
        ug.ensure_system("Everyone", "system")
        admin_gid = conn.execute("SELECT id FROM user_groups WHERE name='Admin'").fetchone()[0]
        UserGroupMembersRepository(conn).add_member("admin1", admin_gid, source="system_seed")

        token_repo = AccessTokenRepository(conn)
        pats: dict[str, str] = {}
        for uid, email in [
            ("owner1", "owner@test.local"),
            ("other1", "other@test.local"),
            ("admin1", "admin@test.local"),
        ]:
            tid = str(uuid.uuid4())
            jwt = create_access_token(uid, email, token_id=tid, typ="pat")
            token_repo.create(
                id=tid,
                user_id=uid,
                name=f"{uid}-pat",
                token_hash=hashlib.sha256(jwt.encode()).hexdigest(),
                prefix=tid.replace("-", "")[:8],
                expires_at=None,
            )
            pats[uid] = jwt
    finally:
        conn.close()

    app = shared_app
    from fastapi.testclient import TestClient

    client = TestClient(app)
    return {
        "client": client,
        "owner_pat": pats["owner1"],
        "other_pat": pats["other1"],
        "admin_pat": pats["admin1"],
        "data_dir": data_dir,
    }


def _create_app_row(slug="myapp", owner_id="owner1", name="My App", state="stopped"):
    from src.db import get_system_db
    from src.repositories.data_apps import DataAppsRepository

    conn = get_system_db()
    try:
        repo = DataAppsRepository(conn)
        app_id = repo.create(slug=slug, name=name, owner_user_id=owner_id)
        if state != "created":
            repo.set_state(app_id, state)
    finally:
        conn.close()
    return app_id


# ---------------------------------------------------------------------------
# List page
# ---------------------------------------------------------------------------


def test_list_page_requires_login(web_env):
    c = web_env["client"]
    resp = c.get("/apps", follow_redirects=False)
    assert resp.status_code in (302, 307, 401, 403)
    if resp.status_code in (302, 307):
        assert "/login" in resp.headers.get("location", "")


def test_list_page_renders_for_authenticated_user(web_env):
    c = web_env["client"]
    resp = c.get("/apps", headers=_auth(web_env["owner_pat"]))
    assert resp.status_code == 200
    assert "Data apps" in resp.text


def test_list_page_shows_owned_app_row(web_env):
    _create_app_row(slug="myapp", owner_id="owner1", name="My App", state="running")
    c = web_env["client"]
    resp = c.get("/apps", headers=_auth(web_env["owner_pat"]))
    assert resp.status_code == 200
    assert "myapp" in resp.text
    assert "My App" in resp.text
    assert "running" in resp.text
    assert "/apps/detail/myapp" in resp.text


def test_list_page_hides_apps_from_stranger(web_env):
    _create_app_row(slug="secretapp", owner_id="owner1", name="Secret")
    c = web_env["client"]
    resp = c.get("/apps", headers=_auth(web_env["other_pat"]))
    assert resp.status_code == 200
    assert "secretapp" not in resp.text


def test_list_page_hides_drafts(web_env):
    """Drafts are excluded from every other data-apps listing surface
    (`GET /api/data-apps`, `agnes app list`, the MCP `data_apps_list` tool)
    via `include_drafts=False` -- the human-facing `/apps` page must not be
    the one place they leak through."""
    from src.db import get_system_db
    from src.repositories.data_apps import DataAppsRepository

    parent_id = _create_app_row(slug="parentapp", owner_id="owner1", name="Parent", state="running")
    conn = get_system_db()
    try:
        DataAppsRepository(conn).create_draft(
            parent_app_id=parent_id,
            slug="parentapp--init",
            branch="init",
            owner_user_id="owner1",
        )
    finally:
        conn.close()

    c = web_env["client"]
    resp = c.get("/apps", headers=_auth(web_env["owner_pat"]))
    assert resp.status_code == 200
    assert "parentapp" in resp.text
    assert "parentapp--init" not in resp.text


def test_list_page_disabled_shows_empty_state(web_env):
    import app.instance_config as instance_config
    from pathlib import Path

    state_dir = Path(web_env["data_dir"]) / "state"
    (state_dir / "instance.yaml").write_text(yaml.dump({"data_apps": {"enabled": False}}))
    instance_config._instance_config = None

    c = web_env["client"]
    resp = c.get("/apps", headers=_auth(web_env["owner_pat"]))
    assert resp.status_code == 200
    assert "enable" in resp.text.lower()
    assert "data_apps" in resp.text or "instance config" in resp.text.lower()


# ---------------------------------------------------------------------------
# Detail page
# ---------------------------------------------------------------------------


def test_detail_page_requires_login(web_env):
    _create_app_row(slug="myapp")
    c = web_env["client"]
    resp = c.get("/apps/detail/myapp", follow_redirects=False)
    assert resp.status_code in (302, 307, 401, 403)


def test_detail_page_renders_for_owner_with_deploy_button(web_env):
    _create_app_row(slug="myapp", owner_id="owner1", state="stopped")
    c = web_env["client"]
    resp = c.get("/apps/detail/myapp", headers=_auth(web_env["owner_pat"]))
    assert resp.status_code == 200
    assert "myapp" in resp.text
    assert "Deploy" in resp.text
    assert "Stop" in resp.text
    assert "/api/data-apps/" in resp.text
    assert "logs?tail=200" in resp.text
    # The grant hint points at the Access workspace — the one place a group's
    # members and its grants are edited. (It used to name the per-group
    # detail page; that page 308s here, and the two surfaces are one now.)
    assert "/admin/access" in resp.text


def test_detail_page_for_granted_non_owner_hides_deploy_and_logs(web_env):
    _create_app_row(slug="granted1", owner_id="owner1", state="running")

    from src.db import get_system_db
    from src.repositories.user_groups import UserGroupsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.resource_grants import ResourceGrantsRepository

    conn = get_system_db()
    try:
        gid = UserGroupsRepository(conn).create(name="Viewers", description="d")["id"]
        UserGroupMembersRepository(conn).add_member("other1", gid, source="admin")
        ResourceGrantsRepository(conn).create(group_id=gid, resource_type="data_app", resource_id="granted1")
    finally:
        conn.close()

    c = web_env["client"]
    resp = c.get("/apps/detail/granted1", headers=_auth(web_env["other_pat"]))
    assert resp.status_code == 200
    assert "granted1" in resp.text
    assert 'id="dda-deploy-btn"' not in resp.text
    assert 'id="dda-stop-btn"' not in resp.text
    assert "logs?tail=200" not in resp.text


def test_detail_page_for_stranger_403s(web_env):
    _create_app_row(slug="myapp", owner_id="owner1")
    c = web_env["client"]
    resp = c.get("/apps/detail/myapp", headers=_auth(web_env["other_pat"]))
    assert resp.status_code == 403


def test_detail_page_missing_app_404s(web_env):
    c = web_env["client"]
    resp = c.get("/apps/detail/does-not-exist", headers=_auth(web_env["owner_pat"]))
    assert resp.status_code == 404


def test_detail_page_renders_for_admin_with_deploy_button(web_env):
    _create_app_row(slug="adminview", owner_id="owner1", state="stopped")
    c = web_env["client"]
    resp = c.get("/apps/detail/adminview", headers=_auth(web_env["admin_pat"]))
    assert resp.status_code == 200
    assert 'id="dda-deploy-btn"' in resp.text


# ---------------------------------------------------------------------------
# Sharing + data identity (TCRD-291) — the Library's reusable Share dialog,
# wired onto the data-app detail page, plus the owner/viewer data-identity
# toggle. `data_identity` is a sibling change to `_serialize()`; these tests
# also pin that the page renders correctly while that field is still absent
# from the serialized row.
# ---------------------------------------------------------------------------


def _grant_data_app(slug: str, user_id: str, group_name: str) -> None:
    """Grant ``user_id`` view access to data app ``slug`` via a fresh group,
    mirroring ``test_detail_page_for_granted_non_owner_hides_deploy_and_logs``
    above."""
    from src.db import get_system_db
    from src.repositories.resource_grants import ResourceGrantsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.user_groups import UserGroupsRepository

    conn = get_system_db()
    try:
        gid = UserGroupsRepository(conn).create(name=group_name, description="d")["id"]
        UserGroupMembersRepository(conn).add_member(user_id, gid, source="admin")
        ResourceGrantsRepository(conn).create(group_id=gid, resource_type="data_app", resource_id=slug)
    finally:
        conn.close()


def test_detail_page_owner_sees_share_chip_and_identity_toggle(web_env):
    """The owner is a manager: the Sharing chip is an interactive control
    (``data-share-open``) and the hosted, non-draft app offers the data
    identity toggle."""
    _create_app_row(slug="ownershare", owner_id="owner1", state="stopped")
    c = web_env["client"]
    resp = c.get("/apps/detail/ownershare", headers=_auth(web_env["owner_pat"]))
    assert resp.status_code == 200
    assert 'id="dda-vis"' in resp.text
    assert "data-share-open" in resp.text
    assert 'data-share-type="data_app"' in resp.text
    assert 'data-share-id="ownershare"' in resp.text
    assert 'id="dda-identity"' in resp.text
    assert 'id="dda-identity-save"' in resp.text
    # share_dialog.js's assets ride along only when the caller may re-share.
    assert "share_dialog.js" in resp.text


def test_detail_page_granted_viewer_sees_readonly_chip_and_no_toggle(web_env):
    """A granted non-owner, non-admin viewer sees the visibility chip as a
    read-out only — no ``data-share-open`` control, and no data-identity
    toggle (owner/Admin-only)."""
    _create_app_row(slug="grantedshare", owner_id="owner1", state="running")
    _grant_data_app("grantedshare", "other1", "dda-share-viewers")

    c = web_env["client"]
    resp = c.get("/apps/detail/grantedshare", headers=_auth(web_env["other_pat"]))
    assert resp.status_code == 200
    assert 'id="dda-vis"' in resp.text
    assert "data-share-open" not in resp.text
    assert 'id="dda-identity"' not in resp.text
    assert 'id="dda-identity-save"' not in resp.text
    # No control for this caller, so the dialog's own assets are not shipped.
    assert "share_dialog.js" not in resp.text


def test_detail_page_owner_mode_shows_publishing_warning(web_env):
    """A hosted app defaults to owner-mode data identity (absent field ==
    ``owner``): the note warns that sharing publishes what the owner's own
    grants can fetch."""
    _create_app_row(slug="ownermode", owner_id="owner1", state="stopped")
    c = web_env["client"]
    resp = c.get("/apps/detail/ownermode", headers=_auth(web_env["owner_pat"]))
    assert resp.status_code == 200
    # The RENDERED note, not the whole page: the save script legitimately
    # carries both texts so it can re-render the note after a mode flip.
    note = _identity_note(resp.text)
    assert "Sharing this app publishes what it fetches." in note
    assert "Each viewer sees only what their own grants allow" not in note


def _identity_note(page_html: str) -> str:
    """Text of the server-rendered ``#dda-identity-note`` paragraph, entities decoded."""
    import html as _html
    import re

    m = re.search(r'<p class="dda-identity-note" id="dda-identity-note">(.*?)</p>', page_html, re.S)
    assert m, "identity note not rendered"
    return _html.unescape(" ".join(m.group(1).split()))


def test_detail_linked_app_share_control_is_admin_only(web_env):
    """A linked row's synthetic ``system`` owner never matches a real
    visitor, so a granted non-admin viewer gets the read-only chip while an
    admin gets the real control -- mirrors the sharing-REST contract."""
    _create_linked_row(slug="kbc-identity-linked", name="Linked")
    _grant_data_app("kbc-identity-linked", "other1", "dda-linked-viewers")

    c = web_env["client"]
    admin_resp = c.get("/apps/detail/kbc-identity-linked", headers=_auth(web_env["admin_pat"]))
    assert admin_resp.status_code == 200
    assert "data-share-open" in admin_resp.text

    viewer_resp = c.get("/apps/detail/kbc-identity-linked", headers=_auth(web_env["other_pat"]))
    assert viewer_resp.status_code == 200
    assert "data-share-open" not in viewer_resp.text
    # Linked apps have no hosted lifecycle, so no identity toggle either way.
    assert 'id="dda-identity"' not in admin_resp.text


def test_detail_page_renders_when_data_identity_absent_from_serialized_row(web_env):
    """`_serialize()` does not (yet) return `data_identity` in this worktree
    -- the router must default it to 'owner' rather than erroring, and the
    toggle must reflect that default."""
    _create_app_row(slug="noidentity", owner_id="owner1", state="stopped")
    c = web_env["client"]
    resp = c.get("/apps/detail/noidentity", headers=_auth(web_env["owner_pat"]))
    assert resp.status_code == 200
    assert 'id="dda-identity"' in resp.text
    assert '<option value="owner" selected>' in resp.text


# ---------------------------------------------------------------------------
# Route-collision regression: /apps/detail/{slug} must hit the web detail
# page, NOT the ingress proxy's `/apps/{slug}/{path:path}` catch-all.
# ---------------------------------------------------------------------------


def test_bare_apps_path_hits_list_page_not_proxy(web_env):
    c = web_env["client"]
    resp = c.get("/apps", headers=_auth(web_env["owner_pat"]))
    assert resp.status_code == 200
    assert "Data apps" in resp.text
    assert "content-type" in {k.lower() for k in resp.headers.keys()}
    assert "text/html" in resp.headers["content-type"]


def test_apps_detail_path_does_not_collide_with_proxy_catchall(web_env):
    """A data app literally named 'detail' would be shadowed by the proxy's
    `/apps/{slug}/{path:path}` catch-all if the web router's `/apps/detail/*`
    route were registered after it. Assert the detail page — not the proxy's
    JSON/redirect/holding-page response for an app named 'detail' — is what
    actually answers `/apps/detail/<real-slug>`."""
    _create_app_row(slug="realslug", owner_id="owner1", state="stopped")
    c = web_env["client"]
    resp = c.get("/apps/detail/realslug", headers=_auth(web_env["owner_pat"]))
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "realslug" in resp.text
    # If the proxy's catch-all had actually swallowed this request (matching
    # slug="detail", path="realslug"), it would 404 with this JSON detail
    # instead of rendering the HTML detail page.
    assert '"data_app_not_found"' not in resp.text


def test_apps_detail_anything_hits_web_route_404_not_proxy(web_env):
    """Documents the OTHER half of the reserved-slug invariant (the create-time
    rejection lives in `src.data_apps.spec.RESERVED_SLUGS`, enforced by
    `app/api/data_apps.py::create_data_app` — see
    `tests/test_data_apps_api.py::test_reserved_slug_rejected`).

    Because no data app can ever be named "detail", `GET /apps/detail/<any
    unknown segment>` is SAFE to own outright as the web route
    (`app/web/router.py`'s `apps_web_router`, registered ahead of the ingress
    proxy) — it always resolves as "detail page for app slug=<segment>", 404s
    with the web route's own `data_app_not_found` when no such app exists, and
    never needs to fall through to the proxy's `/apps/{slug}/{path:path}`
    catch-all for an app literally named "detail" (there can't be one).
    """
    c = web_env["client"]
    resp = c.get("/apps/detail/anything", headers=_auth(web_env["owner_pat"]))
    assert resp.status_code == 404
    assert resp.json()["detail"] == "data_app_not_found"


# ---------------------------------------------------------------------------
# Linked apps (v108)
# ---------------------------------------------------------------------------


def _create_linked_row(slug="kbc-sales", name="Sales", url="https://example.com/apps/sales", desc="synced"):
    from src.db import get_system_db
    from src.repositories.data_apps import DataAppsRepository

    conn = get_system_db()
    try:
        DataAppsRepository(conn).upsert_linked(
            slug=slug, source_ref=f"conn1:{slug}", name=name, description=desc, external_url=url
        )
    finally:
        conn.close()


def test_list_page_shows_linked_row_with_external_open(web_env):
    c = web_env["client"]
    _create_linked_row()
    resp = c.get("/apps", headers=_auth(web_env["admin_pat"]))
    assert resp.status_code == 200
    assert "kbc-sales" in resp.text
    assert "https://example.com/apps/sales" in resp.text  # Open → external URL
    assert ">linked<" in resp.text  # linked badge


def test_detail_linked_hides_deploy_shows_override_form(web_env):
    c = web_env["client"]
    _create_linked_row()
    resp = c.get("/apps/detail/kbc-sales", headers=_auth(web_env["admin_pat"]))
    assert resp.status_code == 200
    # linked apps have no deploy lifecycle
    assert "dda-deploy-btn" not in resp.text
    # admin gets the description-override affordance
    assert "dda-desc-form" in resp.text
    # opens at the external URL
    assert "https://example.com/apps/sales" in resp.text


def test_admin_linked_apps_page_renders_for_an_admin(web_env):
    """Publishing apps from an already-connected server is its own errand, and
    its own page: the MCP builder covers the server being connected NOW, this
    covers the one connected weeks ago. Both render the same panel."""
    c = web_env["client"]
    resp = c.get("/admin/linked-apps", headers=_auth(web_env["admin_pat"]))
    assert resp.status_code == 200, resp.text
    assert 'id="la-view"' in resp.text
    assert "linked_apps_panel.js" in resp.text, "the page does not load the shared panel"


def test_the_old_new_linked_app_path_lands_on_that_page(web_env):
    """There is no separate create step any more — publishing apps IS picking a
    connected server and reading its list."""
    c = web_env["client"]
    resp = c.get("/admin/linked-apps/new", headers=_auth(web_env["admin_pat"]), follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/admin/linked-apps"


def test_admin_linked_apps_wizard_forbidden_for_non_admin(web_env):
    c = web_env["client"]
    resp = c.get("/admin/linked-apps", headers=_auth(web_env["owner_pat"]), follow_redirects=False)
    assert resp.status_code in (302, 307, 401, 403)


def _hide_linked_row(slug: str) -> None:
    """Flip an existing linked row to the soft-deleted state the reconciler
    uses when the app disappears upstream."""
    from src.db import get_system_db

    conn = get_system_db()
    try:
        conn.execute("UPDATE data_apps SET state = 'linked_hidden' WHERE slug = ?", [slug])
    finally:
        conn.close()


def test_list_page_hides_soft_deleted_linked_apps(web_env):
    """`linked_hidden` rows (gone upstream, kept for lossless re-link) are
    excluded by the API list/detail/PATCH — the web /apps page must not be
    the one surface still showing an Open link onto a dead external URL
    (Devin Review on #1116)."""
    _create_linked_row(slug="kbc-gone-abc123", name="Gone")
    _hide_linked_row("kbc-gone-abc123")
    c = web_env["client"]
    resp = c.get("/apps", headers=_auth(web_env["admin_pat"]))
    assert resp.status_code == 200
    assert "kbc-gone-abc123" not in resp.text


def test_detail_page_404s_soft_deleted_linked_app(web_env):
    _create_linked_row(slug="kbc-gone-def456", name="Gone2")
    _hide_linked_row("kbc-gone-def456")
    c = web_env["client"]
    resp = c.get("/apps/detail/kbc-gone-def456", headers=_auth(web_env["admin_pat"]))
    assert resp.status_code == 404


def test_app_publishing_calls_real_access_endpoints():
    """Group/grant calls must hit the mounted /api/admin router — /api/access/*
    does not exist and 404s silently in the UI (Devin Review on #1116).

    Follows the code: both surfaces that publish apps — the MCP builder's
    section and /admin/linked-apps — drive one panel, and the panel is where
    the calls live.
    """
    src = open("app/web/static/js/components/linked_apps_panel.js").read()
    assert "/api/admin/grants" in src
    assert "/api/access/" not in src
    # It is the ONE caller that designates its tool as the data-app lister —
    # without the flag the server never projects a targeted run.
    assert "lister: true" in src


# ---------------------------------------------------------------------------
# URL clickability on the detail page — must follow what the proxy would serve
# ---------------------------------------------------------------------------
#
# Only the DETAIL page is exercised here, deliberately. The Library's
# Artifacts band is the live inventory surface (`/apps` 302s into it whenever
# the feature is on and the caller has a visible app row), and its rows link to
# `/apps/detail/<slug>` (`app/web/router.py::library_page`) — so the detail
# page's URL field is the only place a viewer is offered the app itself.
# `data_apps.html` keeps the same corrected condition for consistency, but it
# renders its table only on the fallback path the redirect leaves behind (the
# caller has nothing visible), where there is no row to assert on.
#
# `reachable` answers TWO gates the proxy applies in order, so both are pinned:
# whether the deployment can serve hosted apps at all, and whether this app's
# state is one the proxy serves from.


def _set_data_apps_config(data_dir, **overrides):
    """Rewrite the `data_apps` overlay `web_env` wrote and drop the cached
    config, so a test can vary the serving configuration."""
    import app.instance_config as instance_config

    cfg = {"enabled": True, **overrides}
    (data_dir / "state" / "instance.yaml").write_text(yaml.dump({"data_apps": cfg}))
    instance_config._instance_config = None


@pytest.mark.parametrize("state", ["running", "sleeping", "deploying"])
def test_detail_page_links_the_url_when_the_proxy_would_serve_it(web_env, state):
    """A sleeping app wakes on request for anyone allowed to view it
    (``data_apps_proxy.proxy_app``: ``sleeping`` triggers a wake and answers
    the holding page, ``deploying`` answers it outright), so its URL has to be
    a link. The page used to test ``state == 'running'`` alone, which left a
    granted viewer nothing to click on the very states that would have worked
    — and no way out, since the endpoint that restarts an app is
    owner/Admin-only.
    """
    _set_data_apps_config(web_env["data_dir"], allow_same_origin=True)
    _create_app_row(slug="wake1", owner_id="owner1", state=state)
    resp = web_env["client"].get("/apps/detail/wake1", headers=_auth(web_env["owner_pat"]))
    assert resp.status_code == 200
    assert '<a href="/apps/wake1/"' in resp.text


@pytest.mark.parametrize("state", ["created", "stopped", "error"])
def test_detail_page_does_not_link_the_url_when_the_proxy_would_refuse(web_env, state):
    _set_data_apps_config(web_env["data_dir"], allow_same_origin=True)
    _create_app_row(slug="dead1", owner_id="owner1", state=state)
    resp = web_env["client"].get("/apps/detail/dead1", headers=_auth(web_env["owner_pat"]))
    assert resp.status_code == 200
    assert '<a href="/apps/dead1/"' not in resp.text
    assert "<code>/apps/dead1/</code>" in resp.text


@pytest.mark.parametrize("state", ["running", "sleeping"])
def test_detail_page_does_not_link_the_url_when_the_deployment_cannot_serve_apps(web_env, state):
    """``_same_origin_serving_refused`` runs BEFORE the state branches, so on a
    deployment with neither ``subdomain_base`` nor ``allow_same_origin`` every
    hosted app is refused with a 403 page — a ``running`` one exactly as
    readily as a sleeping one. Offering the link there is a lie regardless of
    state, and this is the configuration ``web_env`` leaves in place by
    default. (Devin Review on #2336.)
    """
    _create_app_row(slug="noserve", owner_id="owner1", state=state)
    resp = web_env["client"].get("/apps/detail/noserve", headers=_auth(web_env["owner_pat"]))
    assert resp.status_code == 200
    assert '<a href="/apps/noserve/"' not in resp.text
    assert "<code>/apps/noserve/</code>" in resp.text


def test_detail_page_identity_note_is_about_the_owner_not_the_reader(web_env):
    """An Admin managing someone else's app must not read "YOUR grants": the
    app reads data under its OWNER's grants regardless of who is looking at
    the page (Devin Review on #2383). The note names the owner and reads the
    same for the owner and for the admin."""
    _create_app_row(slug="adminview", owner_id="owner1", state="stopped")
    c = web_env["client"]
    admin_resp = c.get("/apps/detail/adminview", headers=_auth(web_env["admin_pat"]))
    owner_resp = c.get("/apps/detail/adminview", headers=_auth(web_env["owner_pat"]))
    import html as _html

    for resp in (admin_resp, owner_resp):
        assert resp.status_code == 200
        page = _html.unescape(resp.text)
        assert "YOUR grants" not in page
        assert "under your grants" not in page
        assert "under the app owner's grants (owner@test.local)" in _identity_note(resp.text)
        assert "runs under the app owner's grants" in page  # the selector label


def test_detail_page_identity_script_rerenders_the_note_from_the_response(web_env):
    """After a successful save the explanatory note must describe the mode the
    server now holds, not the one the page was rendered with — the script
    carries both texts and keys them off ``body.data_identity``; a failed
    save returns before that line, so the old text survives there."""
    _create_app_row(slug="notelive", owner_id="owner1", state="stopped")
    c = web_env["client"]
    resp = c.get("/apps/detail/notelive", headers=_auth(web_env["owner_pat"]))
    assert resp.status_code == 200
    html = resp.text
    assert 'id="dda-identity-note"' in html
    script = html[html.index('const noteEl = document.getElementById("dda-identity-note")') :]
    assert "const NOTES = {" in script
    assert "Sharing this app publishes what it fetches." in script
    assert "Each viewer sees only what their own grants allow" in script
    assert "NOTES[body.data_identity]" in script
    # The re-render happens on the success path only: it sits after the
    # `!r.ok` early return and before the `catch`.
    assert script.index("if (!r.ok)") < script.index("NOTES[body.data_identity]") < script.index("} catch (e)")
