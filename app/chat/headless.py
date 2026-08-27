"""Headless (no-WebSocket) one-shot chat runs — Task 9's
``POST /api/v1/agents/{slug}/responses``.

``HeadlessSink`` is a duck-typed frame sink (``send_json``/``close``,
exactly what ``ChatManager.attach``/``_broadcast`` expect — see
``app/chat/manager.py``) that collects a single turn's frames in memory
instead of writing them to a live WebSocket.

**Frame shape note (verified against ``app/chat/manager.py``, not
assumed):** the task brief's sketch reads an ``assistant_message`` frame's
``text``/``content`` field. The real frame — see
``_pump_subprocess_to_ws``'s ``self._repo.append_message(... content=
frame.get("content", "") ...)`` and ``add_sink``'s history-replay frames
(``{"type": "assistant_message", "content": ..., "sender_email": ...}``)
— only ever carries the text under ``content``, never ``text``. This
sink reads ``content`` only (no ``text`` fallback) to match the real
producer exactly; a differently-shaped duck-typed frame from some future
producer would just leave ``answer`` at its previous value rather than
raise.

Turn completion is the ``"done"`` frame type (``manager.py``'s
``_pump_subprocess_to_ws``, ``ftype == "done"`` branch) — NOT
``assistant_message`` itself, since a turn can in principle emit more than
one frame before the runner signals it's finished (e.g. tool calls interleave
with the answer). ``done_event`` is only set on ``"done"``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from app.chat.types import Surface

logger = logging.getLogger(__name__)


class HeadlessSink:
    """Duck-typed frame sink (``send_json``/``close``) collecting a
    one-shot run's frames in memory.

    ``answer`` tracks the most recent ``assistant_message`` frame's
    ``content`` seen so far — for a normal one-shot turn there is exactly
    one, but taking "most recent" rather than "first" is harmless and
    matches how a live WS client would render sequential frames.
    """

    def __init__(self) -> None:
        self.frames: list[dict] = []
        self.done_event = asyncio.Event()
        self.answer: str = ""

    async def send_json(self, frame: dict) -> None:
        self.frames.append(frame)
        if frame.get("type") == "assistant_message":
            content = frame.get("content")
            if content:
                self.answer = content
        if frame.get("type") == "done":
            self.done_event.set()

    async def close(self) -> None:
        # Mirrors a live WS's disconnect: unblock any waiter rather than
        # hang forever if the sink is torn down (e.g. session killed)
        # before a "done" frame ever arrived.
        self.done_event.set()


async def _harvest_after_turn(
    manager,
    chat_id: str,
    agent_id: Optional[str],
    owner_user_id: Optional[str],
) -> None:
    """Best-effort artifact harvest for a one-shot turn that just completed
    (C4, V1b Task 5) — fired from :func:`_wait_for_sink` right after the
    ``"done"`` frame lands, BEFORE the sink detaches (so the sandbox handle
    is guaranteed still live; detach only starts the pause/linger countdown,
    it doesn't tear anything down synchronously, but harvesting before it
    rather than after removes any race with that countdown entirely).

    No-ops when ``owner_user_id`` is unknown (older/other call sites that
    don't thread it through) or when the manager has no live handle for
    this session (already torn down, or never spawned — e.g. the sink's
    ``close()`` fired ``done_event`` without a real run). Never raises —
    a harvest failure must not turn a successful chat turn into a 500.
    """
    if owner_user_id is None:
        return
    list_live = getattr(manager, "list_live", None)
    if list_live is None:
        return
    try:
        live = next((entry for entry in list_live() if entry.chat_id == chat_id), None)
        if live is None or live.handle is None:
            return
        from app.chat.artifact_harvest import caps_from_manager, harvest_session_artifacts

        await harvest_session_artifacts(
            chat_id,
            agent_id,
            owner_user_id,
            live.handle,
            **caps_from_manager(manager),
        )
    except Exception:
        logger.exception("headless: artifact harvest failed for %s — continuing", chat_id)


async def _wait_for_sink(
    manager,
    chat_id: str,
    sink: "HeadlessSink",
    timeout_s: int,
    *,
    agent_id: Optional[str] = None,
    owner_user_id: Optional[str] = None,
) -> bool:
    """Await ``sink.done_event`` up to ``timeout_s``, always detaching the
    sink afterward. Returns ``True`` if the wait timed out.

    Detaching on timeout is deliberate, not a "give up on the run" signal —
    the sandbox keeps running the turn regardless (see the module
    docstring's timeout-vs-kill contract). ``ChatManager.detach_sink``
    starts the normal linger→pause countdown when this was the last sink;
    a later re-``attach()`` (the sync-timeout-degrades-to-background-job
    path in ``app.api.agent_runtime``) cancels that countdown the same way
    a reconnecting WS client would.

    On a genuine (non-timeout) completion, harvests any artifacts the turn
    produced (:func:`_harvest_after_turn`) before detaching — a timed-out
    wait does NOT harvest, since the turn isn't actually done yet (the
    caller either degrades to a background job that resumes waiting on the
    same session, or gives up; either way there is nothing to harvest yet).
    """
    timed_out = False
    try:
        await asyncio.wait_for(sink.done_event.wait(), timeout=timeout_s)
        await _harvest_after_turn(manager, chat_id, agent_id, owner_user_id)
    except asyncio.TimeoutError:
        timed_out = True
    finally:
        try:
            await manager.detach_sink(chat_id, sink)
        except Exception:
            logger.exception("headless: detach_sink failed for %s — sink leak, non-fatal", chat_id)
    return timed_out


async def run_one_shot(
    manager,
    *,
    user_email: str,
    agent_id: Optional[str],
    prompt: str,
    timeout_s: int,
    owner_user_id: Optional[str] = None,
    profile: Optional[str] = None,
) -> dict[str, Any]:
    """Create a FRESH session, send ``prompt``, and wait up to
    ``timeout_s`` seconds for the turn to complete.

    Returns ``{"chat_id": ..., "answer": ..., "timed_out": bool}``.
    ``answer`` is whatever was collected before the wait ended — the empty
    string if no ``assistant_message`` frame ever arrived (e.g. an
    immediate timeout). A timeout does NOT stop the run: the sandbox keeps
    processing the turn after this function returns; see
    ``app.api.agent_runtime`` for how the caller degrades a sync timeout
    into a background job that resumes waiting on the same ``chat_id``
    (via :func:`await_completion`) instead of re-sending the prompt.

    ``owner_user_id`` (optional, keyword-only) — when provided, threaded
    through to :func:`_wait_for_sink` so a genuine (non-timeout) completion
    triggers an artifact harvest (V1b Task 5, C4) scoped to that owner.
    Callers that don't pass it simply get no harvest, same as before this
    parameter existed.

    ``profile`` (optional, keyword-only) — a registered chat profile slug
    (``app.chat.profiles``) to spawn the session with, for callers with no
    ``agent_id`` of their own (e.g. the semantic-layer auto-draft sweep,
    which authenticates as a system identity rather than a named agent).
    ``None`` (the default, every pre-existing caller) leaves session
    spawning exactly as before.

    ``surface`` is always ``Surface.API`` here, never a caller-supplied
    parameter — every headless one-shot run is unattended by construction,
    and ``Surface.API`` is what makes ``ChatManager._resolve_if_unattended``
    resolve a question/approval instantly instead of waiting out the full
    approval timeout.
    """
    session = await manager.create_session(
        user_email=user_email,
        surface=Surface.API,
        agent_id=agent_id,
        profile=profile,
    )
    chat_id = session.id
    sink = HeadlessSink()
    await manager.attach(chat_id, sink, is_primary=True)
    await manager.send_user_message(chat_id, prompt, sender_email=user_email)
    timed_out = await _wait_for_sink(
        manager,
        chat_id,
        sink,
        timeout_s,
        agent_id=agent_id,
        owner_user_id=owner_user_id,
    )
    return {"chat_id": chat_id, "answer": sink.answer, "timed_out": timed_out}


def _last_assistant_message(manager, chat_id: str) -> Optional[str]:
    """Best-effort read of the most recent persisted assistant message for
    ``chat_id`` — the fallback path in :func:`await_completion` for a turn
    that already finished (and had its ``turn_buffer`` cleared) before the
    job worker attached, so no ``"done"`` frame is ever coming for a fresh
    sink to see.

    Reaches into ``ChatManager._repo`` (private) rather than a public
    accessor — ``ChatManager`` has none for a single "last assistant
    message" read today. Documented adaptation (Task 9): acceptable here
    because ``headless.py`` lives in the same ``app.chat`` package as
    ``manager.py``, and a failure (attribute missing, repo error) is
    swallowed — this is a best-effort fallback, not the primary path.
    """
    repo = getattr(manager, "_repo", None)
    if repo is None:
        return None
    try:
        messages = repo.list_messages(chat_id)
    except Exception:
        logger.exception("headless: _last_assistant_message lookup failed for %s", chat_id)
        return None
    for msg in reversed(messages):
        if getattr(msg, "role", None) == "assistant":
            return getattr(msg, "content", None) or ""
    return None


async def await_completion(
    manager,
    *,
    chat_id: str,
    timeout_s: int,
    agent_id: Optional[str] = None,
    owner_user_id: Optional[str] = None,
) -> dict[str, Any]:
    """Resume waiting on an ALREADY-RUNNING (or paused) session — no
    ``send_user_message`` call, so the original prompt is never resent.

    Used by the ``agent_response`` job worker when a sync call's
    ``run_one_shot`` hit its wait timeout: the job re-``attach()``es a
    fresh ``HeadlessSink`` to the same ``chat_id`` (this reseats the sink
    on an ACTIVE session, or resumes a PAUSED one — see
    ``ChatManager.attach``) and waits again, this time with the job's own
    (typically much longer) timeout.

    **Race the sink can't see (documented adaptation):** the turn may have
    already finished — ``"done"`` frame broadcast, ``turn_buffer`` cleared
    — in the gap between the sync call's timeout and the worker picking up
    the job. A sink attached AFTER that point replays an empty
    ``turn_buffer`` (see ``ChatManager._seat_sink``) and would otherwise
    wait out the full job timeout for a frame that already happened. Right
    after attaching, this checks ``LiveSession.turn_in_flight`` (via
    ``manager.list_live()``); if the turn is not in flight and the sink
    collected nothing, the answer is read straight from persisted storage
    (:func:`_last_assistant_message`) instead of waiting. The same fallback
    also covers the (rarer) case where the wait genuinely timed out but the
    turn actually completed in the interim.

    Returns the same shape as :func:`run_one_shot` minus the answer having
    necessarily come from a prompt sent in THIS call.
    """
    sink = HeadlessSink()
    await manager.attach(chat_id, sink, is_primary=False)

    live = next((entry for entry in manager.list_live() if entry.chat_id == chat_id), None)
    if live is not None and not live.turn_in_flight and not sink.frames:
        answer = _last_assistant_message(manager, chat_id)
        if answer is not None:
            # The turn already finished before this sink attached (no "done"
            # frame is coming for it to see) — harvest here since
            # _wait_for_sink's own harvest hook is never reached on this
            # early-return path.
            await _harvest_after_turn(manager, chat_id, agent_id, owner_user_id)
            await manager.detach_sink(chat_id, sink)
            return {"chat_id": chat_id, "answer": answer, "timed_out": False}

    timed_out = await _wait_for_sink(
        manager,
        chat_id,
        sink,
        timeout_s,
        agent_id=agent_id,
        owner_user_id=owner_user_id,
    )
    if timed_out and not sink.answer:
        fallback = _last_assistant_message(manager, chat_id)
        if fallback:
            return {"chat_id": chat_id, "answer": fallback, "timed_out": False}
    return {"chat_id": chat_id, "answer": sink.answer, "timed_out": timed_out}
