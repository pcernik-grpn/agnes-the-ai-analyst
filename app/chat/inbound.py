"""Inbound command stream: routing a user message to the gateway that
actually owns the session's LiveSession (wave-2F task 4).

Companion to ``app.chat.replay`` (wave-2F task 3), same coordination-backend
stream primitive (``CoordinationBackend.stream_append``/``stream_read``,
wave-2F task 3), opposite direction: outbound (runner -> client) frames
replay via ``chat-out:{chat_id}``; inbound (client -> runner) messages route
via ``chat-in:{chat_id}`` here, sequenced by their own counter
(``chat-in-seq:{chat_id}``) so the two directions never share (or contend
on) a sequence space.

Why this exists: ``app.chat.routing`` (wave-2F task 1) lets any gateway
replica find out which OTHER replica currently hosts a session's live
runner, but gives no way to actually get a user's text there. A WebSocket
connection is inherently sticky to whichever gateway TCP-terminated it, so
``app.api.chat``'s ``ws_stream``/``ws_join`` routes always land on the
owning gateway already (the same connection that ran ``attach()`` and
therefore claimed the lease) -- but a Slack event webhook
(``services.slack_bot.events``) has no such stickiness: a load balancer can
hand it to ANY replica, and that replica's ``ChatManager`` may not have the
session's ``LiveSession`` locally at all. ``app.chat.manager.ChatManager.
send_user_message`` is the single choke point both callers go through, so
it is the seam this module plugs into: when ``send_user_message`` finds no
local ``LiveSession`` AND ``app.chat.routing.owner_of`` says a *different*
gateway holds the lease, it hands the message to :func:`publish_inbound`
instead of racing to spawn a second runner. The owning gateway's per-session
``ChatManager._inbound_consumer_loop`` (started in ``_spawn_live``/
``_resume_from_row``, alongside the existing pump/wait tasks) drains the
stream in seq order and feeds each entry into its local runner's stdin via
the same delivery path the direct-owner call already used.

Memory backend / single-process story: since ``app.chat.routing.
this_gateway_id()`` is stable per PROCESS, and the ``memory`` coordination
backend only ever has one process's state to consult, ``owner_of(...)`` can
never return a value different from ``this_gateway_id()`` under ``memory``
-- there is no "other gateway" to forward to. So under the default
single-process deployment this module's :func:`publish_inbound` is simply
never called; ``send_user_message`` always takes the direct-owner path,
exactly as it did before this task existed.

Failure posture, DELIBERATELY the mirror image of ``app.chat.replay``'s:
outbound frame-replay append is best-effort (a dropped replay frame just
means a client falls back to full history reload -- annoying, not lossy),
so it swallows ``CoordinationUnavailable`` and logs. An inbound USER
message that silently vanished because a coordination-backend blip ate the
publish call is a much worse failure -- the user would see their message
"sent" with no indication the runner never received it. So
:func:`publish_inbound` does NOT swallow a publish failure: it raises
:class:`InboundPublishFailed`, a clean, specific, documented exception
``ChatManager.send_user_message`` lets propagate to the caller (WS route /
Slack event handler) instead of a raw ``CoordinationUnavailable`` leaking
out or the message being dropped with no signal at all.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from app.coordination.base import CoordinationUnavailable
from app.coordination.factory import coordination

logger = logging.getLogger(__name__)

#: TTL for the per-session inbound-seq counter (``chat-in-seq:{chat_id}``).
#: Same margin/reasoning as ``app.chat.frame_seq._SEQ_TTL_SEC`` (the
#: OUTBOUND counter's TTL): must comfortably outlive a session's entire
#: wall-clock lifetime, including a long PAUSED stretch
#: (``ChatConfig.paused_ttl_seconds``, default 7 days) plus another ACTIVE
#: stretch after resume (``ChatConfig.max_session_seconds``, default 4h) --
#: 9 days total, generous margin past the 7-day-plus-4-hour default. Not
#: wired to a live ``ChatConfig`` instance for the same reason
#: ``frame_seq``'s counter isn't: it's a tiny, cheap coordination-backend
#: key, so a large fixed TTL costs nothing.
_SEQ_TTL_SEC = 9 * 24 * 3600

#: Bounded retention for the inbound-message stream, per chat session.
#: Matches ``app.chat.replay.STREAM_MAXLEN`` (the outbound stream's bound)
#: for now -- unlike that stream, an eviction here is NOT just "client
#: reloads": an evicted, never-delivered user message is silently lost.
#: 1000 comfortably covers any plausible backlog while a session is
#: unowned/mid-handoff; revisit if a future takeover story (wave-2F task 5)
#: needs a much longer unowned window.
STREAM_MAXLEN = 1000


class InboundPublishFailed(Exception):
    """A user message could not be published to the inbound stream.

    Raised by :func:`publish_inbound` / :func:`publish_control` when the
    coordination backend is unavailable at publish time
    (``CoordinationUnavailable``) -- see the module docstring for why this
    is a raised, sender-visible error rather than a swallowed-and-logged
    best-effort failure like most of this codebase's other
    coordination-backend helpers. The message was NOT accepted; the caller
    (``ChatManager.send_user_message``) lets this propagate so the
    sender's request fails cleanly instead of silently vanishing or
    crashing on a raw transport exception.
    """


def stream_key(chat_id: str) -> str:
    return f"chat-in:{chat_id}"


def _seq_key(chat_id: str) -> str:
    return f"chat-in-seq:{chat_id}"


def notify_channel(chat_id: str) -> str:
    """Pub/sub channel a session's owner subscribes to for a prompt wake-up
    when another gateway publishes an inbound message -- purely a latency
    optimization. The stream itself (:func:`stream_key`) is the source of
    truth; a missed/undelivered notify (e.g. the owner's consumer wasn't
    subscribed yet, or a redis blip ate the publish) only costs the
    consumer's poll-interval fallback, never correctness or ordering."""
    return f"chat-in-notify:{chat_id}"


