"""Wave 2 — Task 3: the three surfaces that wrote no audit trail at all.

1. apps-runner (no database access) reports container lifecycle events
   best-effort to a new control-plane endpoint
   (``services/apps_runner/audit_report.py`` -> ``POST
   /api/data-apps/runner-events`` -> ``app/api/data_apps.py``).
2. The data-app subdomain proxy (``app/data_apps_subdomain.py``) audits
   end-user traffic with a windowed dedup (first request per (user, app)
   per 15 minutes).
3. The notifications WebSocket (``app/api/notifications_ws.py``) audits
   connect + rejected handshakes.
"""

from __future__ import annotations

import asyncio
import json
import time

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.repositories import audit_repo


def _params(row: dict) -> dict:
    p = row["params"]
    return json.loads(p) if isinstance(p, str) else (p or {})


# ---------------------------------------------------------------------------
# 1a. services/apps_runner/audit_report.py — report_event never raises
# ---------------------------------------------------------------------------


def test_report_event_swallows_a_connection_error(monkeypatch):
    import httpx

    from services.apps_runner import audit_report

    def _raise(*args, **kwargs):
        raise httpx.ConnectError("control plane unreachable")

    monkeypatch.setattr(audit_report.httpx, "post", _raise)

    # Must not raise.
    audit_report.report_event("data_app.container_up", {"slug": "s1"})


def test_report_event_posts_action_and_params(monkeypatch):
    from services.apps_runner import audit_report

    calls = []

    def _capture(url, json=None, headers=None, timeout=None):
        calls.append((url, json, headers))

        class _Resp:
            status_code = 204

        return _Resp()

    monkeypatch.setenv("APPS_RUNNER_TOKEN", "rrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrr")
    monkeypatch.setattr(audit_report.httpx, "post", _capture)

    audit_report.report_event("data_app.container_up", {"slug": "s1"})

    assert len(calls) == 1
    url, body, headers = calls[0]
    assert url.endswith("/api/data-apps/runner-events")
    assert body == {"action": "data_app.container_up", "params": {"slug": "s1"}}
    assert headers["X-Runner-Token"] == "rrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrr"


# ---------------------------------------------------------------------------
# 1b. services/apps_runner/api.py — up/stop/resume report, and never fail
#     the container action even if reporting itself blows up.
# ---------------------------------------------------------------------------


@pytest.fixture
def runner_client(monkeypatch, tmp_path):
    from services.apps_runner import api
    from tests.test_apps_runner import FakeDocker

    monkeypatch.setenv("APPS_RUNNER_TOKEN", "rrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrr")
    monkeypatch.setenv("APPS_RUNNER_IMAGE_PREFIX", "keboolapublic.azurecr.io/data-app-python-js")
    fake = FakeDocker()
    monkeypatch.setattr(api, "_docker", lambda: fake)
    return TestClient(api.app), fake, tmp_path, api


def _spec(tmp):
    return {
        "name": "agnes-dataapp-s",
        "image": "keboolapublic.azurecr.io/data-app-python-js:1.6.2",
        "labels": {"agnes.data-app": "app_1"},
        "network": "agnes-apps",
        "config_dir": str(tmp / "apps" / "s"),
        "cache_volume": "agnes-dataapp-cache-s",
        "mem_limit": "1g",
        "cpus": 1.0,
        "env": {"A": "1"},
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "pids_limit": 512,
        "read_only": False,
        "tmpfs": {},
    }


def test_up_reports_container_up(runner_client):
    client, _fake, tmp, api = runner_client
    calls = []
    monkeypatch_report = lambda action, params: calls.append((action, params))  # noqa: E731
    api.report_event = monkeypatch_report
    try:
        r = client.post("/apps/s/up", headers={"X-Runner-Token": "rrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrr"}, json={"spec": _spec(tmp), "config_json": {}})
        assert r.status_code == 200, r.text
        assert calls == [("data_app.container_up", {"slug": "s"})]
    finally:
        del api.report_event


