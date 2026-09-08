"""Opt-in OTLP trace export (``src/observability/otel.py``).

Drives the real SDK through an in-memory exporter injected into the module
(never the process-global provider, so nothing leaks between tests) and
asserts what a collector would receive: the resource's instance label, one
span per brokered completion with the GenAI attributes, one per
``trace_generation`` call, content only when opted in, and nothing at all
when the endpoint is unset.

Uses ``asyncio.run`` like ``tests/test_broker_routes.py`` — this repo does
not depend on pytest-asyncio.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from src.observability import otel
from src.observability.llm_tracing import trace_generation
from src.repositories import chat_session_repo, ticket_repo


@pytest.fixture
def otel_exporter(monkeypatch):
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    otel.shutdown_otel()
    monkeypatch.delenv(otel.CAPTURE_CONTENT_VAR, raising=False)
    monkeypatch.delenv("OTEL_RESOURCE_ATTRIBUTES", raising=False)
    monkeypatch.delenv("OTEL_SERVICE_NAME", raising=False)
    monkeypatch.setenv("AGNES_DEPLOYMENT_ENV", "agnes-test-dev")
    exporter = InMemorySpanExporter()
    assert otel.configure_otel(role="api", exporter=exporter) is True
    assert otel.is_enabled()
    yield exporter
    otel.shutdown_otel()


# ---------------------------------------------------------------------------
# Lifecycle and resource
# ---------------------------------------------------------------------------


def test_export_off_without_endpoint(monkeypatch):
    otel.shutdown_otel()
    monkeypatch.delenv(otel.ENDPOINT_VAR, raising=False)
    monkeypatch.delenv(otel.TRACES_ENDPOINT_VAR, raising=False)
    assert otel.configure_otel() is False
    assert not otel.is_enabled()
    span = otel.start_completion_span(
        upstream="anthropic", model="m", stream=False, session_id="s", ticket_scope="main"
    )
    assert not span.is_recording()
    otel.end_completion_span(span, status_code=200, request_body=b"{}", response_body=b"{}")  # never raises


def test_resource_carries_the_instance_label(otel_exporter):
    span = otel.start_completion_span(
        upstream="anthropic", model="m", stream=False, session_id="s", ticket_scope="main"
    )
    otel.end_completion_span(span, status_code=200)
    (finished,) = otel_exporter.get_finished_spans()
    res = dict(finished.resource.attributes)
    assert res["service.name"] == "agnes"
    assert res["deployment.environment"] == "agnes-test-dev"
    assert res["agnes.role"] == "api"
    assert ":" in res["service.instance.id"]
    assert res["service.version"]


def test_operator_resource_attributes_win(monkeypatch):
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    otel.shutdown_otel()
    monkeypatch.setenv("AGNES_DEPLOYMENT_ENV", "from-agnes")
    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "deployment.environment=from-operator")
    monkeypatch.setenv("OTEL_SERVICE_NAME", "agnes-custom")
    exporter = InMemorySpanExporter()
    try:
        assert otel.configure_otel(exporter=exporter)
        span = otel.start_completion_span(
            upstream="anthropic", model="m", stream=False, session_id="s", ticket_scope="main"
        )
        otel.end_completion_span(span, status_code=200)
        (finished,) = exporter.get_finished_spans()
        res = dict(finished.resource.attributes)
        assert res["deployment.environment"] == "from-operator"
        assert res["service.name"] == "agnes-custom"
    finally:
        otel.shutdown_otel()


def test_configure_is_idempotent(otel_exporter):
    assert otel.configure_otel() is True  # already on: no second provider


# ---------------------------------------------------------------------------
# trace_generation — server-side calls
# ---------------------------------------------------------------------------


def test_trace_generation_emits_a_span(otel_exporter):
    with trace_generation(provider="anthropic", model="claude-x", distinct_id="u1", purpose="unit") as cap:
        cap.set_input("hello")
        cap.set_tokens(10, 5, cache_read_tokens=7, cache_creation_tokens=1)
        cap.set_output("world!")
    (span,) = otel_exporter.get_finished_spans()
    attrs = dict(span.attributes)
    assert span.name == "chat claude-x"
    assert attrs["gen_ai.system"] == "anthropic"
    assert attrs["gen_ai.request.model"] == "claude-x"
    assert attrs["gen_ai.usage.input_tokens"] == 10
    assert attrs["gen_ai.usage.output_tokens"] == 5
    assert attrs["gen_ai.usage.cache_read_input_tokens"] == 7
    assert attrs["gen_ai.usage.cache_creation_input_tokens"] == 1
    assert attrs["agnes.cost_usd"] > 0  # priced here, not re-derived by a collector
    assert attrs["agnes.purpose"] == "unit"
    assert attrs["agnes.prompt_chars"] == 5
    assert attrs["agnes.completion_chars"] == 6
    assert attrs["agnes.user_id"] == "u1"
    assert attrs["agnes.kind"] == "generation"
    assert "gen_ai.input.messages" not in attrs  # sizes, never text
    assert span.events == ()
    assert span.status.status_code.name == "OK"


def test_trace_generation_marks_a_failure(otel_exporter):
    with pytest.raises(RuntimeError):
        with trace_generation(provider="openai_compat", model="m"):
            raise RuntimeError("boom")
    (span,) = otel_exporter.get_finished_spans()
    assert span.status.status_code.name == "ERROR"
    assert dict(span.attributes)["error.type"] == "RuntimeError"


# ---------------------------------------------------------------------------
# Call context, cost and cache tokens on the spans; ids and remote parents
# ---------------------------------------------------------------------------


def test_a_completion_span_carries_the_call_context_and_never_the_email(otel_exporter):
    from src.observability.llm_context import LlmCallContext

    ctx = LlmCallContext(workload="chat", purpose="completion", turn_id="t1", user_id="u1", agent_id="a1")
    span = otel.start_completion_span(
        upstream="anthropic", model="m", stream=False, session_id="s", ticket_scope="main", context=ctx
    )
    otel.end_completion_span(span, status_code=200, cost_usd=0.125)
    (finished,) = otel_exporter.get_finished_spans()
    attrs = dict(finished.attributes)
    assert attrs["agnes.workload"] == "chat"
    assert attrs["agnes.purpose"] == "completion"
    assert attrs["agnes.turn_id"] == "t1"
    assert attrs["agnes.user_id"] == "u1"
    assert attrs["agnes.agent_id"] == "a1"
    assert attrs["agnes.cost_usd"] == 0.125
    # Identity minimisation: the id joins, the email is personal data with no
    # join value off-instance.
    assert "agnes.user_email" not in attrs


def test_an_explicit_identity_argument_wins_over_the_context(otel_exporter):
    from src.observability.llm_context import LlmCallContext

    span = otel.start_completion_span(
        upstream="anthropic",
        model="m",
        stream=False,
        session_id="explicit",
        ticket_scope="main",
        user_id="explicit-user",
        context=LlmCallContext(session_id="from-context", user_id="from-context"),
    )
    otel.end_completion_span(span, status_code=200)
    (finished,) = otel_exporter.get_finished_spans()
    attrs = dict(finished.attributes)
    assert attrs["agnes.session_id"] == "explicit"
    assert attrs["agnes.user_id"] == "explicit-user"


def test_a_generation_span_carries_cache_tokens_and_cost(otel_exporter):
    from src.observability.llm_context import LlmCallContext

    span = otel.start_generation_span(
        provider="anthropic", model="m", context=LlmCallContext(workload="ocr", purpose="scan_ocr", job_id="j1")
    )
    otel.end_generation_span(
        span,
        input_tokens=10,
        output_tokens=5,
        cache_read_tokens=7,
        cache_creation_tokens=1,
        cost_usd=0.5,
    )
    (finished,) = otel_exporter.get_finished_spans()
    attrs = dict(finished.attributes)
    assert attrs["gen_ai.usage.cache_read_input_tokens"] == 7
    assert attrs["gen_ai.usage.cache_creation_input_tokens"] == 1
    assert attrs["agnes.cost_usd"] == 0.5
    assert attrs["agnes.workload"] == "ocr" and attrs["agnes.job_id"] == "j1"


def test_span_ids_and_remote_parent_context(otel_exporter):
    """The ledger row stores the ids so it joins to the exported span, and a
    span opened in another process can still be the parent."""
    parent = otel.start_completion_span(
        upstream="anthropic", model="m", stream=False, session_id="s", ticket_scope="main"
    )
    trace_id, span_id = otel.span_ids(parent)
    assert trace_id and span_id and len(trace_id) == 32 and len(span_id) == 16
    int(trace_id, 16), int(span_id, 16)  # lowercase hex, parseable

    child = otel.start_completion_span(
        upstream="anthropic",
        model="m",
        stream=False,
        session_id="s",
        ticket_scope="main",
        parent_context=otel.remote_parent_context(trace_id, span_id),
    )
    otel.end_completion_span(child, status_code=200)
    otel.end_completion_span(parent, status_code=200)

    (child_finished,) = [s for s in otel_exporter.get_finished_spans() if s.parent is not None]
    assert child_finished.parent.span_id == int(span_id, 16)
    assert child_finished.context.trace_id == int(trace_id, 16)


def test_span_ids_and_remote_parent_context_degrade_quietly():
    assert otel.span_ids(otel._NoopSpan()) == (None, None)
    assert otel.remote_parent_context("zz", "yy") is None
    assert otel.remote_parent_context(None, None) is None


# ---------------------------------------------------------------------------
# Content helpers
# ---------------------------------------------------------------------------


def test_summarize_sse_reassembles_text_and_tool_use():
    events = [
        ("message_start", {"type": "message_start", "message": {"model": "claude-r", "usage": {"input_tokens": 3}}}),
        (
            "content_block_start",
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        ),
        (
            "content_block_delta",
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hello "}},
        ),
        (
            "content_block_delta",
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "world"}},
        ),
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {"type": "tool_use", "id": "tu_1", "name": "Bash", "input": {}},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "input_json_delta", "partial_json": '{"cmd": '},
            },
        ),
        (
            "content_block_delta",
            {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '"ls"}'}},
        ),
        (
            "message_delta",
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 9}},
        ),
    ]
    body = "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events).encode()
    summary = otel.summarize_completion(body, "text/event-stream")
    assert summary["model"] == "claude-r"
    assert summary["stop_reason"] == "tool_use"
    assert summary["blocks"][0] == {"type": "text", "text": "Hello world"}
    assert summary["blocks"][1]["name"] == "Bash"
    assert summary["blocks"][1]["input"] == {"cmd": "ls"}


def test_summarize_json_completion():
    body = json.dumps(
        {"model": "claude-j", "stop_reason": "end_turn", "content": [{"type": "text", "text": "hi"}]}
    ).encode()
    summary = otel.summarize_completion(body, "application/json")
    assert summary == {"model": "claude-j", "stop_reason": "end_turn", "blocks": [{"type": "text", "text": "hi"}]}
    assert otel.summarize_completion(b"not json", "application/json") == {}


def test_input_messages_from_request_keeps_roles_and_tool_results():
    raw = json.dumps(
        {
            "model": "m",
            "system": "Be brief.",
            "messages": [
                {"role": "user", "content": "run it"},
                {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "id": "tu_1", "name": "Bash", "input": {"cmd": "ls"}}],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "tu_1", "content": [{"type": "text", "text": "a.txt"}]},
                        {"type": "image", "source": {"data": "AAAA"}},
                    ],
                },
            ],
        }
    ).encode()
    messages = otel.input_messages_from_request(raw)
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "user"]
    assert messages[0]["parts"] == [{"type": "text", "content": "Be brief."}]
    assert messages[2]["parts"][0] == {"type": "tool_call", "id": "tu_1", "name": "Bash", "arguments": {"cmd": "ls"}}
    assert messages[3]["parts"][0] == {"type": "tool_call_response", "id": "tu_1", "is_error": False, "result": "a.txt"}
    assert messages[3]["parts"][1] == {"type": "image"}  # bytes never travel
    assert otel.input_messages_from_request(b"garbage") == []


def test_truncate_content_flags_the_cut():
    text, cut = otel.truncate_content("x" * 10, limit=4)
    assert cut and text.startswith("xxxx") and "truncated" in text
    assert otel.truncate_content("short", limit=10) == ("short", False)


# ---------------------------------------------------------------------------
# The chat broker — one span per forwarded completion
# ---------------------------------------------------------------------------


class _FakeUpstream:
    """``httpx.AsyncClient`` stand-in: the broker's outbound client (built with
    ``timeout=`` and no transport) answers with a canned response; the test
    harness's own transport-backed client is delegated to the real class."""

    status_code = 200
    content_type = "application/json"
    body = b"{}"
    sse_chunks: list[bytes] = []
    _real_cls = httpx.AsyncClient

    def __init__(self, *a, **k):
        self._real = self._real_cls(*a, **k) if "transport" in k else None

    async def __aenter__(self):
        return await self._real.__aenter__() if self._real else self

    async def __aexit__(self, *a):
        return await self._real.__aexit__(*a) if self._real else False

    def build_request(self, method, url, *, content=None, headers=None, params=None):
        return {"method": method, "url": url, "content": content, "headers": headers, "params": params}

    async def send(self, req, stream=False):
        cls = type(self)

        class _R:
            status_code = cls.status_code
            headers = {"content-type": cls.content_type}
            content = cls.body
            text = cls.body.decode()

            def json(self):
                return json.loads(cls.body)

            async def aiter_bytes(self):
                for chunk in cls.sse_chunks:
                    yield chunk

            async def aread(self):
                return cls.body

            async def aclose(self):
                return None

        return _R()

    async def aclose(self):
        return None

    def __getattr__(self, name):
        return getattr(self._real, name)


