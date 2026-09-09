"""The LLM call context — who is running a model call, for what, in which turn.

A ``contextvars.ContextVar`` holding an immutable :class:`LlmCallContext`.
A call site pushes one with :func:`llm_context` (``with llm_context(
workload="builder", purpose="entity_builder_turn", user_id=...)``); nested
pushes MERGE — inner values win, unset fields inherit — so an entry point can
label the workload once and a deeper helper can add the purpose. Every
producer (the chat broker, ``trace_generation``) reads
:func:`current_llm_context` and copies the fields onto the span and the
ledger row, which is what lets a builder turn, a corporate-memory extraction
and an auto-title stop looking identical.

``bind_llm_context`` / ``unbind_llm_context`` are the token form for a
runtime that cannot use a ``with`` block around the whole unit of work (the
worker binds ``job_id`` where it already binds ``request_id``).
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, fields, replace
from typing import Any

#: The coarse kinds of work an LLM call can belong to. One closed vocabulary
#: so a cost report can group by it without a free-text cleanup pass.
WORKLOADS: frozenset[str] = frozenset(
    {
        "chat",
        "agent_api",
        "builder",
        "extraction",
        "corporate_memory",
        "knowledge",
        "semantic_layer",
        "anonymization",
        "ocr",
        "vision",
        "auto_title",
        "readiness",
        "store_guardrails",
        "verification",
        "admin_ask",
    }
)


@dataclass(frozen=True)
class LlmCallContext:
    """Labels for one LLM call. Every field optional: an unlabelled call is
    still recorded, just less answerable."""

    workload: str | None = None
    purpose: str | None = None
    session_id: str | None = None
    turn_id: str | None = None
    user_id: str | None = None
    agent_id: str | None = None
    job_id: str | None = None
    subject_id: str | None = None

    def merged(self, **overrides: Any) -> LlmCallContext:
        """A copy with ``overrides`` applied; a ``None`` override keeps the
        inherited value (nesting can only add or replace, never clear)."""
        clean = {k: v for k, v in overrides.items() if v is not None}
        unknown = set(clean) - {f.name for f in fields(self)}
        if unknown:
            raise TypeError(f"unknown llm context field(s): {sorted(unknown)}")
        return replace(self, **clean)

    def span_attributes(self) -> dict[str, str]:
        """The ``agnes.*`` span attributes for the fields that are set."""
        return {f"agnes.{f.name}": str(getattr(self, f.name)) for f in fields(self) if getattr(self, f.name)}


_current: contextvars.ContextVar[LlmCallContext | None] = contextvars.ContextVar("agnes_llm_context", default=None)


def current_llm_context() -> LlmCallContext:
    """The bound context, or an empty one when nothing is bound."""
    return _current.get() or LlmCallContext()


def bind_llm_context(**fields_: Any) -> contextvars.Token:
    """Push ``fields_`` onto the current context; returns the reset token."""
    return _current.set(current_llm_context().merged(**fields_))


def unbind_llm_context(token: contextvars.Token) -> None:
    _current.reset(token)


@contextmanager
def llm_context(**fields_: Any) -> Iterator[LlmCallContext]:
    """Scope ``fields_`` onto the current context for the duration of the block."""
    token = bind_llm_context(**fields_)
    try:
        yield current_llm_context()
    finally:
        unbind_llm_context(token)


__all__ = [
    "WORKLOADS",
    "LlmCallContext",
    "bind_llm_context",
    "current_llm_context",
    "llm_context",
    "unbind_llm_context",
]
