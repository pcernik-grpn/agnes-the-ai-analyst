"""Tests for the /chat web route — HTML rendering and redirect-when-disabled.

Fixture pattern: build a minimal FastAPI app with the web router attached,
set app.state.chat_config manually, and override get_current_user.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth.dependencies import get_current_user


TEST_USER = {"id": "user1", "email": "alice@test.com", "is_admin": False}


def _make_app(*, chat_enabled: bool = True) -> FastAPI:
    """Build a minimal FastAPI test app with the web router attached."""
    from app.web.router import router as web_router

    app = FastAPI()
    app.include_router(web_router)

    # Wire chat_config so the /chat route can check .enabled
    app.state.chat_config = SimpleNamespace(enabled=chat_enabled)

    # Override auth so we don't need a running DuckDB system.db
    app.dependency_overrides[get_current_user] = lambda: TEST_USER

    return app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _grant_chat_access(monkeypatch, tmp_path):
    """Chat is an RBAC resource (default-deny); the /chat route redirects users
    without the grant, and the nav link only shows with an explicit grant.
    These tests cover HTML rendering + the disabled-redirect + nav consistency,
    not the gate, so simulate "access granted" by patching both the route guard
    (`can_access`) and the nav-visibility check (`has_explicit_grant`). The
    default-deny gate is covered by test_chat_api::test_chat_requires_rbac_grant.

    Also pin DATA_DIR to a per-test tmp dir: `_build_context` opens its own
    `get_system_db()` when no conn is threaded (the nav `can_chat` path) and
    the /chat route's `_get_db` opens it too. On the shared default DATA_DIR
    those collide across xdist workers (`_duckdb.IOException: Conflicting
    lock`); an isolated path per test keeps the suite deterministic under -n.
    """
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "sysdb"))
    import app.auth.access as _access

    monkeypatch.setattr(_access, "can_access", lambda *a, **k: True)
    monkeypatch.setattr(_access, "has_explicit_grant", lambda *a, **k: True)


@pytest.fixture
def api_client() -> TestClient:
    return TestClient(_make_app(chat_enabled=True))


@pytest.fixture
def api_client_chat_disabled() -> TestClient:
    return TestClient(_make_app(chat_enabled=False))


@pytest.fixture
def logged_in_user():
    """Dummy fixture referenced by plan tests — value unused, auth is overridden."""
    return TEST_USER


# ---------------------------------------------------------------------------
# Tests (per Task 9.1 Step 1)
# ---------------------------------------------------------------------------


def test_chat_route_html(api_client: TestClient, logged_in_user):
    r = api_client.get("/chat")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    # Template renders `Chat — {{ config.INSTANCE_NAME or (instance_brand or
    # 'Agnes') }}` — the brand fallback fires here because the test env has
    # no instance.yaml. Substring assertion so a real INSTANCE_NAME (e.g.
    # "Agnes Dev") in a deployed env also passes.
    assert "<title>Chat — " in r.text
    # Page must go through _build_context so the Agnes chrome renders —
    # otherwise the four base stylesheets get empty href= and the nav
    # block short-circuits on `{% if session.user %}`. Pin both.
    # The chrome is the rail since Wave 0 (2026-08); `class="app-header"` was
    # the retired topnav's marker.
    assert 'class="rail"' in r.text
    assert "/static/style-custom.css" in r.text
    assert 'class="chat-page-body"' in r.text


def test_chat_route_composer_renders_without_legacy_welcome_cards(api_client: TestClient, logged_in_user):
    """chat.html is single-surface now (no more ``ui_layout`` branching): the
    composer form always renders, and the frozen pre-redesign capability-cards
    partial (deleted alongside the topnav chrome) never does."""
    r = api_client.get("/chat")
    assert r.status_code == 200
    assert 'id="chat-form"' in r.text
    # Markers unique to the deleted legacy partial — proves it isn't reachable
    # by any remaining code path, not just that the literal filename is gone.
    assert "cloud-chat-cap-card" not in r.text
    assert "What can I help you with?" not in r.text
    # The dashboard empty state (its replacement) is what actually renders.
    assert 'id="chat-capabilities"' in r.text
    assert "Ask" in r.text and "anything" in r.text


def test_chat_route_redirects_when_disabled(api_client_chat_disabled: TestClient, logged_in_user):
    r = api_client_chat_disabled.get("/chat", follow_redirects=False)
    assert r.status_code in (302, 307)


def test_can_chat_computed_without_conn_threaded():
    """Regression: the Chat nav link must render on EVERY page for a user with
    access — not only routes that thread a ``conn`` into ``_build_context``.

    The bug: ``_build_context`` set ``can_chat`` from the *passed* ``conn``, but
    most page routes call it with only ``user=`` (no conn). So ``can_chat`` was
    True on /chat + /dashboard (which thread conn) and False everywhere else
    (e.g. /marketplace), making the nav link flicker in and out as you moved
    between pages. The fix opens a short-lived system-db cursor when no conn is
    passed, so visibility is consistent. ``has_explicit_grant`` is patched True
    by the autouse fixture, so this isolates the conn-threading behavior.
    """
    from types import SimpleNamespace as _NS

    from starlette.requests import Request
    from app.web.router import _build_context

    # Minimal ASGI scope + an app whose state carries an enabled chat_config.
    app = _NS(state=_NS(chat_config=_NS(enabled=True)))
    scope = {
        "type": "http",
        "app": app,
        "method": "GET",
        "path": "/",
        "query_string": b"",
        "headers": [],
        "server": ("test", 80),
        "scheme": "http",
        "client": ("1.2.3.4", 9),
    }
    request = Request(scope)

    # Mirror the common route call: user supplied, but NO conn threaded.
    ctx = _build_context(request, user=TEST_USER)
    assert ctx["can_chat"] is True


def test_chrome_ctx_includes_can_chat():
    """Regression: pages rendered through ``_chrome_ctx`` (the studio pages,
    /me/memory-mining, /admin/store/lint) dropped the Chat nav link — the
    helper never computed ``can_chat``, so the header's ``{% if can_chat %}``
    gate saw Jinja-undefined and hid the link while every ``_build_context``
    page showed it. Visibility must be identical across the two context
    builders. ``has_explicit_grant`` is patched True by the autouse fixture."""
    from types import SimpleNamespace as _NS

    from starlette.requests import Request
    from app.web.router import _chrome_ctx

    app = _NS(state=_NS(chat_config=_NS(enabled=True)))
    scope = {
        "type": "http",
        "app": app,
        "method": "GET",
        "path": "/admin/studio",
        "query_string": b"",
        "headers": [],
        "server": ("test", 80),
        "scheme": "http",
        "client": ("1.2.3.4", 9),
    }
    request = Request(scope)

    ctx = _chrome_ctx(request, TEST_USER)
    assert ctx["can_chat"] is True


def test_can_chat_hidden_for_admin_without_explicit_grant(monkeypatch):
    """The nav link tracks the explicit grant, NOT god-mode: an admin with no
    chat grant on any of their groups does not see the Chat link, even though
    `can_access` would let them reach /chat by URL. Pins the decoupling done
    in `_build_context` (has_explicit_grant, not can_access)."""
    from types import SimpleNamespace as _NS

    from starlette.requests import Request
    import app.auth.access as _access
    from app.web.router import _build_context

    # Override the autouse "granted" patch: no group holds a chat grant, but
    # god-mode WOULD grant effective access. The nav must still hide the link.
    monkeypatch.setattr(_access, "has_explicit_grant", lambda *a, **k: False)
    monkeypatch.setattr(_access, "can_access", lambda *a, **k: True)

    app = _NS(state=_NS(chat_config=_NS(enabled=True)))
    scope = {
        "type": "http",
        "app": app,
        "method": "GET",
        "path": "/",
        "query_string": b"",
        "headers": [],
        "server": ("test", 80),
        "scheme": "http",
        "client": ("1.2.3.4", 9),
    }
    request = Request(scope)

    admin_user = {"id": "admin1", "email": "admin@test.com", "is_admin": True}
    ctx = _build_context(request, user=admin_user)
    assert ctx["can_chat"] is False


class TestSkillUploadDialogDestinations:
    """The chat Skill/Agent/Plugin dialog offers its destinations inline.

    It used to be a Store-only "quick submit" that deferred metadata editing to
    ``/store/upload`` — a route that no longer exists (the full form moved to
    ``/store/new``), so the dialog pointed twice at a 404 and gave the uploader
    no say in where the bundle landed.
    """

    def test_no_dead_store_upload_link(self, api_client: TestClient):
        r = api_client.get("/chat")
        assert r.status_code == 200
        assert "/store/upload" not in r.text, (
            "chat dialog links to /store/upload, which 404s — the full upload form is /store/new"
        )
        assert 'href="/store/new"' in r.text

    def test_three_destination_checkboxes_with_private_default(self, api_client: TestClient):
        r = api_client.get("/chat")
        body = r.text
        # Library: stated but not negotiable — every upload lands there.
        assert 'id="chat-store-library"' in body
        lib = body.split('id="chat-store-library"')[1].split(">")[0]
        assert "checked" in lib and "disabled" in lib
        # Stack: a real choice, pre-checked.
        assert 'id="chat-store-stack"' in body
        stack = body.split('id="chat-store-stack"')[1].split(">")[0]
        assert "checked" in stack
        assert "disabled" not in stack
        # Sharing: a real choice, and OFF by default — private is the default
        # for anything uploaded through chat.
        assert 'id="chat-store-share"' in body
        share = body.split('id="chat-store-share"')[1].split(">")[0]
        assert "checked" not in share, "chat uploads must default to private, not shared"

    def test_dialog_no_longer_titled_submit_to_the_store(self, api_client: TestClient):
        """Title has to match what the default action does (a private Library
        save), or the dialog promises a publish the checkboxes didn't ask for."""
        body = api_client.get("/chat").text
        assert "Submit to the Store" not in body
        assert "Upload a Skill, Agent, or Plugin" in body


