"""Tests for the auth-gated ingress proxy (`/apps/{slug}/...`) — Task 8:
wake-on-request, the holding page, touch-debounce, hop-by-hop header
hygiene, subdomain-host rewrite, and the WS bridge's auth-reject path.

Follows the `api_env`-fixture idiom of `tests/test_data_apps_api.py`: real
user/token rows via the DuckDB repos, `data_apps.enabled` flipped on in an
`instance.yaml` overlay, a real `TestClient(app)`.

No `respx` in dev deps (`grep respx pyproject.toml` came up empty) — the
upstream fake monkeypatches the module-level `_upstream_client()` seam
(`app.api.data_apps_proxy._upstream_client`) to return an
`httpx.AsyncClient(transport=httpx.MockTransport(handler))`, wrapped in a
respx-like `.calls[i].request` shape for readability.
"""

from __future__ import annotations

import hashlib
import uuid

import httpx
import pytest
import yaml
from cryptography.fernet import Fernet
from starlette.websockets import WebSocketDisconnect

from src.data_apps.runner_client import RunnerUnavailable


def _auth(pat: str) -> dict:
    return {"Authorization": f"Bearer {pat}"}


@pytest.fixture(autouse=True)
def _inline_spawn_wake(monkeypatch):
    """`_spawn_wake` backgrounds the redeploy in production (fire-and-forget
    `asyncio.create_task`) so a wake-triggering request never blocks on it.
    That's untestable-by-default (nothing guarantees the background task
    has run by the time a test asserts on it right after the response
    returns) — so every test EXCEPT the one that specifically asserts the
    non-blocking behavior (`test_sleeping_recreate_wake_does_not_block_response`,
    which overrides this patch itself) gets `_spawn_wake` replaced with a
    version that `await`s `_run_wake_fn` directly, making its effects
    (`fake_runner.up_calls`, the row's new state) observable synchronously.
    """
    import app.api.data_apps_proxy as proxy_api

    async def _inline(fn, row):
        await proxy_api._run_wake_fn(fn, row)

    monkeypatch.setattr(proxy_api, "_spawn_wake", _inline)


@pytest.fixture(autouse=True)
def _reset_coordination():
    """The `memory` coordination backend is a process-wide singleton
    (`app.coordination.factory._instance`) with no autouse reset in
    `tests/conftest.py` — without this, a wake/touch lease acquired by one
    test (many of this module's fixtures reuse slug `"s"`, so they'd share
    lease names like `dataapp:op:s`) stays held for its full TTL and
    leaks into the next test on the same xdist worker."""
    from app.coordination.factory import reset_coordination_for_tests

    reset_coordination_for_tests()
    yield
    reset_coordination_for_tests()


@pytest.fixture
def proxy_env(e2e_env, monkeypatch, shared_app):
    """Real user/token rows + TestClient(app), data_apps enabled."""
    from app.auth.jwt import create_access_token
    from src.db import get_system_db
    from src.repositories.access_tokens import AccessTokenRepository
    from src.repositories.users import UserRepository

    data_dir = e2e_env["data_dir"]
    monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())

    state = data_dir / "state"
    state.mkdir(parents=True, exist_ok=True)
    # `allow_same_origin` on by default in tests: most proxy tests exercise the
    # path-prefix serving mechanics, which the same-origin gate would otherwise
    # refuse (see `_same_origin_serving_refused`). The gate itself is covered by
    # the dedicated tests below, which flip it off explicitly.
    (state / "instance.yaml").write_text(yaml.dump({"data_apps": {"enabled": True, "allow_same_origin": True}}))
    import app.instance_config as instance_config

    instance_config._instance_config = None

    conn = get_system_db()
    try:
        users = UserRepository(conn)
        users.create(id="owner1", email="owner@test.local", name="Owner")
        users.create(id="other1", email="other@test.local", name="Other")

        token_repo = AccessTokenRepository(conn)
        pats: dict[str, str] = {}
        for uid, email in [("owner1", "owner@test.local"), ("other1", "other@test.local")]:
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
    finally:
        conn.close()

    app = shared_app
    from fastapi.testclient import TestClient

    client = TestClient(app)
    return {
        "client": client,
        "app": app,
        "owner_pat": pats["owner1"],
        "other_pat": pats["other1"],
        "data_dir": data_dir,
    }


def _set_data_apps_config(data_dir, **overrides) -> None:
    """Overlay `instance.yaml`'s `data_apps:` block (merged with `enabled: True`)
    and drop the cached instance_config singleton so the next read picks it up."""
    import app.instance_config as instance_config

    state = data_dir / "state"
    # `allow_same_origin` defaults on (path-prefix mechanics); a test exercising
    # the same-origin gate passes `allow_same_origin=False` to override it.
    base = {"enabled": True, "allow_same_origin": True}
    (state / "instance.yaml").write_text(yaml.dump({"data_apps": {**base, **overrides}}))
    instance_config._instance_config = None


class _AuthedClient:
    """Thin TestClient wrapper that injects a bearer token while still
    letting individual calls pass/override their own headers (e.g. Accept,
    Host)."""

    def __init__(self, client, pat: str):
        self._client = client
        self._auth_headers = {"Authorization": f"Bearer {pat}"}

    def _merge(self, kw: dict) -> dict:
        headers = dict(self._auth_headers)
        headers.update(kw.pop("headers", None) or {})
        kw["headers"] = headers
        return kw

    def get(self, url, **kw):
        return self._client.get(url, **self._merge(kw))

    def post(self, url, **kw):
        return self._client.post(url, **self._merge(kw))

    def websocket_connect(self, url, **kw):
        return self._client.websocket_connect(url, **self._merge(kw))


@pytest.fixture
def client_granted(proxy_env):
    return _AuthedClient(proxy_env["client"], proxy_env["owner_pat"])


@pytest.fixture
def client_stranger(proxy_env):
    return _AuthedClient(proxy_env["client"], proxy_env["other_pat"])


class _FakeRunner:
    def __init__(self):
        self.up_calls: list[tuple] = []
        self.resume_calls: list[str] = []
        self.stop_calls: list[tuple] = []
        self._status: dict = {"container": "running", "ready": True}

    def up(self, slug, spec, config_json):
        self.up_calls.append((slug, spec, config_json))
        return {"container": "running", "ready": True}

    def stop(self, slug, mode="recreate"):
        self.stop_calls.append((slug, mode))
        return {"container": "stopped", "ready": False}

    def resume(self, slug):
        self.resume_calls.append(slug)
        return {"container": "running", "ready": True}

    def status(self, slug):
        return self._status

    def logs(self, slug, tail=200):
        return ""


