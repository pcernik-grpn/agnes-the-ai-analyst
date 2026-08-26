"""Deployment-gate tests for the cloud-chat feature.

Two gates verified here:
1. UVICORN_WORKERS > 1 with the default ``memory`` coordination backend →
   chat_manager is None after lifespan runs (unchanged S-tier posture).
   As of wave-2F task 7, this is no longer an unconditional refusal — see
   tests/test_chat_gate_lift.py for the redis-backend case, where
   UVICORN_WORKERS > 1 (multi-worker/replica) is now ALLOWED because
   tickets/leases/replay/inbound/notifications are all coordination-backed.
2. chat_manager absent  → POST /api/chat/sessions returns 503 with
   kind == "chat_disabled".

The multi-worker test uses a minimal app whose lifespan replicates only
the UVICORN_WORKERS branch of app/main.py's CHAT-INIT block — avoiding
the full app.main lifespan (DuckDB, BQ config, PostHog, …) while still
exercising the exact code path under test, via
``app.main._chat_coordination_backend`` so a monkeypatch of that one
function drives the mirrored branch exactly like the real lifespan would.

The 503 test reuses the api_client_chat_disabled / logged_in_user
fixtures defined in test_chat_api.py.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.main as main_mod
from tests.test_chat_api import api_client_chat_disabled, logged_in_user  # noqa: F401

# ---------------------------------------------------------------------------
# Helpers for test_multi_worker_disables_chat
# ---------------------------------------------------------------------------


def _make_app_with_worker_gate() -> FastAPI:
    """Minimal app whose lifespan runs only the multi-worker / chat-init gate."""

    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        # Mirror the exact CHAT-INIT branch from app/main.py
        from app.chat.config import ChatConfig

        app.state.chat_config = ChatConfig(enabled=True)  # chat enabled

        if app.state.chat_config.enabled:
            if int(os.environ.get("UVICORN_WORKERS", "1")) > 1 and main_mod._chat_coordination_backend() != "redis":
                app.state.chat_manager = None
            else:
                # Normally we'd create a real ChatManager here; in tests
                # the non-multi-worker path is not exercised by this file.
                app.state.chat_manager = object()  # sentinel: "something"

        yield  # server runs
        # teardown — nothing to clean up in this minimal app

    return FastAPI(lifespan=_lifespan)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_multi_worker_disables_chat(monkeypatch):
    """UVICORN_WORKERS=2 with the default memory backend → chat_manager is
    None after lifespan startup (needs backend=memory to hold as of
    wave-2F task 7 — see test_chat_gate_lift.py for the redis case)."""
    monkeypatch.setenv("UVICORN_WORKERS", "2")
    monkeypatch.setattr(main_mod, "_chat_coordination_backend", lambda: "memory")

    app = _make_app_with_worker_gate()

    # TestClient's context manager (__enter__) fires the lifespan startup
    # automatically in this version of Starlette (no lifespan= kwarg needed).
    with TestClient(app):
        assert getattr(app.state, "chat_manager", None) is None


def test_single_worker_enables_chat(monkeypatch):
    """UVICORN_WORKERS=1 (default) → chat_manager is set after lifespan startup."""
    monkeypatch.setenv("UVICORN_WORKERS", "1")

    app = _make_app_with_worker_gate()

    with TestClient(app):
        assert getattr(app.state, "chat_manager", None) is not None


def test_disabled_returns_503(api_client_chat_disabled, logged_in_user):  # noqa: F811
    """When chat_manager is absent, POST /api/chat/sessions returns 503."""
    r = api_client_chat_disabled.post("/api/chat/sessions", json={"surface": "web"})
    assert r.status_code == 503
    assert r.json()["detail"]["kind"] == "chat_disabled"


# ---------------------------------------------------------------------------
# Task D.1 — production JWT secret check
# ---------------------------------------------------------------------------


def test_chat_refuses_without_jwt_secret(monkeypatch):
    """chat.enabled=true with no JWT_SECRET_KEY → helper refuses.

    Without this gate, the chat path would silently mint JWTs against the
    fallback ``test-jwt-secret-key-minimum-32-chars!!`` constant — a
    production deployment would think it's authenticated and the secret
    would be public.
    """
    from app.chat.config import ChatConfig
    from app.main import _chat_jwt_secret_ok

    monkeypatch.delenv("JWT_SECRET_KEY", raising=False)
    monkeypatch.delenv("TESTING", raising=False)
    cfg = ChatConfig(enabled=True)
    assert _chat_jwt_secret_ok(cfg) is False


def test_chat_refuses_short_jwt_secret(monkeypatch):
    """JWT_SECRET_KEY < 32 chars → refused as too weak."""
    from app.chat.config import ChatConfig
    from app.main import _chat_jwt_secret_ok

    monkeypatch.setenv("JWT_SECRET_KEY", "too-short")
    monkeypatch.delenv("TESTING", raising=False)
    cfg = ChatConfig(enabled=True)
    assert _chat_jwt_secret_ok(cfg) is False


def test_chat_accepts_long_jwt_secret(monkeypatch):
    """A 32+-byte JWT_SECRET_KEY is accepted (no fatal)."""
    from app.chat.config import ChatConfig
    from app.main import _chat_jwt_secret_ok

    monkeypatch.setenv(
        "JWT_SECRET_KEY",
        "this-is-a-32-char-or-more-secret-key!!",
    )
    monkeypatch.delenv("TESTING", raising=False)
    assert _chat_jwt_secret_ok(ChatConfig(enabled=True)) is True


def test_chat_skips_jwt_check_when_disabled(monkeypatch):
    """chat.enabled=false → the helper returns True regardless of env."""
    from app.chat.config import ChatConfig
    from app.main import _chat_jwt_secret_ok

    monkeypatch.delenv("JWT_SECRET_KEY", raising=False)
    assert _chat_jwt_secret_ok(ChatConfig(enabled=False)) is True


# ---------------------------------------------------------------------------
# ANTHROPIC_API_KEY presence gate
# ---------------------------------------------------------------------------


def test_chat_refused_without_anthropic_key(monkeypatch):
    """chat.enabled=true with no ANTHROPIC_API_KEY → helper refuses."""
    from app.chat.config import ChatConfig
    from app.main import _chat_anthropic_key_ok

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("TESTING", raising=False)
    cfg = ChatConfig(enabled=True)
    assert _chat_anthropic_key_ok(cfg) is False


def test_chat_accepts_anthropic_key(monkeypatch):
    """chat.enabled=true with ANTHROPIC_API_KEY set → accepted."""
    from app.chat.config import ChatConfig
    from app.main import _chat_anthropic_key_ok

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key-value")
    monkeypatch.delenv("TESTING", raising=False)
    cfg = ChatConfig(enabled=True)
    assert _chat_anthropic_key_ok(cfg) is True


def test_chat_anthropic_key_skipped_when_disabled(monkeypatch):
    """chat.enabled=false → key check is bypassed."""
    from app.chat.config import ChatConfig
    from app.main import _chat_anthropic_key_ok

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert _chat_anthropic_key_ok(ChatConfig(enabled=False)) is True


def _set_wif_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_FEDERATION_RULE_ID", "fdrl_x")
    monkeypatch.setenv("ANTHROPIC_ORGANIZATION_ID", "00000000-0000-0000-0000-000000000000")
    monkeypatch.setenv("ANTHROPIC_SERVICE_ACCOUNT_ID", "svac_x")
    monkeypatch.setenv("ANTHROPIC_IDENTITY_TOKEN", "e.y.z")


def test_chat_workload_identity_accepts_without_static_key(monkeypatch):
    """workload_identity mode: NO static key, but the federation env is set → accepted.

    Regression for Devin #885: the gate previously demanded ANTHROPIC_API_KEY
    unconditionally, silently disabling chat in keyless mode. Runs with TESTING
    unset so the real gate logic is exercised.
    """
    from app.chat.config import ChatConfig
    from app.main import _chat_anthropic_key_ok

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("TESTING", raising=False)
    _set_wif_env(monkeypatch)
    assert _chat_anthropic_key_ok(ChatConfig(enabled=True, llm_auth="workload_identity")) is True


def test_chat_workload_identity_refused_when_federation_env_incomplete(monkeypatch):
    """workload_identity mode with the federation env missing → refuse loudly."""
    from app.chat.config import ChatConfig
    from app.main import _chat_anthropic_key_ok

    for var in (
        "ANTHROPIC_API_KEY",
        "TESTING",
        "ANTHROPIC_FEDERATION_RULE_ID",
        "ANTHROPIC_ORGANIZATION_ID",
        "ANTHROPIC_SERVICE_ACCOUNT_ID",
        "ANTHROPIC_IDENTITY_TOKEN",
        "ANTHROPIC_IDENTITY_TOKEN_FILE",
    ):
        monkeypatch.delenv(var, raising=False)
    assert _chat_anthropic_key_ok(ChatConfig(enabled=True, llm_auth="workload_identity")) is False


def test_secret_status_anthropic_not_required_in_wif_mode():
    """readiness.secret_status must not flag anthropic_api_key as required in WIF mode."""
    from app.chat.config import ChatConfig
    from app.chat.readiness import secret_status

    api_mode = secret_status(ChatConfig(enabled=True, llm_auth="api_key"))
    wif_mode = secret_status(ChatConfig(enabled=True, llm_auth="workload_identity"))
    assert api_mode["secrets"]["anthropic_api_key"]["required"] is True
    assert wif_mode["secrets"]["anthropic_api_key"]["required"] is False


# ---------------------------------------------------------------------------
# Removed-provider refusal (the e2b provider was removed in 0.89.0)
# ---------------------------------------------------------------------------


def test_stale_e2b_provider_names_the_removal_in_the_boot_refusal():
    """The loud-refusal contract for stale ``provider: e2b`` configs: the
    allowlist branch in app/main.py must special-case the removed provider
    with an actionable message (what was removed, what to set, where). This
    pins the message so a refactor cannot silently downgrade it to the
    generic unknown-provider line."""
    from pathlib import Path

    body = Path("app/main.py").read_text()
    assert 'not in ("docker", "kai-agent")' in body
    assert "chat.provider=e2b is no longer supported" in body
    assert "AGNES_CHAT_PROVIDER" in body


# ---------------------------------------------------------------------------
# Docker-provider gates (self-hosted sandbox)
# ---------------------------------------------------------------------------


def _docker_cfg(**over):
    from app.chat.config import ChatConfig

    kwargs = {"enabled": True, "provider": "docker"}
    kwargs.update(over)
    return ChatConfig(**kwargs)


def test_docker_gates_bypassed_for_kai_agent_provider(monkeypatch):
    """A kai-agent deployment must not be refused for lacking a sidecar or a
    non-loopback rails URL — the docker gates fire only on provider=docker."""
    import asyncio

    from app.chat.config import ChatConfig
    from app.main import _chat_docker_rails_url_ok, _chat_docker_sandbox_ok

    monkeypatch.delenv("SERVER_URL", raising=False)
    monkeypatch.delenv("AGNES_INTERNAL_URL", raising=False)
    monkeypatch.delenv("TESTING", raising=False)
    cfg = ChatConfig(enabled=True, provider="kai-agent")
    assert _chat_docker_rails_url_ok(cfg) is True
    assert asyncio.run(_chat_docker_sandbox_ok(cfg)) is True


def test_docker_gates_skipped_when_chat_disabled(monkeypatch):
    import asyncio

    from app.main import _chat_docker_rails_url_ok, _chat_docker_sandbox_ok

    monkeypatch.delenv("TESTING", raising=False)
    cfg = _docker_cfg(enabled=False)
    assert _chat_docker_rails_url_ok(cfg) is True
    assert asyncio.run(_chat_docker_sandbox_ok(cfg)) is True


def test_docker_refuses_without_a_rails_url(monkeypatch):
    """No SERVER_URL / AGNES_INTERNAL_URL → agnes_server_url() would fall back
    to loopback, which inside the sandbox's netns is the sandbox itself. Fail
    fast instead of shipping a dead AGNES_SERVER."""
    from app.main import _chat_docker_rails_url_ok

    monkeypatch.delenv("SERVER_URL", raising=False)
    monkeypatch.delenv("AGNES_INTERNAL_URL", raising=False)
    monkeypatch.delenv("TESTING", raising=False)
    assert _chat_docker_rails_url_ok(_docker_cfg()) is False


@pytest.mark.parametrize("url", ["http://127.0.0.1:8000", "http://localhost:8000", "http://[::1]:8000"])
def test_docker_refuses_a_loopback_rails_url(monkeypatch, url):
    from app.main import _chat_docker_rails_url_ok

    monkeypatch.setenv("SERVER_URL", url)
    monkeypatch.delenv("AGNES_INTERNAL_URL", raising=False)
    monkeypatch.delenv("TESTING", raising=False)
    assert _chat_docker_rails_url_ok(_docker_cfg()) is False


def test_docker_accepts_an_internal_url(monkeypatch):
    """The compose-network pattern data apps already use."""
    from app.main import _chat_docker_rails_url_ok

    monkeypatch.delenv("SERVER_URL", raising=False)
    monkeypatch.setenv("AGNES_INTERNAL_URL", "http://app:8000")
    monkeypatch.delenv("TESTING", raising=False)
    assert _chat_docker_rails_url_ok(_docker_cfg()) is True


def test_docker_refuses_when_the_sidecar_probe_fails(monkeypatch):
    """Unreachable sidecar / missing image → chat disabled with an actionable
    log line, never a crash."""
    import asyncio

    import app.main as main_mod

    monkeypatch.delenv("TESTING", raising=False)

    class _FailingClient:
        def __init__(self, *a, **kw):
            pass

        async def probe(self, image=""):
            return {"ok": False, "detail": f"sandbox image {image} not present on the Docker host"}

    monkeypatch.setattr("app.chat.sandbox_runner_client.SandboxRunnerClient", _FailingClient)
    assert asyncio.run(main_mod._chat_docker_sandbox_ok(_docker_cfg())) is False


def test_docker_accepts_a_healthy_sidecar(monkeypatch):
    import asyncio

    import app.main as main_mod

    monkeypatch.delenv("TESTING", raising=False)
    seen = {}

    class _OkClient:
        def __init__(self, *a, **kw):
            pass

        async def probe(self, image=""):
            seen["image"] = image
            return {"ok": True, "detail": "docker sandbox runner ready"}

    monkeypatch.setattr("app.chat.sandbox_runner_client.SandboxRunnerClient", _OkClient)
    assert asyncio.run(main_mod._chat_docker_sandbox_ok(_docker_cfg(docker_image="agnes-chat-sandbox:1"))) is True
    assert seen["image"] == "agnes-chat-sandbox:1"


def test_docker_sandbox_probe_survives_an_unreachable_sidecar(monkeypatch):
    """A transport error is a refusal, not an exception out of the lifespan."""
    import asyncio

    import app.main as main_mod

    monkeypatch.delenv("TESTING", raising=False)

    class _BoomClient:
        def __init__(self, *a, **kw):
            pass

        async def probe(self, image=""):
            from app.chat.sandbox_runner_client import SandboxRunnerUnavailable

            raise SandboxRunnerUnavailable("connection refused")

    monkeypatch.setattr("app.chat.sandbox_runner_client.SandboxRunnerClient", _BoomClient)
    assert asyncio.run(main_mod._chat_docker_sandbox_ok(_docker_cfg())) is False
