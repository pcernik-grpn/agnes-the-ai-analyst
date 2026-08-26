"""Auth-gated ingress proxy for hosted data apps (Task 8 of the v96 plan).

Composes the ``data_apps`` registry (``src/repositories/data_apps.py``),
the runner sidecar client (``src/data_apps/runner_client.py``), the
control-plane's RBAC predicate and shared deploy pipeline
(``app/api/data_apps.py``), and cross-process coordination
(``app/coordination``) into the public-facing surface end users actually
hit: ``https://<host>/apps/<slug>/...`` (or, in subdomain mode,
``https://<slug>.<subdomain_base>/...`` rewritten by
``app/data_apps_subdomain.py`` before it ever reaches routing).

Routes:

  - ``GET /apps/{slug}``                — redirect to the trailing-slash form
  - ``* /apps/{slug}/{path:path}``      — the proxy/wake/holding-page handler
  - ``WEBSOCKET /apps/{slug}/{path:path}`` — WS bridge to the app container

Per-request flow for the HTTP handler, after resolving the ``data_apps``
row and checking RBAC (``_can_view`` — owner, Admin, or a group grant):

  1. ``_touch`` — debounced ``last_request_at`` bump (coordination KV,
     30s TTL) so a bursty session doesn't hammer the registry with writes.
  2. Branch on ``row["state"]``:

     - ``running``    — stream-proxy to ``http://agnes-dataapp-<slug>:8888/<path>``.
       A connect failure here means the container is gone despite the
       registry believing it's up — flip to ``error`` and 502, rather than
       silently retrying or hanging.
     - ``sleeping``    — fire :func:`_trigger_wake` (idempotent via a
       coordination lease) and answer with the holding page / 503 JSON.
     - ``deploying``   — already waking (this request or a concurrent one
       already holds the wake lease) — same holding page, no second trigger.
     - ``stopped``/``created`` — a stopped app is an operator decision, not
       something a random inbound request should resurrect: 409, no wake.
     - ``error``       — 409 surfacing ``state_detail``.

Wake completion is NOT polled by this module — ``GET
/api/data-apps/{slug}/readiness`` (``app/api/data_apps.py``) flips
``deploying`` -> ``running`` itself once the runner reports ``ready``; the
holding page's own JS polls that endpoint. Two places document this same
fact on purpose (this module's docstring and the readiness endpoint's) so
neither can be edited without a reader noticing the other half of the
contract.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from app.api.data_apps import (
    _PREVIEW_COOKIE_NAME,
    OwnerNotFoundError,
    _can_view,
    _feature_gate,
    preview_cookie_name,
    redeploy_current,
    same_origin_serving_allowed,
    try_acquire_op_lease,
)
from app.auth.dependencies import _get_db, get_current_user
from app.auth.jwt import verify_token
from app.auth.pat_resolver import DATA_APP_PREVIEW_SCOPE_PREFIX, resolve_token_to_user
from app.coordination.base import CoordinationUnavailable
from app.coordination.factory import coordination
from src.data_apps.runner_client import RunnerClient, RunnerError, RunnerUnavailable
from src.repositories import data_apps_repo

logger = logging.getLogger(__name__)

router = APIRouter(tags=["data-apps-proxy"])

# Hop-by-hop headers (RFC 7230 §6.1) plus `host` — stripped in BOTH
# directions. `host` specifically must not ride through to the upstream
# (it would carry the caller's original Host, not `agnes-dataapp-<slug>`)
# nor back to the caller (httpx's own request to the upstream sets its own).
_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
}

# Caller credentials — REQUEST-direction only, never forwarded to the data
# app's own container. `Authorization` carries the caller's Agnes session/PAT
# (meant to authenticate to Agnes, not to the data app) and `Cookie` carries
# the `access_token` session cookie (same reasoning, plus it may now carry
# `Domain=.<parent>` in subdomain mode — see `session_cookie_domain()` — so
# it would otherwise ride straight into a container we don't control).
# Deliberately a separate set from `_HOP_BY_HOP` (different RFC, different
# rationale: hop-by-hop is a *protocol* concept, this is *security*) even
# though both end up filtered the same way on the request side. Response
# headers are never checked against this set — a data app setting its OWN
# `Set-Cookie` on the way back is legitimate and none of this proxy's business.
_CREDENTIAL_HEADERS = {"authorization", "cookie"}

_TOUCH_DEBOUNCE_TTL_S = 30

# Strong references to in-flight background wake tasks. asyncio only holds a
# WEAK reference to a task created via `asyncio.create_task` and not stored
# anywhere else — without this, the task object can be garbage-collected
# mid-flight (a well-known asyncio footgun: https://docs.python.org/3/library/asyncio-task.html#asyncio.create_task
# "Important: Save a reference to the result..."), silently killing the
# redeploy before it ever calls `set_state`. Cleared via the task's own
# done-callback once it finishes, so this never grows unbounded.
_WAKE_TASKS: set[asyncio.Task] = set()


def _runner() -> RunnerClient:
    """Module-level indirection — the seam ``fake_runner``/``dead_runner``
    test fixtures monkeypatch. A SEPARATE seam from
    ``app.api.data_apps._runner`` (that one backs ``redeploy_current``'s
    ``up()`` call) — this one backs the direct ``resume()``/``status()``
    calls this module makes itself. Tests that need both call sites
    observed patch both module-level symbols to the same stub instance.
    """
    return RunnerClient()


def _upstream_client() -> httpx.AsyncClient:
    """Test seam: monkeypatch this to point at an
    ``httpx.MockTransport``-backed client instead of a real socket."""
    return httpx.AsyncClient(timeout=httpx.Timeout(connect=5, read=300, write=60, pool=5))


def _wants_json(request: Request) -> bool:
    accept = (request.headers.get("accept") or "").lower()
    return "application/json" in accept and "text/html" not in accept


def _get_row_or_404(slug: str) -> dict:
    row = data_apps_repo().get_by_slug(slug)
    if not row:
        raise HTTPException(status_code=404, detail="data_app_not_found")
    return row


def _resolve_proxy_caller(request: Request, slug: str, conn: Optional[object]) -> tuple[Optional[dict], bool]:
    """Resolve who's allowed to view ``/apps/<slug>/...``.

    Returns ``(user, via_preview)``. Tries the normal session-cookie/PAT
    chain first (``get_current_user``'s exact resolution, called directly
    rather than via ``Depends`` so its 401 can be caught and traded for the
    preview-token fallback below instead of short-circuiting the route).

    If normal auth fails, falls back to a ``data-app-preview:<slug>``
    scoped token (cookie named ``preview_cookie_name(slug)``, or
    ``Authorization: Bearer``) — mirroring the ``data-app-git`` scope-pin precedent in
    ``app/api/data_apps_git.py``: the resolved identity is trusted to VIEW
    THIS SLUG ONLY when the token's scope claim is exactly
    ``data-app-preview:<slug>``. A token minted for a different app, or one
    that's expired/revoked (caught by ``resolve_token_to_user``'s normal PAT
    checks), resolves to ``(None, False)`` here — never falls through to
    treating the caller as unauthenticated-but-otherwise-fine.

    ``via_preview=True`` tells the caller to skip the normal ``_can_view``
    RBAC check entirely: the mint-time call to
    ``POST /{slug}/preview-grant`` already required ``_can_view`` to pass,
    so a validly-scoped preview token is sufficient on its own for this
    view-only serving path (never accepted on the JSON control-plane API —
    ``resolve_token_to_user`` there defaults to rejecting the scope).
    """
    auth_header = request.headers.get("authorization")
    try:
        user = get_current_user(request=request, authorization=auth_header, conn=conn)
        return user, False
    except HTTPException:
        pass

    token = None
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header.removeprefix("Bearer ")
    if not token:
        # Per-app cookie name first (`preview_cookie_name`); the bare legacy
        # name is still accepted so a preview already open across an upgrade
        # keeps working for the rest of its 30-minute TTL. Either way the scope
        # check below is what decides — reading a cookie grants nothing.
        token = request.cookies.get(preview_cookie_name(slug)) or request.cookies.get(_PREVIEW_COOKIE_NAME)
    if not token:
        return None, False

    user, _reason = resolve_token_to_user(conn, token, request, allow_data_app_preview_scope=True)
    if not user:
        return None, False
    payload = verify_token(token) or {}
    scope = payload.get("scope") or ""
    if scope != f"{DATA_APP_PREVIEW_SCOPE_PREFIX}{slug}":
        return None, False
    return user, True


def _touch(app_row: dict) -> None:
    """Debounced ``last_request_at`` bump — see module docstring step 1.

    Falls back to an un-debounced direct write when the coordination
    backend is unavailable (single-process dev semantics: a missing
    coordination backend must not silently stop idle-tracking from
    working at all, just lose the debounce).
    """
    key = f"dataapp:touch:{app_row['slug']}"
    try:
        if coordination().kv_get(key) is None:
            coordination().kv_set(key, "1", ttl_s=_TOUCH_DEBOUNCE_TTL_S)
            data_apps_repo().touch_last_request(app_row["id"])
    except CoordinationUnavailable:
        data_apps_repo().touch_last_request(app_row["id"])


async def _run_wake_fn(fn, row: dict) -> None:
    """Run ``fn(row)`` (a blocking sync callable — ``redeploy_current`` in
    production, a test double in ``tests/test_data_apps_proxy.py``) off
    the event loop via ``run_in_threadpool``, mapping ANY failure —
    including one raised from inside the backgrounded task itself, since
    nothing else observes it — onto ``set_state(row_id, "error", detail)``
    so a wake attempt never leaves the row wedged in ``deploying`` forever.

    Shared by ``_spawn_wake``'s production background task and its test
    replacement (``tests/test_data_apps_proxy.py``'s inline-await fixture)
    so the error-handling contract can't drift between the two.
    """
    repo = data_apps_repo()
    try:
        await run_in_threadpool(fn, row)
    except OwnerNotFoundError:
        repo.set_state(row["id"], "error", "owner_not_found")
    except (RunnerUnavailable, RunnerError) as exc:
        detail = getattr(exc, "detail", None) or str(exc)
        repo.set_state(row["id"], "error", detail)
    except Exception as exc:  # noqa: BLE001 — must never leave the app wedged in "deploying"
        logger.exception("wake redeploy failed for data app %s", row.get("slug"))
        repo.set_state(row["id"], "error", str(exc))


async def _spawn_wake(fn, row: dict) -> None:
    """Production seam: schedule ``_run_wake_fn(fn, row)`` as a background
    ``asyncio.Task`` and return immediately — awaiting this coroutine costs
    ~nothing (it only awaits the trivial act of scheduling the task, never
    the task itself), so the request handler's wake trigger doesn't block
    on however long ``fn`` (e.g. ``redeploy_current``'s container
    pull/start) actually takes; the holding page is what the caller waits
    on instead, polling ``/readiness`` until the background task's
    eventual ``set_state`` (success -> readiness flip, failure -> "error")
    becomes visible.

    Tests monkeypatch this module-level symbol to ``await
    _run_wake_fn(fn, row)`` directly instead of backgrounding it, so the
    effect (``fake_runner.up_calls``, the row's new state, ...) is
    observable synchronously right after the request returns — see
    ``tests/test_data_apps_proxy.py``.
    """
    task = asyncio.create_task(_run_wake_fn(fn, row))
    _WAKE_TASKS.add(task)
    task.add_done_callback(_WAKE_TASKS.discard)


async def _trigger_wake(app_row: dict) -> None:
    """Wake a sleeping app — at most one in-flight runner-mutating
    operation per app at a time, enforced by the ``dataapp:op:{slug}``
    lease shared with ``deploy_data_app``/``stop_data_app``
    (``app/api/data_apps.py``). Callers that lose the race (another
    request/replica already holds the lease — a concurrent wake, or a
    manual deploy/stop for the same app) just return — their caller
    renders the holding page regardless, and whichever operation is
    already in flight will flip the state for everyone. The lease is
    deliberately never explicitly released on success/failure here —
    it's TTL-only (see ``try_acquire_op_lease``), expiring naturally; an
    explicit release the instant this coroutine returns (well before the
    backgrounded redeploy actually finishes, for the recreate path) would
    let a second concurrent request re-acquire it and fire a duplicate
    wake — or a manual deploy/stop race the still-in-flight redeploy —
    while the first is still going.

    ``sleep_mode="pause"`` unpauses synchronously — cheap enough to await
    inline, so this coroutine sets ``running`` itself once it's done
    (rather than leaving that to the readiness-poll flip, which exists
    for the slower recreate path). ``sleep_mode="recreate"`` fires the
    full mint -> config -> ``runner.up`` pipeline
    (:func:`app.api.data_apps.redeploy_current`) via :func:`_spawn_wake` —
    NOT awaited to completion here, see that function's docstring.
    """
    slug = app_row["slug"]
    acquired, _holder = try_acquire_op_lease(slug)
    if not acquired:
        return  # another request/replica already owns this app's wake, or a manual deploy/stop is in flight

    repo = data_apps_repo()
    if app_row.get("sleep_mode") == "pause":
        try:
            await run_in_threadpool(_runner().resume, slug)
        except (RunnerUnavailable, RunnerError) as exc:
            detail = getattr(exc, "detail", None) or str(exc)
            repo.set_state(app_row["id"], "error", detail)
            return
        repo.set_state(app_row["id"], "running")
        return

    repo.set_state(app_row["id"], "deploying", "waking")
    await _spawn_wake(redeploy_current, app_row)


_STOPPED_HTML = """<!doctype html>
<title>App unavailable</title>
<style>body{{font-family:system-ui;display:grid;place-items:center;height:100vh;margin:0}}</style>
<div><h2>App is stopped</h2><p>This app ({state}) must be restarted by its owner or an
administrator before it can be reached — it does not wake on request.</p></div>
"""


def _not_running_response(slug: str, state: str, accepts_json: bool) -> Response:
    if accepts_json:
        return JSONResponse({"detail": "app_not_running", "state": state}, status_code=409)
    return Response(_STOPPED_HTML.format(state=state), media_type="text/html", status_code=409)


_SAME_ORIGIN_DISABLED_HTML = """<!doctype html>
<title>App unavailable</title>
<style>body{font-family:system-ui;display:grid;place-items:center;height:100vh;margin:0}
div{max-width:34rem;padding:0 1.5rem;text-align:center}</style>
<div><h2>This app cannot be served here</h2>
<p>Hosted apps are not served on this origin because a hosted app's code would
run with your logged-in session. An administrator must serve them from an
isolated origin (configure <code>data_apps.subdomain_base</code>) or explicitly
allow same-origin serving.</p></div>
"""


def _same_origin_serving_refused(request: Request, accepts_json: bool, via_preview: bool) -> Optional[Response]:
    """Refuse serving a hosted app on the MAIN origin unless explicitly allowed.

    A request rewritten from a data-app subdomain
    (``scope["agnes_data_app_subdomain"]``) is already on an isolated origin,
    where the app's JS cannot read the viewer's ``/api`` — served regardless.
    A request that arrived on the main host serves the app SAME-ORIGIN as the
    Agnes API: the app's user-authored JS shares the viewer's session cookie
    and can read ``/api`` (mint a PAT, read admin config). That is refused
    unless:

    - ``via_preview`` — the caller holds a ``data-app-preview:<slug>`` token
      (per-app, short-TTL, minted only to someone who already passed
      ``_can_view`` for THIS slug). The in-chat preview loads the app
      same-origin in an iframe and needs this; scoping the allowance to the
      preview token — rather than the instance-global flag below — keeps a
      plain drive-by navigation to ``/apps/<other-slug>/`` (no preview token)
      refused, so enabling the preview does NOT re-open same-origin serving for
      every app. The previewer's own session is still exposed to the app they
      chose to preview — an inherent, deliberate, per-app preview risk.
    - the operator set ``data_apps.allow_same_origin`` /
      ``AGNES_DATA_APPS_ALLOW_SAME_ORIGIN`` — serve ALL apps same-origin
      (trusted authors only; see ``app/api/data_apps.py::_CONFIG_DEFAULTS`` for
      why no response header can close a same-origin read).

    Returns the refusal ``Response`` (403) when serving must be refused, else
    ``None`` to proceed. Runs after RBAC so it never reveals an app's
    existence to a caller who would otherwise get a plain 401/403.
    """
    if request.scope.get("agnes_data_app_subdomain"):
        return None
    if via_preview:
        return None
    if same_origin_serving_allowed():
        return None
    if accepts_json:
        return JSONResponse({"detail": "data_app_same_origin_disabled"}, status_code=403)
    return Response(_SAME_ORIGIN_DISABLED_HTML, media_type="text/html", status_code=403)


def _readiness_poll_url(request: Request, slug: str) -> str:
    """Where the holding page should poll for readiness.

    Relative by default — correct for the path-prefix form, and it avoids
    pinning a host into the page.

    ABSOLUTE when the request arrived on an app subdomain.
    `DataAppSubdomainMiddleware` rewrites EVERY path on `<slug>.<base>` to
    `/apps/<slug>/…`, with no carve-out for `/api/*` — so a relative poll
    became `/apps/<slug>/api/data-apps/<slug>/readiness`, landed back on the
    proxy, got the holding page's own HTML, threw in `r.json()`, was
    swallowed by the `catch`, and the page spun forever while the app was up
    (Devin Review on this PR).

    A carve-out in the middleware would be the wrong fix: an app may serve
    its own `/api/*` — the scaffolded dashboard does exactly that — so
    diverting those to Agnes would break the app it is hosting.
    """
    if not request.scope.get("agnes_data_app_subdomain"):
        return f"/api/data-apps/{slug}/readiness"
    from app.instance_config import get_public_url

    base = (get_public_url() or "").rstrip("/")
    # With no configured public URL there is nothing to point at but the
    # subdomain itself, which is what swallows the poll. Keep the relative
    # form (unchanged behaviour) rather than guessing a host.
    return f"{base}/api/data-apps/{slug}/readiness" if base else f"/api/data-apps/{slug}/readiness"


def _waking_response(request: Request, slug: str, accepts_json: bool) -> Response:
    if accepts_json:
        return JSONResponse({"status": "waking"}, status_code=503)
    from app.web.router import templates

    return templates.TemplateResponse(
        request,
        "data_app_waking.html",
        {"slug": slug, "readiness_url": _readiness_poll_url(request, slug)},
        status_code=503,
    )


#: How long after a deploy a live-but-silent container is still read as
#: "starting" rather than broken. A first deploy clones the repo, runs
#: `npm install` and builds before anything listens — ~90s measured on a real
#: dashboard — so the window has to clear that with room, while staying short
#: enough that a wedged app becomes a diagnosable error rather than a spinner
#: nobody can explain.
_START_GRACE_SECONDS = 420


def _as_utc(value) -> Optional[datetime]:
    """Coerce a DB timestamp to an aware UTC datetime, or None.

    Naive values are read as UTC. Those columns are zoneless `TIMESTAMP`
    written with SQL `now()`, so the value carries the DB session's zone —
    this reading is the codebase's standing convention (`data_apps.py`,
    `collections.py`, ~15 other places) and containers run UTC.
    """
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _within_start_grace(row: dict) -> bool:
    """True while a running container may still legitimately be booting.

    Measured from the LATER of the last deploy and the row's last write, so a
    missing or unparseable clock returns False: without one the honest answer
    is "this is not provably still starting", and the caller records a real
    error rather than serving a holding page forever.

    Why the later of the two rather than the deploy alone: `record_deploy`
    writes `last_deploy_at` only from `POST /{slug}/deploy`, so the auto-wake
    path (`_trigger_wake` → `redeploy_current`) never refreshes it. A woken
    app's grace would be measured from a deploy that could be days old — and
    the boot a wake performs is exactly the slow clone/install/build this
    window exists for (Devin Review on this PR). `updated_at` moves on the
    wake's own `set_state`, so the maximum covers both without inventing a
    deploy that never happened.

    The comparison is symmetric (`abs`). A naive stamp from a session ahead of
    UTC lands in the future, and a one-sided window would treat that as
    already expired; an offset larger than the grace itself is a misconfigured
    host, where the outcome is the diagnosable one — a recorded error, not a
    silent spinner.
    """
    stamps = [s for s in (_as_utc(row.get("last_deploy_at")), _as_utc(row.get("updated_at"))) if s is not None]
    if not stamps:
        return False
    now = datetime.now(timezone.utc)
    return abs((now - max(stamps)).total_seconds()) < _START_GRACE_SECONDS


def _error_response(row: dict) -> Response:
    return JSONResponse(
        {"detail": "app_error", "state_detail": row.get("state_detail") or ""},
        status_code=409,
    )


async def _proxy(request: Request, slug: str, path: str) -> Response:
    """Stream-proxy one request to ``agnes-dataapp-<slug>``'s runtime
    container.

    Deliberately does NOT use ``async with _upstream_client() as client:
    ...; return StreamingResponse(...)`` (a shape that would close the
    client — and, dependent on the transport, its underlying connection —
    before the response body actually gets streamed by the ASGI server).
    Instead the client is closed from the same ``BackgroundTask`` that
    closes the upstream response, after the streamed body has been fully
    sent to the caller.
    """
    url = f"http://agnes-dataapp-{slug}:8888/{path}"
    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in _HOP_BY_HOP and k.lower() not in _CREDENTIAL_HEADERS
    }
    # A subdomain-origin request (rewritten by
    # app/data_apps_subdomain.py, which stamps this scope marker) serves
    # the app at its own root — there IS no prefix from its point of view,
    # unlike the `/apps/<slug>/...` path-prefix form of the same route.
    if not request.scope.get("agnes_data_app_subdomain"):
        headers["X-Forwarded-Prefix"] = f"/apps/{slug}"

    client = _upstream_client()
    try:
        upstream_request = client.build_request(
            request.method,
            url,
            headers=headers,
            params=request.query_params,
            content=request.stream(),
        )
        resp = await client.send(upstream_request, stream=True)
    except (httpx.ConnectError, httpx.ConnectTimeout):
        # Connect-phase failures only — a mid-stream ReadTimeout on an
        # otherwise-reachable container is a different failure mode
        # (propagates as-is; the caller doesn't treat it as "container is
        # gone", just as a request that timed out).
        await client.aclose()
        raise
    except Exception:
        await client.aclose()
        raise

    async def _close() -> None:
        await resp.aclose()
        await client.aclose()

    return StreamingResponse(
        resp.aiter_raw(),
        status_code=resp.status_code,
        headers={k: v for k, v in resp.headers.items() if k.lower() not in _HOP_BY_HOP},
        background=BackgroundTask(_close),
    )


@router.get("/apps/{slug}")
async def proxy_redirect_trailing_slash(slug: str, request: Request):
    qs = f"?{request.url.query}" if request.url.query else ""
    return RedirectResponse(url=f"/apps/{slug}/{qs}", status_code=307)


@router.api_route(
    "/apps/{slug}/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    include_in_schema=False,
)
async def proxy_app(slug: str, path: str, request: Request, conn=Depends(_get_db)):
    """Excluded from OpenAPI (``include_in_schema=False``): a single FastAPI
    route registered against multiple HTTP methods shares ONE
    ``operation_id``/response-schema across all of them (FastAPI's
    ``generate_unique_id`` keys off ``list(route.methods)[0]``, not the
    method actually being documented) — every method would show identical,
    fictional response codes, and DELETE specifically can't honestly
    declare ``204`` (the status is whatever the proxied app's own DELETE
    handler returns, not something this route controls). This endpoint's
    shape is fundamentally undocumentable via a single static OpenAPI
    operation; behaviour is covered in ``tests/test_data_apps_proxy.py``
    instead of the docs/coverage ratchets that key off the schema.

    Auth is resolved via ``_resolve_proxy_caller`` (not a plain
    ``Depends(get_current_user)``) so a request carrying only a
    ``data-app-preview:<slug>`` scoped cookie/bearer — which
    ``get_current_user``'s normal chain rejects outright, same as
    ``data-app-git`` — still gets a chance to authorize this view-only
    serving path (wave 3C, spec §7).
    """
    _feature_gate()
    row = _get_row_or_404(slug)
    # Off the event loop: _resolve_proxy_caller does DB-backed auth
    # (get_current_user + resolve_token_to_user), which would otherwise
    # serialize every proxied request behind one blocking lookup (503s under
    # load on Postgres) — same run_in_threadpool discipline as the runner calls.
    user, via_preview = await run_in_threadpool(_resolve_proxy_caller, request, slug, conn)
    if user is None:
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    if not via_preview and not _can_view(user, row):
        raise HTTPException(status_code=403, detail="forbidden")

    accepts_json = _wants_json(request)
    # Refuse same-origin serving unless the request arrived on a data-app
    # subdomain (isolated origin), carries a per-app preview token, or the
    # operator opted in. After RBAC so the refusal never leaks an app's
    # existence to an unauthorized caller.
    refused = _same_origin_serving_refused(request, accepts_json, via_preview)
    if refused is not None:
        return refused

    _touch(row)

    state = row["state"]

    if state == "running":
        try:
            return await _proxy(request, slug, path)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            # A refused connection is not proof the app is broken, and this
            # is a latch: nothing clears `error` except a redeploy, because
            # the `state == "error"` branch below only reports the stored
            # detail and never re-checks. So one badly-timed request used to
            # brick a healthy app permanently.
            #
            # That window is not narrow. A first deploy clones, runs
            # `npm install` and builds before anything listens on 8888 —
            # ~90s on a real app — and the deploy marks the row `running` as
            # soon as the runner accepts the container, not when it serves.
            # Watched live: the app finished building and served 200 inside
            # the container while Agnes answered `app_error / container
            # unreachable` to every caller, until an unrelated redeploy
            # happened to reset it.
            #
            # So ask the runner what is actually true before latching. A
            # container that is up but not yet listening is *starting*, and
            # the honest answer is the same "waking" page a sleeping app
            # gets. Only a container that is genuinely gone or stopped is an
            # error worth remembering.
            #
            # The "starting" verdict is TIME-BOUNDED. The runner's status
            # contract is `running | paused | stopped | absent`, so a live
            # container whose app process died or wedged without exiting
            # still reads `running` — and without a bound this would trade a
            # permanent latch for a permanent spinner, which is not obviously
            # better and is harder to diagnose (Devin Review on this PR).
            # Past the grace window a container that still is not listening
            # has stopped being "slow to boot" and become broken.
            try:
                container = (await run_in_threadpool(_runner().status, slug)).get("container")
            except (RunnerUnavailable, RunnerError):
                container = None  # runner itself is down — say nothing about the app
            if container == "paused":
                # Paused is the pause-mode sleep state, not a fault: fall
                # through to the sleeping branch's wake rather than recording
                # an error the caller would have to redeploy away.
                await _trigger_wake(row)
                return _waking_response(request, slug, accepts_json)
            if container == "running" and _within_start_grace(row):
                return _waking_response(request, slug, accepts_json)
            if container is not None:
                detail = "container not listening" if container == "running" else f"container {container}"
                data_apps_repo().set_state(row["id"], "error", detail)
            raise HTTPException(status_code=502, detail="container_unreachable")

    if state == "sleeping":
        await _trigger_wake(row)
        return _waking_response(request, slug, accepts_json)

    if state == "deploying":
        return _waking_response(request, slug, accepts_json)

    if state in ("stopped", "created"):
        return _not_running_response(slug, state, accepts_json)

    if state == "error":
        return _error_response(row)

    # Defensive fallback for any future/unknown state value — fail closed
    # rather than silently proxying to a container the registry has no
    # confirmed-running belief about.
    return _not_running_response(slug, state, accepts_json)


def _ws_authenticate(websocket: WebSocket) -> Optional[dict]:
    """Resolve the caller for a WS handshake using the exact same
    session-cookie/PAT resolution as ``get_current_user`` — called
    directly (not via ``Depends``) because FastAPI's dependency solver
    only fills ``Request``-typed params from HTTP scopes; websocket routes
    in this codebase (``app/api/chat.py``, ``app/api/notifications_ws.py``)
    all authenticate by calling into the auth helper directly for the same
    reason. ``WebSocket`` duck-types every attribute ``get_current_user``
    actually touches (``.cookies``, ``.headers``, ``.state``), so passing
    it in place of a ``Request`` is safe.
    """
    from contextlib import contextmanager

    auth_header = websocket.headers.get("authorization")
    conn_cm = contextmanager(_get_db)
    try:
        with conn_cm() as conn:
            return get_current_user(request=websocket, authorization=auth_header, conn=conn)
    except HTTPException:
        return None


@router.websocket("/apps/{slug}/{path:path}")
async def proxy_ws(websocket: WebSocket, slug: str, path: str):
    from app.instance_config import feature_enabled

    if not feature_enabled("data_apps", "enabled", env_var="AGNES_DATA_APPS_ENABLED", default=False):
        await websocket.close(code=4404, reason="data_apps_disabled")
        return

    user = _ws_authenticate(websocket)
    if user is None:
        await websocket.close(code=4403, reason="forbidden")
        return

    row = data_apps_repo().get_by_slug(slug)
    if not row:
        await websocket.close(code=4404, reason="data_app_not_found")
        return

    if not _can_view(user, row):
        await websocket.close(code=4403, reason="forbidden")
        return

    # Same-origin serving gate — mirrors the HTTP proxy. A WS on the main
    # origin shares the viewer's session with app-authored code; refuse unless
    # the request arrived on a data-app subdomain or the operator opted in.
    if not websocket.scope.get("agnes_data_app_subdomain") and not same_origin_serving_allowed():
        await websocket.close(code=4403, reason="same_origin_disabled")
        return

    if row["state"] != "running":
        await websocket.close(code=4404, reason="app_not_running")
        return

    _touch(row)

    await websocket.accept()

    query = f"?{websocket.url.query}" if websocket.url.query else ""
    upstream_url = f"ws://agnes-dataapp-{slug}:8888/{path}{query}"

    import websockets

    # No caller headers (incl. `Authorization`/`Cookie`) are forwarded to the
    # upstream handshake at all — same credential-hygiene guarantee as the
    # HTTP proxy's `_CREDENTIAL_HEADERS` strip, just trivially satisfied here
    # since this bridge never builds a header dict from `websocket.headers`
    # in the first place.
    try:
        async with websockets.connect(upstream_url) as upstream:

            async def client_to_upstream() -> None:
                try:
                    while True:
                        message = await websocket.receive()
                        if message["type"] == "websocket.disconnect":
                            break
                        text = message.get("text")
                        data = message.get("bytes")
                        if text is not None:
                            await upstream.send(text)
                        elif data is not None:
                            await upstream.send(data)
                finally:
                    await upstream.close()

            async def upstream_to_client() -> None:
                try:
                    async for message in upstream:
                        if isinstance(message, bytes):
                            await websocket.send_bytes(message)
                        else:
                            await websocket.send_text(message)
                finally:
                    try:
                        await websocket.close()
                    except Exception:
                        pass

            await asyncio.gather(client_to_upstream(), upstream_to_client(), return_exceptions=True)
    except Exception:
        # Broad on purpose: covers plain connect failures (OSError — refused/
        # unreachable/DNS) as well as the `websockets` library's own
        # handshake-rejection exceptions (e.g. `InvalidStatus`/
        # `InvalidHandshake` when the container answers but not with a
        # valid WS upgrade). Any of these means the bridge never got a
        # usable upstream connection — close gracefully rather than let an
        # unhandled exception surface as a raw 500-equivalent.
        logger.warning("WS bridge to data app %s failed", slug, exc_info=True)
        try:
            await websocket.close(code=1011, reason="upstream_unreachable")
        except Exception:
            pass