@pytest.fixture
def otel_broker(e2e_env, shared_app, monkeypatch, otel_exporter):
    import app.api.broker as broker_mod

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr(broker_mod.httpx, "AsyncClient", _FakeUpstream)
    _FakeUpstream.status_code = 200
    _FakeUpstream.content_type = "application/json"
    _FakeUpstream.body = b"{}"
    _FakeUpstream.sse_chunks = []
    return shared_app


def _post(app, tok, path, body: dict):
    async def _run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await c.post(path, headers={"Authorization": f"Bearer {tok}"}, json=body)

    return asyncio.run(_run())


def _sse(events: list[tuple[str, dict]]) -> list[bytes]:
    return [f"event: {name}\ndata: {json.dumps(data)}\n\n".encode() for name, data in events]


def test_broker_completion_emits_one_span(otel_broker, otel_exporter):
    _FakeUpstream.body = json.dumps(
        {
            "id": "msg_1",
            "model": "claude-opus-4-7",
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "secret answer"}],
            "usage": {
                "input_tokens": 11,
                "output_tokens": 7,
                "cache_read_input_tokens": 100,
                "cache_creation_input_tokens": 3,
            },
        }
    ).encode()
    tok = ticket_repo().mint("chat_otel_json", "main", ttl_seconds=60)

    r = _post(
        otel_broker,
        tok,
        "/api/broker/anthropic/v1/messages",
        {"model": "claude-opus-4-7", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 200, r.text

    (span,) = otel_exporter.get_finished_spans()
    attrs = dict(span.attributes)
    assert span.name == "chat claude-opus-4-7"
    assert span.kind.name == "CLIENT"
    assert attrs["gen_ai.operation.name"] == "chat"
    assert attrs["gen_ai.system"] == "anthropic"
    assert attrs["gen_ai.request.model"] == "claude-opus-4-7"
    assert attrs["gen_ai.response.model"] == "claude-opus-4-7"
    assert attrs["gen_ai.usage.input_tokens"] == 11
    assert attrs["gen_ai.usage.output_tokens"] == 7
    assert attrs["gen_ai.usage.cache_read_input_tokens"] == 100
    assert attrs["gen_ai.usage.cache_creation_input_tokens"] == 3
    assert tuple(attrs["gen_ai.response.finish_reasons"]) == ("end_turn",)
    assert attrs["agnes.session_id"] == "chat_otel_json"
    assert attrs["agnes.ticket_scope"] == "main"
    assert attrs["agnes.upstream"] == "anthropic"
    assert attrs["agnes.stream"] is False
    assert attrs["http.response.status_code"] == 200
    assert attrs["agnes.kind"] == "completion"
    assert span.status.status_code.name == "OK"
    # Sizes always; content stays home unless opted in — no attribute, no event.
    assert attrs["agnes.prompt_chars"] > 0 and attrs["agnes.completion_chars"] > 0
    assert "gen_ai.input.messages" not in attrs
    assert "gen_ai.output.messages" not in attrs
    assert span.events == ()
    assert "secret answer" not in json.dumps(attrs)


def test_broker_streamed_completion_with_content_and_identity(otel_broker, otel_exporter, monkeypatch):
    from app.chat.types import Surface

    monkeypatch.setenv(otel.CAPTURE_CONTENT_VAR, "1")
    session = chat_session_repo().create_session(user_email="otel-user@test.com", surface=Surface.WEB)
    tok = ticket_repo().mint(session.id, "llm", ttl_seconds=60)  # the engine's egress scope
    _FakeUpstream.content_type = "text/event-stream"
    _FakeUpstream.sse_chunks = _sse(
        [
            (
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "model": "claude-stream",
                        "usage": {"input_tokens": 20, "output_tokens": 1, "cache_read_input_tokens": 50},
                    },
                },
            ),
            (
                "content_block_start",
                {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            ),
            (
                "content_block_delta",
                {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hello "}},
            ),
            (
                "content_block_delta",
                {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "world"}},
            ),
            (
                "message_delta",
                {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 4}},
            ),
        ]
    )

    r = _post(
        otel_broker,
        tok,
        "/api/broker/anthropic/v1/messages",
        {
            "model": "claude-stream",
            "stream": True,
            "system": "Be kind.",
            "messages": [{"role": "user", "content": "say hello"}],
        },
    )
    assert r.status_code == 200, r.text
    assert b"Hello " in r.content  # the stream still reaches the caller

    (span,) = otel_exporter.get_finished_spans()
    attrs = dict(span.attributes)
    assert attrs["agnes.stream"] is True
    assert attrs["agnes.stream_complete"] is True
    assert attrs["agnes.response_bytes"] > 0
    assert attrs["agnes.ticket_scope"] == "llm"
    assert attrs["agnes.session_id"] == session.id
    # Identity minimisation (spec 3.6): the session row is no longer read on
    # the span path at all, and the address never leaves the instance — this
    # session has no bound agent, so the id fields are simply absent too.
    assert "agnes.user_email" not in attrs
    assert attrs["gen_ai.response.model"] == "claude-stream"
    assert attrs["gen_ai.usage.input_tokens"] == 20
    assert attrs["gen_ai.usage.output_tokens"] == 4  # the stream's final (max) figure, not a sum
    assert attrs["gen_ai.usage.cache_read_input_tokens"] == 50
    assert tuple(attrs["gen_ai.response.finish_reasons"]) == ("end_turn",)
    # Content rides two span EVENTS, never attributes: a collector stores the
    # attribute object with keys in alphabetical order, and a long prompt
    # under `gen_ai.input…` pushed everything after it past preview caps.
    assert "gen_ai.input.messages" not in attrs and "gen_ai.output.messages" not in attrs
    events = {e.name: dict(e.attributes) for e in span.events}
    assert set(events) == {otel.PROMPT_EVENT, otel.COMPLETION_EVENT}
    inputs = json.loads(events[otel.PROMPT_EVENT]["gen_ai.prompt"])
    assert inputs[0] == {"role": "system", "parts": [{"type": "text", "content": "Be kind."}]}
    assert inputs[1]["parts"][0]["content"] == "say hello"
    outputs = json.loads(events[otel.COMPLETION_EVENT]["gen_ai.completion"])
    assert outputs == [{"role": "assistant", "parts": [{"type": "text", "content": "Hello world"}]}]
    assert attrs["agnes.prompt_chars"] == len(events[otel.PROMPT_EVENT]["gen_ai.prompt"])
    assert attrs["agnes.completion_chars"] == len(events[otel.COMPLETION_EVENT]["gen_ai.completion"])
    assert "agnes.content_truncated" not in attrs