def test_up_still_returns_when_report_event_raises(runner_client):
    """A container action must never fail because its audit report did."""
    client, _fake, tmp, api = runner_client

    def _raise(action, params):
        raise RuntimeError("control plane unreachable")

    api.report_event = _raise
    try:
        r = client.post("/apps/s/up", headers={"X-Runner-Token": "rrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrr"}, json={"spec": _spec(tmp), "config_json": {}})
        assert r.status_code == 200, r.text
    finally:
        del api.report_event


def test_stop_reports_container_stop_with_mode(runner_client):
    client, fake, tmp, api = runner_client
    client.post("/apps/s/up", headers={"X-Runner-Token": "rrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrr"}, json={"spec": _spec(tmp), "config_json": {}})

    calls = []
    api.report_event = lambda action, params: calls.append((action, params))
    try:
        r = client.post("/apps/s/stop", headers={"X-Runner-Token": "rrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrr"}, json={"mode": "recreate"})
        assert r.status_code == 200, r.text
        assert calls == [("data_app.container_stop", {"slug": "s", "mode": "removed"})]
    finally:
        del api.report_event


def test_stop_absent_does_not_report(runner_client):
    """Nothing changed — no lifecycle event to report."""
    client, _fake, _tmp, api = runner_client
    calls = []
    api.report_event = lambda action, params: calls.append((action, params))
    try:
        r = client.post("/apps/never-existed/stop", headers={"X-Runner-Token": "rrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrr"}, json={"mode": "recreate"})
        assert r.status_code == 200, r.text
        assert calls == []
    finally:
        del api.report_event


def test_resume_reports_container_resume(runner_client):
    client, fake, tmp, api = runner_client
    client.post("/apps/s/up", headers={"X-Runner-Token": "rrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrr"}, json={"spec": _spec(tmp), "config_json": {}})

    calls = []
    api.report_event = lambda action, params: calls.append((action, params))
    try:
        r = client.post("/apps/s/resume", headers={"X-Runner-Token": "rrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrr"})
        assert r.status_code == 200, r.text
        assert calls == [("data_app.container_resume", {"slug": "s"})]
    finally:
        del api.report_event


# ---------------------------------------------------------------------------
# 1c. app/api/data_apps.py::record_runner_event — the receiving endpoint
# ---------------------------------------------------------------------------


@pytest.fixture
def runner_events_client(tmp_path, monkeypatch, shared_app):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("APPS_RUNNER_TOKEN", "rrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrr")
    return TestClient(shared_app)


def test_runner_event_accepted_writes_system_row(runner_events_client):
    r = runner_events_client.post(
        "/api/data-apps/runner-events",
        json={"action": "data_app.container_up", "params": {"slug": "gap-s1"}},
        headers={"X-Runner-Token": "rrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrr"},
    )
    assert r.status_code == 204, r.text

    rows, _ = audit_repo().query(action="data_app.container_up", limit=20)
    matches = [row for row in rows if row["resource"] == "data_app:gap-s1"]
    assert matches, rows
    assert matches[0]["client_kind"] == "system"
    assert _params(matches[0]) == {"slug": "gap-s1"}


def test_runner_event_rejects_unknown_action(runner_events_client):
    r = runner_events_client.post(
        "/api/data-apps/runner-events",
        json={"action": "data_app.delete_everything", "params": {}},
        headers={"X-Runner-Token": "rrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrr"},
    )
    assert r.status_code == 400


def test_runner_event_rejects_bad_token(runner_events_client):
    r = runner_events_client.post(
        "/api/data-apps/runner-events",
        json={"action": "data_app.container_up", "params": {"slug": "gap-s2"}},
        headers={"X-Runner-Token": "wrong"},
    )
    assert r.status_code == 401


def test_runner_event_rejects_missing_token(runner_events_client):
    r = runner_events_client.post(
        "/api/data-apps/runner-events",
        json={"action": "data_app.container_up", "params": {"slug": "gap-s3"}},
    )
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# 2. app/data_apps_subdomain.py — windowed data_app.access dedup
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_subdomain_seen():
    import app.data_apps_subdomain as subdomain_mod

    subdomain_mod._seen.clear()
    yield
    subdomain_mod._seen.clear()


