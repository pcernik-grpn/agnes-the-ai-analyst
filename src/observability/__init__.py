"""Observability helpers.

Signals ride the deployment's own log pipeline by default — there is no
separate telemetry sink to configure. An OTLP trace export is opt-in through
the standard ``OTEL_EXPORTER_OTLP_*`` variables (``src/observability/otel.py``).
See ``docs/observability.md``.

Two things a call site reaches for: :func:`trace_generation` to wrap a model
call, and :func:`llm_context` to say what the work underneath it IS — the
workload/purpose/identity labels that turn a pile of identical-looking
generations into an answerable cost and behaviour record.
"""

from src.observability.llm_context import LlmCallContext, current_llm_context, llm_context
from src.observability.llm_tracing import record_generation, trace_generation
from src.observability.otel import configure_otel, shutdown_otel
from src.observability.otel import is_enabled as otel_enabled

__all__ = [
    "LlmCallContext",
    "configure_otel",
    "current_llm_context",
    "llm_context",
    "otel_enabled",
    "record_generation",
    "shutdown_otel",
    "trace_generation",
]
