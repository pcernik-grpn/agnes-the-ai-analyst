"""The turn record — what the broker needs to parent a completion span under
the chat turn that caused it, without the engine propagating anything.

ChatManager mints a ``turn_id`` per delivered user message, opens the
``agnes.chat.turn`` span and publishes ``{turn_id, trace_id, span_id, …}``
under ``chat:turn:{session_id}`` (24 h TTL, the coordination backend
``app/chat/turn_usage.py`` already uses). The broker reads it on every
completion and opens its span as a child of that context — the two may be
different replicas; the collector stitches on ``trace_id``. The key is
overwritten by the next turn and never deleted at turn end, so a completion
that lands after the assistant frame still attributes to the turn that
caused it. Coordination unavailable → no linkage, everything else recorded.

``ChatManager._close_turn`` re-publishes the SAME record with ``ended_at``
set instead of deleting it, so a reader can tell "still open" from "already
answered" without losing the ids a late completion still needs
(``started_no_later_than`` below). A record whose turn started AFTER the
reader's own reference point (a completion's start, a memory write's start)
is refused rather than attributed — an unattributed row is honest, a
wrongly attributed one is not.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Optional

from app.coordination.base import CoordinationUnavailable
from app.coordination.factory import coordination

logger = logging.getLogger(__name__)

#: Generous, because the record is read by whatever completion lands next —
#: a turn suspended on a tool or an approval can be hours old — and because
#: nothing ever deletes it: the next turn's publish is what retires it.
TURN_TTL_SECONDS = 24 * 3600


def turn_key(session_id: str) -> str:
    return f"chat:turn:{session_id}"


def workload_for_surface(surface: Optional[str]) -> str:
    """The spec's workload vocabulary for a chat surface: a session created
    through the agent API is ``agent_api`` work, every other surface (web,
    Slack, Telegram) is ``chat``."""
    return "agent_api" if str(surface or "") == "api" else "chat"


@dataclass(frozen=True)
class TurnRecord:
    """One live chat turn, as the broker sees it.

    Ids and labels only: what the completion span needs for its parent and
    what its ledger row needs for attribution. No message text ever.

    ``ended_at`` is ``None`` while the turn is open and an ISO timestamp
    once ``ChatManager._close_turn`` re-publishes the SAME record (same
    key, same TTL, every other field unchanged — the broker's late-
    completion linkage still needs a closed turn's ids) with it set.
    ``ended_at_known`` distinguishes a record that genuinely has no
    ``ended_at`` because a replica running before this field existed
    published it — ``from_json`` sets it ``False`` only for that legacy
    shape, so ``is_open()`` can tell "known open" from "can't say" instead
    of guessing open for both.
    """

    turn_id: str
    trace_id: Optional[str]
    span_id: Optional[str]
    started_at: str
    user_id: Optional[str]
    agent_id: Optional[str]
    surface: Optional[str]
    workload: str
    message_id: Optional[str] = None
    ended_at: Optional[str] = None
    ended_at_known: bool = True

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    def is_open(self) -> Optional[bool]:
        """Whether this turn is still live. ``None`` when a legacy record
        (published before ``ended_at`` existed) leaves it unknowable — a
        caller that needs certainty (memory provenance, finding B) must
        treat that as "do not stamp", never as "assume open"."""
        if not self.ended_at_known:
            return None
        return self.ended_at is None

    @classmethod
    def from_json(cls, text: Optional[str]) -> Optional["TurnRecord"]:
        """Parse a published record, or ``None`` for anything that is not
        one — a truncated value, a record from a future shape, an entry
        with no turn id to attribute to."""
        if not text:
            return None
        try:
            data = json.loads(text)
            if not isinstance(data, dict) or not data.get("turn_id"):
                return None
            kwargs = {k: data.get(k) for k in cls.__dataclass_fields__}
            kwargs["ended_at_known"] = "ended_at" in data
            return cls(**kwargs)
        except (ValueError, TypeError):
            return None


def publish_turn(session_id: str, record: TurnRecord) -> None:
    """Announce the turn to whichever replica brokers its completions.
    Never raises: a turn must not fail because it could not be labelled."""
    try:
        coordination().kv_set(turn_key(session_id), record.to_json(), ttl_s=TURN_TTL_SECONDS)
    except CoordinationUnavailable:
        logger.warning("turn record not published for session %s: coordination unavailable", session_id)
    except Exception:  # noqa: BLE001 - instrumentation never costs the turn
        logger.warning("turn record publish failed for session %s", session_id, exc_info=True)


def read_turn(session_id: str) -> Optional[TurnRecord]:
    """The session's current turn, or ``None`` when there is none and when
    the backend cannot say — the caller degrades to a root span."""
    try:
        return TurnRecord.from_json(coordination().kv_get(turn_key(session_id)))
    except CoordinationUnavailable:
        return None
    except Exception:  # noqa: BLE001 - see publish_turn
        logger.debug("turn record read failed for session %s", session_id, exc_info=True)
        return None


def started_no_later_than(record: TurnRecord, reference: datetime) -> bool:
    """True when ``record``'s turn indisputably started at or before
    ``reference`` — the rule two callers apply for different reasons: the
    broker refuses to attribute a late completion to a turn that started
    AFTER the completion began (finding A — an overlapping co-driver turn
    must not steal a call that was not its own), and memory provenance
    refuses to stamp a turn that started after the write it would be
    labelling (finding B). A missing or malformed ``started_at`` makes the
    record unusable rather than trusted true or false on a guess."""
    try:
        started = datetime.fromisoformat(record.started_at)
    except (TypeError, ValueError):
        return False
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return started <= reference


__all__ = [
    "TURN_TTL_SECONDS",
    "TurnRecord",
    "publish_turn",
    "read_turn",
    "started_no_later_than",
    "turn_key",
    "workload_for_surface",
]