@pytest.fixture
def subdomain_access(tmp_path, monkeypatch):
    import app.data_apps_subdomain as subdomain_mod
    import app.instance_config as ic

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(ic, "get_data_apps_config", lambda: {"subdomain_base": "apps.example.com"})
    return subdomain_mod


def _cookie_scope(token: str, path: str = "/dash"):
    return {
        "type": "http",
        "path": path,
        "headers": [
            (b"host", b"s.apps.example.com"),
            (b"cookie", f"access_token={token}".encode()),
        ],
    }


async def _run(subdomain_mod, scope):
    async def inner_app(scope, receive, send):
        pass

    middleware = subdomain_mod.DataAppSubdomainMiddleware(inner_app)
    await middleware(scope, None, None)


def test_two_requests_same_user_yield_one_access_row(subdomain_access):
    from app.auth.jwt import create_access_token

    token = create_access_token("gap-user-1", "gap1@example.com")
    asyncio.run(_run(subdomain_access, _cookie_scope(token, "/a.js")))
    asyncio.run(_run(subdomain_access, _cookie_scope(token, "/b.css")))

    rows, _ = audit_repo().query(action="data_app.access", limit=20)
    matches = [r for r in rows if r["user_id"] == "gap-user-1" and r["resource"] == "data_app:s"]
    assert len(matches) == 1, matches
    assert _params(matches[0]) == {"window_minutes": 15}


def test_second_user_gets_a_second_row(subdomain_access):
    from app.auth.jwt import create_access_token

    token1 = create_access_token("gap-user-2", "gap2@example.com")
    token2 = create_access_token("gap-user-3", "gap3@example.com")
    asyncio.run(_run(subdomain_access, _cookie_scope(token1)))
    asyncio.run(_run(subdomain_access, _cookie_scope(token2)))

    rows, _ = audit_repo().query(action="data_app.access", limit=20)
    users = {r["user_id"] for r in rows if r["resource"] == "data_app:s"}
    assert {"gap-user-2", "gap-user-3"} <= users


def test_repeat_outside_window_is_audited_again(subdomain_access):
    from app.auth.jwt import create_access_token

    token = create_access_token("gap-user-4", "gap4@example.com")
    asyncio.run(_run(subdomain_access, _cookie_scope(token)))
    # Simulate the window having elapsed by rewinding the recorded timestamp.
    key = ("gap-user-4", "s")
    subdomain_access._seen[key] = time.monotonic() - subdomain_access._SEEN_TTL_S - 1
    asyncio.run(_run(subdomain_access, _cookie_scope(token)))

    rows, _ = audit_repo().query(action="data_app.access", limit=20)
    matches = [r for r in rows if r["user_id"] == "gap-user-4" and r["resource"] == "data_app:s"]
    assert len(matches) == 2, matches


def test_no_cookie_is_not_audited(subdomain_access):
    scope = {
        "type": "http",
        "path": "/dash",
        "headers": [(b"host", b"s.apps.example.com")],
    }
    asyncio.run(_run(subdomain_access, scope))

    rows, _ = audit_repo().query(action="data_app.access", limit=20)
    assert rows == []


# ---------------------------------------------------------------------------
# 3. app/api/notifications_ws.py — connect + rejected
# ---------------------------------------------------------------------------

WS_SECRET = "test-secret-notifications-ws-gap"


def _ws_token(payload: dict, secret: str = WS_SECRET) -> str:
    return jwt.encode(payload, secret, algorithm="HS256")


@pytest.fixture
def ws_client(tmp_path, monkeypatch):
    import app.api.notifications_ws as ws_mod
    from app.coordination.factory import reset_coordination_for_tests
    from app.roles import reset_roles_cache

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DESKTOP_JWT_SECRET", WS_SECRET)
    monkeypatch.delenv("AGNES_ROLE", raising=False)
    reset_roles_cache()
    reset_coordination_for_tests()
    ws_mod._connections.clear()

    app = FastAPI()
    app.include_router(ws_mod.router)
    yield TestClient(app)

    ws_mod._connections.clear()
    reset_roles_cache()
    reset_coordination_for_tests()


