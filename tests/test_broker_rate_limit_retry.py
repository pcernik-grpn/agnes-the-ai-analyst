"""Upstream rate-limit handling in the chat broker's LLM forward.

A provider 429 (a Vertex per-minute token/request quota is the usual one)
used to reach the in-sandbox agent on the first attempt and surface in the
conversation as "Something went wrong: runner_exception" — while the
``Retry-After`` header the provider sent, telling the caller exactly how long
to wait, was dropped by ``_to_response``.

Three behaviours are pinned here:
  * a 429 is retried a bounded number of times before the caller sees it,
  * ``Retry-After`` / ``anthropic-ratelimit-*`` survive the forward,
  * a 429 that outlives the retries raises an operator health signal.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.api import broker as broker_mod
from src.repositories import ticket_repo
from tests.test_broker_routes import (  # noqa: F401 — broker_agent_session is a fixture
    _shim_response,
    _StreamShimMixin,
    broker_agent_session,
)


@pytest.fixture
def broker_app(e2e_env, shared_app):
    return shared_app


@pytest.fixture
def fast_retry(monkeypatch):
    """The delay itself is asserted by the ``_retry_after_seconds`` tests
    below; the loop tests must not actually wait. Deliberately NOT autouse —
    it would neuter those helper tests too."""
    monkeypatch.setattr(broker_mod, "_retry_after_seconds", lambda resp, attempt: 0.0)


class _FakeResp:
    def __init__(self, status_code: int, headers: dict | None = None, content: bytes = b"{}"):
        self.status_code = status_code
        self.headers = {"content-type": "application/json", **(headers or {})}
        self.content = content


def _scripted_client(script: list[_FakeResp], attempts: list[str]):
    """An httpx.AsyncClient stand-in that returns ``script`` in order for the
    broker's outbound leg and delegates the test harness's own
    transport-backed client to the real class."""
    real_cls = httpx.AsyncClient

    class _Client(_StreamShimMixin):
        def __init__(self, *a, **k):
            self._real = real_cls(*a, **k) if "transport" in k else None

        async def __aenter__(self):
            return await self._real.__aenter__() if self._real else self

        async def __aexit__(self, *a):
            return await self._real.__aexit__(*a) if self._real else False

        async def request(self, method, url, *a, **k):
            if self._real:
                return await self._real.request(method, url, *a, **k)
            attempts.append(str(url))
            nxt = script.pop(0) if len(script) > 1 else script[0]
            return _shim_response(nxt)

        def __getattr__(self, name):
            return getattr(self._real, name)

    return _Client


def _call_broker(broker_app) -> httpx.Response:
    tok = ticket_repo().mint("chat_retry", "main", ttl_seconds=60)

    async def _run():
        transport = httpx.ASGITransport(app=broker_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await c.post(
                "/api/broker/anthropic/v1/messages",
                headers={"Authorization": f"Bearer {tok}"},
                content=b'{"model":"x"}',
            )

    return asyncio.run(_run())


# ---------------------------------------------------------------------------
# _retry_after_seconds
# ---------------------------------------------------------------------------


def test_retry_after_header_is_honoured():
    resp = _FakeResp(429, {"retry-after": "3"})
    # Jitter is additive and bounded, so the floor is the header value.
    wait = broker_mod._retry_after_seconds(resp, 0)
    assert 3.0 <= wait <= 3.25 + 0.001, wait


def test_retry_after_is_capped_so_a_turn_never_stalls_for_a_minute():
    """A 60s Retry-After is a signal to give up and say so, not to freeze the
    UI. Without the cap an interactive chat turn hangs for the full minute."""
    resp = _FakeResp(429, {"retry-after": "60"})
    wait = broker_mod._retry_after_seconds(resp, 0)
    assert wait <= broker_mod._RETRY_AFTER_CAP_SEC + 0.25 + 0.001, wait


@pytest.mark.parametrize("raw", ["", "soon", "Wed, 21 Oct 2026 07:28:00 GMT", "-5"])
def test_missing_or_unparseable_retry_after_falls_back_to_backoff(raw):
    """Including the HTTP-date form, which is legal per RFC 9110 but not what
    either provider sends — a value we cannot read must not become a 0s
    hot-loop."""
    resp = _FakeResp(429, {"retry-after": raw} if raw else {})
    first = broker_mod._retry_after_seconds(resp, 0)
    second = broker_mod._retry_after_seconds(resp, 1)
    assert first >= broker_mod._RETRY_BASE_DELAY_SEC
    # Exponential: attempt 1 waits strictly longer than attempt 0 even at the
    # unlucky end of the jitter range.
    assert second > first - 0.25


# ---------------------------------------------------------------------------
# _passthrough_response_headers
# ---------------------------------------------------------------------------


def test_ratelimit_headers_are_forwarded():
    resp = _FakeResp(
        429,
        {
            "retry-after": "2",
            "anthropic-ratelimit-input-tokens-remaining": "0",
            "Anthropic-RateLimit-Requests-Reset": "2026-09-03T12:00:00Z",
        },
    )
    out = broker_mod._passthrough_response_headers(resp)
    assert out["retry-after"] == "2"
    assert out["anthropic-ratelimit-input-tokens-remaining"] == "0"
    # Case-insensitive match, original casing preserved.
    assert out["Anthropic-RateLimit-Requests-Reset"] == "2026-09-03T12:00:00Z"


def test_body_framing_headers_are_not_forwarded():
    """An allowlist, not a copy-all: re-sending content-length or
    content-encoding from a response whose body we may have re-read would
    corrupt the forward."""
    resp = _FakeResp(429, {"content-length": "999", "content-encoding": "gzip", "connection": "keep-alive"})
    assert broker_mod._passthrough_response_headers(resp) == {}


# ---------------------------------------------------------------------------
# the retry loop
# ---------------------------------------------------------------------------


def test_transient_upstream_429_is_retried_and_succeeds(broker_app, monkeypatch, fast_retry):
    attempts: list[str] = []
    script = [_FakeResp(429, {"retry-after": "1"}), _FakeResp(200)]
    monkeypatch.setattr(broker_mod.httpx, "AsyncClient", _scripted_client(script, attempts))

    r = _call_broker(broker_app)

    assert r.status_code == 200, r.text
    assert len(attempts) == 2, attempts


def test_sustained_429_is_returned_after_the_retry_budget(broker_app, monkeypatch, fast_retry):
    attempts: list[str] = []
    monkeypatch.setattr(
        broker_mod.httpx,
        "AsyncClient",
        _scripted_client([_FakeResp(429, {"retry-after": "1"})], attempts),
    )

    r = _call_broker(broker_app)

    assert r.status_code == 429
    assert len(attempts) == broker_mod._MAX_UPSTREAM_RETRIES + 1, attempts
    # The caller still learns when to come back — this header is what the
    # in-sandbox SDK's own backoff reads, and dropping it was the original bug.
    assert r.headers.get("retry-after") == "1"


def test_a_non_429_error_is_not_retried(broker_app, monkeypatch, fast_retry):
    """Only 429 means "refused without being processed". Replaying a 400 or a
    500 would double-charge or double-apply the request."""
    attempts: list[str] = []
    monkeypatch.setattr(broker_mod.httpx, "AsyncClient", _scripted_client([_FakeResp(500)], attempts))

    r = _call_broker(broker_app)

    assert r.status_code == 500
    assert len(attempts) == 1, attempts


def test_sustained_rate_limiting_is_an_operator_signal():
    """A 429 that survives the retries is sustained quota exhaustion, not a
    blip. Without it in the diagnostic statuses the only person who finds out
    is whoever's chat happens to be open."""
    assert 429 in broker_mod._LLM_DIAG_STATUSES