class _DeadRunner:
    def up(self, slug, spec, config_json):
        raise RunnerUnavailable("connection refused")

    def resume(self, slug):
        raise RunnerUnavailable("connection refused")

    def status(self, slug):
        raise RunnerUnavailable("connection refused")

    def stop(self, slug, mode="recreate"):
        raise RunnerUnavailable("connection refused")

    def logs(self, slug, tail=200):
        raise RunnerUnavailable("connection refused")


@pytest.fixture
def fake_runner(monkeypatch):
    """Patches BOTH `app.api.data_apps._runner` (used by `redeploy_current`'s
    `up()` call) and `app.api.data_apps_proxy._runner` (used directly by
    `_trigger_wake`'s `resume()` call) to the SAME stub instance — the two
    modules keep independent monkeypatch seams (mirrors the rest of the
    codebase's per-module `_runner()` indirection convention) but a caller
    wants one shared fake to observe both call sites together.
    """
    import app.api.data_apps as data_apps_api
    import app.api.data_apps_proxy as proxy_api

    runner = _FakeRunner()
    monkeypatch.setattr(data_apps_api, "_runner", lambda: runner)
    monkeypatch.setattr(proxy_api, "_runner", lambda: runner)
    return runner


@pytest.fixture
def dead_runner(monkeypatch):
    import app.api.data_apps as data_apps_api
    import app.api.data_apps_proxy as proxy_api

    runner = _DeadRunner()
    monkeypatch.setattr(data_apps_api, "_runner", lambda: runner)
    monkeypatch.setattr(proxy_api, "_runner", lambda: runner)
    return runner


def _create_app_row(slug="s", owner_id="owner1", state="running", sleep_mode="recreate"):
    from src.db import get_system_db
    from src.repositories.data_apps import DataAppsRepository

    conn = get_system_db()
    try:
        repo = DataAppsRepository(conn)
        app_id = repo.create(slug=slug, name=slug.upper(), owner_user_id=owner_id, sleep_mode=sleep_mode)
        if state != "created":
            repo.set_state(app_id, state)
    finally:
        conn.close()
    return app_id


@pytest.fixture
def running_app(proxy_env):
    _create_app_row(slug="s", state="running")
    return "s"


@pytest.fixture
def sleeping_app(proxy_env):
    _create_app_row(slug="s", state="sleeping", sleep_mode="recreate")
    return "s"


class _Call:
    def __init__(self, request):
        self.request = request


class _Recorder:
    def __init__(self):
        self.calls: list[_Call] = []


class _AsyncByteStream(httpx.AsyncByteStream):
    """Real (not pre-materialized) async stream — `httpx.Response(...,
    text=...)` marks the response as already fully read
    (`is_stream_consumed=True`), which makes `_proxy`'s `resp.aiter_raw()`
    raise `StreamConsumed` on its very first read. The proxy implementation
    streams a real upstream response exactly once; this stream shape is
    what makes the MockTransport-backed fake behave like one."""

    def __init__(self, chunks: list[bytes]):
        self._chunks = chunks

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk

    async def aclose(self) -> None:
        pass


@pytest.fixture
def respx_upstream(monkeypatch):
    recorder = _Recorder()

    def handler(request: httpx.Request) -> httpx.Response:
        recorder.calls.append(_Call(request))
        return httpx.Response(
            200,
            headers={"content-type": "text/plain"},
            stream=_AsyncByteStream([b"hello from app"]),
        )

    def _fake_upstream_client():
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    import app.api.data_apps_proxy as proxy_api

    monkeypatch.setattr(proxy_api, "_upstream_client", _fake_upstream_client)
    return recorder


# ---------------------------------------------------------------------------
# HTTP proxy — state routing, wake, debounce, header hygiene
# ---------------------------------------------------------------------------


def test_running_app_is_proxied(client_granted, fake_runner, respx_upstream, running_app):
    r = client_granted.get("/apps/s/hello")
    assert r.status_code == 200, r.text
    assert r.text == "hello from app"
    assert respx_upstream.calls[0].request.headers["x-forwarded-prefix"] == "/apps/s"


def test_proxy_strips_hop_by_hop_headers(client_granted, fake_runner, respx_upstream, running_app):
    r = client_granted.get("/apps/s/hello", headers={"connection": "close", "x-custom": "kept"})
    assert r.status_code == 200
    sent = respx_upstream.calls[0].request.headers
    # httpx always emits its OWN `Connection` header on an outgoing request
    # (an HTTP protocol necessity, added by httpx's client defaults
    # regardless of what we forward) — the hygiene guarantee under test is
    # that the CALLER's hop-by-hop value never rides through unfiltered.
    assert sent["connection"] != "close"
    assert sent["x-custom"] == "kept"


def test_proxy_strips_caller_credentials(client_granted, fake_runner, respx_upstream, running_app):
    """Security: the caller's Agnes credentials (`Authorization` — already
    injected on every `client_granted` call — and `Cookie`, set explicitly
    here) must never reach the proxied data-app container. Distinct from
    the hop-by-hop test above: these aren't protocol headers, they're the
    caller's own auth material."""
    r = client_granted.get("/apps/s/hello", headers={"cookie": "access_token=whatever; other=1"})
    assert r.status_code == 200
    sent = respx_upstream.calls[0].request.headers
    assert "authorization" not in sent
    assert "cookie" not in sent


def test_sleeping_app_returns_holding_page_and_wakes(client_granted, fake_runner, sleeping_app):
    r = client_granted.get("/apps/s/", headers={"accept": "text/html"})
    assert r.status_code == 503
    assert "waking" in r.text.lower()
    assert fake_runner.up_calls  # wake fired exactly once


def test_sleeping_app_json_accept(client_granted, fake_runner, sleeping_app):
    r = client_granted.get("/apps/s/", headers={"accept": "application/json"})
    assert r.status_code == 503
    assert r.json()["status"] == "waking"


def test_sleeping_app_wake_fires_exactly_once_under_repeat_requests(client_granted, fake_runner, sleeping_app):
    """Second request lands while state is already 'deploying' (the wake
    lease + state flip from the first request) — must not fire a second
    redeploy."""
    r1 = client_granted.get("/apps/s/", headers={"accept": "application/json"})
    assert r1.status_code == 503
    r2 = client_granted.get("/apps/s/", headers={"accept": "application/json"})
    assert r2.status_code == 503
    assert len(fake_runner.up_calls) == 1