def test_broker_aborted_stream_is_marked_incomplete(otel_broker, otel_exporter):
    """A stream the client dropped before the model finished — only
    ``message_start`` ever arrived — carries no answer and no final usage;
    the span says so instead of looking like a silently lost export."""
    _FakeUpstream.content_type = "text/event-stream"
    _FakeUpstream.sse_chunks = _sse(
        [
            (
                "message_start",
                {"type": "message_start", "message": {"model": "m", "usage": {"input_tokens": 3, "output_tokens": 1}}},
            )
        ]
    )
    tok = ticket_repo().mint("chat_otel_abort", "main", ttl_seconds=60)

    r = _post(otel_broker, tok, "/api/broker/anthropic/v1/messages", {"model": "m", "stream": True, "messages": []})
    assert r.status_code == 200

    (span,) = otel_exporter.get_finished_spans()
    attrs = dict(span.attributes)
    assert attrs["agnes.stream_complete"] is False
    assert attrs["agnes.response_bytes"] > 0
    assert "gen_ai.response.finish_reasons" not in attrs


def test_broker_upstream_error_marks_the_span(otel_broker, otel_exporter):
    _FakeUpstream.status_code = 400
    _FakeUpstream.body = b'{"error":{"type":"invalid_request_error","message":"bad request"}}'
    tok = ticket_repo().mint("chat_otel_err", "main", ttl_seconds=60)

    r = _post(otel_broker, tok, "/api/broker/anthropic/v1/messages", {"model": "claude-x", "messages": []})
    assert r.status_code == 400

    (span,) = otel_exporter.get_finished_spans()
    attrs = dict(span.attributes)
    assert attrs["http.response.status_code"] == 400
    assert attrs["error.type"] == "400"
    assert span.status.status_code.name == "ERROR"
    assert "gen_ai.usage.input_tokens" not in attrs