def next_inbound_seq(chat_id: str) -> int:
    """Monotonic per-session sequence number for inbound (client -> runner)
    messages -- the mirror of ``app.chat.frame_seq.FrameSequencer`` for the
    opposite direction. Stateless wrapper over the coordination-backend
    counter, same as that sibling: whichever gateway calls this next
    continues the same sequence, no matter which process issued the
    previous number."""
    return coordination().incr(_seq_key(chat_id), ttl_s=_SEQ_TTL_SEC)


def peek_seq(chat_id: str) -> int:
    """Current value of ``chat_id``'s inbound seq counter WITHOUT allocating
    a new seq — an ``amount=0`` no-op increment "peek" (documented on
    ``CoordinationBackend.incr``; same pattern as the outbound counter's
    ``app.chat.frame_seq.current_seq`` and the daily-token quota peek in
    ``app.chat.manager``). Used by ``ChatManager._inbound_consumer_loop``
    to seed a fresh consumer's dedup cursor past the stream's retained
    (already-delivered) entries — wave-2F final review F3. Returns ``0``
    when the backend is unavailable (the caller then starts from the
    beginning — at-least-once, never a crash), the same degrade posture as
    :func:`read_new`."""
    try:
        return coordination().incr(_seq_key(chat_id), amount=0, ttl_s=_SEQ_TTL_SEC)
    except CoordinationUnavailable:
        logger.warning("chat-in seq peek failed for %s; treating as 0 (start of stream)", chat_id)
        return 0


async def _publish_entry(chat_id: str, payload: dict) -> int:
    """Shared append+notify tail for :func:`publish_inbound` /
    :func:`publish_control`: assign the next seq, durably append the entry,
    then best-effort notify. Raises :class:`InboundPublishFailed` if the
    append itself fails (coordination backend unavailable) -- see module
    docstring. The notify publish, by contrast, IS best-effort
    (log-and-continue): a missed notify only delays delivery until the
    owner's next poll tick, it can never lose the entry (already durably
    appended by that point)."""
    try:
        seq = next_inbound_seq(chat_id)
        entry = {"seq": seq, **payload}
        coordination().stream_append(stream_key(chat_id), entry, maxlen=STREAM_MAXLEN)
    except CoordinationUnavailable as exc:
        logger.warning("chat-in publish failed for %s; entry not accepted", chat_id)
        raise InboundPublishFailed(f"could not publish inbound entry for {chat_id}") from exc
    try:
        coordination().publish(notify_channel(chat_id), str(seq))
    except CoordinationUnavailable:
        logger.debug(
            "chat-in notify publish failed for %s; owner's consumer will still pick this up on its next poll",
            chat_id,
        )
    return seq


