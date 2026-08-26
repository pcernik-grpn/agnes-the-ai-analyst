"""Route-auth guard (Task 10 of the 2026-07-14 chat-sandbox-secret-broker
plan, AC-G-route-auth).

The broker routes are ticket-authed rather than session-authed — a novel
auth mode for this codebase. This guard makes sure that novelty doesn't
quietly widen into "some /api/* route has no auth at all": it walks the
FastAPI dependant tree of every live ``/api/*`` route and asserts each one
carries a recognized auth dependency, or is explicitly named in ``_EXEMPT``
with a documented reason.

This is a full sweep, not a forward-only ratchet (contrast
``tests/test_documentation_api_triple_surface.py``) — as of this writing
only 8 routes are legitimately exempt (health/version probes, webhooks
verified by HMAC signature outside the Depends chain, and one deliberate
bootstrap endpoint), so a full sweep stays cheap and actually catches a
newly-added unauthenticated ``/api/*`` handler instead of only guarding the
routes this task happened to touch.
"""

from __future__ import annotations

import os


# Public-by-design /api/* endpoints. Each entry documents WHY it carries no
# FastAPI Depends()-chain auth dependency (webhook HMAC signature verified
# in the handler body, a liveness/build-info probe, or a manual
# get_optional_user + explicit 401/redirect check).
_EXEMPT: dict[str, str] = {
    "/api/health": "liveness probe — no secrets, must be reachable pre-auth",
    "/api/data-apps-tls-check": (
        "Caddy's on-demand-TLS `ask` probe — Caddy sends a plain GET carrying no "
        "credential and reads ANY non-2xx as 'cancel issuance', so a Depends() auth "
        "chain would refuse every certificate and leave hosted apps unreachable. "
        "Leaks nothing new: it answers 2xx only for a registered, non-hidden slug "
        "under the configured data_apps.subdomain_base, and proxy_app already "
        "resolves _get_row_or_404 BEFORE authenticating — so a real slug is already "
        "distinguishable from a made-up one on the proxy itself. Rate-limited. "
        "See app/api/data_apps_proxy.py::tls_check"
    ),
    "/api/version": "build/version info — public, no secrets (app/api/health.py)",
    "/api/sync/status": (
        "public sync-in-flight probe used by the host auto-upgrade cron to avoid "
        "killing a running extractor mid-flight; documented no-auth-by-design in "
        "app/api/sync.py's sync_status docstring"
    ),
    "/api/slack/events": "Slack webhook — authenticated via verify_slack_signature (HMAC), not a session/JWT",
    "/api/slack/commands": "Slack webhook — authenticated via verify_slack_signature (HMAC), not a session/JWT",
    "/api/slack/interactivity": "Slack webhook — authenticated via verify_slack_signature (HMAC), not a session/JWT",
    "/api/auth/exchange-setup-token": (
        "the one deliberately unauthenticated bootstrap endpoint — exchanges a "
        "one-time setup token for a PAT; documented in app/api/cowork_bundle.py's "
        "module docstring as 'no auth required'"
    ),
    "/api/mcp/sources/{source_id}/oauth/authorize": (
        "browser landing page starting the OAuth connect flow — uses "
        "Depends(get_optional_user) plus a manual redirect-to-/login (with "
        "next= back to this URL) so the CLI's printed URL works in a browser "
        "without a live session (Devin Review on #1130)"
    ),
    "/api/mcp/oauth-client/callback": (
        "browser landing page for the OAuth connect flow — uses "
        "Depends(get_optional_user) plus a manual redirect-to-/login in the "
        "handler body so an expired Agnes session mid-flow lands on the login "
        "page instead of a raw 401 JSON body (same pattern as "
        "/api/initial-workspace.zip; Devin Review on #1130)"
    ),
    "/api/data-apps/{slug}/readiness": (
        "authenticates INSIDE the handler via the proxy's own resolver "
        "(app/api/data_apps_proxy.py::_resolve_proxy_caller) rather than a "
        "Depends chain, because the holding page this endpoint serves is "
        "rendered by that proxy and its callers include a "
        "`data-app-preview:<slug>` scoped token — a credential "
        "get_current_user rejects outright, so a Depends(get_current_user) "
        "here 401'd the poll on a page the same request chain had just "
        "served (Devin Review on #1272). Unauthenticated callers still get "
        "401 and the preview token is pinned to the slug being polled; "
        "tests/test_data_apps_proxy.py covers both"
    ),
    "/api/initial-workspace.zip": (
        "uses Depends(get_optional_user) plus a manual 401/redirect check in the "
        "handler body (not a Depends-chain auth dependency) so an anonymous browser "
        "hit can redirect to /login instead of only ever raising a raw 401"
    ),
}