def test_studio_page_keeps_chat_nav_tab(api_client: TestClient, logged_in_user, monkeypatch):
    """Regression: the Studio landing page (``/admin/studio``) renders via the
    reduced-context ``_chrome_ctx`` builder, which used to omit ``can_chat``.
    The header's ``{% if can_chat %}`` gate then evaluated undefined→falsy and
    the Chat nav tab disappeared the moment you clicked Studio — even though the
    user had chat access (patched True by the autouse fixture). It must stay.
    """
    # Studio is off by default since the admin cleanup, and the route then
    # redirects home — this regression is about the CHROME the page renders,
    # so it needs the page to render at all.
    monkeypatch.setenv("AGNES_STUDIO_ENABLED", "1")
    r = api_client.get("/admin/studio")
    assert r.status_code == 200
    # Chat destination present (the thing that regressed) …
    # `data-tour` anchors went with the guided tour and the topnav chrome in
    # Wave 0 (2026-08); the rail's rows are matched on their href.
    assert 'href="/chat"' in r.text
    # … alongside the rail's own rows, proving we didn't just render a bare
    # page with no chrome at all.
    assert 'href="/library"' in r.text
    # And the header carries a real brand object, not the 'Data Analyst Portal'
    # fallback that a missing ``config`` produced on _chrome_ctx pages.
    assert "Data Analyst Portal" not in r.text


