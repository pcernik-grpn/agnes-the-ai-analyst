"""Every brokered completion becomes one ``llm_calls`` row and one span that
carry the same ids and the same price (spec 3.1) — for agent-less sessions
too, and with ``agnes.user_email`` gone from the span (spec 3.6)."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from src.repositories import ticket_repo
from tests.test_otel_export import _FakeUpstream, _post, _sse, otel_broker, otel_exporter  # noqa: F401


@pytest.fixture
def ledger(monkeypatch):
    import src.repositories as repos
    from app.api.broker_agent_policy import usage_accumulator

    rows: list[dict] = []

    class _Repo:
        def insert_batch(self, batch):
            rows.extend(dict(r) for r in batch)
            return len(batch)

    # Only the ledger's own two seams: the accumulator's PG gate and the
    # repo the flush reaches for (Postgres-only, and it lands with the
    # migration). Patching `src.repositories.use_pg` itself would flip EVERY
    # factory to Postgres and the test would die resolving `ticket_repo()`.
    monkeypatch.setattr(repos, "llm_calls_repo", lambda: _Repo(), raising=False)
    monkeypatch.setattr("app.api.broker_agent_policy.use_pg", lambda: True)
    usage_accumulator.flush()
    yield rows
    usage_accumulator.flush()
    rows.clear()


def _json_body():
    return json.dumps(
        {
            "id": "msg_1",
            "model": "claude-sonnet-5-20260101",
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "answer"}],
            "usage": {
                "input_tokens": 1000,
                "output_tokens": 100,
                "cache_read_input_tokens": 5000,
                "cache_creation_input_tokens": 200,
            },
        }
    ).encode()


def test_buffered_completion_writes_a_priced_row_matching_the_span(otel_broker, otel_exporter, ledger):  # noqa: F811
    from app.api.broker_agent_policy import usage_accumulator
    from src.llm_pricing import cost_usd

    _FakeUpstream.body = _json_body()
    tok = ticket_repo().mint("chat_ledger_json", "main", ttl_seconds=60)
    r = _post(
        otel_broker,
        tok,
        "/api/broker/anthropic/v1/messages",
        {"model": "claude-sonnet-5", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 200, r.text
    usage_accumulator.flush()

    (span,) = otel_exporter.get_finished_spans()
    attrs = dict(span.attributes)
    (row,) = ledger
    expected = round(
        cost_usd(
            model="claude-sonnet-5-20260101",
            input_tokens=1000,
            output_tokens=100,
            cache_read_tokens=5000,
            cache_creation_tokens=200,
        ),
        6,
    )
    assert row["cost_usd"] == expected and attrs["agnes.cost_usd"] == expected
    assert row["trace_id"] == format(span.context.trace_id, "032x")
    assert row["span_id"] == format(span.context.span_id, "016x")
    assert row["kind"] == "completion" and row["session_id"] == "chat_ledger_json"
    assert row["workload"] == "chat" and row["purpose"] == "completion"
    assert row["model_requested"] == "claude-sonnet-5" and row["model_response"] == "claude-sonnet-5-20260101"
    assert row["status"] == "ok" and row["http_status"] == 200 and row["stop_reason"] == "end_turn"
    assert row["priced_as"]["price_key"] == "claude-sonnet-5"
    assert row["agent_id"] is None  # agent-less session: recorded all the same
    assert "agnes.user_email" not in attrs
    assert attrs["agnes.workload"] == "chat" and attrs["agnes.purpose"] == "completion"


def test_streamed_completion_writes_a_row_with_stream_completeness(otel_broker, otel_exporter, ledger):  # noqa: F811
    from app.api.broker_agent_policy import usage_accumulator

    _FakeUpstream.content_type = "text/event-stream"
    _FakeUpstream.sse_chunks = _sse(
        [
            (
                "message_start",
                {
                    "type": "message_start",
                    "message": {"model": "claude-stream", "usage": {"input_tokens": 20, "output_tokens": 1}},
                },
            ),
            (
                "content_block_start",
                {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            ),
            (
                "content_block_delta",
                {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hello"}},
            ),
            (
                "message_delta",
                {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 4}},
            ),
        ]
    )
    tok = ticket_repo().mint("chat_ledger_sse", "main", ttl_seconds=60)
    r = _post(
        otel_broker,
        tok,
        "/api/broker/anthropic/v1/messages",
        {"model": "claude-stream", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 200
    usage_accumulator.flush()
    (row,) = ledger
    assert row["stream_complete"] is True and row["stop_reason"] == "end_turn"
    assert row["input_tokens"] == 20 and row["output_tokens"] == 4
    assert row["latency_ms"] is not None and row["latency_ms"] >= 0


def test_interrupted_stream_is_not_an_ok_row(otel_broker, otel_exporter, ledger):  # noqa: F811
    """A stream that stops before its `message_delta` still returns HTTP 200,
    so a status taken from the status code alone would file a half-delivered
    answer under `ok` and flatter every summary built on this ledger. It gets
    its own `incomplete` status instead (review finding)."""
    from app.api.broker_agent_policy import usage_accumulator

    _FakeUpstream.content_type = "text/event-stream"
    _FakeUpstream.sse_chunks = _sse(
        [
            (
                "message_start",
                {
                    "type": "message_start",
                    "message": {"model": "claude-stream", "usage": {"input_tokens": 20, "output_tokens": 1}},
                },
            ),
            (
                "content_block_delta",
                {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hel"}},
            ),
        ]
    )
    tok = ticket_repo().mint("chat_ledger_cut", "main", ttl_seconds=60)
    r = _post(
        otel_broker,
        tok,
        "/api/broker/anthropic/v1/messages",
        {"model": "claude-stream", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 200
    usage_accumulator.flush()
    (row,) = ledger
    assert row["stream_complete"] is False
    assert row["status"] == "incomplete"
    assert row["error_type"] == "stream_incomplete"
    assert row["http_status"] == 200


def test_a_complete_stream_and_a_buffered_reply_both_stay_ok(otel_broker, otel_exporter, ledger):  # noqa: F811
    """The counterpart to the test above: `incomplete` must not leak onto a
    stream that finished, nor onto a non-streaming call (which never sets
    `stream_complete` at all)."""
    from app.api.broker_agent_policy import usage_accumulator

    _FakeUpstream.content_type = "application/json"
    _FakeUpstream.sse_chunks = None
    tok = ticket_repo().mint("chat_ledger_ok", "main", ttl_seconds=60)
    r = _post(
        otel_broker,
        tok,
        "/api/broker/anthropic/v1/messages",
        {"model": "claude-buffered", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 200
    usage_accumulator.flush()
    (row,) = ledger
    assert row["status"] == "ok"
    assert row["error_type"] is None
    assert row["stream_complete"] is None


def test_upstream_error_is_an_error_row(otel_broker, otel_exporter, ledger):  # noqa: F811
    from app.api.broker_agent_policy import usage_accumulator

    _FakeUpstream.status_code = 529
    _FakeUpstream.body = b'{"error": {"type": "overloaded_error", "message": "busy"}}'
    tok = ticket_repo().mint("chat_ledger_err", "main", ttl_seconds=60)
    r = _post(
        otel_broker,
        tok,
        "/api/broker/anthropic/v1/messages",
        {"model": "claude-sonnet-5", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 529
    usage_accumulator.flush()
    (row,) = ledger
    assert row["status"] == "error" and row["http_status"] == 529 and row["error_type"] == "529"
    assert row["cost_usd"] == 0.0


def test_unreachable_upstream_is_an_error_row(otel_broker, otel_exporter, ledger, monkeypatch):  # noqa: F811
    """A completion the provider never answered at all (connect refused, DNS,
    connect timeout) is a call too — recorded as a zero-cost error row, and
    its span is finished rather than abandoned mid-flight."""
    import app.api.broker as broker_mod
    from app.api.broker_agent_policy import usage_accumulator

    class _Unreachable(_FakeUpstream):
        async def send(self, req, stream=False):
            raise httpx.ConnectError("upstream down")

    monkeypatch.setattr(broker_mod.httpx, "AsyncClient", _Unreachable)
    tok = ticket_repo().mint("chat_ledger_unreachable", "main", ttl_seconds=60)
    r = _post(
        otel_broker,
        tok,
        "/api/broker/anthropic/v1/messages",
        {"model": "claude-sonnet-5", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 503
    usage_accumulator.flush()
    (row,) = ledger
    assert row["status"] == "error" and row["error_type"] == "ConnectError"
    assert row["http_status"] is None and row["cost_usd"] == 0.0

    (span,) = otel_exporter.get_finished_spans()
    assert span.status.status_code.name == "ERROR"
    assert row["trace_id"] == format(span.context.trace_id, "032x")


def test_ledger_rows_are_written_even_when_export_is_off(e2e_env, shared_app, monkeypatch, ledger):
    import app.api.broker as broker_mod
    from app.api.broker_agent_policy import usage_accumulator
    from src.observability import otel

    otel.shutdown_otel()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr(broker_mod.httpx, "AsyncClient", _FakeUpstream)
    _FakeUpstream.status_code = 200
    _FakeUpstream.content_type = "application/json"
    _FakeUpstream.body = _json_body()
    _FakeUpstream.sse_chunks = []
    tok = ticket_repo().mint("chat_ledger_noexport", "main", ttl_seconds=60)
    r = _post(
        shared_app,
        tok,
        "/api/broker/anthropic/v1/messages",
        {"model": "claude-sonnet-5", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 200
    usage_accumulator.flush()
    (row,) = ledger
    assert row["trace_id"] is None and row["span_id"] is None and row["cost_usd"] > 0


def _oversized_stream_events():
    """One ``message_start`` (all four token kinds), enough
    ``content_block_delta`` filler to exceed the broker's 8 MiB full-body
    mirror, and a closing ``message_delta`` with a stop reason and more
    output tokens."""
    events = [
        (
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "model": "claude-stream-big",
                    "usage": {
                        "input_tokens": 500,
                        "output_tokens": 1,
                        "cache_read_input_tokens": 300,
                        "cache_creation_input_tokens": 50,
                    },
                },
            },
        ),
    ]
    chunk_text = "x" * 500_000
    for _ in range(20):  # ~10 MB of content_block_delta payload
        events.append(
            (
                "content_block_delta",
                {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": chunk_text}},
            )
        )
    events.append(
        (
            "message_delta",
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 4321}},
        )
    )
    return events


def test_oversized_streamed_completion_recovers_usage_from_edges(otel_broker, otel_exporter, ledger):  # noqa: F811
    """A stream whose body blows past the 8 MiB full mirror still yields a
    priced, complete ``llm_calls`` row and span — from the bounded head/tail
    edges (spec 3.1), not the (now-overflowed) full mirror."""
    from app.api.broker_agent_policy import usage_accumulator

    _FakeUpstream.content_type = "text/event-stream"
    _FakeUpstream.sse_chunks = _sse(_oversized_stream_events())
    tok = ticket_repo().mint("chat_ledger_oversized", "main", ttl_seconds=60)
    r = _post(
        otel_broker,
        tok,
        "/api/broker/anthropic/v1/messages",
        {"model": "claude-stream-big", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 200
    usage_accumulator.flush()

    (span,) = otel_exporter.get_finished_spans()
    attrs = dict(span.attributes)
    (row,) = ledger

    assert attrs["gen_ai.usage.input_tokens"] == 500
    assert attrs["gen_ai.usage.output_tokens"] == 4321
    assert attrs["gen_ai.usage.cache_read_input_tokens"] == 300
    assert attrs["gen_ai.usage.cache_creation_input_tokens"] == 50
    assert tuple(attrs["gen_ai.response.finish_reasons"]) == ("end_turn",)
    assert attrs["agnes.response_truncated"] is True
    assert attrs["agnes.cost_usd"] > 0

    assert row["input_tokens"] == 500
    assert row["output_tokens"] == 4321
    assert row["cache_read_tokens"] == 300
    assert row["cache_creation_tokens"] == 50
    assert row["stop_reason"] == "end_turn"
    assert row["stream_complete"] is True
    assert row["response_truncated"] is True
    assert row["cost_usd"] > 0


def test_streamed_completion_under_cap_is_not_flagged_truncated(otel_broker, otel_exporter, ledger):  # noqa: F811
    """An ordinary (non-overflowing) stream is byte-for-byte unchanged by the
    edge buffers: no truncation flag, on the row or the span."""
    from app.api.broker_agent_policy import usage_accumulator

    _FakeUpstream.content_type = "text/event-stream"
    _FakeUpstream.sse_chunks = _sse(
        [
            (
                "message_start",
                {
                    "type": "message_start",
                    "message": {"model": "claude-stream", "usage": {"input_tokens": 20, "output_tokens": 1}},
                },
            ),
            (
                "content_block_delta",
                {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hello"}},
            ),
            (
                "message_delta",
                {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 4}},
            ),
        ]
    )
    tok = ticket_repo().mint("chat_ledger_under_cap", "main", ttl_seconds=60)
    r = _post(
        otel_broker,
        tok,
        "/api/broker/anthropic/v1/messages",
        {"model": "claude-stream", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 200
    usage_accumulator.flush()

    (span,) = otel_exporter.get_finished_spans()
    attrs = dict(span.attributes)
    (row,) = ledger

    assert row["input_tokens"] == 20 and row["output_tokens"] == 4
    assert row["stop_reason"] == "end_turn" and row["stream_complete"] is True
    assert row["response_truncated"] is False
    assert "agnes.response_truncated" not in attrs


def test_tail_recovers_usage_when_head_misses_message_start(otel_broker, otel_exporter, ledger):  # noqa: F811
    """The pathological case: the head buffer fills before ``message_start``
    ever arrives (a huge non-usage event first). The tail's own
    ``message_delta`` still yields output tokens, model and stop reason —
    only the input/cache tokens (which never left ``message_start``) are
    lost."""
    from app.api.broker_agent_policy import usage_accumulator

    padding_text = "p" * (70 * 1024)  # > the 64 KiB head cap
    events = [
        ("ping", {"type": "ping", "padding": padding_text}),
        (
            "message_start",
            {
                "type": "message_start",
                "message": {"model": "claude-stream-tail", "usage": {"input_tokens": 999, "output_tokens": 1}},
            },
        ),
    ]
    chunk_text = "x" * 500_000
    for _ in range(20):  # push the body past the 8 MiB overflow cap
        events.append(
            (
                "content_block_delta",
                {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": chunk_text}},
            )
        )
    events.append(
        (
            "message_delta",
            {
                "type": "message_delta",
                "model": "claude-stream-tail-final",
                "delta": {"stop_reason": "max_tokens"},
                "usage": {"output_tokens": 777},
            },
        )
    )

    _FakeUpstream.content_type = "text/event-stream"
    _FakeUpstream.sse_chunks = _sse(events)
    tok = ticket_repo().mint("chat_ledger_tail_only", "main", ttl_seconds=60)
    r = _post(
        otel_broker,
        tok,
        "/api/broker/anthropic/v1/messages",
        {"model": "claude-stream-tail", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 200
    usage_accumulator.flush()
    (row,) = ledger

    # message_start's input usage never reached the head buffer — lost, not
    # crashed — but the tail's message_delta still yields output tokens,
    # model and stop reason rather than an empty result.
    assert row["input_tokens"] == 0
    assert row["output_tokens"] == 777
    assert row["model_response"] == "claude-stream-tail-final"
    assert row["stop_reason"] == "max_tokens"
    assert row["response_truncated"] is True


class _ContentNegotiatingUpstream(_FakeUpstream):
    """A provider that honours ``accept-encoding``, the way the real one does.

    An encoding this client cannot decode reaches the broker as opaque bytes
    — httpx leaves a body alone when no decoder in its table matches the
    response's ``content-encoding``. That is exactly what a forwarded ``br``
    did to every non-streaming completion.
    """

    json_body = b"{}"
    # The head of a real Brotli frame, captured off the wire from the
    # provider: whatever it decodes to, it is not JSON.
    opaque_body = b"\x83\xea\x00\x00\x80\xaa\xaa\xaa\xea\xfft\xf5\xe0\xb3:\x80"

    async def send(self, req, stream=False):
        cls = type(self)
        asked = {
            token.strip().lower()
            for key, value in (req["headers"] or {}).items()
            if key.lower() == "accept-encoding"
            for token in value.split(",")
        }
        body = cls.opaque_body if "br" in asked else cls.json_body

        # Not `_FakeUpstream`'s own response: that one eagerly does
        # `body.decode()`, so a compressed body would blow up inside the FAKE
        # and never reach the code under test. A real httpx response hands
        # opaque bytes over without complaint, and so must this one.
        class _R:
            status_code = 200
            headers = {"content-type": "application/json"}
            content = body
            text = body.decode("utf-8", "replace")

            def json(self):
                return json.loads(body)

            async def aread(self):
                return body

            async def aclose(self):
                return None

        return _R()


def test_non_streaming_completion_is_priced_though_the_sandbox_asked_for_brotli(
    otel_broker,  # noqa: F811
    ledger,
    monkeypatch,
):
    """A turn's non-streaming completions must reach the ledger with their
    real tokens, whatever encoding the in-sandbox SDK happened to ask for.

    The SDK sends ``accept-encoding: br, gzip, deflate``. Forwarded verbatim,
    the provider answered in Brotli, httpx (gzip/deflate only) handed
    ``parse_usage`` the compressed bytes, and the row said a billed call cost
    nothing — eight such rows per chat turn, next to the two streamed ones
    that parsed fine because SSE comes back uncompressed.
    """
    import app.api.broker as broker_mod
    from app.api.broker_agent_policy import usage_accumulator

    _ContentNegotiatingUpstream.json_body = _json_body()
    monkeypatch.setattr(broker_mod.httpx, "AsyncClient", _ContentNegotiatingUpstream)
    tok = ticket_repo().mint("chat_ledger_br", "main", ttl_seconds=60)

    async def _run():
        transport = httpx.ASGITransport(app=otel_broker)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await c.post(
                "/api/broker/anthropic/v1/messages",
                headers={"Authorization": f"Bearer {tok}", "accept-encoding": "br, gzip, deflate"},
                json={"model": "claude-sonnet-5", "messages": [{"role": "user", "content": "hi"}]},
            )

    r = asyncio.run(_run())
    assert r.status_code == 200
    usage_accumulator.flush()

    (row,) = ledger
    assert (row["input_tokens"], row["output_tokens"]) == (1000, 100)
    assert row["cache_read_tokens"] == 5000 and row["cache_creation_tokens"] == 200
    assert row["cost_usd"] > 0
    assert row["model_response"] == "claude-sonnet-5-20260101" and row["stop_reason"] == "end_turn"
    # ...and the sandbox got a body it can read, not the provider's compressed
    # one under a bare `application/json` (this forward drops the response's
    # `content-encoding`, so an undecoded body would be unreadable there too).
    assert json.loads(r.content)["usage"]["input_tokens"] == 1000


def test_the_negotiating_upstream_really_would_hide_usage_behind_brotli():
    """Guard for the test above: prove the fake is not vacuously cooperative.

    If a forwarded ``br`` did NOT produce an unparseable body, the regression
    test would pass with or without the fix.
    """
    from app.api.broker_agent_policy import parse_usage

    assert parse_usage(_json_body(), "application/json") is not None
    assert parse_usage(_ContentNegotiatingUpstream.opaque_body, "application/json") is None