def test_sleeping_recreate_wake_does_not_block_response(client_granted, fake_runner, sleeping_app, monkeypatch):
    """Overrides the `_inline_spawn_wake` autouse fixture with a no-op
    recorder that never actually runs `fn` — proving the PRODUCTION
    `_spawn_wake` call site is genuinely fire-and-forget (the holding page
    response arrives regardless of how long the real redeploy would take),
    not just fast in this test suite because the fake runner is fast."""
    import app.api.data_apps_proxy as proxy_api

    calls = []

    async def _recorder(fn, row):
        calls.append((fn, row))
        # Deliberately does NOT call `fn` — if the handler awaited this
        # coroutine's *effect* rather than just scheduling it, a `fn` that
        # never resolves would hang the request forever. It doesn't hang,
        # which is exactly what this test is checking.

    monkeypatch.setattr(proxy_api, "_spawn_wake", _recorder)

    r = client_granted.get("/apps/s/", headers={"accept": "application/json"})
    assert r.status_code == 503
    assert r.json()["status"] == "waking"
    assert len(calls) == 1
    fn, row = calls[0]
    assert fn is proxy_api.redeploy_current
    assert row["slug"] == "s"
    assert not fake_runner.up_calls  # the recorder never actually ran fn


def test_sleeping_pause_mode_resumes_and_sets_running(client_granted, fake_runner):
    _create_app_row(slug="p", state="sleeping", sleep_mode="pause")
    r = client_granted.get("/apps/p/", headers={"accept": "application/json"})
    assert r.status_code == 503
    assert fake_runner.resume_calls == ["p"]
    assert not fake_runner.up_calls  # pause mode never redeploys

    from src.repositories import data_apps_repo

    row = data_apps_repo().get_by_slug("p")
    assert row["state"] == "running"


def test_sleeping_app_wake_suppressed_by_op_lease_held_elsewhere(client_granted, fake_runner, sleeping_app):
    """The `dataapp:op:{slug}` lease is shared with `deploy_data_app`/
    `stop_data_app` (`app/api/data_apps.py`) — a manual deploy/stop
    currently in flight for this slug must suppress an auto-wake the same
    way a concurrent wake already suppresses a second one (see
    `test_sleeping_app_wake_fires_exactly_once_under_repeat_requests`).
    Simulates the "manual op in flight" side by holding the lease
    directly rather than actually racing a real deploy request — the
    real-request race is covered on the deploy/stop side in
    `tests/test_data_apps_api.py::TestOpLeaseSerialization`."""
    from app.api.data_apps import release_op_lease, try_acquire_op_lease

    acquired, holder = try_acquire_op_lease("s")
    assert acquired
    try:
        r = client_granted.get("/apps/s/", headers={"accept": "application/json"})
        assert r.status_code == 503
        assert not fake_runner.up_calls  # wake never triggered — op lease was already held

        from src.repositories import data_apps_repo

        assert data_apps_repo().get_by_slug("s")["state"] == "sleeping"  # never flipped to "deploying"
    finally:
        release_op_lease("s", holder)


def test_stranger_gets_403(client_stranger, running_app):
    assert client_stranger.get("/apps/s/").status_code == 403


def test_missing_app_404s(client_granted):
    assert client_granted.get("/apps/does-not-exist/").status_code == 404


def test_touch_debounced(client_granted, running_app, respx_upstream):
    from src.repositories import data_apps_repo

    client_granted.get("/apps/s/")
    first = data_apps_repo().get_by_slug("s")["last_request_at"]
    client_granted.get("/apps/s/")
    assert data_apps_repo().get_by_slug("s")["last_request_at"] == first


def test_stopped_app_no_auto_wake_json(client_granted, fake_runner):
    _create_app_row(slug="st", state="stopped")
    r = client_granted.get("/apps/st/", headers={"accept": "application/json"})
    assert r.status_code == 409
    assert r.json()["detail"] == "app_not_running"
    assert not fake_runner.up_calls
    assert not fake_runner.resume_calls


def test_stopped_app_holding_page_html(client_granted, fake_runner):
    _create_app_row(slug="st2", state="stopped")
    r = client_granted.get("/apps/st2/", headers={"accept": "text/html"})
    assert r.status_code == 409
    assert "stopped" in r.text.lower()
    assert not fake_runner.up_calls


def test_created_app_no_auto_wake(client_granted, fake_runner):
    _create_app_row(slug="cr", state="created")
    r = client_granted.get("/apps/cr/", headers={"accept": "application/json"})
    assert r.status_code == 409
    assert r.json()["detail"] == "app_not_running"
    assert not fake_runner.up_calls


def test_error_state_returns_409_with_detail(client_granted, fake_runner):
    from src.db import get_system_db
    from src.repositories.data_apps import DataAppsRepository

    conn = get_system_db()
    try:
        repo = DataAppsRepository(conn)
        app_id = repo.create(slug="err1", name="ERR1", owner_user_id="owner1")
        repo.set_state(app_id, "error", "boom")
    finally:
        conn.close()

    r = client_granted.get("/apps/err1/")
    assert r.status_code == 409
    assert r.json()["state_detail"] == "boom"


def _refuse_connections(monkeypatch):
    import app.api.data_apps_proxy as proxy_api

    def _broken_client():
        def handler(request):
            raise httpx.ConnectError("connection refused", request=request)

        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(proxy_api, "_upstream_client", _broken_client)


def test_unreachable_while_the_container_is_up_does_not_latch_error(
    client_granted, fake_runner, monkeypatch, running_app
):
    """A container that is up but not yet listening is STARTING, not broken.

    `error` is a latch — nothing clears it but a redeploy — so latching on a
    refused connection let one badly-timed request brick a healthy app. The
    window is wide: a first deploy clones, installs and builds before
    anything listens, while the row reads `running` from the moment the
    runner accepted the container. Watched live on a running instance: the
    app served 200 inside its container while Agnes answered `app_error /
    container unreachable` to every caller.
    """
    fake_runner._status = {"container": "running", "ready": False}
    _refuse_connections(monkeypatch)

    r = client_granted.get("/apps/s/hello")
    assert r.status_code == 503, "a starting app gets the waking page, not an error"

    from src.repositories import data_apps_repo

    assert data_apps_repo().get_by_slug("s")["state"] == "running", "state must not be latched"


def test_unreachable_with_a_dead_container_still_sets_error(client_granted, fake_runner, monkeypatch, running_app):
    """The latch is still right when the container is genuinely gone."""
    fake_runner._status = {"container": "stopped", "ready": False}
    _refuse_connections(monkeypatch)

    r = client_granted.get("/apps/s/hello")
    assert r.status_code == 502

    from src.repositories import data_apps_repo

    row = data_apps_repo().get_by_slug("s")
    assert row["state"] == "error"
    assert "stopped" in (row["state_detail"] or ""), "the detail should name what the container was"


