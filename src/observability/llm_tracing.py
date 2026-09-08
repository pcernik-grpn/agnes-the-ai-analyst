"""LLM call instrumentation — one call record per generation, three sinks.

Wrap the synchronous provider call in :func:`trace_generation`; on exit it
builds one :class:`src.observability.llm_record.LlmCallRecord` and emits it
to everything that wants it: the ``llm_generation`` structured log record
(provider, model, the four token kinds, latency, cost, whether it failed),
the opt-in OTLP span (``src/observability/otel.py``) and the on-instance
``llm_calls`` ledger (``src/observability/llm_ledger.py``, Postgres-only).
Every field the record carries about *who* ran the call and *for what*
comes from the ambient :mod:`src.observability.llm_context` — a call site
labels its work once and every generation underneath inherits the label.

Prompts and completions are deliberately not recorded in the log record or
the ledger row — in this product they routinely carry customer data, and a
log pipeline is the wrong place to hold it. Their *sizes* are recorded,
because a size is the part that explains a cost or a latency. The capture
keeps the texts in memory for one consumer only: the OTLP span, which emits
them as content events if — and only if — the instance's recorded
content-export policy allows it (:mod:`src.observability.content_policy`).

Nothing in here may break the call it wraps: every accessor on a provider
response is defensive, and a failure inside the instrumentation is logged and
swallowed.

Example::

    from src.observability import trace_generation

    with trace_generation(provider="anthropic", model="claude-opus-4",
                          purpose="digest") as cap:
        cap.set_input(prompt)
        response = client.messages.create(...)
        cap.set_output_from_anthropic(response)
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from src.observability import otel as _otel
from src.observability.llm_context import current_llm_context
from src.observability.llm_ledger import record_call
from src.observability.llm_record import build_record, usage_from_anthropic, usage_from_openai

logger = logging.getLogger(__name__)


class _RecordedError(Exception):
    """Carries an already-observed error type through :func:`trace_generation`
    so a result collected after the fact (a batch job) is recorded as an
    error without a live exception to re-raise."""

    def __init__(self, recorded_type: str) -> None:
        super().__init__(recorded_type)
        self.recorded_type = recorded_type


class _Capture:
    """Collects what the record will carry. Every setter is best-effort: an
    unreadable response shape costs the numbers, never the LLM call."""

    def __init__(self) -> None:
        self.input_tokens: int | None = None
        self.output_tokens: int | None = None
        self.cache_read_tokens: int | None = None
        self.cache_creation_tokens: int | None = None
        self.prompt_chars: int | None = None
        self.completion_chars: int | None = None
        self.model_response: str | None = None
        self.stop_reason: str | None = None
        #: The texts themselves, held in memory for the span exporter only.
        #: They never reach the log record or the ledger row (both carry
        #: sizes), and they leave the process only when the content-export
        #: policy allows it — see src/observability/content_policy.py.
        self.prompt_text: str | None = None
        self.completion_text: str | None = None
        self.extra: dict[str, Any] = {}

    @staticmethod
    def _size(value: Any) -> int | None:
        try:
            if value is None:
                return None
            return len(value if isinstance(value, str) else str(value))
        except Exception:  # noqa: BLE001 - a size is never worth an exception
            return None

    def set_input(self, prompt: Any) -> None:
        self.prompt_chars = self._size(prompt)
        self.prompt_text = prompt if isinstance(prompt, str) else None

    def set_output(self, output: Any) -> None:
        self.completion_chars = self._size(output)
        self.completion_text = output if isinstance(output, str) else None

    def set_tokens(
        self,
        input_tokens: int | None,
        output_tokens: int | None,
        *,
        cache_read_tokens: int | None = None,
        cache_creation_tokens: int | None = None,
    ) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_read_tokens = cache_read_tokens
        self.cache_creation_tokens = cache_creation_tokens

    def _set_usage(self, usage: dict[str, int]) -> None:
        self.input_tokens = usage["input_tokens"]
        self.output_tokens = usage["output_tokens"]
        self.cache_read_tokens = usage["cache_read_tokens"]
        self.cache_creation_tokens = usage["cache_creation_tokens"]

    def set_output_from_anthropic(self, response: Any) -> None:
        try:
            usage = getattr(response, "usage", None)
            if usage is not None:
                self._set_usage(usage_from_anthropic(usage))
            self.model_response = getattr(response, "model", None) or None
            self.stop_reason = getattr(response, "stop_reason", None) or None
            texts = [
                block.text
                for block in (getattr(response, "content", None) or [])
                if getattr(block, "type", None) == "text" and getattr(block, "text", None)
            ]
            if texts:
                self.completion_chars = sum(len(t) for t in texts)
                self.completion_text = "".join(texts)
        except Exception:  # noqa: BLE001 - see the class docstring
            logger.debug("llm tracing: unreadable anthropic response shape", exc_info=True)

    def set_output_from_openai(self, response: Any) -> None:
        try:
            usage = getattr(response, "usage", None)
            if usage is not None:
                self._set_usage(usage_from_openai(usage))
            self.model_response = getattr(response, "model", None) or None
            choices = getattr(response, "choices", None) or []
            if choices:
                message = getattr(choices[0], "message", None)
                content = getattr(message, "content", None)
                self.completion_chars = self._size(content)
                self.completion_text = content if isinstance(content, str) else None
                self.stop_reason = getattr(choices[0], "finish_reason", None) or None
        except Exception:  # noqa: BLE001 - see the class docstring
            logger.debug("llm tracing: unreadable openai response shape", exc_info=True)

    def usage(self) -> dict[str, int]:
        """The four token kinds in the record's shape, zeros for unreported."""
        return {
            "input_tokens": int(self.input_tokens or 0),
            "output_tokens": int(self.output_tokens or 0),
            "cache_read_tokens": int(self.cache_read_tokens or 0),
            "cache_creation_tokens": int(self.cache_creation_tokens or 0),
        }


