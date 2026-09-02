"""Observability helpers.

Signals ride the deployment's own log pipeline — there is no separate
telemetry sink to configure. See ``docs/observability.md``.
"""

from src.observability.llm_tracing import trace_generation

__all__ = ["trace_generation"]