def test_unreachable_with_a_dead_runner_says_nothing_about_the_app(
    client_granted, dead_runner, monkeypatch, running_app
):
    """If the runner itself is down we know nothing about the container, and
    guessing `error` would blame the app for the sidecar's outage — with a
    latch the user can only clear by redeploying."""
    _refuse_connections(monkeypatch)

    r = client_granted.get("/apps/s/hello")
    assert r.status_code == 502

    from src.repositories import data_apps_repo

    assert data_apps_repo().get_by_slug("s")["state"] == "running"


def test_get_apps_slug_redirects_to_trailing_slash(client_granted, running_app):
    r = client_granted.get("/apps/s", follow_redirects=False)
    assert r.status_code in (307, 308)
    assert r.headers["location"] == "/apps/s/"


def test_subdomain_host_rewrite(client_granted, running_app, respx_upstream, proxy_env):
    _set_data_apps_config(proxy_env["data_dir"], subdomain_base="apps.example.com")
    r = client_granted.get("/", headers={"host": "s.apps.example.com"})
    assert r.status_code == 200  # reached the proxy handler for slug s


def test_subdomain_request_omits_x_forwarded_prefix(client_granted, running_app, respx_upstream, proxy_env):
    """Spec S6: an app served at its own subdomain root must not receive
    X-Forwarded-Prefix (there IS no prefix from its point of view) — unlike
    the same app reached via the path-prefix form, see
    `test_running_app_is_proxied` for the contrast."""
    _set_data_apps_config(proxy_env["data_dir"], subdomain_base="apps.example.com")
    r = client_granted.get("/", headers={"host": "s.apps.example.com"})
    assert r.status_code == 200
    assert "x-forwarded-prefix" not in respx_upstream.calls[0].request.headers


def test_path_prefix_request_still_gets_x_forwarded_prefix(client_granted, running_app, respx_upstream):
    r = client_granted.get("/apps/s/hello")
    assert r.status_code == 200
    assert respx_upstream.calls[0].request.headers["x-forwarded-prefix"] == "/apps/s"


# ---------------------------------------------------------------------------
# readiness flip: deploying -> running once the runner reports ready
# ---------------------------------------------------------------------------


def test_readiness_flips_deploying_to_running(client_granted, fake_runner):
    from src.repositories import data_apps_repo

    _create_app_row(slug="dep", state="deploying")
    fake_runner._status = {"container": "running", "ready": True}

    r = client_granted.get("/api/data-apps/dep/readiness")
    assert r.status_code == 200
    assert r.json() == {"state": "running", "ready": True}
    assert data_apps_repo().get_by_slug("dep")["state"] == "running"


def test_readiness_stays_deploying_when_not_ready(client_granted, fake_runner):
    from src.repositories import data_apps_repo

    _create_app_row(slug="dep2", state="deploying")
    fake_runner._status = {"container": "starting", "ready": False}

    r = client_granted.get("/api/data-apps/dep2/readiness")
    assert r.status_code == 200
    assert r.json() == {"state": "deploying", "ready": False}
    assert data_apps_repo().get_by_slug("dep2")["state"] == "deploying"


# ---------------------------------------------------------------------------
# WebSocket bridge — auth-reject path only (no live upstream in this suite)
# ---------------------------------------------------------------------------


def test_ws_stranger_rejected_with_4403(client_stranger, running_app):
    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client_stranger.websocket_connect("/apps/s/ws"):
            pass
    assert excinfo.value.code == 4403


def test_ws_missing_app_rejected_with_4404(client_granted):
    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client_granted.websocket_connect("/apps/does-not-exist/ws"):
            pass
    assert excinfo.value.code == 4404


def test_ws_sleeping_app_rejected_with_4404(client_granted, sleeping_app):
    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client_granted.websocket_connect("/apps/s/ws"):
            pass
    assert excinfo.value.code == 4404


# ---------------------------------------------------------------------------
# Same-origin serving gate (security): a hosted app served on the MAIN origin
# shares the viewer's session with app-authored JS, which can read /api. Refused
# unless the request arrived on a data-app subdomain (isolated origin) or the
# operator opted in via data_apps.allow_same_origin.
# ---------------------------------------------------------------------------


def test_same_origin_serving_refused_by_default(client_granted, running_app, respx_upstream, proxy_env):
    _set_data_apps_config(proxy_env["data_dir"], allow_same_origin=False)
    r = client_granted.get("/apps/s/hello")
    assert r.status_code == 403
    assert "isolated origin" in r.text  # the operator-facing HTML explains the fix
    assert respx_upstream.calls == []  # hard stop — never proxied to the container


def test_same_origin_serving_refused_json(client_granted, running_app, respx_upstream, proxy_env):
    _set_data_apps_config(proxy_env["data_dir"], allow_same_origin=False)
    r = client_granted.get("/apps/s/hello", headers={"accept": "application/json"})
    assert r.status_code == 403
    assert r.json()["detail"] == "data_app_same_origin_disabled"


def test_same_origin_serving_allowed_with_ack(client_granted, running_app, respx_upstream, proxy_env):
    _set_data_apps_config(proxy_env["data_dir"], allow_same_origin=True)
    r = client_granted.get("/apps/s/hello")
    assert r.status_code == 200
    assert r.text == "hello from app"


def test_same_origin_gate_honors_env_override(client_granted, running_app, respx_upstream, proxy_env, monkeypatch):
    """The env override wins over an instance.yaml `allow_same_origin: false`."""
    _set_data_apps_config(proxy_env["data_dir"], allow_same_origin=False)
    monkeypatch.setenv("AGNES_DATA_APPS_ALLOW_SAME_ORIGIN", "1")
    r = client_granted.get("/apps/s/hello")
    assert r.status_code == 200
    assert r.text == "hello from app"


def test_subdomain_request_served_without_ack(client_granted, running_app, respx_upstream, proxy_env):
    """A subdomain-origin request is already on an isolated origin, so it is
    served even with same-origin serving off."""
    _set_data_apps_config(proxy_env["data_dir"], subdomain_base="apps.example.com", allow_same_origin=False)
    r = client_granted.get("/", headers={"host": "s.apps.example.com"})
    assert r.status_code == 200
    assert respx_upstream.calls  # reached and proxied to the container


def test_same_origin_gate_runs_after_rbac(client_stranger, running_app, respx_upstream, proxy_env):
    """The gate runs AFTER RBAC: a stranger still gets a plain 403 `forbidden`,
    so the refusal never reveals an app's existence to an unauthorized caller."""
    _set_data_apps_config(proxy_env["data_dir"], allow_same_origin=False)
    r = client_stranger.get("/apps/s/hello", headers={"accept": "application/json"})
    assert r.status_code == 403
    assert r.json()["detail"] == "forbidden"