def test_successful_connect_is_audited(ws_client):
    with ws_client.websocket_connect("/api/notifications/ws") as ws:
        ws.send_json({"type": "auth", "token": _ws_token({"sub": "gap-ws-user", "exp": int(time.time()) + 3600})})
        frame = ws.receive_json()
        assert frame == {"type": "auth_ok", "username": "gap-ws-user"}

    rows, _ = audit_repo().query(action="notifications.ws_connect", limit=20)
    matches = [r for r in rows if r["user_id"] == "gap-ws-user"]
    assert matches, rows
    assert matches[0]["result"] == "success"


def test_invalid_token_is_audited_as_rejected(ws_client):
    with ws_client.websocket_connect("/api/notifications/ws") as ws:
        ws.send_json({"type": "auth", "token": "not-a-real-token"})
        frame = ws.receive_json()
        assert frame == {"type": "auth_error", "message": "Invalid token"}

    rows, _ = audit_repo().query(action="notifications.ws_rejected", limit=20)
    matches = [r for r in rows if _params(r).get("reason") == "invalid_token"]
    assert matches, rows
    assert matches[0]["result"] == "denied"


def test_too_many_connections_is_audited_with_the_known_user(ws_client, monkeypatch):
    import app.api.notifications_ws as ws_mod

    monkeypatch.setattr(ws_mod, "MAX_CONNECTIONS_PER_USER", 0)
    with ws_client.websocket_connect("/api/notifications/ws") as ws:
        ws.send_json({"type": "auth", "token": _ws_token({"sub": "gap-ws-capped", "exp": int(time.time()) + 3600})})
        frame = ws.receive_json()
        assert frame == {"type": "auth_error", "message": "Too many connections"}

    rows, _ = audit_repo().query(action="notifications.ws_rejected", limit=20)
    matches = [r for r in rows if r["user_id"] == "gap-ws-capped"]
    assert matches, rows
    assert _params(matches[0]) == {"reason": "too_many_connections"}


# ---------------------------------------------------------------------------
# Shared-secret hardening (RBAC review finding, 2026-08-29)
# ---------------------------------------------------------------------------


def test_runner_token_below_length_floor_is_refused(monkeypatch, seeded_app):
    """A too-short secret means "auth disabled", not "weak auth".

    The route sits on the public /api/data-apps router, so an operator typo
    that left a 3-character token in place must not leave a guessable door
    open — it must close the door entirely.
    """
    monkeypatch.setenv("APPS_RUNNER_TOKEN", "short")
    r = seeded_app["client"].post(
        "/api/data-apps/runner-events",
        json={"action": "data_app.container_up", "params": {"slug": "demo"}},
        headers={"X-Runner-Token": "short"},
    )
    assert r.status_code == 401


def test_runner_event_params_size_is_capped(monkeypatch, seeded_app):
    """An oversized params dict is refused rather than written verbatim."""
    monkeypatch.setenv("APPS_RUNNER_TOKEN", "r" * 40)
    r = seeded_app["client"].post(
        "/api/data-apps/runner-events",
        json={
            "action": "data_app.container_up",
            "params": {"slug": "demo", "junk": "x" * 4000},
        },
        headers={"X-Runner-Token": "r" * 40},
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "params_too_large"


def test_runner_token_check_is_constant_time():
    """The comparison must go through hmac.compare_digest.

    A plain `!=` leaks the token a byte at a time under timing analysis, and
    this endpoint is internet-reachable. Asserted structurally because a
    timing assertion would be inherently flaky.
    """
    import inspect

    from app.api.data_apps import _check_runner_token

    src = inspect.getsource(_check_runner_token)
    assert "compare_digest" in src, "shared-secret compare must be constant-time"
    assert "!= expected" not in src