def test_chrome_ctx_matches_build_context_can_chat_and_config():
    """Anti-drift unit test: the two chrome builders must agree on ``can_chat``
    and both must provide ``config`` for the SAME user. Divergence here is
    exactly what hid the Chat tab (and flipped the brand) on the Studio pages,
    so pin them together.
    """
    from types import SimpleNamespace as _NS

    from starlette.requests import Request
    from app.web.router import _build_context, _chrome_ctx

    app = _NS(state=_NS(chat_config=_NS(enabled=True)))
    scope = {
        "type": "http",
        "app": app,
        "method": "GET",
        "path": "/admin/studio",
        "query_string": b"",
        "headers": [],
        "server": ("test", 80),
        "scheme": "http",
        "client": ("1.2.3.4", 9),
    }
    request = Request(scope)

    full = _build_context(request, user=TEST_USER)
    chrome = _chrome_ctx(request, TEST_USER)

    # can_chat is patched True by the autouse fixture; the point is that the two
    # builders return the SAME value, not the specific truthiness.
    assert chrome["can_chat"] == full["can_chat"] is True
    # Both expose a config object with the branding attributes the header reads.
    assert chrome["config"] is not None
    assert hasattr(chrome["config"], "INSTANCE_NAME")
    assert hasattr(chrome["config"], "LOGO_SVG")


def test_build_context_is_a_superset_of_chrome_ctx():
    """Drift guard for #996: ``_build_context`` composes ``_chrome_ctx`` (the
    single owner of every chrome-level key), so it must carry EVERY key
    ``_chrome_ctx`` provides, with the same value for the same request/user.

    This is the structural fix for the bug class the two spot-checks above
    (``can_chat`` / ``config``) each caught by hand after the fact: a chrome
    key added to ``_chrome_ctx`` but forgotten in ``_build_context`` (or vice
    versa) used to render as Jinja-undefined — falsy/empty, nothing raising —
    on whichever builder's pages missed it. Once a new key only has to be
    added in one place, this test either stays green for free or fails loudly
    the moment someone reintroduces a hand-copied, independently-computed key
    in only one of the two builders.
    """
    from types import SimpleNamespace as _NS

    from starlette.requests import Request
    from app.web.router import _build_context, _chrome_ctx

    app = _NS(state=_NS(chat_config=_NS(enabled=True)))
    scope = {
        "type": "http",
        "app": app,
        "method": "GET",
        "path": "/admin/studio",
        "query_string": b"",
        "headers": [],
        "server": ("test", 80),
        "scheme": "http",
        "client": ("1.2.3.4", 9),
    }
    request = Request(scope)

    full = _build_context(request, user=TEST_USER)
    chrome = _chrome_ctx(request, TEST_USER)

    missing = set(chrome) - set(full)
    assert not missing, f"_build_context is missing chrome key(s) _chrome_ctx provides: {sorted(missing)}"

    # A handful of chrome values are callables / freshly-built classes
    # (`get_flashed_messages`, `url_for`, `config`) — two independently
    # constructed closures/classes are never `==` to each other even when
    # behaviorally identical, so compare those by calling/introspecting
    # instead of raw equality. Everything else compares directly.
    for key, chrome_value in chrome.items():
        if key in ("get_flashed_messages", "url_for", "config"):
            continue
        assert full[key] == chrome_value, (
            f"key {key!r} disagrees between _build_context and _chrome_ctx: {full[key]!r} != {chrome_value!r}"
        )

    assert full["get_flashed_messages"]() == chrome["get_flashed_messages"]()
    assert full["url_for"]("static", filename="app.js") == chrome["url_for"]("static", filename="app.js")
    assert full["config"].INSTANCE_NAME == chrome["config"].INSTANCE_NAME
    assert full["config"].LOGO_SVG == chrome["config"].LOGO_SVG