def test_ws_same_origin_refused_by_default(client_granted, running_app, proxy_env):
    """The WS bridge mirrors the HTTP gate — owner passes RBAC, so the only
    reason for the 4403 close here is the same-origin refusal."""
    _set_data_apps_config(proxy_env["data_dir"], allow_same_origin=False)
    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client_granted.websocket_connect("/apps/s/ws"):
            pass
    assert excinfo.value.code == 4403


# ---------------------------------------------------------------------------
# same_origin_serving_warning() — the startup log when apps are enabled but no
# hosted app can be served (same-origin off, no isolated origin configured).
# ---------------------------------------------------------------------------


def test_same_origin_warning_fires_when_enabled_without_isolation(proxy_env):
    from app.api.data_apps import same_origin_serving_warning

    _set_data_apps_config(proxy_env["data_dir"], allow_same_origin=False)
    msg = same_origin_serving_warning()
    assert msg is not None and "subdomain_base" in msg


def test_same_origin_warning_silent_with_subdomain(proxy_env):
    from app.api.data_apps import same_origin_serving_warning

    _set_data_apps_config(proxy_env["data_dir"], subdomain_base="apps.example.com", allow_same_origin=False)
    assert same_origin_serving_warning() is None


def test_same_origin_warning_silent_with_ack(proxy_env):
    from app.api.data_apps import same_origin_serving_warning

    _set_data_apps_config(proxy_env["data_dir"], allow_same_origin=True)
    assert same_origin_serving_warning() is None


# ---------------------------------------------------------------------------
# Session-cookie domain — regression: no Domain= attribute when
# data_apps.subdomain_base is unset (today's exact behavior).
# ---------------------------------------------------------------------------


def test_session_cookie_no_domain_when_subdomain_base_unset(proxy_env):
    from starlette.responses import Response

    from app.auth.providers.password import _set_login_cookie

    resp = Response()
    _set_login_cookie(resp, "owner1", "owner@test.local")
    set_cookie_header = resp.headers.get("set-cookie", "")
    assert set_cookie_header  # sanity: a cookie was actually set
    assert "domain=" not in set_cookie_header.lower()


def test_session_cookie_gets_parent_domain_when_subdomain_base_set(proxy_env):
    from starlette.responses import Response

    from app.auth.providers.password import _set_login_cookie

    _set_data_apps_config(proxy_env["data_dir"], subdomain_base="apps.example.com")
    resp = Response()
    _set_login_cookie(resp, "owner1", "owner@test.local")
    set_cookie_header = resp.headers.get("set-cookie", "")
    assert "domain=.example.com" in set_cookie_header.lower()


# ---------------------------------------------------------------------------
# get_data_apps_config() hardening — a `None` from get_value (bad/absent
# config, or a config-not-loaded-yet bootstrap state) must never crash the
# subdomain middleware or session_cookie_domain(); the accessor itself
# always returns a dict.
# ---------------------------------------------------------------------------


def test_get_data_apps_config_hardened_against_none(monkeypatch):
    import app.instance_config as instance_config

    monkeypatch.setattr(instance_config, "get_value", lambda *a, **k: None)
    assert instance_config.get_data_apps_config() == {}
    assert instance_config.session_cookie_domain() is None


def test_subdomain_middleware_survives_none_config(monkeypatch):
    import asyncio

    import app.data_apps_subdomain as subdomain_mod
    import app.instance_config as instance_config

    monkeypatch.setattr(instance_config, "get_value", lambda *a, **k: None)

    seen_paths = []

    async def inner_app(scope, receive, send):
        seen_paths.append(scope["path"])

    middleware = subdomain_mod.DataAppSubdomainMiddleware(inner_app)
    scope = {"type": "http", "path": "/metrics", "headers": [(b"host", b"example.com")]}

    asyncio.run(middleware(scope, None, None))
    assert seen_paths == ["/metrics"]  # no-op passthrough, no crash


# ---------------------------------------------------------------------------
# Wave 3C — scoped `data-app-preview:<slug>` token accept on the serving
# path (Task 5), rejected everywhere else.
# ---------------------------------------------------------------------------


@pytest.fixture
def proxy_client(proxy_env):
    """Unauthenticated raw client — the preview tests send ONLY the
    preview-token cookie/header, never a normal session PAT."""
    return proxy_env["client"]


class _PreviewToken:
    def __init__(self, jwt_token: str, cookie: str):
        self.jwt = jwt_token
        self.cookie = cookie


@pytest.fixture
def mint_preview(proxy_env):
    """Mint a `data-app-preview:<slug>` token directly via
    `_mint_preview_token` (Task 5) — the same helper
    `POST /{slug}/preview-grant` calls, exercised here without going through
    the endpoint so the proxy-side accept/reject logic is tested in
    isolation from the grant endpoint's own RBAC (covered separately in
    `tests/test_data_apps_preview.py`)."""
    from app.api.data_apps import _mint_preview_token
    from src.repositories import data_apps_repo, users_repo

    def _mint(slug: str, ttl_s: int = 1800) -> _PreviewToken:
        row = data_apps_repo().get_by_slug(slug)
        requester = users_repo().get_by_id(row["owner_user_id"])
        jwt_token, cookie = _mint_preview_token(row, requester, ttl_s=ttl_s)
        return _PreviewToken(jwt_token, cookie)

    return _mint


def test_preview_token_authorizes_iframe(proxy_client, fake_runner, respx_upstream, running_app, mint_preview):
    tok = mint_preview("s", ttl_s=1800)
    r = proxy_client.get("/apps/s/hello", headers={"cookie": tok.cookie})
    assert r.status_code == 200, r.text
    # Body, not just status: a 401 here is redirected to `/login`, which also
    # answers 200, so status alone passes whether or not the token was read.
    assert r.text == "hello from app"


def test_preview_token_serves_same_origin_without_global_ack(
    proxy_client, fake_runner, respx_upstream, running_app, mint_preview, proxy_env
):
    """A per-app preview token serves same-origin even with
    `allow_same_origin=False` — the in-chat preview works WITHOUT the global
    flag. A plain navigation (no preview token) to the same origin is still
    refused (see `test_same_origin_serving_refused_by_default`), so enabling the
    preview does not re-open drive-by same-origin serving for every app."""
    _set_data_apps_config(proxy_env["data_dir"], allow_same_origin=False)
    tok = mint_preview("s", ttl_s=1800)
    r = proxy_client.get("/apps/s/hello", headers={"cookie": tok.cookie})
    assert r.status_code == 200, r.text
    assert r.text == "hello from app"