def test_agnes_own_budget_refusal_never_reaches_the_retry_loop(broker_app, broker_agent_session, monkeypatch):
    """Agnes's OWN 429 — the per-agent ``budget_exhausted`` refusal — is raised
    before the forward and deliberately carries no Retry-After so SDKs do not
    auto-retry it. Retrying it would be pointless (the budget does not refill
    in 3 seconds) and would triple the latency of every over-budget turn.

    Proven by attempt count, not by reading the source: the outbound client is
    never constructed at all.
    """
    import uuid as _uuid

    from src.repositories import llm_usage_repo

    attempts: list[str] = []
    monkeypatch.setattr(broker_mod.httpx, "AsyncClient", _scripted_client([_FakeResp(429)], attempts))

    ctx = broker_agent_session(model="claude-opus-4-7", token_budget_monthly=10)
    llm_usage_repo().insert_batch(
        [
            {
                "id": str(_uuid.uuid4()),
                "agent_id": ctx["agent_id"],
                "user_id": ctx["user_id"],
                "session_id": ctx["session_id"],
                "model": "claude-opus-4-7",
                "input_tokens": 50,
                "output_tokens": 50,
                "cache_read_tokens": 0,
                "cache_creation_tokens": 0,
            }
        ]
    )

    async def _run():
        transport = httpx.ASGITransport(app=broker_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await c.post(
                "/api/broker/anthropic/v1/messages",
                headers={"Authorization": f"Bearer {ctx['tok']}"},
                json={"model": "claude-opus-4-7", "messages": []},
            )

    r = asyncio.run(_run())

    assert r.status_code == 429
    assert r.json()["detail"]["code"] == "budget_exhausted"
    assert attempts == [], "the budget refusal must not reach the upstream forward"
    assert r.headers.get("retry-after") is None, "a budget exhaustion must never invite an auto-retry"
