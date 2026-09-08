"""Broker-observed per-turn usage counters.

The engine provider emits assistant frames with no usage on them, yet every
LLM byte of an engine turn transits the broker, which parses the provider's
own usage block. This module is the seam between the two: the broker
accumulates each session-bound completion's usage here
(``app/api/broker.py``), and ChatManager drains the counters exactly once
per turn at the ``assistant_message`` persist seam to hydrate a usage-less
frame (``ChatManager._hydrate_frame_usage``).

Coordination-backed, because the broker replica that forwarded the call need
not be the gateway process holding the live chat session. The drain is
DESTRUCTIVE (``kv_delete`` — Redis ``GETDEL``, the in-memory backend's pop),
which is the design's safety property: no watermark state anywhere, so a
process restart or gateway takeover cannot double-count.

Precision, stated rather than implied: counters are conserved, not
turn-perfect — a completion landing exactly at the drain boundary attributes
to the adjacent turn; counters a session never drains again expire with the
TTL; a multi-model turn keeps the LAST completion's model. Every function
here swallows every failure: a measurement must never cost a turn or a
forward.

Completion TIMING rides the same seam as a second, independent pair
(``add_turn_timing`` / ``drain_turn_timing``): the broker records each
completion's wall time and time-to-first-byte, the manager drains them once
per turn onto the assistant message. Kept apart from the token counters so a
completion whose usage could not be parsed still keeps its timing, and so a
timing-only turn can never hydrate a frame with zero tokens.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from app.coordination.base import CoordinationUnavailable
from app.coordination.factory import coordination

logger = logging.getLogger(__name__)

#: Keys live only between two drains of one session — one turn — but a turn
#: can run tools for a long time, so the TTL is generous. It only
#: garbage-collects counters of sessions that never come back for another
#: turn (the memory backend's incr keeps the FIRST write's expiry).
_TTL_SECONDS = 24 * 3600

_COUNTER_KINDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
)


def _key(session_id: str, kind: str) -> str:
    return f"chat:turnusage:{session_id}:{kind}"


def add_turn_usage(session_id: str, usage: dict[str, Any]) -> None:
    """Accumulate one completion's usage onto ``session_id``'s turn counters.

    ``usage`` is ``parse_usage``'s normalized shape (the four token kinds +
    ``model``). Zero amounts are skipped, so an all-zero usage leaves no
    trace — a later drain of an untouched session stays ``None``. Never
    raises.
    """
    try:
        coord = coordination()
        for kind in _COUNTER_KINDS:
            amount = int(usage.get(kind) or 0)
            if amount:
                coord.incr(_key(session_id, kind), amount=amount, ttl_s=_TTL_SECONDS)
        model = usage.get("model")
        if model:
            coord.kv_set(_key(session_id, "model"), str(model), ttl_s=_TTL_SECONDS)
    except CoordinationUnavailable:
        logger.warning(
            "turn-usage counters unavailable; completion not attributed for session %s",
            session_id,
        )
    except Exception:
        logger.warning("turn-usage accumulate failed for session %s", session_id, exc_info=True)


def drain_turn_usage(session_id: str) -> Optional[dict[str, Any]]:
    """Atomically read-and-reset ``session_id``'s turn counters.

    Returns the four token kinds (ints) plus ``model`` (str | None), or
    ``None`` when no counter was recorded since the last drain — including
    when the backend is unavailable. Never raises.
    """
    totals: dict[str, Any] = {}
    any_counter = False
    try:
        coord = coordination()
        for kind in _COUNTER_KINDS:
            raw = coord.kv_delete(_key(session_id, kind))
            if raw is not None:
                any_counter = True
            totals[kind] = int(raw) if raw is not None else 0
        totals["model"] = coord.kv_delete(_key(session_id, "model"))
    except CoordinationUnavailable:
        logger.warning("turn-usage counters unavailable; turn not hydrated for session %s", session_id)
        return None
    except Exception:
        logger.warning("turn-usage drain failed for session %s", session_id, exc_info=True)
        return None
    if not any_counter:
        return None
    return totals


# ---------------------------------------------------------------------------
# Completion timing
# ---------------------------------------------------------------------------

_TIMING_KINDS = ("llm_calls", "llm_duration_ms", "llm_ttfb_ms")


def add_turn_timing(session_id: str, *, duration_ms: int, ttfb_ms: int) -> None:
    """Accumulate one completion's timing onto ``session_id``'s turn counters.

    ``duration_ms`` is request start → last upstream byte; ``ttfb_ms`` is
    request start → first upstream byte (the response head for a buffered
    reply, the first body chunk of a stream). Every call increments
    ``llm_calls`` — a sub-millisecond completion is still a completion, and
    the count is what keeps the per-turn averages honest. Never raises.
    """
    try:
        coord = coordination()
        coord.incr(_key(session_id, "llm_calls"), amount=1, ttl_s=_TTL_SECONDS)
        for kind, amount in (("llm_duration_ms", duration_ms), ("llm_ttfb_ms", ttfb_ms)):
            amount = int(amount or 0)
            if amount:
                coord.incr(_key(session_id, kind), amount=amount, ttl_s=_TTL_SECONDS)
    except CoordinationUnavailable:
        logger.warning(
            "turn-timing counters unavailable; completion timing not attributed for session %s",
            session_id,
        )
    except Exception:
        logger.warning("turn-timing accumulate failed for session %s", session_id, exc_info=True)


def drain_turn_timing(session_id: str) -> Optional[dict[str, int]]:
    """Atomically read-and-reset ``session_id``'s completion-timing counters.

    Returns ``llm_calls`` / ``llm_duration_ms`` / ``llm_ttfb_ms`` (ints), or
    ``None`` when no completion was recorded since the last drain —
    including when the backend is unavailable. Never raises.
    """
    totals: dict[str, int] = {}
    any_counter = False
    try:
        coord = coordination()
        for kind in _TIMING_KINDS:
            raw = coord.kv_delete(_key(session_id, kind))
            if raw is not None:
                any_counter = True
            totals[kind] = int(raw) if raw is not None else 0
    except CoordinationUnavailable:
        logger.warning("turn-timing counters unavailable; turn timing not hydrated for session %s", session_id)
        return None
    except Exception:
        logger.warning("turn-timing drain failed for session %s", session_id, exc_info=True)
        return None
    if not any_counter:
        return None
    return totals