def test_expired_preview_token_403(proxy_client, running_app, mint_preview):
    """A GET returning a raw 401 on a non-API path is redirected to
    ``/login`` by the app-wide browser-friendly error handler
    (``app/main.py::_html_auth_redirect_handler`` — existing contract,
    unrelated to this feature); ``follow_redirects=False`` observes the
    real status this route raised rather than the login page it redirects
    to."""
    tok = mint_preview("s", ttl_s=-1)  # already expired
    r = proxy_client.get("/apps/s/", headers={"cookie": tok.cookie}, follow_redirects=False)
    assert r.status_code in (401, 403, 302, 307)


def test_preview_token_scoped_to_slug(proxy_client, running_app, mint_preview):
    _create_app_row(slug="other", state="running")
    tok = mint_preview("s", ttl_s=1800)  # token minted for slug "s"
    r = proxy_client.get("/apps/other/", headers={"cookie": tok.cookie}, follow_redirects=False)
    assert r.status_code in (401, 403, 302, 307)


def test_preview_token_rejected_on_control_plane(proxy_client, running_app, mint_preview):
    tok = mint_preview("s", ttl_s=1800)
    r = proxy_client.get("/api/data-apps/s", headers={"Authorization": f"Bearer {tok.jwt}"})
    assert r.status_code == 401


def test_preview_token_via_bearer_also_authorizes(proxy_client, fake_runner, respx_upstream, running_app, mint_preview):
    """Same accept path via `Authorization: Bearer` instead of the cookie —
    the spec's cookie delivery is the frontend's choice, not the only shape
    the proxy accepts."""
    tok = mint_preview("s", ttl_s=1800)
    r = proxy_client.get("/apps/s/hello", headers={"Authorization": f"Bearer {tok.jwt}"})
    assert r.status_code == 200, r.text


def test_stranger_with_no_token_at_all_gets_401(proxy_client, running_app):
    """A raw 401 on a GET to a non-API path is redirected to `/login` by the
    app-wide browser-friendly error handler (existing contract, unrelated to
    this feature) — the 302 IS the 401, just wrapped for a browser nav."""
    r = proxy_client.get("/apps/s/", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"].startswith("/login")


def test_a_live_but_silent_container_latches_once_the_grace_window_passes(
    client_granted, fake_runner, monkeypatch, running_app
):
    """ "Starting" has to be time-bounded.

    The runner reports `running | paused | stopped | absent`, so a container
    whose app process died or wedged WITHOUT the container exiting still
    reads `running`. Treating that as "starting" forever would trade a
    permanent latch for a permanent spinner — no better, and harder to
    diagnose (Devin Review on this PR).
    """
    from datetime import datetime, timedelta, timezone

    import app.api.data_apps_proxy as proxy_api
    from src.repositories import data_apps_repo

    fake_runner._status = {"container": "running", "ready": False}
    _refuse_connections(monkeypatch)
    monkeypatch.setattr(
        proxy_api,
        "_within_start_grace",
        lambda row: False,
        raising=True,
    )

    r = client_granted.get("/apps/s/hello")
    assert r.status_code == 502
    row = data_apps_repo().get_by_slug("s")
    assert row["state"] == "error"
    assert "not listening" in (row["state_detail"] or ""), "the detail must not blame the container's state"
    _ = (datetime, timedelta, timezone)


def test_a_paused_container_wakes_instead_of_latching(client_granted, fake_runner, monkeypatch, running_app):
    """`paused` is the pause-mode sleep state, not a fault — recording an
    error there would make the caller redeploy to clear something that only
    needed a resume."""
    from src.repositories import data_apps_repo

    fake_runner._status = {"container": "paused", "ready": False}
    _refuse_connections(monkeypatch)

    r = client_granted.get("/apps/s/hello")
    assert r.status_code == 503, "a paused app gets the waking page"
    assert data_apps_repo().get_by_slug("s")["state"] != "error"


def test_the_grace_window_reads_the_deploy_timestamp():
    """Measured from when the boot began, not from now-ish defaults."""
    from datetime import datetime, timedelta, timezone

    from app.api.data_apps_proxy import _within_start_grace

    fresh = datetime.now(timezone.utc) - timedelta(seconds=5)
    stale = datetime.now(timezone.utc) - timedelta(hours=3)
    assert _within_start_grace({"last_deploy_at": fresh}) is True
    assert _within_start_grace({"last_deploy_at": stale}) is False
    assert _within_start_grace({"last_deploy_at": None, "updated_at": fresh}) is True
    # A naive stamp from a DB session ahead of UTC lands in the "future";
    # the window is symmetric so a clock offset either way still reads as
    # recent rather than instantly expired (Devin Review on this PR).
    future = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=2)
    assert _within_start_grace({"last_deploy_at": future}) is True
    # No clock at all → not provably still starting.
    assert _within_start_grace({}) is False
    assert _within_start_grace({"last_deploy_at": "not-a-timestamp"}) is False


def test_the_grace_window_follows_a_wake_not_only_a_deploy():
    """`last_deploy_at` is written only by `POST /{slug}/deploy`; the auto-wake
    path never refreshes it. Measuring from it alone would give a woken app a
    grace computed from a deploy days old, so the first refused connection
    after a wake would latch instead of holding (Devin Review on this PR)."""
    from datetime import datetime, timedelta, timezone

    from app.api.data_apps_proxy import _within_start_grace

    old_deploy = datetime.now(timezone.utc) - timedelta(days=3)
    just_woken = datetime.now(timezone.utc) - timedelta(seconds=10)
    assert _within_start_grace({"last_deploy_at": old_deploy, "updated_at": just_woken}) is True
    # ...and a stale row on both clocks is still out of grace.
    assert _within_start_grace({"last_deploy_at": old_deploy, "updated_at": old_deploy}) is False


def test_a_preview_token_can_poll_readiness(proxy_client, fake_runner, running_app, mint_preview):
    """The holding page's poll must work for the credential the holding page
    is served to.

    `_waking_response` is rendered by the proxy, which accepts a
    `data-app-preview:<slug>` token — but `/readiness`, the endpoint its poll
    loop hits, resolved through `get_current_user`, which rejects that scope
    outright. So an in-chat preview iframe got a holding page whose poll 401'd
    forever and never noticed the app come up (Devin Review on this PR).
    """
    tok = mint_preview("s", ttl_s=1800)
    r = proxy_client.get("/api/data-apps/s/readiness", headers={"cookie": tok.cookie})
    assert r.status_code == 200, r.text
    assert "ready" in r.json()


