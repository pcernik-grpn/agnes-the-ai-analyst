"""CSRF defense for the cookie-authenticated JSON API (F2, second layer).

The double-submit ``web_csrf`` token (``tests/test_web_csrf.py``) covers only
the handful of pure-HTML form POST handlers. The much larger ``/api/**``
JSON-POST surface (admin, sync, …) is driven by ``fetch(credentials:"include")``
and rides cookie-session auth with no token. Its practical protection was an
*implicit* one: a Pydantic body forces ``application/json`` → a cross-origin
fetch needs a preflight the CORS allowlist rejects, and ``SameSite=Lax`` keeps
the session cookie off a truly cross-site POST. Two gaps in that story:

* endpoints that mutate with **no JSON body** (``POST /api/sync/trigger``, the
  admin ``run-*`` family — query-param / empty-body) are CORS *simple* requests:
  no preflight, no content-type barrier; and
* when data-apps are hosted on sibling sub-domains the session cookie is scoped
  to the parent (``Domain=.<base>``), so a **same-site** request from
  user-authored app code *does* carry it.

``CsrfOriginMiddleware`` closes both by rejecting a cookie-authenticated
state-changing request that carries positive cross-origin evidence
(``Sec-Fetch-Site: same-site|cross-site`` or a foreign ``Origin``) unless the
origin is on the operator's explicit CORS allowlist. It stays silent when there
is no such evidence, so bearer/PAT callers, same-origin UI fetches and the
header-less test/tooling clients are never touched.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.middleware.csrf_origin import CsrfOriginMiddleware

_COOKIE = {"access_token": "cookie-value-irrelevant-to-the-header-check"}


def _app(*, enabled: bool = True, allowed: set[str] | None = None) -> TestClient:
    app = FastAPI()
    app.add_middleware(
        CsrfOriginMiddleware,
        allowed_origins=allowed or set(),
        enabled=enabled,
    )

    @app.post("/mutate")
    async def mutate():  # pragma: no cover - trivial
        return {"ran": True}

    @app.get("/read")
    async def read():  # pragma: no cover - trivial
        return {"ran": True}

    @app.post("/apps/some-slug/do")
    async def proxied_app():  # pragma: no cover - trivial
        return {"ran": True}

    # base_url fixes the request origin to http://testserver.
    return TestClient(app)


# ---------------------------------------------------------------------------
# Allowed: no cross-origin evidence, safe methods, exempt auth
# ---------------------------------------------------------------------------


def test_no_headers_is_allowed():
    """The header-less case — every existing test and every non-browser client
    — must pass untouched, or the middleware would break the whole suite."""
    r = _app().post("/mutate", cookies=_COOKIE)
    assert r.status_code == 200


def test_same_origin_sec_fetch_site_is_allowed():
    r = _app().post("/mutate", cookies=_COOKIE, headers={"sec-fetch-site": "same-origin"})
    assert r.status_code == 200


def test_none_sec_fetch_site_is_allowed():
    """``none`` = user-initiated (typed URL / bookmark), not a page-driven forge."""
    r = _app().post("/mutate", cookies=_COOKIE, headers={"sec-fetch-site": "none"})
    assert r.status_code == 200


def test_matching_origin_fallback_is_allowed():
    """Old browsers omit Sec-Fetch-Site; a same-origin Origin still passes."""
    r = _app().post("/mutate", cookies=_COOKIE, headers={"origin": "http://testserver"})
    assert r.status_code == 200


def test_get_is_never_blocked():
    r = _app().get("/read", cookies=_COOKIE, headers={"sec-fetch-site": "cross-site"})
    assert r.status_code == 200


def test_bearer_request_is_exempt():
    """A PAT/JWT API caller (CLI, MCP, agent API) is explicitly authenticated,
    not ambient-cookie authenticated, so it is never a CSRF target."""
    r = _app().post(
        "/mutate",
        headers={"sec-fetch-site": "cross-site", "authorization": "Bearer pat_xxx"},
    )
    assert r.status_code == 200


def test_no_session_cookie_is_exempt():
    """No ``access_token`` cookie → nothing ambient to forge with."""
    r = _app().post("/mutate", headers={"sec-fetch-site": "cross-site"})
    assert r.status_code == 200


def test_similarly_named_cookie_does_not_trigger_the_gate():
    """The session-cookie check matches the ``access_token`` name exactly, not a
    substring — a request carrying only e.g. ``xsrf_access_token`` is not
    cookie-session authed and must pass."""
    c = _app()
    c.cookies.set("xsrf_access_token", "unrelated")
    r = c.post("/mutate", headers={"sec-fetch-site": "cross-site"})
    assert r.status_code == 200


def test_proxied_data_app_path_is_skipped():
    """`/apps/<slug>/...` is reverse-proxied user-authored content with its own
    request semantics — the Agnes CSRF gate must not sit in front of it."""
    r = _app().post(
        "/apps/some-slug/do",
        cookies=_COOKIE,
        headers={"sec-fetch-site": "cross-site"},
    )
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Blocked: positive cross-origin evidence on a cookie-authed mutation
# ---------------------------------------------------------------------------


def test_cross_site_sec_fetch_site_is_blocked():
    r = _app().post("/mutate", cookies=_COOKIE, headers={"sec-fetch-site": "cross-site"})
    assert r.status_code == 403


def test_same_site_sec_fetch_site_is_blocked():
    """The data-app-subdomain vector: a sibling sub-domain is *same-site*, so
    Lax alone would let its cookie ride. This is the case Lax cannot catch."""
    r = _app().post(
        "/mutate",
        cookies=_COOKIE,
        headers={"sec-fetch-site": "same-site", "origin": "http://evil.testserver"},
    )
    assert r.status_code == 403


def test_foreign_origin_fallback_is_blocked():
    r = _app().post("/mutate", cookies=_COOKIE, headers={"origin": "http://evil.example"})
    assert r.status_code == 403


def test_block_response_is_json_403():
    r = _app().post("/mutate", cookies=_COOKIE, headers={"sec-fetch-site": "cross-site"})
    assert r.status_code == 403
    assert r.headers["content-type"].startswith("application/json")
    assert "cross-origin" in r.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Operator CORS allowlist opt-in and the kill switch
# ---------------------------------------------------------------------------


def test_allowlisted_cross_origin_is_permitted():
    """An origin the operator explicitly put in CORS_ORIGINS (a configured
    credentialed cross-origin SPA) must not be blocked by the CSRF gate."""
    c = _app(allowed={"http://spa.example"})
    r = c.post(
        "/mutate",
        cookies=_COOKIE,
        headers={"sec-fetch-site": "cross-site", "origin": "http://spa.example"},
    )
    assert r.status_code == 200


def test_wildcard_allowlist_permits_any_origin():
    c = _app(allowed={"*"})
    r = c.post(
        "/mutate",
        cookies=_COOKIE,
        headers={"sec-fetch-site": "cross-site", "origin": "http://anything.example"},
    )
    assert r.status_code == 200


def test_disabled_middleware_passes_everything():
    r = _app(enabled=False).post("/mutate", cookies=_COOKIE, headers={"sec-fetch-site": "cross-site"})
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Wiring: the real app installs the gate in front of the admin surface
# ---------------------------------------------------------------------------


def test_real_app_blocks_cross_site_admin_post(seeded_app):
    """Proves the middleware is wired into create_app() and gates the flagged
    JSON admin surface. Rejection happens in the middleware, before the
    handler and its side effects run, so a real admin endpoint is safe to use."""
    c = seeded_app["client"]
    r = c.post(
        "/api/admin/run-blocked-purge",
        cookies={"access_token": seeded_app["admin_token"]},
        headers={"sec-fetch-site": "cross-site", "origin": "http://evil.example"},
    )
    assert r.status_code == 403
    assert "cross-origin" in r.json()["detail"].lower()


def test_real_app_allows_same_origin_admin_get(seeded_app):
    """A same-origin read is untouched (safe method + same-origin)."""
    c = seeded_app["client"]
    r = c.get(
        "/api/admin/registry",
        cookies={"access_token": seeded_app["admin_token"]},
        headers={"sec-fetch-site": "same-origin"},
    )
    assert r.status_code == 200