def _live_api_routes():
    """Return the live (path, route) pairs for every APIRoute under /api/.

    Builds the app under a pinned, canonical env so the route set is
    deterministic regardless of what a sibling test left in ``os.environ``
    (some tests raw-mutate ``LOCAL_DEV_MODE`` / ``DEBUG`` / ``TESTING`` and can
    run earlier on the same xdist worker). Without this the enumeration was
    order-sensitive under the sharded CI run.
    """
    os.environ["TESTING"] = "1"
    for var in ("LOCAL_DEV_MODE", "DEBUG", "AGNES_E2E"):
        os.environ.pop(var, None)
    from fastapi.routing import APIRoute
    from app.main import create_app

    app = create_app()
    return [route for route in app.routes if isinstance(route, APIRoute) and route.path.startswith("/api/")]


def _is_authenticated(dependant, recognized_calls, seen=None) -> bool:
    """Recursively walk a FastAPI dependant tree for a recognized auth dependency.

    ``require_resource_access`` is a dependency *factory* (returns a fresh
    closure per call site), so it's matched by qualname prefix rather than
    identity; everything else is matched by direct callable identity.
    """
    if seen is None:
        seen = set()
    for sub in dependant.dependencies:
        call = sub.call
        key = id(call)
        if key in seen:
            continue
        seen.add(key)
        if call in recognized_calls:
            return True
        if getattr(call, "__qualname__", "").startswith("require_resource_access.<locals>"):
            return True
        if _is_authenticated(sub, recognized_calls, seen):
            return True
    return False


def test_all_routes_authenticated():
    """Every live /api/* route is either auth-gated or explicitly exempted.

    Recognized auth dependencies: ``require_admin``, anything derived from
    ``require_resource_access`` (incl. its convenience aliases like
    ``require_chat_access``), ``get_current_user``, ``require_session_token``,
    and the broker's ``require_broker_ticket`` (opaque short-lived ticket,
    not a user session — but still a real auth gate, per AC-G-ticket-reuse
    and AC-G-rbac-fidelity elsewhere in this suite).

    ``app/api/kai.py``'s ``_require_session_credential`` is recognized on the
    same footing: same opaque-credential store, resolved the same way, plus a
    scope check that a broker ticket cannot satisfy. It is the embedded turn
    engine's equivalent gate, not an exemption.
    """
    from app.api.broker import require_broker_ticket
    from app.api.kai import _require_session_credential
    from app.auth.access import require_admin
    from app.auth.dependencies import get_current_user, require_session_token

    recognized_calls = (
        require_admin,
        get_current_user,
        require_session_token,
        require_broker_ticket,
        _require_session_credential,
    )

    offenders = []
    for route in _live_api_routes():
        if route.path in _EXEMPT:
            continue
        if not _is_authenticated(route.dependant, recognized_calls):
            offenders.append(route.path)

    assert not offenders, (
        f"{len(offenders)} /api/* route(s) without a recognized auth dependency:\n  "
        + "\n  ".join(sorted(set(offenders)))
        + "\n\nAdd Depends(require_admin) / Depends(require_resource_access(...)) / "
        "Depends(get_current_user) / Depends(require_session_token) / "
        "Depends(require_broker_ticket) to the route, or — only if it is genuinely "
        "public or authenticated outside the Depends chain (webhook signature, "
        "liveness probe) — add it to _EXEMPT above with a documented reason."
    )


def test_exempt_has_no_stale_entries():
    """Every _EXEMPT path must still be a live /api/* route.

    Prevents the exemption list from silently accumulating entries for
    routes that were renamed or removed (which would otherwise make the
    guard above quietly weaker than it looks)."""
    live_paths = {route.path for route in _live_api_routes()}
    stale = set(_EXEMPT) - live_paths
    assert not stale, f"_EXEMPT lists path(s) no longer live (remove them): {sorted(stale)}"
