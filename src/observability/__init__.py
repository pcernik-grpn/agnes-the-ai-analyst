"""Observability helpers.

Signals ride the deployment's own log pipeline by default — there is no
separate telemetry sink to configure. An OTLP trace export is opt-in through
the standard ``OTEL_EXPORTER_OTLP_*`` variables (``src/observability/otel.py``).
See ``docs/observability.md``.
"""

from src.observability.llm_tracing import trace_generation
from src.observability.otel import configure_otel, shutdown_otel
from src.observability.otel import is_enabled as otel_enabled

__all__ = ["configure_otel", "otel_enabled", "shutdown_otel", "trace_generation"]
