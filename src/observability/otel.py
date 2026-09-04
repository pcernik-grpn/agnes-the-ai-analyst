"""Opt-in OpenTelemetry export — LLM completions as OTLP spans.

Off by default. Setting the standard ``OTEL_EXPORTER_OTLP_ENDPOINT`` (plus
``OTEL_EXPORTER_OTLP_HEADERS`` for the collector's credential) turns on one
OTLP/HTTP trace exporter for this process. Nothing else changes: the log
pipeline keeps carrying what it carried, and an unset endpoint leaves the
process with the API's no-op tracer — every span below is then a
non-recording stub that costs a dictionary lookup.

What is exported, and why it lives here:

- One span per LLM completion that transits the chat broker
  (``app/api/broker.py``) — every chat surface, every engine, because every
  byte of a session's LLM traffic goes through that one route. The span
  carries the OTel GenAI attributes (model, the four token kinds, finish
  reason) plus the Agnes identity of the call (session, user, agent, ticket
  scope). Prompt and completion content is exported ONLY when
  ``AGNES_OTEL_CAPTURE_CONTENT`` is set: in this product it routinely
  carries customer data, so the default is the same as for logs — sizes and
  counts, never the text. When it is on, the text rides two span EVENTS
  (``gen_ai.content.prompt`` / ``gen_ai.content.completion``), never span
  attributes: a collector stores attributes as one JSON object with keys in
  alphabetical order, and a prompt that is hundreds of KiB pushes every key
  after ``gen_ai.input…`` past any preview or size cap — the answer was the
  first casualty. Events keep the attribute object small and parseable.
- One span per server-side generation wrapped in
  :func:`src.observability.llm_tracing.trace_generation` (summaries,
  extraction, semantic layer) — the same tracer, so both kinds land in one
  table and filter on one resource.

The resource (what a collector shows as the origin) defaults to
``service.name=agnes``, ``service.version=<release>``,
``deployment.environment=<AGNES_DEPLOYMENT_ENV|RELEASE_CHANNEL|unknown>`` and
``service.instance.id=<hostname:pid>`` — the same instance label the logs
carry, so one deployment is one value in both signals. The standard
``OTEL_SERVICE_NAME`` / ``OTEL_RESOURCE_ATTRIBUTES`` override any of them.

Nothing in here may break the call it observes: every accessor is
defensive, and a failure inside the instrumentation is logged and swallowed.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from typing import Any, Mapping, Optional

try:
    from opentelemetry import trace
    from opentelemetry.trace import SpanKind, StatusCode

    _OTEL_API = True
except ImportError:  # a core (CLI-only) install carries no OTel packages
    trace = None  # type: ignore[assignment]
    SpanKind = None  # type: ignore[assignment]
    StatusCode = None  # type: ignore[assignment]
    _OTEL_API = False

logger = logging.getLogger(__name__)

ENDPOINT_VAR = "OTEL_EXPORTER_OTLP_ENDPOINT"
TRACES_ENDPOINT_VAR = "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"
CAPTURE_CONTENT_VAR = "AGNES_OTEL_CAPTURE_CONTENT"
TRACER_NAME = "agnes"
SERVICE_NAME = "agnes"
#: Span events carrying the exchange's text when content capture is on —
#: the GenAI semantic-convention event names, each with the matching
#: ``gen_ai.prompt`` / ``gen_ai.completion`` attribute holding the messages
#: as JSON in the ``[{role, parts}]`` shape.
PROMPT_EVENT = "gen_ai.content.prompt"
COMPLETION_EVENT = "gen_ai.content.completion"

#: Per-attribute cap on exported message content. A long agent session
#: re-sends its whole history on every completion, so an uncapped input
#: attribute would grow with the conversation; a capped one still carries
#: the system prompt and the recent turns, which is what a behaviour review
#: reads. Truncation is flagged on the span (``agnes.content_truncated``).
MAX_CONTENT_CHARS = 256 * 1024

_lock = threading.Lock()
_provider: Any = None


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def _deployment_env() -> str:
    """Mirror of ``app.logging_config._deployment_env`` — kept in step so the
    trace resource and the log lines agree on the instance label."""
    for var in ("AGNES_DEPLOYMENT_ENV", "RELEASE_CHANNEL"):
        value = os.environ.get(var, "").strip()
        if value:
            return value
    return "unknown"


def _app_version() -> str:
    try:
        return _pkg_version("agnes-the-ai-analyst")
    except PackageNotFoundError:
        return "0.0.0+dev"


def _build_resource(role: Optional[str]) -> Any:
    from opentelemetry.sdk.resources import OTELResourceDetector, Resource

    defaults: dict[str, Any] = {
        "service.name": SERVICE_NAME,
        "service.version": _app_version(),
        "service.instance.id": f"{socket.gethostname()}:{os.getpid()}",
        "deployment.environment": _deployment_env(),
    }
    if role:
        defaults["agnes.role"] = role
    # The operator's own OTEL_SERVICE_NAME / OTEL_RESOURCE_ATTRIBUTES win over
    # the defaults — a deployment that already labels itself keeps its label.
    try:
        env_attrs = dict(OTELResourceDetector().detect().attributes)
    except Exception:  # noqa: BLE001 - a malformed env value costs the override, not the export
        env_attrs = {}
    return Resource.create({**defaults, **env_attrs})


def endpoint_configured() -> bool:
    return bool(os.environ.get(ENDPOINT_VAR, "").strip() or os.environ.get(TRACES_ENDPOINT_VAR, "").strip())


def configure_otel(*, role: Optional[str] = None, exporter: Any = None) -> bool:  # noqa: C901
    """Install the process-wide tracer provider. Idempotent; returns whether
    export is on.

    Without an endpoint (and no injected ``exporter``) this is a no-op that
    returns ``False``. ``exporter`` exists for tests: an injected exporter is
    wired through a synchronous processor and NOT registered as the global
    API provider, so a test never leaks a provider into the process.
    """
    global _provider
    with _lock:
        if _provider is not None:
            return True
        if exporter is None and not endpoint_configured():
            return False
        if not _OTEL_API:
            logger.warning("otel: %s is set but the opentelemetry packages are not installed", ENDPOINT_VAR)
            return False
        try:
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor

            injected = exporter is not None
            if injected:
                processor: Any = SimpleSpanProcessor(exporter)
            else:
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

                # The exporter reads OTEL_EXPORTER_OTLP_ENDPOINT / _HEADERS /
                # _TRACES_ENDPOINT itself and appends ``/v1/traces`` to a base
                # endpoint, exactly as the OTLP spec says an SDK must.
                processor = BatchSpanProcessor(OTLPSpanExporter())
            provider = TracerProvider(resource=_build_resource(role))
            provider.add_span_processor(processor)
        except Exception:  # noqa: BLE001 - tracing setup must never take the process down
            logger.exception("otel: exporter setup failed; trace export stays off")
            return False
        _provider = provider
        if not injected:
            try:
                trace.set_tracer_provider(provider)
            except Exception:  # noqa: BLE001 - the module-level tracer works without the global
                logger.debug("otel: global tracer provider already set", exc_info=True)
            logger.info(
                "otel: OTLP trace export enabled",
                extra={"deployment_env": _deployment_env(), "capture_content": capture_content_enabled()},
            )
        return True


def is_enabled() -> bool:
    return _provider is not None


class _NoopSpan:
    """Stand-in when the OTel API is not installed: same surface, no effect."""

    def is_recording(self) -> bool:
        return False

    def set_attribute(self, key: str, value: Any) -> None:
        return None

    def set_status(self, status: Any, description: Optional[str] = None) -> None:
        return None

    def add_event(self, name: str, attributes: Optional[Mapping[str, Any]] = None) -> None:
        return None

    def end(self, end_time: Optional[int] = None) -> None:
        return None


class _NoopTracer:
    def start_span(self, name: str, **kwargs: Any) -> _NoopSpan:
        return _NoopSpan()


def tracer() -> Any:
    """The process tracer: the configured provider's, else the API's no-op
    one (a non-recording span per call), else a local stub when the OTel
    packages are absent altogether."""
    provider = _provider
    if provider is not None:
        return provider.get_tracer(TRACER_NAME, _app_version())
    if not _OTEL_API:
        return _NoopTracer()
    return trace.get_tracer(TRACER_NAME)


def shutdown_otel(timeout_ms: int = 5000) -> None:
    """Flush and drop the provider. Safe to call when export was never on."""
    global _provider
    with _lock:
        provider, _provider = _provider, None
    if provider is None:
        return
    try:
        provider.force_flush(timeout_ms)
        provider.shutdown()
    except Exception:  # noqa: BLE001 - shutdown is best-effort
        logger.debug("otel: provider shutdown failed", exc_info=True)


def capture_content_enabled() -> bool:
    return os.environ.get(CAPTURE_CONTENT_VAR, "").strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Completion spans (the chat broker's one span per forwarded completion)
# ---------------------------------------------------------------------------


def _clean(attrs: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in attrs.items() if v is not None and v != ""}


def start_completion_span(
    *,
    upstream: str,
    model: Optional[str],
    stream: bool,
    session_id: Optional[str],
    ticket_scope: Optional[str],
    user_email: Optional[str] = None,
    user_id: Optional[str] = None,
    agent_id: Optional[str] = None,
) -> Any:
    """Open the span for one brokered completion. ``upstream`` names where
    the request goes (``anthropic``, ``vertex``, ``dispatcher``); the GenAI
    ``system`` is the provider whose API shape the call speaks."""
    system = "gcp.vertex_ai" if upstream == "vertex" else "anthropic"
    attrs = _clean(
        {
            "gen_ai.operation.name": "chat",
            "gen_ai.system": system,
            "gen_ai.request.model": model,
            "agnes.kind": "completion",
            "agnes.upstream": upstream,
            "agnes.stream": stream,
            "agnes.session_id": session_id,
            "agnes.ticket_scope": ticket_scope,
            "agnes.user_email": user_email,
            "agnes.user_id": user_id,
            "agnes.agent_id": agent_id,
        }
    )
    name = f"chat {model}" if model else "chat"
    return _open_span(name, attrs)


def _open_span(name: str, attrs: Mapping[str, Any]) -> Any:
    """``tracer().start_span`` that cannot raise: a span processor that
    fails on ``on_start`` costs the span, never the LLM call it observes."""
    if not _OTEL_API:
        return _NoopSpan()
    try:
        return tracer().start_span(name, kind=SpanKind.CLIENT, attributes=dict(attrs))
    except Exception:  # noqa: BLE001 - see the module docstring
        logger.debug("otel: could not open span %s", name, exc_info=True)
        return _NoopSpan()


def start_generation_span(*, provider: str, model: str) -> Any:
    """Open the span for one server-side generation (``trace_generation``)."""
    attrs = _clean(
        {
            "gen_ai.operation.name": "chat",
            "gen_ai.system": provider,
            "gen_ai.request.model": model,
            "agnes.kind": "generation",
        }
    )
    return _open_span(f"chat {model}" if model else "chat", attrs)


def end_generation_span(
    span: Any,
    *,
    input_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
    prompt_chars: Optional[int] = None,
    completion_chars: Optional[int] = None,
    error_type: Optional[str] = None,
    user_id: Optional[str] = None,
) -> None:
    """Finish a generation span with the counts ``trace_generation`` collected.
    Sizes, never text: these calls run over documents and query results."""
    try:
        if not span.is_recording():
            return
        for attr, value in (
            ("gen_ai.usage.input_tokens", input_tokens),
            ("gen_ai.usage.output_tokens", output_tokens),
            ("agnes.prompt_chars", prompt_chars),
            ("agnes.completion_chars", completion_chars),
        ):
            if isinstance(value, int) and not isinstance(value, bool):
                span.set_attribute(attr, value)
        if user_id:
            span.set_attribute("agnes.user_id", user_id)
        if error_type:
            span.set_attribute("error.type", error_type)
            span.set_status(StatusCode.ERROR, error_type)
        else:
            span.set_status(StatusCode.OK)
    except Exception:  # noqa: BLE001 - see the module docstring
        logger.debug("otel: could not finish the generation span", exc_info=True)
    finally:
        try:
            span.end()
        except Exception:  # noqa: BLE001
            pass


def set_usage_attributes(span: Any, usage: Optional[Mapping[str, Any]]) -> None:
    """Map the broker's normalized usage shape (``parse_usage``) onto the
    GenAI usage attributes. Cache tokens are emitted separately — Anthropic
    reports ``input_tokens`` as the UNCACHED input only, so folding them in
    would undercount a cache-heavy agentic run by orders of magnitude."""
    if not usage:
        return
    mapping = {
        "input_tokens": "gen_ai.usage.input_tokens",
        "output_tokens": "gen_ai.usage.output_tokens",
        "cache_read_tokens": "gen_ai.usage.cache_read_input_tokens",
        "cache_creation_tokens": "gen_ai.usage.cache_creation_input_tokens",
    }
    for key, attr in mapping.items():
        value = usage.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            span.set_attribute(attr, value)
    model = usage.get("model")
    if model:
        span.set_attribute("gen_ai.response.model", str(model))


def end_completion_span(  # noqa: C901
    span: Any,
    *,
    status_code: Optional[int] = None,
    usage: Optional[Mapping[str, Any]] = None,
    request_body: Optional[bytes] = None,
    response_body: Optional[bytes] = None,
    content_type: str = "",
    error: Optional[BaseException] = None,
    response_truncated: bool = False,
) -> None:
    """Finish a completion span with whatever the forward produced. Never
    raises — the response has already been (or is being) delivered."""
    try:
        if not span.is_recording():
            return
        if status_code is not None:
            span.set_attribute("http.response.status_code", int(status_code))
        set_usage_attributes(span, usage)
        summary: dict[str, Any] = {}
        if response_body is not None:
            # A completion the client walked away from (or that never got
            # past the headers) leaves an empty or partial body: no usage,
            # no answer. Say so on the span, so an analysis can tell a turn
            # the model never finished from one it did — the two look the
            # same otherwise, and a burst of them is a broken engine, not a
            # gap in the export.
            span.set_attribute("agnes.response_bytes", len(response_body))
        if response_body is not None and not response_truncated:
            summary = summarize_completion(response_body, content_type)
            if "text/event-stream" in (content_type or "").lower():
                span.set_attribute("agnes.stream_complete", bool(summary.get("stop_reason")))
            if summary.get("model") and not (usage and usage.get("model")):
                span.set_attribute("gen_ai.response.model", str(summary["model"]))
            if summary.get("stop_reason"):
                span.set_attribute("gen_ai.response.finish_reasons", [str(summary["stop_reason"])])
        if response_truncated:
            span.set_attribute("agnes.response_truncated", True)
        prompt_json: Optional[str] = None
        completion_json: Optional[str] = None
        if request_body is not None:
            prompt_json = json.dumps(input_messages_from_request(request_body), ensure_ascii=False)
            span.set_attribute("agnes.prompt_chars", len(prompt_json))
        if summary.get("blocks") is not None:
            out = [{"role": "assistant", "parts": _parts_from_content(summary["blocks"])}]
            completion_json = json.dumps(out, ensure_ascii=False)
            span.set_attribute("agnes.completion_chars", len(completion_json))
        if capture_content_enabled():
            # Content goes on EVENTS (see the module docstring): the span's
            # attribute object stays small and parseable however long the
            # conversation is, and each side of the exchange is its own
            # record a collector can map, cap or drop independently.
            truncated = False
            if prompt_json is not None:
                text, cut = truncate_content(prompt_json)
                truncated = truncated or cut
                span.add_event(PROMPT_EVENT, {"gen_ai.prompt": text})
            if completion_json is not None:
                text, cut = truncate_content(completion_json)
                truncated = truncated or cut
                span.add_event(COMPLETION_EVENT, {"gen_ai.completion": text})
            if truncated:
                span.set_attribute("agnes.content_truncated", True)
        if error is not None:
            span.set_attribute("error.type", type(error).__name__)
            span.set_status(StatusCode.ERROR, str(error)[:200])
        elif status_code is not None and status_code >= 400:
            span.set_attribute("error.type", str(status_code))
            span.set_status(StatusCode.ERROR, f"upstream {status_code}")
        else:
            span.set_status(StatusCode.OK)
    except Exception:  # noqa: BLE001 - see the module docstring
        logger.debug("otel: could not finish the completion span", exc_info=True)
    finally:
        try:
            span.end()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Content helpers — request messages and the response, in the OTel GenAI
# message shape (a list of {role, parts}) rather than provider-native JSON
# ---------------------------------------------------------------------------


def truncate_content(text: str, limit: int = MAX_CONTENT_CHARS) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit] + "…[truncated]", True


def _text_of(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_text_of(v) for v in value)
    if isinstance(value, dict):
        if value.get("type") == "text":
            return str(value.get("text", ""))
        return ""
    return "" if value is None else str(value)


def _parts_from_content(content: Any) -> list[dict[str, Any]]:
    """Anthropic content (a string or a list of typed blocks) → GenAI parts.
    Binary blocks (image/document) keep only their type; a part never
    carries bytes."""
    if isinstance(content, str):
        return [{"type": "text", "content": content}]
    parts: list[dict[str, Any]] = []
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            parts.append({"type": "text", "content": str(block.get("text", ""))})
        elif kind == "tool_use":
            parts.append(
                {
                    "type": "tool_call",
                    "id": block.get("id"),
                    "name": block.get("name"),
                    "arguments": block.get("input"),
                }
            )
        elif kind == "tool_result":
            parts.append(
                {
                    "type": "tool_call_response",
                    "id": block.get("tool_use_id"),
                    "is_error": bool(block.get("is_error", False)),
                    "result": _text_of(block.get("content")),
                }
            )
        elif kind == "thinking":
            parts.append({"type": "reasoning", "content": str(block.get("thinking", ""))})
        else:
            parts.append({"type": str(kind)})
    return parts


def input_messages_from_request(raw: bytes) -> list[dict[str, Any]]:
    """The Messages-API request body → GenAI input messages, system first."""
    try:
        body = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(body, dict):
        return []
    messages: list[dict[str, Any]] = []
    system = body.get("system")
    if system:
        messages.append({"role": "system", "parts": _parts_from_content(system)})
    for message in body.get("messages") or []:
        if isinstance(message, dict):
            messages.append(
                {"role": str(message.get("role", "")), "parts": _parts_from_content(message.get("content"))}
            )
    return messages


def summarize_completion(body: bytes, content_type: str) -> dict[str, Any]:
    """``{model, stop_reason, blocks}`` from a buffered Messages-API response
    — the JSON shape, or an SSE stream re-assembled block by block. Missing
    or unparseable input yields an empty dict, never an exception."""
    try:
        if "text/event-stream" in (content_type or "").lower():
            return _summarize_sse(body)
        data = json.loads(body)
        if not isinstance(data, dict):
            return {}
        return {
            "model": data.get("model"),
            "stop_reason": data.get("stop_reason"),
            "blocks": data.get("content") if isinstance(data.get("content"), list) else [],
        }
    except Exception:  # noqa: BLE001
        logger.debug("otel: unreadable completion body", exc_info=True)
        return {}


def _summarize_sse(body: bytes) -> dict[str, Any]:
    text = body.decode("utf-8", errors="replace")
    model: Optional[str] = None
    stop_reason: Optional[str] = None
    blocks: dict[int, dict[str, Any]] = {}
    partial_json: dict[int, list[str]] = {}
    event_type: Optional[str] = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            event_type = None
            continue
        if line.startswith("event:"):
            event_type = line[len("event:") :].strip()
            continue
        if not line.startswith("data:"):
            continue
        try:
            data = json.loads(line[len("data:") :].strip())
        except (ValueError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        kind = event_type or data.get("type")
        if kind == "message_start":
            message = data.get("message") or {}
            model = message.get("model") or model
        elif kind == "content_block_start":
            index = int(data.get("index", len(blocks)))
            block = dict(data.get("content_block") or {})
            if block.get("type") == "text":
                block.setdefault("text", "")
            elif block.get("type") == "tool_use":
                partial_json[index] = []
            elif block.get("type") == "thinking":
                block.setdefault("thinking", "")
            blocks[index] = block
        elif kind == "content_block_delta":
            index = int(data.get("index", 0))
            delta = data.get("delta") or {}
            block = blocks.setdefault(index, {"type": "text", "text": ""})
            if delta.get("type") == "text_delta":
                block["text"] = str(block.get("text", "")) + str(delta.get("text", ""))
            elif delta.get("type") == "input_json_delta":
                partial_json.setdefault(index, []).append(str(delta.get("partial_json", "")))
            elif delta.get("type") == "thinking_delta":
                block["thinking"] = str(block.get("thinking", "")) + str(delta.get("thinking", ""))
        elif kind == "message_delta":
            delta = data.get("delta") or {}
            stop_reason = delta.get("stop_reason") or stop_reason
    for index, chunks in partial_json.items():
        joined = "".join(chunks)
        block = blocks.get(index)
        if block is None:
            continue
        try:
            block["input"] = json.loads(joined) if joined else {}
        except ValueError:
            block["input"] = joined
    return {
        "model": model,
        "stop_reason": stop_reason,
        "blocks": [blocks[i] for i in sorted(blocks)],
    }


__all__ = [
    "CAPTURE_CONTENT_VAR",
    "COMPLETION_EVENT",
    "PROMPT_EVENT",
    "ENDPOINT_VAR",
    "MAX_CONTENT_CHARS",
    "capture_content_enabled",
    "configure_otel",
    "end_completion_span",
    "end_generation_span",
    "endpoint_configured",
    "input_messages_from_request",
    "is_enabled",
    "set_usage_attributes",
    "shutdown_otel",
    "start_completion_span",
    "start_generation_span",
    "summarize_completion",
    "tracer",
    "truncate_content",
]