@contextmanager
def trace_generation(
    *,
    provider: str,
    model: str,
    distinct_id: str | None = None,
    purpose: str | None = None,
    subject_id: str | None = None,
    batch: bool = False,
) -> Iterator[_Capture]:
    """Time one LLM call and emit its record to the log, the span and the
    ledger. Re-raises whatever the call raises.

    ``purpose`` and ``subject_id`` label THIS call on top of the ambient
    context (an unset ``purpose`` inherits the context's); ``distinct_id``
    is the caller's user id, kept under its old name for the existing call
    sites and merged into the context as ``user_id``.
    """
    capture = _Capture()
    context = current_llm_context().merged(purpose=purpose, user_id=distinct_id, subject_id=subject_id)
    started = time.monotonic()
    error_type: str | None = None
    span = _otel.start_generation_span(provider=provider, model=model, context=context)
    try:
        yield capture
    except BaseException as exc:
        error_type = getattr(exc, "recorded_type", None) or type(exc).__name__
        raise
    finally:
        latency_ms = int((time.monotonic() - started) * 1000)
        trace_id, span_id = _otel.span_ids(span)
        record = None
        try:
            record = build_record(
                kind="generation",
                context=context,
                provider=provider,
                upstream=provider,
                model_requested=model,
                model_response=capture.model_response,
                usage=capture.usage(),
                latency_ms=latency_ms,
                status="error" if error_type else "ok",
                error_type=error_type,
                prompt_chars=capture.prompt_chars,
                completion_chars=capture.completion_chars,
                stop_reason=capture.stop_reason,
                trace_id=trace_id,
                span_id=span_id,
                batch=batch,
            )
        except Exception:  # noqa: BLE001 - instrumentation never fails the call
            logger.debug("llm tracing: could not build the call record", exc_info=True)
        fields: dict[str, Any] = {
            "event": "llm_generation",
            "provider": provider,
            "model": model,
            "latency_ms": latency_ms,
            "input_tokens": capture.input_tokens,
            "output_tokens": capture.output_tokens,
            "cache_read_tokens": capture.cache_read_tokens,
            "cache_creation_tokens": capture.cache_creation_tokens,
            "prompt_chars": capture.prompt_chars,
            "completion_chars": capture.completion_chars,
            "is_error": error_type is not None,
            "workload": context.workload,
            "purpose": context.purpose,
            "cost_usd": record.cost_usd if record else None,
            **capture.extra,
        }
        if error_type is not None:
            fields["error_type"] = error_type
        if context.user_id:
            fields["user_id"] = context.user_id
        try:
            logger.info("llm generation", extra=fields)
        except Exception:  # noqa: BLE001 - instrumentation never fails the call
            logger.debug("llm tracing: could not emit the generation record", exc_info=True)
        _otel.end_generation_span(
            span,
            input_tokens=capture.input_tokens,
            output_tokens=capture.output_tokens,
            cache_read_tokens=capture.cache_read_tokens,
            cache_creation_tokens=capture.cache_creation_tokens,
            cost_usd=record.cost_usd if record else None,
            prompt_chars=capture.prompt_chars,
            completion_chars=capture.completion_chars,
            error_type=error_type,
            user_id=context.user_id,
            # Text, not sizes — the span emitter drops or pseudonymises it
            # per the content-export policy; nothing here decides that.
            prompt_text=capture.prompt_text,
            completion_text=capture.completion_text,
        )
        if record is not None:
            record_call(record)


def record_generation(
    *,
    provider: str,
    model: str,
    purpose: str,
    usage: Any,
    latency_ms: int | None = None,
    prompt_chars: int | None = None,
    completion_chars: int | None = None,
    subject_id: str | None = None,
    batch: bool = False,
    error_type: str | None = None,
    model_response: str | None = None,
    stop_reason: str | None = None,
) -> None:
    """Emit the record for a generation that was NOT timed in this process —
    a Batches-API result collected later.

    ``usage`` is an Anthropic usage object, an already-normalized dict, or
    ``None``. The in-process ``latency_ms`` of such a record is meaningless,
    so a reported one rides as ``reported_latency_ms`` rather than pretending
    to be a measurement. Never raises.
    """
    try:
        with trace_generation(
            provider=provider, model=model, purpose=purpose, subject_id=subject_id, batch=batch
        ) as cap:
            if isinstance(usage, dict) and "cache_read_tokens" in usage:
                normalized = {k: int(usage.get(k) or 0) for k in cap.usage()}
            else:
                normalized = usage_from_anthropic(usage)
            cap.set_tokens(
                normalized["input_tokens"],
                normalized["output_tokens"],
                cache_read_tokens=normalized["cache_read_tokens"],
                cache_creation_tokens=normalized["cache_creation_tokens"],
            )
            cap.prompt_chars, cap.completion_chars = prompt_chars, completion_chars
            cap.model_response, cap.stop_reason = model_response, stop_reason
            if latency_ms is not None:
                cap.extra["reported_latency_ms"] = latency_ms
            if error_type:
                raise _RecordedError(error_type)
    except _RecordedError:
        pass
    except Exception:  # noqa: BLE001 - instrumentation never fails the call
        logger.debug("llm tracing: record_generation failed", exc_info=True)


__all__ = ["record_generation", "trace_generation"]
