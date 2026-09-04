"""``POST /api/broker/otlp/v1/{signal}`` — the broker half of the embedded
engine's telemetry egress.

The sandbox's OTLP exporters send their batches to the in-sandbox relay's
``otlp`` scope, which forwards them here with a ``kai_otlp`` ticket and no
other credential. The route must (a) accept exactly that scope, (b) forward
the batch byte-for-byte to the collector the instance's own export points
at, with the operator's headers injected server-side, (c) refuse anything
that is not one of the three OTLP signals, and (d) never echo the
collector's error text back across the isolation boundary.

Uses ``asyncio.run`` like ``tests/test_broker_routes.py``.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from src.repositories import ticket_repo

ENDPOINT = "https://collector.example/otlp/proj/source"


class _FakeCollectorClient:
    """``httpx.AsyncClient`` stand-in for the broker's outbound leg (built with
    ``timeout=`` and no transport); the harness's own transport-backed client
    is delegated to the real class."""

    status_code = 200
    body = b"\x0a\x00"
    response_headers: dict = {"content-type": "application/x-protobuf"}
    raise_exc: Exception | None = None
    captured: dict = {}
    _real_cls = httpx.AsyncClient

    def __init__(self, *a, **k):
        self._real = self._real_cls(*a, **k) if "transport" in k else None

    async def __aenter__(self):
        return await self._real.__aenter__() if self._real else self

    async def __aexit__(self, *a):
        return await self._real.__aexit__(*a) if self._real else False

    async def post(self, url, *, content=None, headers=None, **k):
        cls = type(self)
        if cls.raise_exc is not None:
            raise cls.raise_exc
        cls.captured = {"url": url, "content": content, "headers": dict(headers or {})}

        class _R:
            status_code = cls.status_code
            headers = dict(cls.response_headers)
            content = cls.body

        return _R()

    def __getattr__(self, name):
        return getattr(self._real, name)


@pytest.fixture
def otlp_broker(e2e_env, shared_app, monkeypatch):
    import app.api.broker as broker_mod

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", ENDPOINT + "/")  # trailing slash must not double up
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "Authorization=Bearer%20s3cr3t, x-team=agnes,")
    monkeypatch.setattr(broker_mod.httpx, "AsyncClient", _FakeCollectorClient)
    _FakeCollectorClient.status_code = 200
    _FakeCollectorClient.body = b"\x0a\x00"
    _FakeCollectorClient.response_headers = {"content-type": "application/x-protobuf"}
    _FakeCollectorClient.raise_exc = None
    _FakeCollectorClient.captured = {}
    return shared_app


def _post(app, tok, signal, body=b"\x0a\x03abc", headers=None):
    async def _run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await c.post(
                f"/api/broker/otlp/v1/{signal}",
                headers={"Authorization": f"Bearer {tok}", "content-type": "application/x-protobuf", **(headers or {})},
                content=body,
            )

    return asyncio.run(_run())


def test_forwards_a_batch_with_the_operator_credential_injected(otlp_broker):
    tok = ticket_repo().mint("chat_otlp", "kai_otlp", ttl_seconds=60)
    r = _post(
        otlp_broker, tok, "traces", body=b"\x0a\x05hello", headers={"content-encoding": "gzip", "x-api-key": "dummy"}
    )
    assert r.status_code == 200, r.text
    assert r.content == b"\x0a\x00"  # the collector's OTLP success response, as-is
    sent = _FakeCollectorClient.captured
    assert sent["url"] == ENDPOINT + "/v1/traces"
    assert sent["content"] == b"\x0a\x05hello"
    lowered = {k.lower(): v for k, v in sent["headers"].items()}
    assert lowered["authorization"] == "Bearer s3cr3t"  # percent-decoded, injected server-side
    assert lowered["x-team"] == "agnes"
    assert lowered["content-type"] == "application/x-protobuf"
    assert lowered["content-encoding"] == "gzip"
    assert "x-api-key" not in lowered  # the sandbox's dummy credential never reaches the collector


@pytest.mark.parametrize("signal", ["metrics", "logs"])
def test_all_three_signals_route_to_their_own_path(otlp_broker, signal):
    tok = ticket_repo().mint("chat_otlp_sig", "kai_otlp", ttl_seconds=60)
    assert _post(otlp_broker, tok, signal).status_code == 200
    assert _FakeCollectorClient.captured["url"] == f"{ENDPOINT}/v1/{signal}"


def test_unknown_signal_is_refused_before_the_collector(otlp_broker):
    tok = ticket_repo().mint("chat_otlp_bad", "kai_otlp", ttl_seconds=60)
    r = _post(otlp_broker, tok, "profiles")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "otlp_signal_not_supported"
    assert _FakeCollectorClient.captured == {}


@pytest.mark.parametrize("scope", ["main", "llm", "kai_mcp"])
def test_other_scopes_are_refused_with_the_scope_mismatch_audit(otlp_broker, scope):
    tok = ticket_repo().mint("chat_otlp_scope", scope, ttl_seconds=60)
    r = _post(otlp_broker, tok, "traces")
    assert r.status_code == 401
    assert r.json()["detail"] == "ticket_scope_mismatch"
    assert _FakeCollectorClient.captured == {}


def test_without_a_configured_export_the_route_says_so(otlp_broker, monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    tok = ticket_repo().mint("chat_otlp_off", "kai_otlp", ttl_seconds=60)
    r = _post(otlp_broker, tok, "traces")
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "otlp_export_not_configured"


def test_oversized_batch_is_refused(otlp_broker):
    import app.api.broker as broker_mod

    tok = ticket_repo().mint("chat_otlp_big", "kai_otlp", ttl_seconds=60)
    r = _post(otlp_broker, tok, "traces", body=b"x" * (broker_mod._OTLP_MAX_BODY_BYTES + 1))
    assert r.status_code == 413
    assert _FakeCollectorClient.captured == {}


def test_collector_errors_come_back_as_status_only(otlp_broker):
    _FakeCollectorClient.status_code = 503
    _FakeCollectorClient.body = b'{"error":"tenant quota exceeded for project 12345"}'
    _FakeCollectorClient.response_headers = {"content-type": "application/json", "retry-after": "7"}
    tok = ticket_repo().mint("chat_otlp_err", "kai_otlp", ttl_seconds=60)
    r = _post(otlp_broker, tok, "traces")
    assert r.status_code == 503
    assert r.content == b""  # never the collector's text
    assert r.headers.get("retry-after") == "7"  # the exporter's retry honours it


def test_unreachable_collector_is_a_typed_502(otlp_broker):
    _FakeCollectorClient.raise_exc = httpx.ConnectError("dns")
    tok = ticket_repo().mint("chat_otlp_down", "kai_otlp", ttl_seconds=60)
    r = _post(otlp_broker, tok, "traces")
    assert r.status_code == 502
    assert r.json()["detail"]["code"] == "otlp_collector_unreachable"


def test_headers_parser_handles_the_baggage_form():
    from app.api.broker import _parse_otlp_headers

    assert _parse_otlp_headers("Authorization=Bearer%20abc,x=1, ,=junk,y = 2 ") == {
        "Authorization": "Bearer abc",
        "x": "1",
        "y": "2",
    }
    assert _parse_otlp_headers("") == {}
