"""Server-rendered HTML must carry `Cache-Control: no-store`.

Regression guard for the stale-`/home` install bug: the setup hero bakes
render-time values into the markup — RBAC-filtered plugin grants, the live
connector manifest, the operator's instance brand/host. (The install prompt's
CLI step used to also bake a version-pinned `/cli/wheel/{name}` URL that
404s the moment the server upgrades between render and execution; it now
downloads via the unversioned `/cli/download` endpoint instead, immune to
that race.) If the browser heuristically caches the HTML, a redeploy leaves
the user with a stale page. The middleware sets `no-store` on text/html so
every load re-renders against the live build.

Also covers the sibling `/static` cache-control split (VersionedStaticFiles,
app/web/cover_files.py): every template already appends the `?v=<mtime>`
cache-buster via `_static_url` (app/web/router.py), so a versioned request
is safe to cache for a year; an unversioned one must still revalidate.
"""

from fastapi.testclient import TestClient


def test_html_page_carries_no_store():
    from app.main import app

    client = TestClient(app)
    # /login is an unauthenticated HTML page (renders the provider form).
    resp = client.get("/login")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert resp.headers.get("cache-control") == "no-store"


def test_json_api_is_not_marked_no_store():
    from app.main import app

    client = TestClient(app)
    # /api/version is JSON (application/json) — the no-store rule is text/html
    # only, so it must not pick up the directive.
    resp = client.get("/api/version")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.headers.get("cache-control") != "no-store"


def test_versioned_static_asset_is_immutable():
    from app.main import app

    client = TestClient(app)
    # app.js exists in app/web/static/ and is referenced via _static_url,
    # which always appends ?v=<mtime> — this is what a real page load sends.
    resp = client.get("/static/app.js?v=123")
    assert resp.status_code == 200
    cache_control = resp.headers.get("cache-control", "")
    assert "max-age=31536000" in cache_control
    assert "immutable" in cache_control


def test_unversioned_static_asset_revalidates():
    from app.main import app

    client = TestClient(app)
    # Same asset, no ?v= — must not be treated as immutable.
    resp = client.get("/static/app.js")
    assert resp.status_code == 200
    cache_control = resp.headers.get("cache-control", "")
    assert "immutable" not in cache_control
    assert "no-cache" in cache_control