async def publish_inbound(
    chat_id: str,
    text: str,
    *,
    slack: Optional[dict] = None,
    turn_id: Optional[str] = None,
    message_id: Optional[str] = None,
) -> int:
    """Append a user message to ``chat_id``'s inbound stream and best-effort
    notify any subscribed owner. Returns the assigned seq.

    Entries carry a typed envelope (``type: "user_message"``) so the same
    stream can also route CONTROL commands cross-gateway (see
    :func:`publish_control`). ``slack``, when given, is a small
    ``{"channel": ..., "thread_ts": ...}`` origin marker for messages that
    entered via a Slack webhook on a NON-owning gateway: the owner's
    consumer uses it to (re-)establish a ``SlackSinkBridge`` for that
    channel before delivering, so the runner's reply actually reaches
    Slack (``ChatManager._ensure_slack_sink``). Empty values are dropped
    from the marker; a fully-empty marker is omitted.

    ``turn_id``, when given, is the id the caller already minted BEFORE
    persisting the user row it corresponds to (mirroring the direct-owner
    path's ``send_user_message`` -- see
    ``app.chat.manager.produce_inbound_user_message``), so the owning
    gateway's consumer can reuse the SAME id for the turn instead of
    minting a fresh one that would leave the user row and the assistant
    row disagreeing. ``message_id`` is that same persisted user row's own
    id (#2365 review: the producer used to discard it, so a forwarded
    turn's memory provenance could record ``source_turn_id`` but never
    ``source_message_id``). Both are omitted entirely when not given, so an
    entry published by an older replica (before either field existed)
    round-trips unchanged and the consumer falls back to minting its own
    turn id, as before.
    """
    payload: dict = {"type": "user_message", "text": text}
    if slack:
        origin = {k: v for k, v in slack.items() if v}
        if origin:
            payload["slack"] = origin
    if turn_id:
        payload["turn_id"] = turn_id
    if message_id:
        payload["message_id"] = message_id
    return await _publish_entry(chat_id, payload)


async def publish_control(
    chat_id: str,
    command: str,
    *,
    reason: Optional[str] = None,
    extra: Optional[dict] = None,
) -> int:
    """Append a CONTROL command (``"kill"`` / ``"cancel"`` / ``"approval"`` /
    ``"question"``) to ``chat_id``'s
    inbound stream, for the owning gateway's consumer to execute against
    its LOCAL session (``ChatManager._inbound_consumer_loop`` dispatches
    ``type == "control"`` entries to the local ``kill``/``cancel`` instead
    of the runner's stdin). Returns the assigned seq.

    This is how a webhook/REST request landing on a NON-owning replica
    (DELETE /api/chat/sessions/{id}, /agnes-new, the Slack Stop button)
    reaches the replica that actually hosts the sandbox -- without it,
    ``ChatManager.kill``/``cancel`` were process-local no-ops there,
    leaving the foreign owner's sandbox running while the caller archived
    the row. Same raise-on-append-failure posture as
    :func:`publish_inbound` (a silently dropped kill is worse than a
    visible error).
    """
    payload: dict = {"type": "control", "command": command}
    if reason:
        payload["reason"] = reason
    if extra:
        # command-specific fields (e.g. "approval" carries request_id +
        # decision); reserved keys can't be clobbered
        payload.update({k: v for k, v in extra.items() if k not in payload and k not in ("type", "seq")})
    return await _publish_entry(chat_id, payload)


def read_new(chat_id: str, after_seq: int) -> list[dict]:
    """Inbound entries for ``chat_id`` with ``seq > after_seq``, already
    sorted by seq (see ``CoordinationBackend.stream_read``'s contract) --
    thin wrapper so callers (``ChatManager._inbound_consumer_loop``) don't
    need to know the key convention. Returns ``[]`` (never raises) on a
    coordination-backend hiccup -- the caller's poll loop just tries again
    on its next tick, same degrade-to-"nothing new" posture
    ``app.chat.replay.replay_since`` uses for its own read failures."""
    try:
        return coordination().stream_read(stream_key(chat_id), after_seq=after_seq)
    except CoordinationUnavailable:
        logger.warning("chat-in read failed for %s; will retry on next poll", chat_id)
        return []


def subscribe_notify(chat_id: str, handler: Callable[[str], None]):
    """Subscribe ``handler`` to ``chat_id``'s notify channel. Returns an
    unsubscribe callable, or ``None`` if the coordination backend is
    unavailable right now (the consumer degrades to poll-only -- see
    ``ChatManager._inbound_consumer_loop``)."""
    try:
        return coordination().subscribe(notify_channel(chat_id), handler)
    except CoordinationUnavailable:
        logger.warning("chat-in notify subscribe failed for %s; consumer will poll only", chat_id)
        return None
