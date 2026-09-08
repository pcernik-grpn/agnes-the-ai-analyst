"""``LlmCallRecord`` — one LLM call, in the one shape both sinks consume.

The chat broker (``app/api/broker.py``) and ``trace_generation``
(``src/observability/llm_tracing.py``) each build one of these per call and
hand it to two sinks: the span (``src/observability/otel.py``) and the
on-instance ledger (``src/observability/llm_ledger.py`` → ``llm_calls``).
Both carry the same ids, so a row and a span describe the same call.

Pricing happens HERE, once, at write time (``src.llm_pricing.cost_usd``),
with the rates stored beside the figure (``priced_as``) so any row can be
re-derived — and an unknown model, priced at the most expensive
general-purpose tier like every other surface, says so
(``priced_as["price_key"] == "default"``) instead of passing as a
measurement.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from src.llm_pricing import BATCH_PRICE_MULTIPLIER, cost_usd, resolve_price, resolve_price_key
from src.observability.llm_context import LlmCallContext

#: The four token kinds every producer reports, in the ledger's column names.
TOKEN_KINDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens")


def _int(value: Any) -> int:
    try:
        if value is None or isinstance(value, bool):
            return 0
        return int(value)
    except (TypeError, ValueError):
        return 0


def _get(obj: Any, key: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, Mapping):
        return obj.get(key)
    return getattr(obj, key, None)


def usage_from_anthropic(usage: Any) -> dict[str, int]:
    """The four token kinds off an Anthropic ``usage`` object (or a dict)."""
    if usage is None:
        return dict.fromkeys(TOKEN_KINDS, 0)
    return {
        "input_tokens": _int(_get(usage, "input_tokens")),
        "output_tokens": _int(_get(usage, "output_tokens")),
        "cache_read_tokens": _int(_get(usage, "cache_read_input_tokens")),
        "cache_creation_tokens": _int(_get(usage, "cache_creation_input_tokens")),
    }


def usage_from_openai(usage: Any) -> dict[str, int]:
    """The four token kinds off an OpenAI-shaped ``usage``. ``prompt_tokens``
    INCLUDES the cached prefix there, so the uncached input is the difference."""
    if usage is None:
        return dict.fromkeys(TOKEN_KINDS, 0)
    prompt = _int(_get(usage, "prompt_tokens"))
    details = _get(usage, "prompt_tokens_details")
    cached = _int(_get(details, "cached_tokens")) if details is not None else 0
    return {
        "input_tokens": max(prompt - cached, 0),
        "output_tokens": _int(_get(usage, "completion_tokens")),
        "cache_read_tokens": cached,
        "cache_creation_tokens": 0,
    }


def priced_as_for(model: str | None, *, batch: bool = False) -> dict[str, Any]:
    """The rates a record was priced with — stored beside the figure so any
    row can be re-derived, and so a default-priced guess is visibly one."""
    price = resolve_price(model)
    return {
        "price_key": resolve_price_key(model) or "default",
        "input_per_mtok": price.input_per_mtok,
        "output_per_mtok": price.output_per_mtok,
        "cache_read_per_mtok": round(price.cache_read_per_mtok, 6),
        "cache_write_per_mtok": round(price.cache_write_per_mtok, 6),
        "batch_multiplier": BATCH_PRICE_MULTIPLIER if batch else 1.0,
    }


@dataclass(frozen=True)
class LlmCallRecord:
    """One LLM call: who, for what, in which turn, at what price."""

    id: str
    created_at: datetime
    kind: str
    workload: str | None
    purpose: str | None
    session_id: str | None
    turn_id: str | None
    user_id: str | None
    agent_id: str | None
    job_id: str | None
    subject_id: str | None
    trace_id: str | None
    span_id: str | None
    provider: str
    upstream: str
    model_requested: str | None
    model_response: str | None
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    cost_usd: float
    priced_as: dict[str, Any]
    latency_ms: int | None
    status: str
    error_type: str | None
    http_status: int | None
    prompt_chars: int | None
    completion_chars: int | None
    stop_reason: str | None
    stream_complete: bool | None

    def to_row(self) -> dict[str, Any]:
        """The ledger row — column names are the field names, one to one."""
        return asdict(self)

    def context(self) -> LlmCallContext:
        return LlmCallContext(
            workload=self.workload,
            purpose=self.purpose,
            session_id=self.session_id,
            turn_id=self.turn_id,
            user_id=self.user_id,
            agent_id=self.agent_id,
            job_id=self.job_id,
            subject_id=self.subject_id,
        )

    def span_attributes(self) -> dict[str, Any]:
        return {**self.context().span_attributes(), "agnes.cost_usd": self.cost_usd}


def build_record(
    *,
    kind: str,
    context: LlmCallContext,
    provider: str,
    upstream: str,
    model_requested: str | None,
    model_response: str | None,
    usage: Mapping[str, Any] | None,
    latency_ms: int | None,
    status: str,
    error_type: str | None = None,
    http_status: int | None = None,
    prompt_chars: int | None = None,
    completion_chars: int | None = None,
    stop_reason: str | None = None,
    stream_complete: bool | None = None,
    trace_id: str | None = None,
    span_id: str | None = None,
    batch: bool = False,
    created_at: datetime | None = None,
) -> LlmCallRecord:
    """Build (and price) the record for one call.

    ``usage`` is ``parse_usage``'s normalized dict shape, or ``None`` for a
    call that never produced one — a failure is a real, zero-cost row, not a
    gap in the ledger. The price follows the model the RESPONSE reported when
    there is one (an alias can resolve upstream to a different family) and
    the requested model otherwise.
    """
    tokens = {k: _int((usage or {}).get(k)) for k in TOKEN_KINDS}
    priced_model = model_response or model_requested
    cost = round(cost_usd(model=priced_model, batch=batch, **tokens), 6)
    return LlmCallRecord(
        id=str(uuid4()),
        created_at=created_at or datetime.now(timezone.utc),
        kind=kind,
        workload=context.workload,
        purpose=context.purpose,
        session_id=context.session_id,
        turn_id=context.turn_id,
        user_id=context.user_id,
        agent_id=context.agent_id,
        job_id=context.job_id,
        subject_id=context.subject_id,
        trace_id=trace_id,
        span_id=span_id,
        provider=provider,
        upstream=upstream,
        model_requested=model_requested,
        model_response=model_response,
        cost_usd=cost,
        priced_as=priced_as_for(priced_model, batch=batch),
        latency_ms=latency_ms,
        status=status,
        error_type=error_type,
        http_status=http_status,
        prompt_chars=prompt_chars,
        completion_chars=completion_chars,
        stop_reason=stop_reason,
        stream_complete=stream_complete,
        **tokens,
    )


__all__ = [
    "TOKEN_KINDS",
    "LlmCallRecord",
    "build_record",
    "priced_as_for",
    "usage_from_anthropic",
    "usage_from_openai",
]
