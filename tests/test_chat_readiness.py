"""Tests for cloud-chat readiness — secret presence + live key probes + the
admin endpoints that surface/set them.

The readiness module never returns secret *values* — only presence — and the
live probes classify auth failures distinctly from connectivity errors.
"""

from __future__ import annotations

from types import SimpleNamespace

import duckdb
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth.access import require_admin
from app.auth.dependencies import _get_db, get_current_user
from app.chat import readiness
from src.db import _ensure_schema

TEST_ADMIN = {"id": "admin1", "email": "admin@test.com", "is_admin": True}


def _cfg(**kw):
    base = dict(enabled=True, provider="e2b", e2b_template_id="agnes-chat")
    base.update(kw)
    return SimpleNamespace(**base)


# ---------------------------------------------------------------------------
# secret_status — presence only, required vs set
# ---------------------------------------------------------------------------


def test_secret_status_disabled_config_is_never_ready():
    s = readiness.secret_status(None)
    assert s["enabled"] is False
    assert s["ready"] is False
    # Nothing is "required" when chat is disabled.
    assert all(not v["required"] for v in s["secrets"].values())


def test_secret_status_ready_when_all_present(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-xxx")
    monkeypatch.setenv("E2B_API_KEY", "e2b_xxx")
    monkeypatch.setenv("JWT_SECRET_KEY", "x" * 40)
    s = readiness.secret_status(_cfg())
    assert s["ready"] is True
    assert s["missing"] == []
    assert s["secrets"]["e2b_api_key"]["set"] is True
    # No secret value is echoed back anywhere in the payload.
    assert "sk-ant-xxx" not in str(s)
    assert "e2b_xxx" not in str(s)


def test_secret_status_flags_missing_required(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("E2B_API_KEY", raising=False)
    monkeypatch.setenv("JWT_SECRET_KEY", "x" * 40)
    s = readiness.secret_status(_cfg())
    assert s["ready"] is False
    assert "anthropic_api_key" in s["missing"]
    assert "e2b_api_key" in s["missing"]


def test_secret_status_weak_jwt_is_not_set(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "short")  # < 32 bytes
    s = readiness.secret_status(_cfg())
    assert s["secrets"]["jwt_secret_key"]["set"] is False
    assert "jwt_secret_key" in s["missing"]


def test_secret_status_e2b_not_required_for_other_provider(monkeypatch):
    monkeypatch.delenv("E2B_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk")
    monkeypatch.setenv("JWT_SECRET_KEY", "x" * 40)
    s = readiness.secret_status(_cfg(provider="local"))
    assert s["secrets"]["e2b_api_key"]["required"] is False
    assert "e2b_api_key" not in s["missing"]


def _docker_cfg(**kw):
    base = dict(enabled=True, provider="docker", docker_image="agnes-chat-sandbox:latest")
    base.update(kw)
    return SimpleNamespace(**base)


def test_secret_status_docker_rows_required_only_for_the_docker_provider(monkeypatch):
    """A docker deployment needs the sidecar token and an image, and needs no
    E2B account at all — the readiness surface must say exactly that."""
    monkeypatch.delenv("E2B_API_KEY", raising=False)
    monkeypatch.delenv("APPS_RUNNER_TOKEN", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk")
    monkeypatch.setenv("JWT_SECRET_KEY", "x" * 40)

    s = readiness.secret_status(_docker_cfg())
    assert s["secrets"]["e2b_api_key"]["required"] is False
    assert s["secrets"]["apps_runner_token"]["required"] is True
    assert s["secrets"]["apps_runner_token"]["set"] is False
    assert s["secrets"]["chat_docker_image"]["required"] is True
    assert s["secrets"]["chat_docker_image"]["set"] is True
    assert "apps_runner_token" in s["missing"]
    assert s["ready"] is False

    e2b = readiness.secret_status(_cfg())
    assert e2b["secrets"]["apps_runner_token"]["required"] is False
    assert e2b["secrets"]["chat_docker_image"]["required"] is False


def test_secret_status_docker_ready_when_token_and_image_present(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk")
    monkeypatch.setenv("JWT_SECRET_KEY", "x" * 40)
    monkeypatch.setenv("APPS_RUNNER_TOKEN", "runner-token")
    s = readiness.secret_status(_docker_cfg())
    assert s["missing"] == []
    assert s["ready"] is True
    assert "runner-token" not in str(s)


def test_secret_status_docker_flags_a_missing_image(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk")
    monkeypatch.setenv("JWT_SECRET_KEY", "x" * 40)
    monkeypatch.setenv("APPS_RUNNER_TOKEN", "runner-token")
    s = readiness.secret_status(_docker_cfg(docker_image=""))
    assert "chat_docker_image" in s["missing"]


# ---------------------------------------------------------------------------
# Live probes — docker sandbox runner
# ---------------------------------------------------------------------------


def test_test_docker_sandbox_ok(monkeypatch):
    import asyncio

    class _OkClient:
        def __init__(self, *a, **kw):
            pass

        async def probe(self, image=""):
            assert image == "agnes-chat-sandbox:1"
            return {"ok": True, "daemon": True, "image": True, "detail": "docker sandbox runner ready"}

    monkeypatch.setattr("app.chat.sandbox_runner_client.SandboxRunnerClient", _OkClient)
    r = asyncio.run(readiness.test_docker_sandbox("agnes-chat-sandbox:1"))
    assert r == {"ok": True, "detail": "docker sandbox runner ready"}


def test_test_docker_sandbox_reports_a_missing_image(monkeypatch):
    import asyncio

    class _MissingImage:
        def __init__(self, *a, **kw):
            pass

        async def probe(self, image=""):
            return {"ok": False, "daemon": True, "image": False, "detail": f"sandbox image {image} not present"}

    monkeypatch.setattr("app.chat.sandbox_runner_client.SandboxRunnerClient", _MissingImage)
    r = asyncio.run(readiness.test_docker_sandbox("agnes-chat-sandbox:9"))
    assert r["ok"] is False
    assert "agnes-chat-sandbox:9" in r["detail"]


def test_test_docker_sandbox_classifies_an_unreachable_sidecar(monkeypatch):
    """A sidecar that isn't running must read as a clear operator message, not
    an exception out of the admin endpoint."""
    import asyncio

    class _Boom:
        def __init__(self, *a, **kw):
            pass

        async def probe(self, image=""):
            from app.chat.sandbox_runner_client import SandboxRunnerUnavailable

            raise SandboxRunnerUnavailable("connection refused")

    monkeypatch.setattr("app.chat.sandbox_runner_client.SandboxRunnerClient", _Boom)
    r = asyncio.run(readiness.test_docker_sandbox("img:1"))
    assert r["ok"] is False
    assert "apps-runner" in r["detail"]
    assert "connection refused" in r["detail"]


# ---------------------------------------------------------------------------
# Live probes — E2B
# ---------------------------------------------------------------------------


def test_test_e2b_key_missing(monkeypatch):
    monkeypatch.delenv("E2B_API_KEY", raising=False)
    import asyncio

    r = asyncio.run(readiness.test_e2b_key())
    assert r["ok"] is False
    assert "not set" in r["detail"]


# On the modern e2b SDK ``AsyncSandbox.list`` is a *synchronous* factory that
# returns an ``AsyncSandboxPaginator``; the authenticated round trip happens
# when the first page is awaited via ``next_items()``. These fakes mirror that
# contract exactly — a coroutine-returning ``list`` (the previous fake) hid the
# real ``TypeError: object AsyncSandboxPaginator can't be used in 'await'``.


class _FakePaginator:
    def __init__(self, *, items=None, error=None):
        self._items = items or []
        self._error = error

    async def next_items(self, *a, **k):
        if self._error is not None:
            raise self._error
        return self._items


def test_test_e2b_key_valid(monkeypatch):
    import asyncio

    import e2b

    def _fake_list(*a, **k):  # sync factory, NOT a coroutine
        return _FakePaginator(items=[])

    monkeypatch.setattr(e2b.AsyncSandbox, "list", staticmethod(_fake_list))
    r = asyncio.run(readiness.test_e2b_key(api_key="e2b_good"))
    assert r["ok"] is True


def test_test_e2b_key_auth_failure_classified(monkeypatch):
    import asyncio

    import e2b

    class _AuthErr(Exception):
        status_code = 401

    def _fake_list(*a, **k):  # error surfaces from the awaited first page
        return _FakePaginator(error=_AuthErr("unauthorized"))

    monkeypatch.setattr(e2b.AsyncSandbox, "list", staticmethod(_fake_list))
    r = asyncio.run(readiness.test_e2b_key(api_key="e2b_bad"))
    assert r["ok"] is False
    assert "authentication failed" in r["detail"]
    # Guard: _FakePaginator is deliberately NOT awaitable, so if the impl ever
    # reverts to ``await AsyncSandbox.list(...)`` the valid-key test above fails
    # with the exact production TypeError instead of passing silently.


# ---------------------------------------------------------------------------
# Live probes — Anthropic
# ---------------------------------------------------------------------------


def test_test_anthropic_key_missing(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    import asyncio

    r = asyncio.run(readiness.test_anthropic_key())
    assert r["ok"] is False
    assert "not set" in r["detail"]


def test_test_anthropic_key_valid(monkeypatch):
    import asyncio

    import anthropic

    class _Msgs:
        def create(self, **kw):
            return SimpleNamespace(content=[])

    class _FakeClient:
        def __init__(self, **kw):
            self.messages = _Msgs()

    monkeypatch.setattr(anthropic, "Anthropic", _FakeClient)
    r = asyncio.run(readiness.test_anthropic_key(api_key="sk-good"))
    assert r["ok"] is True


def test_test_anthropic_key_auth_failure_classified(monkeypatch):
    import asyncio

    import anthropic

    class _AuthErr(Exception):
        status_code = 401

    class _Msgs:
        def create(self, **kw):
            raise _AuthErr("invalid x-api-key")

    class _FakeClient:
        def __init__(self, **kw):
            self.messages = _Msgs()

    monkeypatch.setattr(anthropic, "Anthropic", _FakeClient)
    r = asyncio.run(readiness.test_anthropic_key(api_key="sk-bad"))
    assert r["ok"] is False
    assert "authentication failed" in r["detail"]


# ---------------------------------------------------------------------------
# classify_llm_failure — shared auth/credit/provider classifier (#884)
# ---------------------------------------------------------------------------


def test_classify_llm_failure_auth():
    d = readiness.classify_llm_failure(401, "invalid x-api-key")
    assert d["reason"] == readiness.LLM_REASON_AUTH
    assert "authentication failed" in d["detail"]


def test_classify_llm_failure_credit_wins_over_status():
    # A 400 whose body is the credit-balance error classifies as credit, not
    # a generic provider error — even though 400 is not an auth status.
    d = readiness.classify_llm_failure(400, "Your credit balance is too low to access the API.")
    assert d["reason"] == readiness.LLM_REASON_CREDIT
    assert "credit balance too low" in d["detail"]


def test_classify_llm_failure_provider():
    d = readiness.classify_llm_failure(529, "overloaded_error")
    assert d["reason"] == readiness.LLM_REASON_PROVIDER
    assert "overloaded_error" in d["detail"]


def test_classify_credit_exception_via__classify():
    class _CreditErr(Exception):
        status_code = 400

    detail = readiness._classify(_CreditErr("Your credit balance is too low"))
    assert "credit balance too low" in detail


def test_runtime_diagnostic_record_and_clear():
    state = SimpleNamespace()
    assert readiness.get_llm_runtime_diagnostic(state) is None
    diag = readiness.record_llm_runtime_failure(state, 401, "invalid x-api-key")
    assert diag["reason"] == readiness.LLM_REASON_AUTH
    assert diag["status_code"] == 401 and diag["at"]
    assert readiness.get_llm_runtime_diagnostic(state)["reason"] == readiness.LLM_REASON_AUTH
    readiness.clear_llm_runtime_diagnostic(state)
    assert readiness.get_llm_runtime_diagnostic(state) is None


# ---------------------------------------------------------------------------
# Admin endpoints
# ---------------------------------------------------------------------------


def _make_app(*, chat_enabled: bool = True) -> tuple[TestClient, duckdb.DuckDBPyConnection]:
    from app.api.admin_chat import router as admin_chat_router

    app = FastAPI()
    app.include_router(admin_chat_router)
    app.state.chat_config = SimpleNamespace(
        enabled=chat_enabled,
        provider="e2b",
        e2b_template_id="agnes-chat",
    )
    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    app.dependency_overrides[require_admin] = lambda: TEST_ADMIN
    app.dependency_overrides[_get_db] = lambda: conn
    return TestClient(app), conn


def test_readiness_endpoint_returns_presence(monkeypatch):
    monkeypatch.delenv("E2B_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk")
    monkeypatch.setenv("JWT_SECRET_KEY", "x" * 40)
    client, _ = _make_app()
    r = client.get("/admin/chat/readiness")
    assert r.status_code == 200
    body = r.json()
    assert body["secrets"]["e2b_api_key"]["set"] is False
    assert body["secrets"]["anthropic_api_key"]["set"] is True
    assert "e2b_api_key" in body["missing"]


def test_readiness_endpoint_surfaces_llm_runtime(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk")
    monkeypatch.setenv("E2B_API_KEY", "e2b")
    monkeypatch.setenv("JWT_SECRET_KEY", "x" * 40)
    client, _ = _make_app()
    # Healthy → null.
    assert client.get("/admin/chat/readiness").json()["llm_runtime"] is None
    # Broker records a runtime failure on app.state → endpoint surfaces it.
    readiness.record_llm_runtime_failure(client.app.state, 401, "invalid x-api-key")
    body = client.get("/admin/chat/readiness").json()
    assert body["llm_runtime"]["reason"] == readiness.LLM_REASON_AUTH
    assert "authentication failed" in body["llm_runtime"]["detail"]


def test_set_secrets_persists_only_provided(monkeypatch):
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "app.secrets.persist_overlay_token",
        lambda name, value: calls.append((name, value)),
    )
    client, _ = _make_app()
    r = client.post("/admin/chat/secrets", json={"e2b_api_key": "e2b_new"})
    assert r.status_code == 200
    body = r.json()
    assert body["changed"] == ["e2b_api_key"]
    assert body["restart_required"] is True
    # Only the provided key was persisted; the omitted one was untouched.
    assert calls == [("E2B_API_KEY", "e2b_new")]


def test_set_secrets_rejects_empty_payload():
    client, _ = _make_app()
    r = client.post("/admin/chat/secrets", json={})
    assert r.status_code == 422


def test_set_secrets_audits_without_value(monkeypatch, e2e_env):
    # The endpoint writes its audit row through the backend-aware
    # ``audit_repo()`` factory, which resolves to ``get_system_db()`` on
    # DuckDB — not the isolated ``conn`` this fixture wires up for the
    # request's own ``_get_db`` override. Read the row back from the same
    # system DB (``e2e_env`` gives it a fresh, test-scoped DATA_DIR).
    from src.db import get_system_db

    monkeypatch.setattr("app.secrets.persist_overlay_token", lambda name, value: None)
    client, _conn = _make_app()
    r = client.post("/admin/chat/secrets", json={"anthropic_api_key": "sk-secret-value"})
    assert r.status_code == 200
    sys_conn = get_system_db()
    row = sys_conn.execute("SELECT action, params FROM audit_log WHERE action = 'chat.secrets.update'").fetchone()
    sys_conn.close()
    assert row is not None
    # The secret value must never land in the audit row.
    assert "sk-secret-value" not in (row[1] or "")
    assert "anthropic_api_key" in (row[1] or "")


def test_secrets_endpoints_require_admin(monkeypatch):
    """Without the require_admin override, a non-admin is refused."""
    from app.api.admin_chat import router as admin_chat_router

    app = FastAPI()
    app.include_router(admin_chat_router)
    app.state.chat_config = SimpleNamespace(enabled=True, provider="e2b", e2b_template_id="t")
    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    # get_current_user returns a non-admin; require_admin runs for real and 403s.
    app.dependency_overrides[get_current_user] = lambda: {"id": "u1", "email": "u@test.com"}
    app.dependency_overrides[_get_db] = lambda: conn
    client = TestClient(app)
    assert client.get("/admin/chat/readiness").status_code == 403
    assert client.post("/admin/chat/secrets", json={"e2b_api_key": "x"}).status_code == 403
    assert client.post("/admin/chat/secrets/test").status_code == 403


# --- kai-agent: the two cost caps the engine cannot feed ---


def _kai_cfg(**over):
    from types import SimpleNamespace

    base = dict(
        enabled=True,
        provider="kai-agent",
        kai_agent_url="http://kai-agent:3000",
        e2b_template_id=None,
        docker_image=None,
        daily_anthropic_spend_usd=20.0,
        max_session_tokens=200_000,
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_readiness_names_the_caps_the_engine_cannot_meter(monkeypatch):
    """`daily_anthropic_spend_usd` and `max_session_tokens` are enforced off
    `chat_messages.tokens_in/out`, which only a usage-carrying frame writes.
    The engine's SSE stream carries none, so on this provider both are inert —
    and both ship LIVE defaults, so flipping one YAML key silently removes two
    budgets instance-wide. An operator should not learn that from a bill.
    """
    from app.chat.readiness import secret_status

    monkeypatch.setenv("KAI_HOST_JWT_SECRET", "s")
    out = secret_status(_kai_cfg())
    assert set(out["unmetered_caps"]) == {"daily_anthropic_spend_usd", "max_session_tokens"}


def test_a_cap_explicitly_disabled_is_not_reported_as_unmetered(monkeypatch):
    """Only a cap the operator actually set is worth warning about — one
    already turned off is not a surprise waiting to happen."""
    from app.chat.readiness import secret_status

    monkeypatch.setenv("KAI_HOST_JWT_SECRET", "s")
    out = secret_status(_kai_cfg(daily_anthropic_spend_usd=0, max_session_tokens=0))
    assert out["unmetered_caps"] == []


def test_other_providers_meter_normally(monkeypatch):
    """Non-vacuity: the native runner writes usage, so its caps are live and
    must not be reported as inert."""
    from types import SimpleNamespace

    from app.chat.readiness import secret_status

    cfg = SimpleNamespace(
        enabled=True,
        provider="e2b",
        e2b_template_id="t",
        docker_image=None,
        daily_anthropic_spend_usd=20.0,
        max_session_tokens=200_000,
    )
    assert secret_status(cfg)["unmetered_caps"] == []