def test_the_preview_cookie_is_scoped_to_a_path_the_poll_actually_uses(mint_preview, running_app):
    """The sibling test above sets `cookie:` by hand, so it passes under ANY
    `Path=` — including one a browser would never send.

    That is not a nitpick: the cookie was minted `Path=/apps/<slug>/` while the
    holding page polls `/api/data-apps/<slug>/readiness`. A browser only
    attaches a cookie whose `Path` is a prefix of the request path, so the real
    poll went out with no credential, 401'd, and the template's `catch`
    swallowed it — the preview spun forever while the app was up, which is the
    exact failure the preview scope was added to fix (Devin Review on this PR).

    So assert the scoping itself, against the URL the page really polls.
    """
    from http.cookies import SimpleCookie

    from app.api.data_apps import preview_cookie_name
    from app.api.data_apps_proxy import _readiness_poll_url

    cookie = SimpleCookie()
    cookie.load(mint_preview("s", ttl_s=1800).cookie)
    path = cookie[preview_cookie_name("s")]["path"]

    poll_url = _readiness_poll_url(_FakeRequest(subdomain=False), "s")
    assert poll_url.startswith(path), (
        f"cookie Path={path!r} does not cover the readiness poll {poll_url!r} — "
        "a browser will not attach the credential"
    )
    # The pin that makes the wide path safe lives in the token scope, not here.
    assert "HttpOnly" in mint_preview("s").cookie


class _FakeRequest:
    """Minimal stand-in for `_readiness_poll_url`'s only input."""

    def __init__(self, *, subdomain: bool) -> None:
        self.scope = {"agnes_data_app_subdomain": subdomain}


def test_a_preview_token_for_another_app_cannot_poll_readiness(proxy_client, fake_runner, running_app, mint_preview):
    """The scope pin is what makes skipping the grant check safe."""
    _create_app_row(slug="other2", state="running")
    tok = mint_preview("other2", ttl_s=1800)
    r = proxy_client.get("/api/data-apps/s/readiness", headers={"cookie": tok.cookie}, follow_redirects=False)
    assert r.status_code in (401, 403), r.text


def test_a_legacy_named_preview_cookie_still_authorizes(
    proxy_client, fake_runner, respx_upstream, running_app, mint_preview
):
    """A preview open across the upgrade that introduced the per-app cookie
    name must not break: its browser holds the credential under the old bare
    `adp_preview` name, and the token behind it is still valid for the rest of
    its TTL. The reader accepts either name; the scope check is what decides.

    Asserted on the upstream's own body, not just a 200: a 401 on this path is
    turned into a redirect to `/login` by the app-wide browser-friendly error
    handler, and that page answers 200 too — so status alone would pass
    whether or not the cookie was ever read.
    """
    tok = mint_preview("s", ttl_s=1800)
    r = proxy_client.get("/apps/s/hello", headers={"cookie": f"adp_preview={tok.jwt}"})
    assert r.status_code == 200, r.text
    assert r.text == "hello from app", "served the login page, not the app — the cookie was not accepted"


def test_a_legacy_named_cookie_is_still_pinned_to_its_own_slug(proxy_client, fake_runner, running_app, mint_preview):
    """The rollout fallback must not become a way around the slug pin."""
    _create_app_row(slug="other3", state="running")
    tok = mint_preview("other3", ttl_s=1800)
    r = proxy_client.get(
        "/api/data-apps/s/readiness", headers={"cookie": f"adp_preview={tok.jwt}"}, follow_redirects=False
    )
    assert r.status_code in (401, 403), r.text


def test_an_anonymous_probe_cannot_tell_a_real_slug_from_a_missing_one(proxy_client, running_app):
    """Readiness must resolve the caller BEFORE the registry read.

    With auth moved from the `Depends` chain into the handler body, a
    lookup-first ordering answered an anonymous `GET /readiness` 404 for a
    made-up slug and 401 for a real one — a credential-free way to enumerate
    which hosted apps exist on the instance (Devin Review on this PR). Both
    must answer the same 401 the old `Depends(get_current_user)` signature
    enforced.
    """
    real = proxy_client.get("/api/data-apps/s/readiness", follow_redirects=False)
    fake = proxy_client.get("/api/data-apps/no-such-app/readiness", follow_redirects=False)
    assert real.status_code == 401, real.text
    assert fake.status_code == 401, (
        f"an unknown slug answered {fake.status_code} while a real one answers 401 — "
        "anonymous callers can enumerate app slugs"
    )


# ---------------------------------------------------------------------------
# Subdomain-mode readiness CORS — route-scoped, per-app, never app-wide.
# ---------------------------------------------------------------------------


def test_readiness_answers_cors_for_the_apps_own_subdomain_origin(
    proxy_client, proxy_env, fake_runner, running_app, mint_preview
):
    """On a subdomain-served instance the holding page lives on
    `https://<slug>.<base>` and its poll must be able to READ the readiness
    JSON from the main host — a credentialed cross-origin simple GET, so the
    response headers are the entire CORS surface needed."""
    tok = mint_preview("s", ttl_s=1800)

    # No subdomain_base configured -> no CORS grant, even for a matching shape.
    r = proxy_client.get(
        "/api/data-apps/s/readiness",
        headers={"cookie": tok.cookie, "origin": "https://s.apps.example.com"},
    )
    assert r.status_code == 200, r.text
    assert "access-control-allow-origin" not in r.headers

    _set_data_apps_config(proxy_env["data_dir"], subdomain_base="apps.example.com")
    r = proxy_client.get(
        "/api/data-apps/s/readiness",
        headers={"cookie": tok.cookie, "origin": "https://s.apps.example.com"},
    )
    assert r.status_code == 200, r.text
    assert r.headers.get("access-control-allow-origin") == "https://s.apps.example.com"
    assert r.headers.get("access-control-allow-credentials") == "true"
    assert "origin" in (r.headers.get("vary") or "").lower()


def test_readiness_cors_is_pinned_to_the_apps_own_origin(
    proxy_client, proxy_env, fake_runner, running_app, mint_preview
):
    """A sibling app's subdomain — user-authored code — must not be able to
    read another app's readiness, and non-subdomain origins get nothing."""
    _set_data_apps_config(proxy_env["data_dir"], subdomain_base="apps.example.com")
    tok = mint_preview("s", ttl_s=1800)
    for origin in (
        "https://other.apps.example.com",  # a sibling app's own origin
        "https://evil.example.com",
        "https://s.apps.example.com.evil.com",  # suffix-spoof
        "ftp://s.apps.example.com",
    ):
        r = proxy_client.get(
            "/api/data-apps/s/readiness",
            headers={"cookie": tok.cookie, "origin": origin},
        )
        assert r.status_code == 200, r.text
        assert "access-control-allow-origin" not in r.headers, f"origin {origin} must not be CORS-readable"
        # Refusals still declare the answer varies by Origin: without it, a
        # URL-keyed cache could store THIS header-less variant and replay it
        # to the app's own origin, wedging the holding page (Devin on #1321).
        # Substring: GZipMiddleware folds its own "Accept-Encoding" into Vary.
        assert "Origin" in r.headers.get("vary", ""), f"origin {origin} refusal must still carry Vary: Origin"


