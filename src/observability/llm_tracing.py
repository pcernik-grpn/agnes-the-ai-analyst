"""LLM call instrumentation — one structured log record per generation.

Wrap the synchronous provider call in :func:`trace_generation`; on exit it
emits a single ``llm_generation`` record carrying provider, model, token
counts, latency and whether the call failed. There is no second sink and no
vendor account: the record goes to the logger every deployment already has,
and ``app/logging_config.py``'s JSON formatter promotes the fields so they
stay filterable.

Prompts and completions are deliberately not recorded — in this product they
routinely carry customer data, and a log pipeline is the wrong place to hold
it. Their *sizes* are recorded, because a size is the part that explains a
cost or a latency.

Nothing in here may break the call it wraps: every accessor on a provider
response is defensive, and a failure inside the instrumentation is logged and
swallowed.

Example::

    from src.observability import trace_generation

    with trace_generation(provider="anthropic", model="claude-opus-4") as cap:
        cap.set_input(prompt)
        response = client.messages.create(...)
        cap.set_output_from_anthropic(response)
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Any, Iterator

logger = logging.getLogger(__name__)


class _Capture:
    """Collects what the record will carry. Every setter is best-effort: an
    unreadable response shape costs the numbers, never the LLM call."""

    def __init__(self) -> None:
        self.input_tokens: int | None = None
        self.output_tokens: int | None = None
        self.prompt_chars: int | None = None
        self.completion_chars: int | None = None
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

    def set_output(self, output: Any) -> None:
        self.completion_chars = self._size(output)

    def set_tokens(self, input_tokens: int | None, output_tokens: int | None) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens

    def set_output_from_anthropic(self, response: Any) -> None:
        try:
            usage = getattr(response, "usage", None)
            if usage is not None:
                self.input_tokens = getattr(usage, "input_tokens", None)
                self.output_tokens = getattr(usage, "output_tokens", None)
            texts = [
                block.text
                for block in (getattr(response, "content", None) or [])
                if getattr(block, "type", None) == "text" and getattr(block, "text", None)
            ]
            if texts:
                self.completion_chars = sum(len(t) for t in texts)
        except Exception:  # noqa: BLE001 - see the class docstring
            logger.debug("llm tracing: unreadable anthropic response shape", exc_info=True)

    def set_output_from_openai(self, response: Any) -> None:
        try:
            usage = getattr(response, "usage", None)
            if usage is not None:
                self.input_tokens = getattr(usage, "prompt_tokens", None)
                self.output_tokens = getattr(usage, "completion_tokens", None)
            choices = getattr(response, "choices", None) or []
            if choices:
                message = getattr(choices[0], "message", None)
                self.completion_chars = self._size(getattr(message, "content", None))
        except Exception:  # noqa: BLE001 - see the class docstring
            logger.debug("llm tracing: unreadable openai response shape", exc_info=True)


@contextmanager
def trace_generation(
    *,
    provider: str,
    model: str,
    distinct_id: str | None = None,
) -> Iterator[_Capture]:
    """Time one LLM call and emit its record. Re-raises whatever the call raises."""
    capture = _Capture()
    started = time.monotonic()
    error_type: str | None = None
    try:
        yield capture
    except BaseException as exc:
        error_type = type(exc).__name__
        raise
    finally:
        fields: dict[str, Any] = {
            "event": "llm_generation",
            "provider": provider,
            "model": model,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "input_tokens": capture.input_tokens,
            "output_tokens": capture.output_tokens,
            "prompt_chars": capture.prompt_chars,
            "completion_chars": capture.completion_chars,
            "is_error": error_type is not None,
            **capture.extra,
        }
        if error_type is not None:
            fields["error_type"] = error_type
        if distinct_id:
            fields["user_id"] = distinct_id
        try:
            logger.info("llm generation", extra=fields)
        except Exception:  # noqa: BLE001 - instrumentation never fails the call
            logger.debug("llm tracing: could not emit the generation record", exc_info=True)