def test_broker_count_tokens_is_not_a_completion(otel_broker, otel_exporter):
    _FakeUpstream.body = b'{"input_tokens": 5}'
    tok = ticket_repo().mint("chat_otel_count", "main", ttl_seconds=60)

    r = _post(otel_broker, tok, "/api/broker/anthropic/v1/messages/count_tokens", {"model": "claude-x", "messages": []})
    assert r.status_code == 200
    assert otel_exporter.get_finished_spans() == ()


def test_broker_emits_nothing_when_export_is_off(e2e_env, shared_app, monkeypatch):
    """The default: no provider, so the broker never opens a span and never
    reads the session row for a label."""
    import app.api.broker as broker_mod

    otel.shutdown_otel()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr(broker_mod.httpx, "AsyncClient", _FakeUpstream)
    _FakeUpstream.status_code = 200
    _FakeUpstream.content_type = "application/json"
    _FakeUpstream.body = b'{"model":"m","usage":{"input_tokens":1,"output_tokens":1}}'
    calls: list[str] = []
    monkeypatch.setattr(broker_mod, "_start_otel_completion_span", lambda **kw: calls.append("opened"))
    tok = ticket_repo().mint("chat_otel_off", "main", ttl_seconds=60)

    r = _post(shared_app, tok, "/api/broker/anthropic/v1/messages", {"model": "m", "messages": []})
    assert r.status_code == 200
    assert calls == []


# ---------------------------------------------------------------------------
# Failure isolation — instrumentation must never cost the call it observes
# ---------------------------------------------------------------------------


class _ExplodingTracer:
    def start_span(self, *a, **k):
        raise RuntimeError("span processor exploded")


def test_span_open_failure_never_breaks_the_broker_call(otel_broker, otel_exporter, monkeypatch):
    monkeypatch.setattr(otel, "tracer", lambda: _ExplodingTracer())
    _FakeUpstream.body = (
        b'{"model":"m","stop_reason":"end_turn","content":[],"usage":{"input_tokens":1,"output_tokens":1}}'
    )
    tok = ticket_repo().mint("chat_otel_boom", "main", ttl_seconds=60)

    r = _post(otel_broker, tok, "/api/broker/anthropic/v1/messages", {"model": "m", "messages": []})
    assert r.status_code == 200, r.text
    assert otel_exporter.get_finished_spans() == ()


def test_span_open_failure_never_breaks_trace_generation(otel_exporter, monkeypatch):
    monkeypatch.setattr(otel, "tracer", lambda: _ExplodingTracer())
    with trace_generation(provider="anthropic", model="m") as cap:
        cap.set_tokens(1, 1)
    assert otel_exporter.get_finished_spans() == ()