def test_readiness_cors_grant_is_withheld_under_wildcard_cors(
    proxy_client, proxy_env, fake_runner, running_app, mint_preview, monkeypatch
):
    """With CORS_ORIGINS='*' the app-wide middleware stamps a credential-less
    `ACAO: *` over handler headers while a handler-set `Allow-Credentials`
    would survive — a browser-invalid pair. The helper withholds the grant
    entirely (the poll is broken under the wildcard either way; main.py logs
    a dedicated error), leaving only `Vary: Origin` (Devin on #1321)."""
    _set_data_apps_config(proxy_env["data_dir"], subdomain_base="apps.example.com")
    # The verdict is captured on app.state at build time (same read the
    # middleware sees); flip it there — a request-time env change must NOT
    # move the helper, that divergence is the bug this pins.
    proxy_env["app"].state.cors_has_wildcard = True
    tok = mint_preview("s", ttl_s=1800)
    r = proxy_client.get(
        "/api/data-apps/s/readiness",
        headers={"cookie": tok.cookie, "origin": "https://s.apps.example.com"},
    )
    assert r.status_code == 200, r.text
    # The grant marker is Allow-ORIGIN: the app-wide middleware contributes a
    # bare Allow-Credentials on every Origin-carrying response under an
    # explicit-origins config (Starlette puts it in simple_headers), which is
    # inert without a matching Allow-Origin — what must NOT appear is the
    # helper's per-app origin echo.
    assert "access-control-allow-origin" not in r.headers
    assert "Origin" in r.headers.get("vary", "")


def test_data_app_subdomains_get_no_app_wide_cors(proxy_env):
    """The one readable response is the readiness poll's — NOT every endpoint.

    The first cut allowed `*.{subdomain_base}` app-wide via CORSMiddleware's
    `allow_origin_regex` with `allow_credentials=True`. Data-app subdomains
    serve user-authored JS, and the session cookie already rides from them to
    the main host (`Domain=.<parent>`, same-site) — so that policy let any
    hosted app's code read every authenticated Agnes endpoint as its viewer
    (Devin Review on this PR). The middleware must not reflect those origins
    even when `subdomain_base` is configured at app-build time.
    """
    from fastapi.testclient import TestClient

    from app.main import create_app

    _set_data_apps_config(proxy_env["data_dir"], subdomain_base="apps.example.com")
    _create_app_row(slug="corsapp", state="created")
    client = TestClient(create_app())

    r = client.get(
        "/api/data-apps/corsapp",
        headers={"Authorization": f"Bearer {proxy_env['owner_pat']}", "origin": "https://corsapp.apps.example.com"},
    )
    assert r.status_code == 200, r.text
    assert "access-control-allow-origin" not in r.headers, (
        "an authenticated control-plane response is CORS-readable from a data-app "
        "subdomain — the app-wide credentialed origin grant is back"
    )


def test_a_slug_that_would_break_the_cookie_header_is_refused(mint_preview, running_app):
    """The slug is interpolated into a `Set-Cookie` NAME, so it is re-checked
    at mint rather than trusted to have come from a validated create — a
    header assembled from an unvalidated name is the shape CRLF injection
    needs. Refusing is right here: there is no safe cookie to emit, and
    silently mangling the name would hand back a credential no request can
    ever carry.

    And the refusal must be SIDE-EFFECT-FREE: the check first ran after the
    JWT was minted and the `access_tokens` row persisted, so each rejected
    slug below left a live, orphaned 30-minute credential in the DB that
    nothing would ever hand out or revoke (Devin Review on this PR).
    """
    from app.api.data_apps import _mint_preview_token
    from src.repositories import access_token_repo, data_apps_repo, users_repo

    row = dict(data_apps_repo().get_by_slug("s"))
    requester = users_repo().get_by_id(row["owner_user_id"])
    for bad in ("s\r\nSet-Cookie: admin=1", "s; Domain=evil.example.com", "s=x", "s app"):
        row["slug"] = bad
        with pytest.raises(ValueError):
            _mint_preview_token(row, requester)

    orphans = [
        t
        for t in access_token_repo().list_for_user(requester["id"], include_revoked=False)
        if (t.get("name") or "").startswith("data-app-preview:")
    ]
    assert orphans == [], f"a refused mint must not leave a live credential row behind: {orphans}"


def test_the_waking_page_polls_a_relative_url_on_the_path_form(client_granted, fake_runner, sleeping_app):
    """No host pinned into the page when it is not needed."""
    r = client_granted.get("/apps/s/", headers={"accept": "text/html"})
    assert r.status_code == 503
    assert 'fetch("/api/data-apps/s/readiness", { credentials: "include" })' in r.text


def test_the_waking_page_polls_an_absolute_url_on_a_subdomain(monkeypatch):
    """`DataAppSubdomainMiddleware` rewrites EVERY path on `<slug>.<base>` to
    `/apps/<slug>/…` with no `/api` carve-out, so a relative poll came back as
    this very page: `r.json()` threw, the `catch` swallowed it, and the page
    spun forever while the app was up (Devin Review on this PR).

    A middleware carve-out would be the wrong fix — an app may serve its own
    `/api/*`, as the scaffolded dashboard does.
    """
    import app.instance_config as public_url_mod
    from app.api.data_apps_proxy import _readiness_poll_url

    monkeypatch.setattr(public_url_mod, "get_public_url", lambda: "https://agnes.example.com/", raising=True)

    class _Req:
        scope = {"agnes_data_app_subdomain": "s"}

    assert _readiness_poll_url(_Req(), "s") == "https://agnes.example.com/api/data-apps/s/readiness"

    class _PathReq:
        scope: dict = {}

    assert _readiness_poll_url(_PathReq(), "s") == "/api/data-apps/s/readiness"


def test_the_subdomain_poll_falls_back_rather_than_guessing_a_host(monkeypatch):
    """With no configured public URL there is nothing to point at — keep the
    previous (relative) behaviour instead of inventing an origin."""
    import app.instance_config as public_url_mod
    from app.api.data_apps_proxy import _readiness_poll_url

    monkeypatch.setattr(public_url_mod, "get_public_url", lambda: "", raising=True)

    class _Req:
        scope = {"agnes_data_app_subdomain": "s"}

    assert _readiness_poll_url(_Req(), "s") == "/api/data-apps/s/readiness"
